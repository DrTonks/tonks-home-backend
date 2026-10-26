"""DeepSeek-only bounded web search. No HTML scraping or arbitrary URL fetching."""
from __future__ import annotations

import hashlib
import html
import http.client
import json
import logging
import re
import sqlite3
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from .config import PetAIConfig
from .search_store import SearchStateError, SearchStore

LOGGER = logging.getLogger(__name__)
ENDPOINT = "https://api.deepseek.com/v1/tools/search"
MAX_RESPONSE_BYTES = 524_288


class SearchError(RuntimeError):
    def __init__(self, code: str, *, cooldown: int = 0):
        super().__init__(code)
        self.code = code
        self.cooldown = cooldown


def _clean(value: object, maximum: int) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", html.unescape(value))).strip()[:maximum]


def normalize_query(query: str) -> str:
    if not isinstance(query, str):
        raise SearchError('invalid_query')
    query = re.sub(r"\s+", " ", unicodedata.normalize('NFKC', query)).strip()
    if not query or len(query) > 128 or re.search(r"[\x00-\x1f\x7f]", query):
        raise SearchError('invalid_query')
    return query


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the Authorization header to a redirected host.
        return None


class DeepSeekSearchClient:
    def __init__(self, api_key: str, timeout: int = 6, count: int = 3):
        self.api_key = api_key
        self.timeout = timeout
        self.count = max(1, min(count, 5))
        self._opener = urllib.request.build_opener(_NoRedirect())

    def search(self, query: str) -> list[dict]:
        query = normalize_query(query)
        if not self.api_key:
            raise SearchError('search_not_configured')
        request = urllib.request.Request(ENDPOINT, method='POST',
            data=json.dumps({'query': query, 'count': self.count, 'need_content': False}, ensure_ascii=False).encode(),
            headers={'Authorization': f'Bearer {self.api_key}', 'Content-Type': 'application/json',
                     'Accept': 'application/json', 'User-Agent': 'SleepyPetAI/2.0'})
        try:
            deadline = time.monotonic() + self.timeout
            with self._opener.open(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise SearchError('search_http_error', cooldown=5)
                chunks, size = [], 0
                while True:
                    if time.monotonic() >= deadline:
                        raise SearchError('search_timeout', cooldown=5)
                    # read1 returns after one underlying read, so a trickling
                    # response cannot indefinitely reset the socket timeout.
                    chunk = response.read1(min(8192, MAX_RESPONSE_BYTES + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise SearchError('search_response_too_large', cooldown=5)
                raw = b''.join(chunks)
        except urllib.error.HTTPError as exc:
            code = {401: 'search_auth_failed', 402: 'search_insufficient_balance',
                    429: 'search_rate_limited'}.get(exc.code, 'search_http_error')
            cooldown = 60 if exc.code in {401, 402, 403} else 5
            if exc.code == 429:
                try:
                    cooldown = max(5, min(300, int(exc.headers.get('Retry-After', '30'))))
                except (ValueError, TypeError):
                    cooldown = 30
            exc.close()
            raise SearchError(code, cooldown=cooldown) from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
            raise SearchError('search_unavailable', cooldown=5) from None
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError):
            raise SearchError('search_invalid_response', cooldown=5) from None
        source = payload.get('results') if isinstance(payload, dict) else None
        if not isinstance(source, list):
            raise SearchError('search_invalid_response', cooldown=5)
        results, seen = [], set()
        for item in source[:50]:
            if not isinstance(item, dict):
                continue
            title = _clean(item.get('title'), 200)
            excerpt = _clean(item.get('summary'), 800) or _clean(item.get('snippet'), 800)
            url = item.get('url')
            if (not title or not excerpt or not isinstance(url, str) or len(url) > 2048
                    or re.search(r'[\x00-\x20\x7f]', url)):
                continue
            try:
                parsed = urllib.parse.urlsplit(url)
                if parsed.scheme not in {'https', 'http'} or not parsed.hostname or parsed.username or parsed.password:
                    continue
            except ValueError:
                continue
            url = urllib.parse.urlunsplit(parsed._replace(fragment=''))
            if url in seen:
                continue
            seen.add(url)
            results.append({'title': title, 'snippet': excerpt, 'url': url,
                            'site_name': _clean(item.get('site_name'), 100),
                            'date': _clean(item.get('date'), 40)})
            if len(results) == self.count:
                break
        if source and not results:
            raise SearchError('search_invalid_response', cooldown=5)
        return results


class DeepSeekWebSearch:
    def __init__(self, config: PetAIConfig, *, client=None, store=None):
        self.config = config
        self.available = bool(config.search_enabled and config.effective_search_api_key)
        self.client = client or DeepSeekSearchClient(config.effective_search_api_key,
                                                    config.search_timeout_seconds, config.search_max_results)
        self.store = store or SearchStore(config.search_db_path, config.search_cache_max_entries)

    def search(self, query: str) -> list[dict]:
        if not self.available:
            raise SearchError('search_not_configured')
        query = normalize_query(query)
        # Version/count distinguish cache entries when the excerpt contract changes.
        key = hashlib.sha256(f'v1|{self.config.search_max_results}|{query}'.encode()).hexdigest()
        started = time.monotonic()
        outcome = 'error'
        try:
            while True:
                claim = self.store.claim(key, time.time(), self.config.daily_search_limit,
                                         2 * self.config.search_timeout_seconds + 10)
                if claim.kind == 'cache':
                    outcome = 'cache_hit'
                    return claim.results
                if claim.kind == 'owner':
                    break
                if time.monotonic() - started >= self.config.search_timeout_seconds:
                    raise SearchError('search_busy')
                time.sleep(.1)
            try:
                results = self.client.search(query)
            except SearchError as exc:
                self.store.finish(key, claim, time.time(), results=None, cooldown=exc.cooldown)
                raise
            ttl = self.config.search_cache_ttl_seconds if results else 300
            self.store.finish(key, claim, time.time(), results=results, ttl=ttl)
            outcome = 'success' if results else 'empty'
            return results
        except SearchStateError as exc:
            outcome = str(exc)
            raise SearchError(outcome) from None
        except (sqlite3.Error, OSError):
            outcome = 'search_storage_unavailable'
            # Never bypass the persistent budget if storage is unavailable.
            raise SearchError(outcome) from None
        except SearchError as exc:
            outcome = exc.code
            raise
        finally:
            LOGGER.info('pet_search outcome=%s elapsed_ms=%d', outcome, (time.monotonic() - started) * 1000)
