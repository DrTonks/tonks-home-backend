"""Model discovery selection, isolation, cache and concurrency contracts."""
import concurrent.futures
import io
import json
import threading
import time
import unittest
import urllib.error
from unittest import mock

from test_pet_ai import config, FakeHttpResponse
from pet_ai.models import ModelResolver, select_model
from pet_ai.provider import OpenAICompatibleProvider


class Response(io.BytesIO):
    status = 200


def response(entries):
    return Response(json.dumps({'data': entries}).encode())


class ModelsTests(unittest.TestCase):
    def setUp(self):
        self.config = config(endpoint='https://api.deepseek.com/chat/completions')
        self.now = [100.0]
        self.opener = mock.Mock()
        self.resolver = ModelResolver(self.config, opener=self.opener, clock=lambda: self.now[0])

    def test_selects_flash_by_id_or_display_name_then_first_valid(self):
        for entries, expected in [
            ([{'id': 'pro'}, {'id': 'Next-Flash'}], 'Next-Flash'),
            ([{'id': 'pro'}, {'id': 'next', 'name': 'DeepSeek Flash'}], 'next'),
            ([None, {'id': ''}, {'id': 'pro'}, {'id': 'other'}], 'pro'),
        ]:
            self.assertEqual(select_model({'data': entries}), expected)
        for bad in [None, [], {'data': []}, {'data': 'oops'}, {'data': [{'id': 123}]}]:
            with self.assertRaises(ValueError):
                select_model(bad)

    def test_official_request_is_cached_and_refreshes_after_six_hours(self):
        self.opener.open.side_effect = [response([{'id': 'flash-a'}]), response([{'id': 'flash-b'}])]
        self.assertEqual(self.resolver.resolve(), 'flash-a')
        self.assertEqual(self.resolver.resolve(), 'flash-a')
        self.assertEqual(self.opener.open.call_count, 1)
        req = self.opener.open.call_args.args[0]
        self.assertEqual(req.full_url, 'https://api.deepseek.com/models')
        self.assertEqual(req.get_header('Authorization'), 'Bearer test-key')
        self.assertEqual(self.opener.open.call_args.kwargs['timeout'], 3)
        self.now[0] += 21601
        self.assertEqual(self.resolver.resolve(), 'flash-b')

    def test_failure_reuses_stale_selection_and_waits_before_retry(self):
        self.opener.open.side_effect = [response([{'id': 'flash-a'}]), OSError('sensitive-body'), response([{'id': 'flash-b'}])]
        self.resolver.resolve()
        self.now[0] += 21601
        with self.assertLogs('pet_ai.models', level='WARNING') as logs:
            self.assertEqual(self.resolver.resolve(), 'flash-a')
        self.assertNotIn('sensitive-body', str(logs.output))
        self.assertEqual(self.resolver.resolve(), 'flash-a')
        self.assertEqual(self.opener.open.call_count, 2)
        self.now[0] += 61
        self.assertEqual(self.resolver.resolve(), 'flash-b')

    def test_cold_failure_uses_configured_model_and_caps_response(self):
        for upstream in [Response(b'x' * 131073), response([]), Response(b'not-json')]:
            resolver = ModelResolver(self.config, opener=self.opener)
            self.opener.open.return_value = upstream
            with self.assertLogs('pet_ai.models', level='WARNING'):
                self.assertEqual(resolver.resolve(), 'test-model')
            self.assertTrue(upstream.closed)

    def test_manual_and_nonofficial_endpoints_do_not_discover(self):
        for overrides in [dict(model_auto=False), dict(api_key=''),
                          dict(endpoint='https://proxy.example/chat/completions'),
                          dict(endpoint='https://api.deepseek.com.evil.test/'),
                          dict(endpoint='http://api.deepseek.com/'),
                          dict(endpoint='https://user@api.deepseek.com/'),
                          dict(endpoint='https://api.deepseek.com:444/')]:
            from dataclasses import replace
            resolver = ModelResolver(replace(self.config, **overrides), opener=self.opener)
            self.assertEqual(resolver.resolve(), 'test-model')
        self.opener.open.assert_not_called()

    def test_slow_continuous_body_exits_and_releases_cache_lock(self):
        clock = self.now
        class SlowResponse(Response):
            def read1(self, maximum):
                clock[0] += 2
                return b' '
        upstream = SlowResponse()
        self.opener.open.return_value = upstream
        with self.assertLogs('pet_ai.models', level='WARNING'):
            self.assertEqual(self.resolver.resolve(), 'test-model')
        self.assertTrue(upstream.closed)
        self.assertEqual(self.now[0], 104)
        self.assertEqual(self.resolver.resolve(), 'test-model')
        self.assertEqual(self.opener.open.call_count, 1)

    def test_parallel_cold_requests_only_discover_once(self):
        barrier = threading.Barrier(8)
        def fetch(*args, **kwargs):
            time.sleep(.03)
            return response([{'id': 'flash-a'}])
        self.opener.open.side_effect = fetch
        def run(_):
            barrier.wait()
            return self.resolver.resolve()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(list(pool.map(run, range(8))), ['flash-a'] * 8)
        self.assertEqual(self.opener.open.call_count, 1)

    def test_reply_pins_model_across_rounds_and_overlapping_replies(self):
        provider = OpenAICompatibleProvider(self.config)
        provider.models = self.resolver
        self.opener.open.side_effect = [response([{'id': 'flash-a'}]), response([{'id': 'flash-b'}])]
        first = provider.for_reply()
        self.now[0] += 21601
        second = provider.for_reply()
        with mock.patch('pet_ai.provider.urllib.request.urlopen', return_value=FakeHttpResponse()) as send:
            first.chat([])
            second.chat([])
            first.chat([])
        self.assertEqual([json.loads(c.args[0].data)['model'] for c in send.call_args_list],
                         ['flash-a', 'flash-b', 'flash-a'])

    def test_redirect_is_not_followed(self):
        from pet_ai.models import _NoRedirect
        handler = _NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, '', {}, 'https://evil.test'))
        err = urllib.error.HTTPError('https://api.deepseek.com/models', 302, 'redirect', {}, io.BytesIO())
        self.opener.open.side_effect = err
        with self.assertLogs('pet_ai.models', level='WARNING'):
            self.assertEqual(self.resolver.resolve(), 'test-model')
        self.assertTrue(err.fp.closed)
