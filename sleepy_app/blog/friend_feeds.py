"""Daily RSS/Atom snapshots for the blog's configured friends (no public fetch proxy)."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import Request, HTTPRedirectHandler, build_opener
import copy
import ipaddress
import json
import logging
import math
import os
import re
import socket
import threading
import time
import uuid
import xml.etree.ElementTree as ET

TTL = 86400
MAX_BYTES = 4 * 1024 * 1024
log = logging.getLogger(__name__)


def web_url(value, base=None):
    if not isinstance(value, str):
        raise ValueError('Expected a URL string')
    value = urljoin(base or '', (value or '').strip())
    parsed = urlparse(value)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Expected an HTTP(S) URL')
    return value


def public_url(value):
    value = web_url(value)
    parsed = urlparse(value)
    for address in socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80), type=socket.SOCK_STREAM):
        if not ipaddress.ip_address(address[4][0]).is_global:
            raise ValueError('Feed address must be public')
    return value


class PublicRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, public_url(newurl))


def fetch_feed(url):
    url = public_url(url)
    request = Request(url, headers={'User-Agent': 'TonksBlog/1.0 (+https://blog.tonks.top/)', 'Accept': 'application/rss+xml, application/atom+xml, application/xml, text/xml'})
    with build_opener(PublicRedirects()).open(request, timeout=10) as response:
        deadline = time.monotonic() + 20
        parts, size = [], 0
        while True:
            if time.monotonic() > deadline:
                raise ValueError('Feed exceeds time limit')
            # read() waits to fill its buffer. A trickling peer could keep that
            # call alive indefinitely without triggering the socket timeout.
            part = response.read1(65536)
            if time.monotonic() > deadline:
                raise ValueError('Feed exceeds time limit')
            if not part:
                break
            size += len(part)
            if size > MAX_BYTES or time.monotonic() > deadline:
                raise ValueError('Feed exceeds size/time limit')
            parts.append(part)
        return b''.join(parts)


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.skip += 1
        elif tag in ('p', 'br', 'div', 'li', 'h1', 'h2', 'h3'):
            self.parts.append(' ')

    def handle_endtag(self, tag):
        if tag in ('script', 'style') and self.skip:
            self.skip -= 1
        elif tag in ('p', 'div', 'li'):
            self.parts.append(' ')

    def handle_data(self, text):
        if not self.skip:
            self.parts.append(text)


def plain_text(value, limit):
    parser = PlainText()
    parser.feed(value or '')
    text = re.sub(r'\s+', ' ', unescape(''.join(parser.parts))).strip()
    return text[:limit].rstrip() + ('…' if len(text) > limit else '')


def timestamp(value):
    try:
        date = parsedate_to_datetime(value)
    except (ValueError, TypeError, OverflowError):
        try:
            date = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
        except (ValueError, TypeError, AttributeError):
            return None
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    try:
        return date.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def parse_feed(data, feed_url, now=None):
    if len(data) > MAX_BYTES:
        raise ValueError('Feed too large')
    # Decode first so UTF-16 declarations cannot bypass the DTD/entity check.
    if isinstance(data, bytes):
        if data.startswith((b'\xff\xfe', b'\xfe\xff')):
            data = data.decode('utf-16')
        else:
            data = data.decode('utf-8-sig')
    if re.search(r'<!\s*(DOCTYPE|ENTITY)', data, re.I):
        raise ValueError('DTD/entity declarations are unsupported')
    root = ET.fromstring(data)
    name = lambda node: node.tag.split('}')[-1]
    if name(root) not in ('rss', 'feed', 'RDF'):
        raise ValueError('Not an RSS/Atom document')
    now = time.time() if now is None else now
    candidates = []
    for item in root.iter():
        if name(item) not in ('item', 'entry'):
            continue
        fields = {}
        links = []
        for child in item:
            tag = name(child)
            fields[tag] = ''.join(child.itertext()) if not list(child) else ET.tostring(child, encoding='unicode', method='html')
            if tag == 'link':
                if child.attrib.get('rel', 'alternate') == 'alternate':
                    links.append(child.attrib.get('href') or child.text or '')
        title = plain_text(fields.get('title'), 200)
        date = next((parsed for key in ('pubDate', 'published', 'date', 'updated') if (parsed := timestamp(fields.get(key))) is not None), None)
        if not title or date is None or date > now + 300:
            continue
        raw_link = next((link for link in links if link.strip()), fields.get('guid', ''))
        if not raw_link:
            continue
        try:
            link = web_url(raw_link, feed_url)
        except ValueError:
            continue
        summary = next((plain_text(fields.get(key), 180) for key in ('description', 'summary', 'encoded', 'content') if plain_text(fields.get(key), 180)), '')
        candidates.append({'feedUrl': feed_url, 'title': title, 'url': link, 'summary': summary or '这篇文章没有提供摘要，去朋友的博客读读吧。', 'publishedAt': datetime.fromtimestamp(date, timezone.utc).isoformat().replace('+00:00', 'Z')})
    if not candidates:
        raise ValueError('No dated articles in feed')
    return max(candidates, key=lambda article: timestamp(article['publishedAt']))


class FriendFeeds:
    def __init__(self, manifest, cache, fetcher=fetch_feed, clock=time.time, background=True):
        self.manifest, self.cache = Path(manifest), Path(cache)
        self.fetcher, self.clock, self.background = fetcher, clock, background
        self._lock, self._refresh_lock = threading.Lock(), threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self.state = {'schema': 1, 'checkedAt': 0, 'feeds': [], 'articles': []}
        try:
            saved = json.loads(self.cache.read_text(encoding='utf-8'))
            if (isinstance(saved, dict) and saved.get('schema') == 1
                    and isinstance(saved.get('articles'), list)
                    and all(isinstance(a, dict) and all(isinstance(a.get(key), str) for key in ('feedUrl', 'title', 'url', 'summary', 'publishedAt'))
                            and isinstance(a.get('lastSuccessAt'), (int, float)) and math.isfinite(a['lastSuccessAt']) for a in saved['articles'])
                    and isinstance(saved.get('feeds'), list) and all(isinstance(feed, str) for feed in saved['feeds'])
                    and isinstance(saved.get('checkedAt'), (int, float)) and math.isfinite(saved['checkedAt'])):
                self.state = saved
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def configured(self):
        document = json.loads(self.manifest.read_text(encoding='utf-8'))
        if not isinstance(document, dict) or document.get('schema') != 1 or not isinstance(document.get('feeds'), list) or len(document['feeds']) > 100:
            raise ValueError('Invalid friend feed manifest')
        if not all(isinstance(friend, dict) and isinstance(friend.get('rss'), str) for friend in document['feeds']):
            raise ValueError('Invalid friend feed entry')
        return sorted(set(web_url(friend['rss']) for friend in document['feeds']))

    def refresh(self):
        if not self._refresh_lock.acquire(blocking=False):
            return
        try:
            feeds = self.configured()
            now = self.clock()
            with self._lock:
                previous = copy.deepcopy(self.state)
            elapsed = now - previous['checkedAt']
            if feeds == previous['feeds'] and previous['checkedAt'] and 0 <= elapsed < TTL:
                return
            old = {article['feedUrl']: article for article in previous['articles'] if isinstance(article, dict) and article.get('feedUrl') in feeds}
            def update(feed):
                try:
                    article = parse_feed(self.fetcher(feed), feed, now)
                    return {**article, 'stale': False, 'lastSuccessAt': now}
                except Exception as error:
                    log.warning('Friend feed update failed (%s): %s', feed, type(error).__name__)
                    return {**old[feed], 'stale': True} if feed in old else None
            with ThreadPoolExecutor(max_workers=4, thread_name_prefix='friend-rss') as pool:
                articles = [article for article in pool.map(update, feeds) if article]
            state = {'schema': 1, 'checkedAt': now, 'feeds': feeds, 'articles': articles}
            with self._lock:
                self.state = state
            self.cache.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.cache.with_name(f'{self.cache.name}.{uuid.uuid4().hex}.tmp')
            try:
                temporary.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
                temporary.replace(self.cache)
            finally:
                temporary.unlink(missing_ok=True)
        except (OSError, ValueError, TypeError, KeyError) as error:
            log.warning('Friend feed manifest/cache unavailable: %s', type(error).__name__)
        finally:
            self._refresh_lock.release()

    def _run(self):
        while not self._stop.is_set():
            self.refresh()
            self._stop.wait(60)

    def start(self):
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name='friend-feed-cache', daemon=True)
                self._thread.start()

    def get(self):
        if self.background:
            self.start()
        else:
            self.refresh()
        with self._lock:
            result = copy.deepcopy(self.state)
        try:
            allowed = self.configured()
        except (OSError, ValueError, TypeError, KeyError):
            allowed = []
        result['articles'] = [a for a in result['articles'] if isinstance(a, dict) and a.get('feedUrl') in allowed]
        result['pending'] = bool(allowed) and (not result['checkedAt'] or result['feeds'] != allowed)
        for article in result['articles']:
            if self.clock() - article.get('lastSuccessAt', 0) >= TTL:
                article['stale'] = True
        return result

    def close(self):
        self._stop.set()


def register_friend_feeds(app, paths):
    from flask import jsonify
    service = FriendFeeds(os.environ.get('SLEEPY_FRIEND_FEEDS_MANIFEST') or Path(paths.article_manifest).with_name('friend-feeds.json'), os.environ.get('SLEEPY_FRIEND_FEEDS_CACHE') or Path(paths.data_file).with_name('friend-feeds-cache.json'))
    app.extensions['friend_feeds'] = service
    @app.get('/blog/friend-feeds')
    def friend_feeds():
        response = jsonify(service.get())
        response.headers['Cache-Control'] = 'no-store'
        return response
