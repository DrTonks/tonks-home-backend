#!/usr/bin/env python3
# coding: utf-8
"""
上传 Claude Code 使用统计到 Sleepy 服务器。
每次运行时会先从JSONL会话文件重新计算统计数据，更新stats-cache.json，再上传。

用法:
    python upload_agent_stats.py
    python upload_agent_stats.py --server http://your-server:9010 --secret YOUR_ADMIN_SECRET
    python upload_agent_stats.py --no-compute   # 跳过重新计算，直接上传已有缓存
    python upload_agent_stats.py --dry-run      # 仅显示即将上传的数据

可配合 Windows 任务计划程序或 cron 定时执行。
"""

import json
import os
import platform
import sys
import urllib.request
import urllib.error
import argparse
from datetime import datetime, timedelta, timezone
from collections import defaultdict


# === 路径工具 ===

def get_claude_dir():
    home = os.path.expanduser('~')
    return os.path.join(home, '.claude')


def get_stats_cache_path():
    return os.path.join(get_claude_dir(), 'stats-cache.json')


def get_projects_dir():
    return os.path.join(get_claude_dir(), 'projects')


def get_codex_sessions_dir():
    """Return the Codex CLI/Desktop session-log directory."""
    return os.path.join(os.path.expanduser('~'), '.codex', 'sessions')


# === stats-cache.json 读写 ===

def load_stats_cache(cache_path=None):
    path = cache_path or get_stats_cache_path()
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_stats_cache(cache, cache_path=None):
    path = cache_path or get_stats_cache_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


# === JSONL 会话文件解析 ===

TRANSCRIPT_TYPES = {'user', 'assistant', 'attachment', 'system'}

def is_transcript_message(entry):
    return entry.get('type') in TRANSCRIPT_TYPES


def find_session_files():
    """扫描 ~/.claude/projects/ 下所有 JSONL 文件。
    返回 [(filepath, is_subagent, parent_session_id)]
    """
    projects_dir = get_projects_dir()
    if not os.path.isdir(projects_dir):
        return []

    files = []
    for project_name in os.listdir(projects_dir):
        project_path = os.path.join(projects_dir, project_name)
        if not os.path.isdir(project_path):
            continue

        for entry_name in os.listdir(project_path):
            entry_path = os.path.join(project_path, entry_name)

            if os.path.isfile(entry_path) and entry_name.endswith('.jsonl'):
                # 主会话文件: {sessionId}.jsonl
                session_id = entry_name[:-6]
                files.append((entry_path, False, session_id))

                # 子 agent 文件: {sessionId}/subagents/agent-*.jsonl
                subagents_dir = os.path.join(entry_path[:-6], 'subagents')
                if os.path.isdir(subagents_dir):
                    for sf in os.listdir(subagents_dir):
                        if sf.startswith('agent-') and sf.endswith('.jsonl'):
                            files.append((
                                os.path.join(subagents_dir, sf),
                                True,
                                session_id  # 父会话 ID
                            ))

    return files


def process_session_file(filepath, is_subagent, parent_session_id):
    """解析单个 JSONL 文件，返回按日期聚合的临时统计。
    返回: {date: {'msg': int, 'tool': int, 'sids': set}}
    """
    daily = defaultdict(lambda: {'msg': 0, 'tool': 0, 'sids': set()})

    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if not is_transcript_message(entry):
                    continue

                # 主会话文件：排除子 agent 的旁链消息
                if not is_subagent and entry.get('isSidechain'):
                    continue

                ts = entry.get('timestamp', '')
                if not ts:
                    continue
                date_key = ts[:10]  # YYYY-MM-DD

                msg_type = entry.get('type', '')

                # messageCount: 仅主会话的非侧链消息 (user + assistant)
                if not is_subagent and msg_type in ('user', 'assistant'):
                    daily[date_key]['msg'] += 1

                # toolCallCount: 主会话 + 子 agent 的 assistant 消息
                if msg_type == 'assistant':
                    content = entry.get('message', {}).get('content', [])
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get('type') == 'tool_use':
                                daily[date_key]['tool'] += 1

                # 标记该 parentSessionId 在当天有活动
                daily[date_key]['sids'].add(parent_session_id)

    except Exception as e:
        print(f'  警告: 处理文件失败 {filepath}: {e}')

    return daily


def find_codex_session_files():
    """Recursively find Codex rollout JSONL session logs."""
    sessions_dir = get_codex_sessions_dir()
    if not os.path.isdir(sessions_dir):
        return []

    files = []
    for root, _, names in os.walk(sessions_dir):
        for name in names:
            if name.endswith('.jsonl'):
                files.append(os.path.join(root, name))
    return files


def process_codex_session_file(filepath):
    """Aggregate activity from one Codex session without reading token usage.

    User-visible messages are stored as event_msg records, while tool calls are
    response_item records. Only their counts are retained.
    """
    daily = defaultdict(lambda: {'msg': 0, 'tool': 0, 'sids': set()})
    session_id = os.path.splitext(os.path.basename(filepath))[0]

    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                timestamp = entry.get('timestamp', '')
                if not timestamp:
                    continue
                date_key = timestamp[:10]
                entry_type = entry.get('type')
                payload = entry.get('payload', {})

                if entry_type == 'event_msg' and isinstance(payload, dict):
                    if payload.get('type') in {'user_message', 'agent_message'}:
                        daily[date_key]['msg'] += 1
                        daily[date_key]['sids'].add(session_id)
                elif entry_type == 'response_item' and isinstance(payload, dict):
                    if payload.get('type') in {'custom_tool_call', 'function_call'}:
                        daily[date_key]['tool'] += 1
                        daily[date_key]['sids'].add(session_id)
    except Exception as e:
        print(f'  Warning: unable to process Codex session {filepath}: {e}')

    return daily


# === 统计计算 ===

def compute_daily_activity():
    """从所有 JSONL 会话文件重新计算 dailyActivity。
    返回与 stats-cache.json 兼容的 dailyActivity 列表。
    """
    claude_files = find_session_files()
    codex_files = find_codex_session_files()
    if not claude_files and not codex_files:
        return []

    print(f'  Found {len(claude_files)} Claude Code and {len(codex_files)} Codex session files...')

    aggregated = defaultdict(lambda: {'msg': 0, 'tool': 0, 'sids': set()})

    for filepath, is_subagent, parent_sid in claude_files:
        daily = process_session_file(filepath, is_subagent, parent_sid)
        for date_key, stats in daily.items():
            aggregated[date_key]['msg'] += stats['msg']
            aggregated[date_key]['tool'] += stats['tool']
            aggregated[date_key]['sids'].update(stats['sids'])

    for filepath in codex_files:
        daily = process_codex_session_file(filepath)
        for date_key, stats in daily.items():
            aggregated[date_key]['msg'] += stats['msg']
            aggregated[date_key]['tool'] += stats['tool']
            # Prefix prevents equal IDs from the two applications collapsing.
            aggregated[date_key]['sids'].update(
                f'codex:{sid}' for sid in stats['sids']
            )

    result = []
    for date_key in sorted(aggregated.keys()):
        stats = aggregated[date_key]
        result.append({
            'date': date_key,
            'messageCount': stats['msg'],
            'sessionCount': len(stats['sids']),
            'toolCallCount': stats['tool'],
        })

    return result


def update_stats_cache(cache_path=None):
    """重新计算统计并更新 stats-cache.json。
    返回更新后的 dailyActivity 列表。
    """
    print('[1/3] 从会话文件计算统计数据...')

    new_activities = compute_daily_activity()
    if not new_activities:
        print('  未找到任何活动数据')
        return []

    date_range = f"{new_activities[0]['date']} ~ {new_activities[-1]['date']}"
    total_msgs = sum(a['messageCount'] for a in new_activities)
    total_sessions = sum(a['sessionCount'] for a in new_activities)
    total_tools = sum(a['toolCallCount'] for a in new_activities)
    print(f'  计算完成: {len(new_activities)} 天, {total_msgs} 消息, {total_sessions} 会话, {total_tools} 工具调用')
    print(f'  日期范围: {date_range}')

    # 加载已有缓存并合并
    print('[2/3] 更新缓存...')
    existing = load_stats_cache(cache_path)

    if existing and existing.get('dailyActivity'):
        merged = {}
        for a in existing['dailyActivity']:
            merged[a['date']] = a
        for a in new_activities:
            merged[a['date']] = a  # 新数据覆盖同日旧数据
        merged_activities = sorted(merged.values(), key=lambda x: x['date'])
    else:
        merged_activities = new_activities

    # 构建/更新缓存
    if existing:
        cache = existing
    else:
        cache = {'version': 7}

    cache['lastComputedDate'] = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    cache['dailyActivity'] = merged_activities

    save_stats_cache(cache, cache_path)
    print(f'  缓存已更新: {len(merged_activities)} 条记录')

    return merged_activities


# === 上传 ===

def get_machine_id():
    """返回本机标识，优先读取环境变量 SLEEPY_MACHINE_ID，否则使用 hostname"""
    env_id = os.environ.get('SLEEPY_MACHINE_ID')
    if env_id:
        return env_id
    return platform.node()


def upload_stats(server_url, secret, machine_id, activities):
    """上传 dailyActivity 到服务器（携带 machineId）"""
    url = f"{server_url.rstrip('/')}/agent-activity?secret={secret}"
    payload = {
        "machineId": machine_id,
        "dailyActivity": activities,
    }
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')

    req = urllib.request.Request(
        url,
        data=data,
        headers={'Content-Type': 'application/json'},
        method='POST'
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode('utf-8'))
        return result
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8', errors='replace')
        return {'success': False, 'code': f'HTTP {e.code}', 'message': error_body}
    except urllib.error.URLError as e:
        return {'success': False, 'code': 'connection error', 'message': str(e.reason)}


# === 主流程 ===

def main():
    parser = argparse.ArgumentParser(description='上传 Claude Code 使用统计到 Sleepy 服务器')
    parser.add_argument('--server', default=os.environ.get('SLEEPY_SERVER_URL', 'http://127.0.0.1:9010'),
                        help='服务器地址（默认读取 SLEEPY_SERVER_URL）')
    parser.add_argument('--secret', default=os.environ.get('SLEEPY_ADMIN_SECRET'),
                        help='管理员密钥（默认读取 SLEEPY_ADMIN_SECRET）')
    parser.add_argument('--days', type=int, default=30,
                        help='仅上传最近 N 天的数据 (默认: 30)')
    parser.add_argument('--no-compute', action='store_true',
                        help='跳过会话文件扫描，直接使用已有缓存上传')
    parser.add_argument('--dry-run', action='store_true',
                        help='仅显示即将上传的数据，不实际发送')
    parser.add_argument('--cache-file', default=None,
                        help='stats-cache.json 路径 (默认自动查找)')
    args = parser.parse_args()

    if not args.secret and not args.dry_run:
        parser.error('需要 --secret 或环境变量 SLEEPY_ADMIN_SECRET')

    cache_path = args.cache_file

    # === Step 1: 更新 stats-cache.json ===
    if args.no_compute:
        print('[跳过] 统计计算 (--no-compute)')
        cache = load_stats_cache(cache_path)
        if not cache:
            print('错误: 找不到 stats-cache.json，且 --no-compute 下不会自动创建')
            sys.exit(1)
        activities = cache.get('dailyActivity', [])
    else:
        activities = update_stats_cache(cache_path)

    if not activities:
        print('没有 dailyActivity 数据，无需上传')
        return

    # === Step 2: 筛选最近 N 天 ===
    cutoff = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime('%Y-%m-%d')
    recent = [a for a in activities if a.get('date', '') >= cutoff]
    if not recent:
        print(f'最近 {args.days} 天内没有活动数据，无需上传')
        return

    date_range = f"{recent[0]['date']} ~ {recent[-1]['date']}" if recent else 'N/A'
    total_msgs = sum(a.get('messageCount', 0) for a in recent)
    total_sessions = sum(a.get('sessionCount', 0) for a in recent)
    total_tool_calls = sum(a.get('toolCallCount', 0) for a in recent)

    print(f'\n[3/3] 准备上传:')
    machine_id = get_machine_id()
    print(f'  机器标识: {machine_id}')
    print(f'  日期范围: {date_range}')
    print(f'  记录数: {len(recent)}')
    print(f'  总消息数: {total_msgs}')
    print(f'  总会话数: {total_sessions}')
    print(f'  总工具调用: {total_tool_calls}')

    if args.dry_run:
        print(f'\n[dry-run] 未实际发送。')
        print(f'将 POST 到: {args.server.rstrip("/")}/agent-activity?secret=***')
        return

    print(f'\n上传到: {args.server}')
    result = upload_stats(args.server, args.secret, machine_id, recent)

    if result.get('success'):
        new = result.get('new', result.get('count', '?'))
        total = result.get('total', '?')
        removed = result.get('removed', 0)
        print(f'[OK] Upload success: {new} new, {total} total stored, {removed} expired cleaned')
    else:
        print(f'[FAIL] Upload failed: {result.get("code", "unknown")} - {result.get("message", "")}')
        sys.exit(1)


if __name__ == '__main__':
    main()
