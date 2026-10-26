import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from clients import upload_agent_stats as stats


DAY = '2026-10-01'


def record(kind, payload, timestamp=DAY + 'T09:00:00.000Z'):
    return {'timestamp': timestamp, 'type': kind, 'payload': payload}


def meta(session_id='session-a', **kwargs):
    return record('session_meta', {'id': session_id, **kwargs})


def message(role, text, **kwargs):
    return record('response_item', {
        'type': 'message', 'role': role,
        'content': [{'type': 'input_text' if role == 'user' else 'output_text', 'text': text}],
        **kwargs,
    })


def event(role, text):
    return record('event_msg', {'type': 'user_message' if role == 'user' else 'agent_message', 'message': text})


def tool(call_id='call-1', kind='function_call'):
    return record('response_item', {'type': kind, 'call_id': call_id, 'name': 'test', 'arguments': '{}'})


class AgentActivityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # macOS exposes /var through /private/var; discovery returns real paths.
        self.root = Path(self.tmp.name).resolve()

    def log(self, name, entries):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('\n'.join(json.dumps(e) for e in entries) + '\n', encoding='utf8')
        return str(path)

    def test_new_desktop_messages_without_legacy_events(self):
        path = self.log('new.jsonl', [
            meta(), message('user', 'question', id='u1'),
            message('assistant', 'progress', phase='commentary', id='a1'),
            message('assistant', 'answer', phase='final_answer', id='a2'),
            tool(), tool('call-2', 'custom_tool_call'),
            record('response_item', {'type': 'function_call_output', 'call_id': 'call-1', 'output': 'ok'}),
        ])
        self.assertEqual(stats.process_codex_session_file(path)[DAY], {
            'msg': 3, 'tool': 2, 'sids': {'session-a'},
        })

    def test_context_reasoning_and_guardian_are_not_visible_messages(self):
        path = self.log('context.jsonl', [
            meta(), message('developer', 'instructions'),
            message('user', '<environment_context>context</environment_context>'),
            message('user', 'setup', internal_chat_message_metadata_passthrough={
                'content_item_kinds': ['environments.environment_context']}),
            message('assistant', 'reasoning', channel='analysis'),
            message('assistant', 'summary', phase='summary'),
            meta('background', thread_source='guardian_review'),
            message('user', 'review request'), message('assistant', 'review result'), tool(),
            meta('session-a'), message('user', 'real question'),
        ])
        self.assertEqual(stats.process_codex_session_file(path)[DAY], {
            'msg': 1, 'tool': 0, 'sids': {'session-a'},
        })

    def test_legacy_and_response_mirrors_are_not_added_twice(self):
        path = self.log('legacy.jsonl', [
            meta(), event('user', 'hello'), message('user', 'hello'),
            event('assistant', 'answer'), message('assistant', 'answer'),
            event('user', 'hello'), message('user', 'hello'),
            # A format switch within the same session/day still counts new messages.
            message('user', 'new-format-only'), event('assistant', 'legacy-only'),
        ])
        self.assertEqual(stats.process_codex_session_file(path)[DAY]['msg'], 5)

    def test_archived_copies_and_resume_segments_are_deduplicated(self):
        initial = [meta(), message('user', 'question', id='m1'), tool()]
        paths = [
            self.log('sessions/first.jsonl', initial),
            self.log('archived_sessions/copy.jsonl', initial),
            self.log('sessions/resumed.jsonl', initial + [message('assistant', 'answer', id='m2'), tool('call-2')]),
            self.log('sessions/other.jsonl', [meta('session-b'), message('user', 'question', id='m1')]),
        ]
        self.assertEqual(stats.process_codex_session_files(paths)[DAY], {
            'msg': 3, 'tool': 2, 'sids': {'session-a', 'session-b'},
        })

    def test_same_prompt_after_format_switch_is_not_lost(self):
        later = message('user', 'continue')
        later['timestamp'] = DAY + 'T10:00:00Z'
        path = self.log('switch.jsonl', [meta(), event('user', 'continue'), later])
        self.assertEqual(stats.process_codex_session_file(path)[DAY]['msg'], 2)

    def test_mirrors_with_different_timestamps_match_once(self):
        response = message('assistant', 'answer')
        response['timestamp'] = DAY + 'T09:00:00.200Z'
        path = self.log('mirrors.jsonl', [meta(), event('assistant', 'answer'), response])
        self.assertEqual(stats.process_codex_session_file(path)[DAY]['msg'], 1)

    def test_no_id_copies_preserve_real_repeated_messages(self):
        entries = [meta(), event('user', 'again'), event('user', 'again'), tool()]
        paths = [self.log('a.jsonl', entries), self.log('b.jsonl', entries)]
        self.assertEqual(stats.process_codex_session_files(paths)[DAY]['msg'], 2)

    def test_image_only_user_message_counts(self):
        path = self.log('image.jsonl', [meta(), record('response_item', {
            'type': 'message', 'role': 'user', 'content': [{'type': 'input_image', 'image_url': 'local-image'}],
        })])
        self.assertEqual(stats.process_codex_session_file(path)[DAY]['msg'], 1)

    def test_malformed_lines_do_not_abort_remaining_records(self):
        path = self.log('bad.jsonl', [None, [], {}, record('response_item', None),
            record('event_msg', {}, timestamp=3),
            record('event_msg', {'type': 'user_message', 'message': 'bad-date'}, timestamp='2026-99-99'),
            meta(), message('user', 'valid'),
        ])
        with open(path, 'a', encoding='utf8') as f:
            f.write('{incomplete\n' + json.dumps(message('assistant', 'still valid')) + '\n')
        self.assertEqual(stats.process_codex_session_file(path)[DAY]['msg'], 2)

    def test_custom_home_active_and_archive_discovery(self):
        active = self.log('custom/sessions/2026/active.jsonl', [])
        archived = self.log('custom/archived_sessions/old.jsonl', [])
        with patch.dict(os.environ, {'CODEX_HOME': str(self.root / 'custom')}):
            self.assertEqual(set(stats.find_codex_session_files()), {active, archived})

    def test_archive_without_active_directory_is_scanned(self):
        archived = self.log('custom/archived_sessions/only.jsonl', [])
        with patch.dict(os.environ, {'CODEX_HOME': str(self.root / 'custom')}):
            self.assertEqual(stats.find_codex_session_files(), [archived])

    def test_claude_and_codex_counts_remain_separate(self):
        claude = self.log('claude.jsonl', [
            {'type': 'user', 'timestamp': DAY + 'T09:00:00Z'},
            {'type': 'assistant', 'timestamp': DAY + 'T09:00:01Z', 'message': {'content': [{'type': 'tool_use'}]}},
        ])
        codex = self.log('codex.jsonl', [meta('same-id'), message('user', 'hi'), tool()])
        with patch.object(stats, 'find_session_files', return_value=[(claude, False, 'same-id')]), \
             patch.object(stats, 'find_codex_session_files', return_value=[codex]), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(stats.compute_daily_activity(), [{
                'date': DAY, 'messageCount': 3, 'sessionCount': 2, 'toolCallCount': 2,
            }])

    def test_cache_merge_is_monotonic_and_preview_does_not_write(self):
        cache_path = self.root / 'stats-cache.json'
        original = {'version': 7, 'unrelated': {'keep': True}, 'dailyActivity': [{
            'date': DAY, 'messageCount': 0, 'sessionCount': 5, 'toolCallCount': 20,
        }]}
        cache_path.write_text(json.dumps(original), encoding='utf8')
        original_bytes = cache_path.read_bytes()
        new = [{'date': DAY, 'messageCount': 10, 'sessionCount': 3, 'toolCallCount': 2}]
        with patch.object(stats, 'compute_daily_activity', return_value=new), contextlib.redirect_stdout(io.StringIO()):
            preview = stats.update_stats_cache(str(cache_path), write=False)
            self.assertEqual(cache_path.read_bytes(), original_bytes)
            actual = stats.update_stats_cache(str(cache_path))
        self.assertEqual(actual, preview)
        self.assertEqual(actual[0], {'date': DAY, 'messageCount': 10, 'sessionCount': 5, 'toolCallCount': 20})
        self.assertEqual(json.loads(cache_path.read_text())['unrelated'], {'keep': True})

    def test_upload_sends_aggregate_counts_and_header_auth(self):
        activity = [{'date': DAY, 'messageCount': 2, 'sessionCount': 1, 'toolCallCount': 3}]
        with patch.object(stats.urllib.request, 'urlopen') as request:
            request.return_value.__enter__.return_value.read.return_value = b'{"success": true}'
            result = stats.upload_stats('https://example.com/api/', 'test-secret', 'test-machine', activity)
        self.assertTrue(result['success'])
        req = request.call_args.args[0]
        self.assertEqual(req.full_url, 'https://example.com/api/agent-activity')
        self.assertEqual(req.get_header('X-admin-secret'), 'test-secret')
        self.assertEqual(json.loads(req.data), {'machineId': 'test-machine', 'dailyActivity': activity})

    def test_cli_dry_run_neither_writes_cache_nor_uploads(self):
        today = stats.datetime.now(stats.timezone.utc).strftime('%Y-%m-%d')
        with patch('sys.argv', ['upload_agent_stats.py', '--dry-run']), \
             patch.object(stats, 'compute_daily_activity', return_value=[{
                 'date': today, 'messageCount': 2, 'sessionCount': 1, 'toolCallCount': 1}]), \
             patch.object(stats, 'load_stats_cache', return_value=None), \
             patch.object(stats, 'save_stats_cache') as save, \
             patch.object(stats, 'upload_stats') as upload, contextlib.redirect_stdout(io.StringIO()):
            stats.main()
        save.assert_not_called()
        upload.assert_not_called()


if __name__ == '__main__':
    unittest.main()
