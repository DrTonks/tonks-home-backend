# coding: utf-8
"""端到端模拟测试：模拟两台机器上报 → 验证 SQLite 加算逻辑 → 清理。

使用 Flask test client 避免 waitress 依赖。
运行: python tests/simulate_multi_machine.py
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_DIR))


def main():
    temp_dir = Path(tempfile.mkdtemp(prefix="sleepy-sim-"))
    admin_secret = "test-sim-secret"
    print(f"临时目录: {temp_dir}")

    try:
        # ---- 准备环境 ----
        (temp_dir / "music").mkdir(exist_ok=True)

        config = {
            "version": 2,
            "debug": False,
            "host": "127.0.0.1",
            "port": 9010,
            "secret": "sim-secret",
            "admin_secret": admin_secret,
            "status": 0,
            "app_name": "SimTest",
            "timestamp": 0,
            "status_list": [{"id": 0, "name": "On", "desc": "", "color": "awake"}],
            "github_token": "",
            "agent_activity": [],
            "calendar_events": [],
            "music_files": [],
            "todos": [],
        }
        (temp_dir / "data.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

        os.environ["SLEEPY_MUSIC_DIR"] = str(temp_dir / "music")
        os.environ["SLEEPY_AGENT_ACTIVITY_DB"] = str(temp_dir / "agent_activity.sqlite3")
        os.environ["SLEEPY_ANALYTICS_DB"] = str(temp_dir / "analytics.sqlite3")
        os.environ["SLEEPY_CORS_ORIGINS"] = ""

        os.chdir(str(temp_dir))

        import server as backend
        backend.app.config.update(TESTING=True)
        # 用 temp 目录的路径重建 store
        backend.agent_store = backend.AgentActivityStore(
            str(temp_dir / "agent_activity.sqlite3")
        )
        backend.blog_analytics = backend.BlogAnalytics(
            str(temp_dir / "analytics.sqlite3")
        )

        client = backend.app.test_client()

        def post_activity(body_dict):
            resp = client.post(
                f"/agent-activity?secret={admin_secret}",
                json=body_dict,
            )
            return resp.get_json()

        def get_activity():
            resp = client.get("/agent-activity")
            return resp.get_json()

        # ================================================================
        # 测试 1: Windows PC 上报
        # ================================================================
        print("--- 测试 1: Windows PC 上报 (2026-08-01 ~ 2026-08-06) ---")
        r = post_activity({
            "machineId": "windows-pc",
            "dailyActivity": [
                {"date": "2026-08-01", "messageCount": 100, "sessionCount": 3, "toolCallCount": 50},
                {"date": "2026-08-02", "messageCount": 200, "sessionCount": 4, "toolCallCount": 80},
                {"date": "2026-08-03", "messageCount": 150, "sessionCount": 2, "toolCallCount": 60},
                {"date": "2026-08-06", "messageCount": 300, "sessionCount": 6, "toolCallCount": 120},
            ],
        })
        assert r.get("success"), f"上报失败: {r}"
        assert r["machineId"] == "windows-pc"
        print(f"  OK: {r['new']} upserted, {r['total']} total rows")

        # ================================================================
        # 测试 2: Mac 上报（同日不同机器 → 应加总）
        # ================================================================
        print("--- 测试 2: Mac 上报 (同日部分重叠) ---")
        r = post_activity({
            "machineId": "macbook",
            "dailyActivity": [
                {"date": "2026-08-03", "messageCount": 50, "sessionCount": 1, "toolCallCount": 20},
                {"date": "2026-08-04", "messageCount": 80, "sessionCount": 2, "toolCallCount": 30},
                {"date": "2026-08-05", "messageCount": 120, "sessionCount": 3, "toolCallCount": 45},
            ],
        })
        assert r.get("success"), f"上报失败: {r}"
        print(f"  OK: {r['new']} upserted, {r['total']} total rows")

        # ================================================================
        # 测试 3: 验证 GET 聚合 — 核心：跨机器加总
        # ================================================================
        print("--- 测试 3: 验证 GET 跨机器聚合 ---")
        agg = get_activity()
        assert agg.get("success"), f"GET 失败: {agg}"
        activities = agg["activities"]
        print(f"  返回 {len(activities)} 天")

        expected = {
            "2026-08-01": {"messageCount": 100, "sessionCount": 3, "toolCallCount": 50},
            "2026-08-02": {"messageCount": 200, "sessionCount": 4, "toolCallCount": 80},
            "2026-08-03": {"messageCount": 200, "sessionCount": 3, "toolCallCount": 80},  # 150+50
            "2026-08-04": {"messageCount": 80,  "sessionCount": 2, "toolCallCount": 30},
            "2026-08-05": {"messageCount": 120, "sessionCount": 3, "toolCallCount": 45},
            "2026-08-06": {"messageCount": 300, "sessionCount": 6, "toolCallCount": 120},
        }

        for a in activities:
            date = a["date"]
            want = expected.get(date, {})
            for field in ["messageCount", "sessionCount", "toolCallCount"]:
                got_val = a[field]
                want_val = want.get(field, 0)
                if got_val != want_val:
                    print(f"  FAIL [{date}] {field}: 期望 {want_val}, 实际 {got_val}")
                    sys.exit(1)
            assert "intensity" in a, f"缺少 intensity 字段: {a}"
            del expected[date]
        if expected:
            print(f"  FAIL: 缺少日期: {list(expected.keys())}")
            sys.exit(1)
        print("  OK: 所有日期聚合正确，跨机器已加总")

        # ================================================================
        # 测试 4: 同一机器重复上报 → 应覆盖而非累加
        # ================================================================
        print("--- 测试 4: Windows PC 重复上报 (同机器同日 → 覆盖) ---")
        r = post_activity({
            "machineId": "windows-pc",
            "dailyActivity": [
                {"date": "2026-08-06", "messageCount": 400, "sessionCount": 7, "toolCallCount": 150},
            ],
        })
        assert r.get("success"), f"上报失败: {r}"

        agg2 = get_activity()
        for a in agg2["activities"]:
            if a["date"] == "2026-08-06":
                assert a["messageCount"] == 400, f"覆盖失败: {a['messageCount']} != 400"
                print(f"  OK: messageCount={a['messageCount']} (400=覆盖值, 不是 700)")

        # ================================================================
        # 测试 5: 老格式向后兼容（裸数组）
        # ================================================================
        print("--- 测试 5: 老格式兼容 (裸数组, 无 machineId) ---")
        r = post_activity([
            {"date": "2026-08-07", "messageCount": 99, "sessionCount": 1, "toolCallCount": 9},
        ])
        assert r.get("success"), f"老格式上报失败: {r}"
        assert r["machineId"] == "unknown"
        print("  OK: machineId=unknown 自动填充")

        # ================================================================
        # 测试 6: 懒清除（>365 天数据在下次上报时被清理）
        # ================================================================
        print("--- 测试 6: 懒清除 (>365天旧数据) ---")
        db_path = str(temp_dir / "agent_activity.sqlite3")
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        old_ts = int(time.time()) - 400 * 24 * 60 * 60
        conn.execute(
            "INSERT OR REPLACE INTO agent_activity VALUES (?, ?, ?, ?, ?, ?)",
            ("2025-06-01", "ancient-pc", 999, 99, 999, old_ts),
        )
        conn.execute("DELETE FROM agent_activity_meta WHERE key = 'last_cleanup_date'")
        conn.commit()
        conn.close()

        # 再上报一次 — 触发清理
        r = post_activity({
            "machineId": "windows-pc",
            "dailyActivity": [
                {"date": "2026-08-08", "messageCount": 1, "sessionCount": 1, "toolCallCount": 1},
            ],
        })
        assert r.get("success"), f"上报失败: {r}"

        # 验证旧记录已被删除
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT date FROM agent_activity").fetchall()
        dates = {r["date"] for r in rows}
        conn.close()
        assert "2025-06-01" not in dates, f"过期数据未被清理: {dates}"
        print(f"  OK: 2025-06-01 已被懒清除，当前 {len(dates)} 行")

        # ================================================================
        # 全部通过
        # ================================================================
        print("\n" + "=" * 60)
        print("  所有 6 个模拟测试通过！")
        print("=" * 60)

    finally:
        # 清理环境变量
        for name in ("SLEEPY_MUSIC_DIR", "SLEEPY_AGENT_ACTIVITY_DB",
                     "SLEEPY_ANALYTICS_DB", "SLEEPY_CORS_ORIGINS"):
            os.environ.pop(name, None)
        if str(REPO_DIR) in sys.path:
            sys.path.remove(str(REPO_DIR))

        # 清理临时目录
        print(f"清理临时目录: {temp_dir}")
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
