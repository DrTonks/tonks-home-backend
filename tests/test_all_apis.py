# coding: utf-8
"""Contract regression tests for every public Sleepy API route.

Run from the sleepy directory:
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock


REPO_DIR = Path(__file__).resolve().parents[1]
TEST_CONFIG = {
    "version": 2,
    "debug": False,
    "host": "127.0.0.1",
    "port": 9010,
    "secret": "status-test-secret",
    "admin_secret": "admin-test-secret",
    "status": 0,
    "app_name": "Desktop",
    "timestamp": 0,
    "status_list": [
        {"id": 0, "name": "Online", "desc": "Active", "color": "awake"},
        {"id": 1, "name": "Offline", "desc": "Away", "color": "sleeping"},
    ],
    "github_token": "",
    "agent_activity": [],
    "calendar_events": [],
    "blog_base_url": "https://blog.test",
    "blog_data_url": "https://blog.test/data",
    "music_files": [],
    "todos": [],
}

workspace = None
backend = None
original_cwd = None


def setUpModule():
    global workspace, backend, original_cwd
    original_cwd = os.getcwd()
    test_temp_root = REPO_DIR / ".test-tmp"
    test_temp_root.mkdir(exist_ok=True)
    workspace = test_temp_root / f"sleepy-api-tests-{uuid.uuid4().hex}"
    workspace.mkdir()
    root = workspace
    (root / "music").mkdir()
    (root / "data.json").write_text(
        json.dumps(TEST_CONFIG, ensure_ascii=False),
        encoding="utf-8",
    )
    os.environ["SLEEPY_MUSIC_DIR"] = str(root / "music")
    os.environ["SLEEPY_ANALYTICS_DB"] = str(root / "analytics.sqlite3")
    os.environ["SLEEPY_AGENT_ACTIVITY_DB"] = str(root / "agent_activity.sqlite3")
    os.environ["SLEEPY_RECOMMENDATIONS_DB"] = str(root / "recommendations.sqlite3")
    os.environ["SLEEPY_ANALYTICS_SALT"] = "analytics-test-salt"
    os.environ["SLEEPY_CORS_ORIGINS"] = "http://127.0.0.1:4321"
    os.environ["SLEEPY_ENV_FILE"] = str(root / "missing-test.env")
    os.environ["SLEEPY_STATUS_SECRET"] = TEST_CONFIG["secret"]
    os.environ["SLEEPY_ADMIN_SECRET"] = TEST_CONFIG["admin_secret"]
    os.environ["SLEEPY_GITHUB_TOKEN"] = TEST_CONFIG["github_token"]
    os.chdir(root)
    sys.path.insert(0, str(REPO_DIR))
    import server as imported_backend

    backend = imported_backend
    backend.app.config.update(TESTING=True)


def tearDownModule():
    os.chdir(original_cwd)
    for name in (
        "SLEEPY_MUSIC_DIR",
        "SLEEPY_ANALYTICS_DB",
        "SLEEPY_AGENT_ACTIVITY_DB",
        "SLEEPY_RECOMMENDATIONS_DB",
        "SLEEPY_ANALYTICS_SALT",
        "SLEEPY_CORS_ORIGINS",
        "SLEEPY_ENV_FILE",
        "SLEEPY_STATUS_SECRET",
        "SLEEPY_ADMIN_SECRET",
        "SLEEPY_GITHUB_TOKEN",
    ):
        os.environ.pop(name, None)
    if str(REPO_DIR) in sys.path:
        sys.path.remove(str(REPO_DIR))
    workspace_resolved = workspace.resolve()
    temp_root_resolved = (REPO_DIR / ".test-tmp").resolve()
    if workspace_resolved.is_relative_to(temp_root_resolved):
        shutil.rmtree(workspace_resolved)


class AllApiRoutesTest(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        root = workspace
        (root / "data.json").write_text(
            json.dumps(TEST_CONFIG, ensure_ascii=False),
            encoding="utf-8",
        )
        for suffix in ("", "-shm", "-wal"):
            path = root / f"analytics.sqlite3{suffix}"
            if path.exists():
                path.unlink()
            path = root / f"agent_activity.sqlite3{suffix}"
            if path.exists():
                path.unlink()
            path = root / f"recommendations.sqlite3{suffix}"
            if path.exists():
                path.unlink()
        for path in (root / "music").iterdir():
            path.unlink()
        backend.d.load()
        backend.blog_analytics = backend.BlogAnalytics(
            str(root / "analytics.sqlite3")
        )
        backend.agent_store = backend.AgentActivityStore(
            str(root / "agent_activity.sqlite3")
        )
        backend.recommendation_store = backend.RecommendationStore(
            str(root / "recommendations.sqlite3")
        )
        backend.recommendation_limiter = backend.RecommendationRateLimiter(100, 100)
        backend.online_users.clear()
        backend.geoip_cache.clear()
        backend.geoip_last_attempt.clear()
        backend.geoip_upstream_attempts.clear()
        self.client = backend.app.test_client()

    def json(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/json")
        return response.get_json()

    def test_route_inventory_matches_tested_contract(self):
        expected = {
            ("GET", "/"),
            ("GET", "/geoip"),
            ("GET", "/query"),
            ("GET", "/get/status_list"),
            ("GET", "/online_count"),
            ("GET", "/set"),
            ("GET", "/agent-activity"),
            ("POST", "/agent-activity"),
            ("GET", "/blog-posts"),
            ("GET", "/blog/views"),
            ("POST", "/blog/views/<path:slug>"),
            ("GET", "/images/<path:filename>"),
            ("GET", "/music/list"),
            ("GET", "/music/<path:filename>"),
            ("GET", "/music/lyrics/<path:filename>"),
            ("GET", "/music/cover/<path:filename>"),
            ("POST", "/music/upload"),
            ("POST", "/music/cover/upload"),
            ("POST", "/music/delete"),
            ("POST", "/music/reorder"),
            ("GET", "/calendar/events"),
            ("POST", "/calendar/events"),
            ("GET", "/calendar/holidays"),
            ("GET", "/github/stats"),
            ("GET", "/todos"),
            ("POST", "/todos"),
            ("POST", "/pet/reply"),
            ("GET", "/pet/recommendations"),
            ("POST", "/pet/recommendations"),
            ("DELETE", "/pet/recommendations/<int:recommendation_id>"),
        }
        actual = {
            (method, rule.rule)
            for rule in backend.app.url_map.iter_rules()
            if rule.endpoint != "static"
            for method in rule.methods
            if method not in {"HEAD", "OPTIONS"}
        }
        self.assertEqual(actual, expected)

    def test_health_device_status_and_online_routes(self):
        health = self.client.get(
            "/",
            headers={"Origin": "http://127.0.0.1:4321"},
        )
        self.assertTrue(self.json(health)["success"])
        self.assertEqual(
            health.headers.get("Access-Control-Allow-Origin"),
            "http://127.0.0.1:4321",
        )

        with mock.patch.object(
            backend,
            "fetch_ip_api_location",
            return_value={
                "success": True,
                "city": "福州",
                "region": "福建",
                "country": "CN",
                "lat": 26.08,
                "lon": 119.30,
            },
        ) as lookup:
            geoip = self.client.get(
                "/geoip",
                headers={"X-Forwarded-For": "110.80.172.21"},
            )
        self.assertEqual(geoip.status_code, 200)
        self.assertEqual(geoip.headers.get("Cache-Control"), "private, no-store")
        self.assertEqual(geoip.get_json()["city"], "福州")
        self.assertNotIn("ip", geoip.get_json())
        lookup.assert_called_once_with("110.80.172.21")

        # 同一访客重复请求命中加盐哈希内存缓存，不再次消耗 ip-api 额度。
        cached_geoip = self.client.get(
            "/geoip",
            headers={"X-Forwarded-For": "110.80.172.21"},
        )
        self.assertEqual(cached_geoip.status_code, 200)
        self.assertEqual(cached_geoip.get_json()["city"], "福州")
        lookup.assert_called_once()
        self.assertNotIn(
            "110.80.172.21",
            json.dumps(list(backend.geoip_cache.keys())),
        )

        statuses = self.json(self.client.get("/get/status_list"))
        self.assertEqual(statuses[0]["name"], "Online")

        result = self.json(
            self.client.get(
                "/set",
                query_string={
                    "secret": TEST_CONFIG["secret"],
                    "status": 1,
                    "app_name": "Sleeping",
                    "timestamp": int(time.time()),
                },
            )
        )
        self.assertTrue(result["success"])

        query = self.json(self.client.get("/query"))
        self.assertEqual(query["status"], 1)
        self.assertGreater(query["timestamp"], 0)

        online = self.json(
            self.client.get(
                "/online_count",
                headers={"X-Client-ID": "contract-test-client", "isMobile": "true"},
            )
        )
        self.assertGreaterEqual(online["online_count"], 1)
        self.assertGreaterEqual(online["mobile_count"], 1)

        denied = self.json(
            self.client.get(
                "/set",
                query_string={"secret": "wrong", "status": 0, "app_name": "Nope"},
            )
        )
        self.assertFalse(denied["success"])

    def test_geoip_distinguishes_client_rate_limit_and_upstream_failures(self):
        invalid = self.client.get("/geoip")
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.get_json()["code"], "geoip unavailable")

        backend.geoip_last_attempt.clear()
        backend.geoip_upstream_attempts.clear()
        with mock.patch.object(
            backend,
            "fetch_ip_api_location",
            side_effect=backend.GeoIpUpstreamError("provider failed"),
        ):
            upstream = self.client.get(
                "/geoip",
                headers={"X-Forwarded-For": "110.80.172.21"},
            )
        self.assertEqual(upstream.status_code, 502)
        self.assertEqual(upstream.get_json()["code"], "geoip upstream error")

        backend.geoip_last_attempt.clear()
        backend.geoip_upstream_attempts.clear()
        backend.geoip_upstream_attempts.extend(
            [time.monotonic()] * backend.GEOIP_UPSTREAM_LIMIT_PER_MINUTE
        )
        with mock.patch.object(backend, "fetch_ip_api_location") as lookup:
            limited = self.client.get(
                "/geoip",
                headers={"X-Forwarded-For": "110.80.172.21"},
            )
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.get_json()["code"], "geoip rate limited")
        self.assertGreaterEqual(int(limited.headers.get("Retry-After")), 1)
        self.assertLessEqual(int(limited.headers.get("Retry-After")), 60)
        lookup.assert_not_called()

    def test_geoip_treats_provider_payload_failures_as_upstream_errors(self):
        failed_response = mock.MagicMock()
        failed_response.__enter__.return_value.read.return_value = b'{"status":"fail"}'
        with mock.patch.object(
            backend.urllib.request,
            "urlopen",
            return_value=failed_response,
        ):
            with self.assertRaises(backend.GeoIpUpstreamError):
                backend.fetch_ip_api_location("110.80.172.21")

        malformed_request = mock.MagicMock()
        malformed_request.environ = {}
        malformed_request.remote_addr = "not-an-ip"
        with self.assertRaises(backend.GeoIpClientError):
            backend.get_geoip_client_address(malformed_request)

        malformed_response = mock.MagicMock()
        malformed_response.__enter__.return_value.read.return_value = (
            b'{"status":"success","lat":null,"lon":119.3}'
        )
        with mock.patch.object(
            backend.urllib.request,
            "urlopen",
            return_value=malformed_response,
        ):
            with self.assertRaises(backend.GeoIpUpstreamError):
                backend.fetch_ip_api_location("110.80.172.21")

    def test_pet_recommendations_full_lifecycle(self):
        anonymous = self.json(
            self.client.post(
                "/pet/recommendations",
                json={"category": "book", "content": "献给阿尔吉侬的花束"},
                headers={"X-Client-ID": "reader-1"},
            )
        )["recommendation"]
        self.assertEqual(anonymous["user_name"], "unknown")
        self.assertEqual(anonymous["city"], "unknown")

        named = self.json(
            self.client.post(
                "/pet/recommendations",
                json={
                    "category": "music",
                    "content": "晴天",
                    "user_name": "Tonks",
                    "city": "福州",
                },
                headers={"X-Client-ID": "reader-2"},
            )
        )["recommendation"]
        self.assertEqual(named["user_name"], "Tonks")
        self.assertEqual(named["city"], "福州")

        denied_list = self.json(self.client.get("/pet/recommendations"))
        self.assertFalse(denied_list["success"])

        all_items = self.json(
            self.client.get(
                "/pet/recommendations",
                query_string={"secret": TEST_CONFIG["admin_secret"]},
            )
        )
        self.assertEqual(all_items["count"], 2)
        self.assertEqual(all_items["recommendations"][0]["id"], named["id"])

        music = self.json(
            self.client.get(
                "/pet/recommendations",
                query_string={
                    "category": "music",
                    "secret": TEST_CONFIG["admin_secret"],
                },
            )
        )
        self.assertEqual(music["recommendations"], [named])

        today = time.strftime("%Y-%m-%d")
        dated = self.json(
            self.client.get(
                "/pet/recommendations",
                query_string={
                    "date": today,
                    "category": "book",
                    "secret": TEST_CONFIG["admin_secret"],
                },
            )
        )
        self.assertEqual(dated["recommendations"], [anonymous])

        invalid = self.json(
            self.client.post(
                "/pet/recommendations",
                json={"category": "food", "content": "nope"},
            )
        )
        self.assertFalse(invalid["success"])
        self.assertEqual(invalid["code"], "unknown_category")

        denied = self.json(
            self.client.delete(
                f"/pet/recommendations/{anonymous['id']}",
                query_string={"secret": "wrong"},
            )
        )
        self.assertFalse(denied["success"])

        deleted = self.json(
            self.client.delete(
                f"/pet/recommendations/{anonymous['id']}",
                query_string={"secret": TEST_CONFIG["admin_secret"]},
            )
        )
        self.assertEqual(deleted["deleted"], anonymous["id"])
        self.assertEqual(
            self.json(
                self.client.get(
                    "/pet/recommendations",
                    query_string={"secret": TEST_CONFIG["admin_secret"]},
                )
            )["count"],
            1,
        )

    def test_agent_activity_get_and_post(self):
        payload = {
            "dailyActivity": [
                {
                    "date": "2026-07-29",
                    "messageCount": 4,
                    "sessionCount": 2,
                    "toolCallCount": 8,
                }
            ]
        }
        posted = self.json(
            self.client.post(
                "/agent-activity",
                query_string={"secret": TEST_CONFIG["admin_secret"]},
                json=payload,
            )
        )
        self.assertTrue(posted["success"])
        result = self.json(self.client.get("/agent-activity"))
        self.assertEqual(result["activities"][0]["messageCount"], 4)
        self.assertIn("intensity", result["activities"][0])

    def test_agent_activity_multi_machine_summing(self):
        """两台机器同日上报 → 加总；同机器同日重复上报 → 覆盖"""
        admin_qs = {"secret": TEST_CONFIG["admin_secret"]}
        date = "2026-08-06"

        # --- 1. Windows 上报 ---
        r1 = self.json(
            self.client.post(
                "/agent-activity",
                query_string=admin_qs,
                json={
                    "machineId": "windows-pc",
                    "dailyActivity": [
                        {"date": date, "messageCount": 10, "sessionCount": 2, "toolCallCount": 5}
                    ],
                },
            )
        )
        self.assertTrue(r1["success"])
        self.assertEqual(r1["machineId"], "windows-pc")

        # --- 2. Mac 同日上报 → 应加总 ---
        r2 = self.json(
            self.client.post(
                "/agent-activity",
                query_string=admin_qs,
                json={
                    "machineId": "macbook",
                    "dailyActivity": [
                        {"date": date, "messageCount": 20, "sessionCount": 3, "toolCallCount": 8}
                    ],
                },
            )
        )
        self.assertTrue(r2["success"])

        agg = self.json(self.client.get("/agent-activity"))
        day = agg["activities"][0]
        self.assertEqual(day["date"], date)
        self.assertEqual(day["messageCount"], 30)    # 10 + 20
        self.assertEqual(day["sessionCount"], 5)     # 2 + 3
        self.assertEqual(day["toolCallCount"], 13)   # 5 + 8

        # --- 3. Windows 再次上报同日（messageCount 升，sessionCount/toolCallCount 降）→
        #     只升不降：messageCount 应更新为 15，sessionCount 保持 2，toolCallCount 保持 5 ---
        r3 = self.json(
            self.client.post(
                "/agent-activity",
                query_string=admin_qs,
                json={
                    "machineId": "windows-pc",
                    "dailyActivity": [
                        {"date": date, "messageCount": 15, "sessionCount": 1, "toolCallCount": 3}
                    ],
                },
            )
        )
        self.assertTrue(r3["success"])

        agg2 = self.json(self.client.get("/agent-activity"))
        day2 = agg2["activities"][0]
        self.assertEqual(day2["messageCount"], 35)    # 15 + 20 (messageCount 升，接受)
        self.assertEqual(day2["sessionCount"], 5)     # 2 + 3  (sessionCount 降，保持旧值)
        self.assertEqual(day2["toolCallCount"], 13)   # 5 + 8  (toolCallCount 降，保持旧值)

        # --- 4. 多日跨机器 ---
        day2_date = "2026-08-07"
        self.json(
            self.client.post(
                "/agent-activity",
                query_string=admin_qs,
                json={
                    "machineId": "windows-pc",
                    "dailyActivity": [
                        {"date": day2_date, "messageCount": 5, "sessionCount": 1, "toolCallCount": 2}
                    ],
                },
            )
        )
        agg3 = self.json(self.client.get("/agent-activity"))
        self.assertEqual(len(agg3["activities"]), 2)  # 两天
        self.assertEqual(agg3["activities"][1]["date"], day2_date)

        # --- 5. 向后兼容：老格式（裸数组，无 machineId） ---
        r5 = self.json(
            self.client.post(
                "/agent-activity",
                query_string=admin_qs,
                json=[
                    {"date": "2026-08-08", "messageCount": 7, "sessionCount": 1, "toolCallCount": 4}
                ],
            )
        )
        self.assertTrue(r5["success"])
        self.assertEqual(r5["machineId"], "unknown")

        # --- 6. 只升不降：值全部下降 → 全部被拒绝，保留旧值 ---
        r6 = self.json(
            self.client.post(
                "/agent-activity",
                query_string=admin_qs,
                json={
                    "machineId": "windows-pc",
                    "dailyActivity": [
                        {"date": "2026-08-06", "messageCount": 5, "sessionCount": 0, "toolCallCount": 0}
                    ],
                },
            )
        )
        self.assertTrue(r6["success"])
        agg6 = self.json(self.client.get("/agent-activity"))
        day6 = [a for a in agg6["activities"] if a["date"] == "2026-08-06"][0]
        # 旧值应是: messageCount=15(windows)+20(mac)=35, sessionCount=2+3=5, toolCallCount=5+8=13
        self.assertEqual(day6["messageCount"], 35)
        self.assertEqual(day6["sessionCount"], 5)
        self.assertEqual(day6["toolCallCount"], 13)

    def test_blog_posts_and_article_views(self):
        posts = [
            {
                "title": "Test post",
                "link": "https://blog.test/posts/test-post/",
                "date": "2026-07-29",
                "summary": "Summary",
            }
        ]
        extra = {"featuredProject": None, "featuredTimeline": None}
        with (
            mock.patch.object(backend, "fetch_blog_rss", return_value=posts),
            mock.patch.object(backend, "fetch_blog_extra", return_value=extra),
        ):
            result = self.json(self.client.get("/blog-posts?count=3"))
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["posts"][0]["title"], "Test post")

        first = self.json(
            self.client.post(
                "/blog/views/guides/test-post",
                headers={"X-Client-ID": "reader-a"},
            )
        )
        duplicate = self.json(
            self.client.post(
                "/blog/views/guides/test-post",
                headers={"X-Client-ID": "reader-a"},
            )
        )
        second_reader = self.json(
            self.client.post(
                "/blog/views/guides/test-post",
                headers={"X-Client-ID": "reader-b"},
            )
        )
        self.assertEqual((first["views"], first["counted"]), (1, True))
        self.assertEqual((duplicate["views"], duplicate["counted"]), (1, False))
        self.assertEqual((second_reader["views"], second_reader["counted"]), (2, True))

        totals = self.json(
            self.client.get(
                "/blog/views",
                query_string=[
                    ("slugs", "guides/test-post"),
                    ("slugs", "never-viewed"),
                ],
            )
        )
        self.assertEqual(totals["views"], {
            "guides/test-post": 2,
            "never-viewed": 0,
        })
        invalid = self.json(self.client.get("/blog/views?slugs=../bad"))
        self.assertFalse(invalid["success"])

    def test_image_route(self):
        response = self.client.get("/images/projects/calculator.png")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.mimetype.startswith("image/"))
        response.close()
        missing = self.json(self.client.get("/images/projects/missing.png"))
        self.assertFalse(missing["success"])

    def test_music_routes_full_lifecycle(self):
        upload = self.json(
            self.client.post(
                "/music/upload",
                query_string={"secret": TEST_CONFIG["admin_secret"]},
                data={
                    "file": (io.BytesIO(b"fake mp3 bytes"), "contract.mp3"),
                    "lyrics": (io.BytesIO(b"[00:00.00]hello"), "contract.lrc"),
                    "cover": (io.BytesIO(b"fake png bytes"), "cover.png"),
                    "title": "Contract song",
                    "artist": "Test artist",
                },
                content_type="multipart/form-data",
            )
        )
        filename = upload["file"]["filename"]
        self.assertTrue(upload["file"]["hasLyrics"])
        self.assertTrue(upload["file"]["hasCover"])

        listing = self.json(self.client.get("/music/list"))
        self.assertEqual(listing["music"][0]["filename"], filename)
        self.assertTrue(listing["music"][0]["hasLyrics"])
        self.assertTrue(listing["music"][0]["hasCover"])

        cover = self.client.get(f"/music/cover/{filename}")
        self.assertEqual(cover.status_code, 200)
        self.assertEqual(cover.get_data(), b"fake png bytes")
        cover.close()

        replaced = self.json(
            self.client.post(
                "/music/cover/upload",
                query_string={"secret": TEST_CONFIG["admin_secret"]},
                data={
                    "filename": filename,
                    "cover": (io.BytesIO(b"replacement webp"), "replacement.webp"),
                },
                content_type="multipart/form-data",
            )
        )
        self.assertTrue(replaced["hasCover"])
        replaced_cover = self.client.get(f"/music/cover/{filename}")
        self.assertEqual(replaced_cover.get_data(), b"replacement webp")
        replaced_cover.close()

        stream = self.client.get(f"/music/{filename}")
        self.assertEqual(stream.status_code, 200)
        self.assertEqual(stream.mimetype, "audio/mpeg")
        stream.get_data()
        stream.close()
        lyrics = self.client.get(f"/music/lyrics/{filename}")
        self.assertEqual(lyrics.status_code, 200)
        self.assertIn("hello", lyrics.get_data(as_text=True))
        lyrics.close()

        reordered = self.json(
            self.client.post(
                "/music/reorder",
                query_string={"secret": TEST_CONFIG["admin_secret"]},
                json={"order": [filename]},
            )
        )
        self.assertTrue(reordered["success"])

        deleted = self.json(
            self.client.post(
                "/music/delete",
                query_string={"secret": TEST_CONFIG["admin_secret"]},
                json={"filename": filename},
            )
        )
        self.assertEqual(deleted["deleted"], filename)
        deleted_cover = self.json(self.client.get(f"/music/cover/{filename}"))
        self.assertFalse(deleted_cover["success"])
        missing = self.json(self.client.get(f"/music/{filename}"))
        self.assertFalse(missing["success"])

    def test_calendar_routes_full_lifecycle(self):
        added = self.json(
            self.client.post(
                "/calendar/events",
                query_string={"secret": TEST_CONFIG["admin_secret"]},
                json={
                    "action": "add",
                    "event": {
                        "date": "2026-07-29",
                        "name": "Contract event",
                        "type": "holiday",
                    },
                },
            )
        )
        event_id = added["event"]["id"]

        updated = self.json(
            self.client.post(
                "/calendar/events",
                query_string={"secret": TEST_CONFIG["admin_secret"]},
                json={
                    "action": "update",
                    "event": {"id": event_id, "name": "Updated event"},
                },
            )
        )
        self.assertEqual(updated["event"]["name"], "Updated event")

        events = self.json(self.client.get("/calendar/events?date=2026-07"))
        self.assertEqual(len(events["events"]), 1)
        holidays = self.json(self.client.get("/calendar/holidays?year=2026"))
        self.assertEqual(holidays["year"], 2026)
        self.assertEqual(holidays["customHolidays"][0]["id"], event_id)

        deleted = self.json(
            self.client.post(
                "/calendar/events",
                query_string={"secret": TEST_CONFIG["admin_secret"]},
                json={"action": "delete", "id": event_id},
            )
        )
        self.assertEqual(deleted["deleted"], event_id)

    def test_github_stats_route(self):
        github_payload = {
            "username": "contract-user",
            "totalContributions": 7,
            "days": [],
            "topLanguages": [],
        }
        with mock.patch.object(
            backend,
            "fetch_github_contributions",
            return_value=github_payload,
        ):
            result = self.json(self.client.get("/github/stats"))
        self.assertTrue(result["success"])
        self.assertEqual(result["username"], "contract-user")

    def test_todos_routes_full_lifecycle(self):
        added = self.json(
            self.client.post(
                "/todos",
                query_string={"secret": TEST_CONFIG["admin_secret"]},
                json={"action": "add", "text": "Contract todo"},
            )
        )
        todo_id = added["todo"]["id"]

        completed = self.json(
            self.client.post(
                "/todos",
                query_string={"secret": TEST_CONFIG["admin_secret"]},
                json={"action": "complete", "id": todo_id},
            )
        )
        self.assertTrue(completed["todo"]["done"])
        listing = self.json(self.client.get("/todos"))
        self.assertEqual(listing["todos"][0]["id"], todo_id)

        deleted = self.json(
            self.client.post(
                "/todos",
                query_string={"secret": TEST_CONFIG["admin_secret"]},
                json={"action": "delete", "id": todo_id},
            )
        )
        self.assertEqual(deleted["deleted"], todo_id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
