"""Search contract, paid-call safety and durable concurrent state regression tests."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import io
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.error
import urllib.request

REPO_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_DIR))
from pet_ai.config import PetAIConfig
from pet_ai.search import (DeepSeekSearchClient, DeepSeekWebSearch, ENDPOINT,
                           MAX_RESPONSE_BYTES, SearchError, _NoRedirect)
from pet_ai.search_store import SearchStore, SearchStateError


def config(**kwargs):
    with mock.patch.dict(os.environ, {'SLEEPY_AI_API_KEY': 'secret-key'}, clear=True):
        return replace(PetAIConfig.from_env(), **kwargs)


def result(**kwargs):
    return dict({'title': '公开作品', 'url': 'https://example.org/work',
                 'snippet': '短摘要', 'summary': '供模型核实作品的长摘要'}, **kwargs)


class Response(io.BytesIO):
    status = 200


def claim_process(path):
    try:
        return SearchStore(path).claim('query', 1700000000, 1, 30).kind
    except SearchStateError as exc:
        return str(exc)


class ConfigTests(unittest.TestCase):
    def test_key_reuse_only_for_official_https_host(self):
        self.assertEqual(config().effective_search_api_key, 'secret-key')
        for endpoint in ['https://proxy.test/v1', 'http://api.deepseek.com',
                         'https://api.deepseek.com.evil.test', 'https://api.deepseek.com@evil.test',
                         'https://user@api.deepseek.com', 'https://api.deepseek.com:9999', 'https://[invalid']:
            self.assertEqual(config(endpoint=endpoint).effective_search_api_key, '')
        self.assertEqual(config(endpoint='https://proxy.test', search_api_key='official-key').effective_search_api_key,
                         'official-key')

    def test_bounded_config_and_absolute_data_path(self):
        with mock.patch.dict(os.environ, {'SLEEPY_DATA_DIR': '/tmp/sleepy-test-data',
                             'SLEEPY_AI_SEARCH_TIMEOUT': '999', 'SLEEPY_AI_SEARCH_MAX_RESULTS': '-1',
                             'SLEEPY_AI_SEARCH_CACHE_TTL': 'bad', 'SLEEPY_AI_SEARCH_DB': 'search/db.sqlite3'}, clear=True):
            c = PetAIConfig.from_env()
        self.assertEqual(c.search_timeout_seconds, 15)
        self.assertEqual(c.search_max_results, 1)
        self.assertEqual(c.search_cache_ttl_seconds, 21600)
        self.assertEqual(c.search_db_path, str(Path('/tmp/sleepy-test-data/search/db.sqlite3').resolve()))


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.client = DeepSeekSearchClient('private-key')

    def search(self, payload, query='作品 动画'):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        with mock.patch.object(self.client._opener, 'open', return_value=Response(raw)) as opened:
            results = self.client.search(query)
        return results, opened.call_args

    def test_official_contract_and_bounded_summary(self):
        results, args = self.search({'results': [result(summary='长'*1200, content='do not include full text')]})
        request = args.args[0]
        self.assertEqual(request.full_url, ENDPOINT)
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(request.get_header('Authorization'), 'Bearer private-key')
        self.assertEqual(json.loads(request.data), {'query': '作品 动画', 'count': 3, 'need_content': False})
        self.assertEqual(len(results[0]['snippet']), 800)
        self.assertNotIn('content', results[0])
        self.assertEqual(args.kwargs['timeout'], 6)

    def test_snippet_fallback_duplicate_and_unsafe_url_filtering(self):
        results, _ = self.search({'results': [result(url='javascript:alert(1)'),
            result(url='https://user:pass@example.org'), result(url='https://example.org/\nbad'),
            result(summary=None), result(url='https://example.org/work#duplicate'),
            result(url='https://example.org/other')]})
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]['snippet'], '短摘要')

    def test_empty_results_are_valid_but_malformed_results_are_not(self):
        self.assertEqual(self.search({'results': []})[0], [])
        for payload in [b'not json', b'\xff', {}, [], {'results': {}}, {'results': [None]}, {'results': [result(summary='', snippet='')]}]:
            with self.subTest(payload=payload), self.assertRaises(SearchError):
                self.search(payload)

    def test_query_normalization_and_validation_without_http(self):
        self.assertEqual(json.loads(self.search({'results': []}, '  Ａ作品\n 动画  ')[1].args[0].data)['query'], 'A作品 动画')
        for query in ['', ' ', 'x'*129, None, 'title\0bad']:
            with mock.patch.object(self.client._opener, 'open') as opened, self.assertRaises(SearchError):
                self.client.search(query)
            opened.assert_not_called()

    def test_response_size_and_stream_deadline(self):
        with self.assertRaisesRegex(SearchError, 'too_large'):
            self.search(b'x'*(MAX_RESPONSE_BYTES+1))
        with mock.patch('pet_ai.search.time.monotonic', side_effect=[0,7]), self.assertRaisesRegex(SearchError, 'timeout'):
            self.search({'results': []})

    def test_http_errors_do_not_retry_or_expose_response(self):
        for status, code in [(401,'auth_failed'), (402,'insufficient_balance'), (429,'rate_limited'), (503,'http_error'), (302,'http_error')]:
            error = urllib.error.HTTPError(ENDPOINT, status, 'private upstream body', {'Retry-After':'999'}, io.BytesIO(b'private-key'))
            with mock.patch.object(self.client._opener, 'open', side_effect=error) as opened:
                with self.assertRaises(SearchError) as caught:
                    self.client.search('作品')
            self.assertIn(code, str(caught.exception))
            self.assertNotIn('private', str(caught.exception))
            self.assertEqual(opened.call_count, 1)
            if status==429:
                self.assertEqual(caught.exception.cooldown, 300)

    def test_network_failure_and_redirect_blocking(self):
        with mock.patch.object(self.client._opener,'open',side_effect=TimeoutError), self.assertRaisesRegex(SearchError,'unavailable'):
            self.client.search('作品')
        self.assertIsNone(_NoRedirect().redirect_request(urllib.request.Request(ENDPOINT),None,302,'',{},'https://evil.test'))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'search.sqlite3')
        self.store = SearchStore(self.path, max_entries=2)
        self.now = 1700000000.0

    def test_cache_survives_restart_and_is_free_when_budget_exhausted(self):
        claim=self.store.claim('a',self.now,1,30)
        self.store.finish('a',claim,self.now,results=[result()],ttl=100)
        restarted=SearchStore(self.path)
        self.assertEqual(restarted.claim('a',self.now+1,1,30).kind,'cache')
        with self.assertRaisesRegex(SearchStateError,'daily_limit'):
            restarted.claim('b',self.now+1,1,30)
        stats=restarted.stats()[0]
        self.assertEqual((stats['attempts'],stats['successes'],stats['cache_hits']),(1,1,1))
        self.assertEqual(restarted.claim('b',self.now+86400,1,30).kind,'owner')

    def test_errors_cooldown_empty_cache_and_expiry(self):
        a=self.store.claim('a',self.now,3,30)
        self.store.finish('a',a,self.now,results=None,cooldown=10)
        with self.assertRaisesRegex(SearchStateError,'cooldown'):
            self.store.claim('b',self.now+1,3,30)
        b=self.store.claim('b',self.now+11,3,30)
        self.store.finish('b',b,self.now+11,results=[],ttl=300)
        self.assertEqual(self.store.claim('b',self.now+12,3,30).results,[])
        self.assertEqual(self.store.claim('b',self.now+312,3,30).kind,'owner')
        self.assertEqual(self.store.stats()[0]['attempts'],3)

    def test_cross_process_reservation_and_crash_lease_expiry(self):
        context=multiprocessing.get_context('spawn')
        with context.Pool(3) as pool:
            results=pool.map(claim_process,[self.path]*3)
        self.assertEqual(results.count('owner'),1)
        self.assertEqual(results.count('wait'),2)
        # Crash does not refund an uncertain billed request when its lease expires.
        with self.assertRaisesRegex(SearchStateError,'daily_limit'):
            self.store.claim('query',self.now+31,1,30)

    def test_parallel_different_queries_cannot_exceed_budget(self):
        def claim(i):
            try:
                return SearchStore(self.path).claim(str(i),self.now,2,30).kind
            except SearchStateError as exc:
                return str(exc)
        with ThreadPoolExecutor(max_workers=5) as pool:
            results=list(pool.map(claim,range(10)))
        self.assertEqual(results.count('owner'),2)
        self.assertEqual(self.store.stats()[0]['attempts'],2)

    def test_cache_capacity_and_stale_owner_cannot_replace_new_result(self):
        old=self.store.claim('a',self.now,10,1)
        new=self.store.claim('a',self.now+2,10,30)
        self.store.finish('a',new,self.now+2,results=[{'title':'new'}],ttl=100)
        self.store.finish('a',old,self.now+3,results=[{'title':'old'}],ttl=100)
        self.assertEqual(self.store.claim('a',self.now+4,10,30).results,[{'title':'new'}])
        for i,key in enumerate(['b','c']):
            c=self.store.claim(key,self.now+5+i,10,30)
            self.store.finish(key,c,self.now+5+i,results=[],ttl=100)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM search_cache').fetchone()[0],2)
            self.assertIsNone(db.execute("SELECT 1 FROM search_cache WHERE key='a'").fetchone())


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config=config(search_db_path=str(Path(self.tmp.name)/'search.sqlite3'))

    def test_concurrent_identical_queries_share_one_call_and_log_no_query(self):
        started=threading.Event()
        def search(query):
            started.set()
            time.sleep(.15)
            return [result()]
        client=mock.Mock(search=mock.Mock(side_effect=search))
        first=DeepSeekWebSearch(self.config,client=client)
        second=DeepSeekWebSearch(self.config,client=client)
        with self.assertLogs('pet_ai.search',level='INFO') as logs, ThreadPoolExecutor(2) as pool:
            a=pool.submit(first.search,'私密输入作品')
            self.assertTrue(started.wait(2))
            b=pool.submit(second.search,'私密输入作品')
            self.assertEqual(a.result(),b.result())
        self.assertEqual(client.search.call_count,1)
        self.assertNotIn('私密输入',str(logs.output))
        self.assertNotIn('secret-key',str(logs.output))
        with sqlite3.connect(self.config.search_db_path) as db:
            self.assertNotIn('私密输入',str(db.execute('SELECT * FROM search_cache').fetchall()))

    def test_failed_calls_retain_budget_and_negative_cache_is_free(self):
        client=mock.Mock()
        client.search.side_effect=[SearchError('timeout'),[]]
        search=DeepSeekWebSearch(replace(self.config,daily_search_limit=2),client=client)
        with self.assertRaises(SearchError):search.search('a')
        self.assertEqual(search.search('b'),[])
        self.assertEqual(search.search('b'),[])
        with self.assertRaisesRegex(SearchError,'daily_limit'):search.search('c')
        self.assertEqual(client.search.call_count,2)
        stats=search.store.stats()[0]
        self.assertEqual((stats['attempts'],stats['errors'],stats['empty'],stats['cache_hits']),(2,1,1,1))

    def test_missing_key_or_storage_failure_never_bypasses_budget(self):
        client=mock.Mock()
        for c in [replace(self.config,endpoint='https://proxy.test'),replace(self.config,search_enabled=False)]:
            with self.assertRaises(SearchError):DeepSeekWebSearch(c,client=client).search('作品')
        store=mock.Mock()
        store.claim.side_effect=sqlite3.OperationalError('sensitive db path')
        with self.assertRaisesRegex(SearchError,'storage_unavailable'):
            DeepSeekWebSearch(self.config,client=client,store=store).search('作品')
        client.search.assert_not_called()

    def test_successful_cached_results_work_during_provider_cooldown(self):
        client=mock.Mock()
        client.search.side_effect=[[result()],SearchError('search_insufficient_balance',cooldown=60)]
        search=DeepSeekWebSearch(self.config,client=client)
        search.search('cached')
        with self.assertRaises(SearchError):search.search('uncached')
        self.assertEqual(search.search('cached')[0]['title'],'公开作品')
        with self.assertRaisesRegex(SearchError,'cooldown'):search.search('other')
        self.assertEqual(client.search.call_count,2)


class OperationsTests(unittest.TestCase):
    def test_status_command_is_local_and_does_not_print_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            env = {k:v for k,v in os.environ.items() if not k.startswith('SLEEPY_')}
            env.update(SLEEPY_DATA_DIR=temporary, SLEEPY_AI_API_KEY='secret-marker',
                       SLEEPY_ENV_FILE=str(Path(temporary)/'absent.env'))
            run = subprocess.run([sys.executable,'-m','pet_ai.search_status'],
                cwd=REPO_DIR,env=env,capture_output=True,text=True,check=True)
            data = json.loads(run.stdout)
            self.assertEqual(data['days'], [])
            self.assertTrue(data['key_configured'])
            self.assertNotIn('secret-marker',run.stdout+run.stderr)

    def test_search_info_logs_are_enabled_once_with_warning_root(self):
        code = """
import logging
from sleepy_app.common.logging import configure_pet_search_logging
logging.basicConfig(level=logging.WARNING)
configure_pet_search_logging()
configure_pet_search_logging()
logging.getLogger('pet_ai.search').info('pet_search outcome=cache_hit elapsed_ms=1')
"""
        run = subprocess.run([sys.executable,'-c',code],cwd=REPO_DIR,
                             capture_output=True,text=True,check=True)
        self.assertEqual(run.stderr.count('pet_search outcome=cache_hit'),1)


if __name__=='__main__':unittest.main(verbosity=2)
