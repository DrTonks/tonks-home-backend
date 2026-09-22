import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock
from unittest.mock import patch
from types import SimpleNamespace
from sleepy_app.blog.friend_feeds import FriendFeeds, fetch_feed, parse_feed, public_url, TTL

NOW = 1790035200  # 2026-09-22 UTC
FEED = 'https://example.com/rss.xml'

def rss(title='Latest'):
    return f'''<rss version="2.0"><channel>
      <item><title>Old pinned post</title><link>https://example.com/old</link><pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate></item>
      <item><title>{title}</title><link>/new</link><pubDate>Mon, 21 Sep 2026 00:00:00 GMT</pubDate><description><![CDATA[<p>Hello &amp; friends</p><script>bad()</script>]]></description></item>
      <item><title>Future</title><link>/future</link><pubDate>Fri, 01 Jan 2027 00:00:00 GMT</pubDate></item>
    </channel></rss>'''.encode()

class FeedTests(unittest.TestCase):
    def test_fetch_assembles_short_network_chunks_without_buffer_filling_read(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.side_effect = AssertionError('read() can block while filling its buffer')
        response.read1.side_effect = [b'<rss>', b'</rss>', b'']
        opener = Mock()
        opener.open.return_value = response
        with patch('sleepy_app.blog.friend_feeds.public_url', return_value=FEED), \
                patch('sleepy_app.blog.friend_feeds.build_opener', return_value=opener), \
                patch('sleepy_app.blog.friend_feeds.time.monotonic', return_value=0):
            self.assertEqual(fetch_feed(FEED), b'<rss></rss>')
        response.read.assert_not_called()
        response.__exit__.assert_called_once()

    def test_fetch_stops_a_trickling_stream_at_the_total_deadline(self):
        elapsed = [0]
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.side_effect = AssertionError('read() would wait for thousands of tiny chunks')
        def trickle(_size):
            # Data arrives often enough to avoid the ten-second socket timeout.
            elapsed[0] += 4
            if elapsed[0] > 24:
                raise AssertionError('Fetching continued beyond the total deadline')
            return b'x'
        response.read1.side_effect = trickle
        opener = Mock()
        opener.open.return_value = response
        with patch('sleepy_app.blog.friend_feeds.public_url', return_value=FEED), \
                patch('sleepy_app.blog.friend_feeds.build_opener', return_value=opener), \
                patch('sleepy_app.blog.friend_feeds.time.monotonic', side_effect=lambda: elapsed[0]):
            with self.assertRaisesRegex(ValueError, 'time limit'):
                fetch_feed(FEED)
        self.assertLessEqual(elapsed[0], 24)
        response.read.assert_not_called()
        response.__exit__.assert_called_once()

    def test_fetch_rejects_eof_that_arrives_after_the_deadline(self):
        elapsed = [0]
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.side_effect = AssertionError('Expected an incremental read')
        def late_eof(_size):
            elapsed[0] = 21
            return b''
        response.read1.side_effect = late_eof
        opener = Mock()
        opener.open.return_value = response
        with patch('sleepy_app.blog.friend_feeds.public_url', return_value=FEED), \
                patch('sleepy_app.blog.friend_feeds.build_opener', return_value=opener), \
                patch('sleepy_app.blog.friend_feeds.time.monotonic', side_effect=lambda: elapsed[0]):
            with self.assertRaisesRegex(ValueError, 'time limit'):
                fetch_feed(FEED)
        response.__exit__.assert_called_once()

    def test_rss_newest_by_date_plain_text_and_relative_link(self):
        item = parse_feed(rss(), FEED, NOW)
        self.assertEqual(item['title'], 'Latest')
        self.assertEqual(item['url'], 'https://example.com/new')
        self.assertEqual(item['summary'], 'Hello & friends')

    def test_atom_links_and_xhtml_summary(self):
        item = parse_feed(b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Atom</title><published>2026-09-21T12:00:00+08:00</published><updated>2026-09-22T00:00:00Z</updated><link rel="self" href="/api/entry"/><link rel="alternate" href="/article"/><summary type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml">A <b>small</b> note</div></summary></entry></feed>''', FEED, NOW)
        self.assertEqual(item['url'], 'https://example.com/article')
        self.assertEqual(item['publishedAt'], '2026-09-21T04:00:00Z')
        self.assertIn('small', item['summary'])

    def test_reject_malformed_html_entities_future_and_unsafe_links(self):
        for data in [b'<html>Not a feed</html>', b'<rss>', b'<!DOCTYPE rss [<!ENTITY x "x">]><rss/>', '<!DOCTYPE rss><rss/>'.encode('utf-16'), rss().replace(b'/new', b'javascript:alert(1)').replace(b'https://example.com/old', b'data:x')]:
            with self.assertRaises(ValueError if data != b'<rss>' else Exception):
                parse_feed(data, FEED, NOW)
        with self.assertRaises(ValueError): public_url('http://127.0.0.1/feed')
        with self.assertRaises(ValueError): public_url('file:///etc/passwd')

    def test_daily_cache_restart_stale_and_removed_feeds(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, cache = Path(directory)/'manifest.json', Path(directory)/'cache.json'
            manifest.write_text(json.dumps({'schema':1,'feeds':[{'rss':FEED}]}))
            clock=Mock(return_value=NOW)
            fetch=Mock(return_value=rss())
            store=FriendFeeds(manifest,cache,fetch,clock,background=False)
            first=store.get()
            self.assertEqual(len(first['articles']),1)
            store.get()
            self.assertEqual(fetch.call_count,1)
            restarted=FriendFeeds(manifest,cache,fetch,clock,background=False)
            self.assertEqual(restarted.get(),first)
            self.assertEqual(fetch.call_count,1)
            clock.return_value += TTL+1
            fetch.side_effect=OSError('down')
            stale=restarted.get()
            self.assertTrue(stale['articles'][0]['stale'])
            self.assertEqual(stale['articles'][0]['title'],'Latest')
            restarted.get()
            self.assertEqual(fetch.call_count,2)
            manifest.write_text(json.dumps({'schema':1,'feeds':[]}))
            self.assertEqual(restarted.get()['articles'],[])
            self.assertEqual(list(Path(directory).glob('*.tmp')),[])

    def test_one_failed_feed_does_not_hide_others_and_does_not_hammer_upstream(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest=Path(directory)/'manifest.json'
            manifest.write_text(json.dumps({'schema':1,'feeds':[{'rss':FEED},{'rss':'https://other.example/rss'}]}))
            def fetch(url):
                if url!=FEED:raise OSError('down')
                return rss()
            fetch=Mock(side_effect=fetch)
            store=FriendFeeds(manifest,Path(directory)/'cache.json',fetch,lambda:NOW,background=False)
            self.assertEqual(len(store.get()['articles']),1)
            store.get()
            self.assertEqual(fetch.call_count,2)

    def test_missing_manifest_has_no_fetches(self):
        with tempfile.TemporaryDirectory() as directory:
            fetch=Mock()
            store=FriendFeeds(Path(directory)/'missing.json',Path(directory)/'cache.json',fetch,background=False)
            self.assertEqual(store.get()['articles'],[])
            fetch.assert_not_called()

    def test_bad_manifest_or_cache_cannot_crash_the_reader(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, cache = Path(directory)/'manifest.json', Path(directory)/'cache.json'
            manifest.write_text('[]')
            cache.write_text('{"schema":1,"checkedAt":"bad","articles":[],"feeds":[]}')
            fetch=Mock(return_value=rss())
            store=FriendFeeds(manifest,cache,fetch,lambda:NOW,background=False)
            self.assertEqual(store.get()['articles'],[])
            fetch.assert_not_called()
            manifest.write_text(json.dumps({'schema':1,'feeds':[{'rss':FEED}]}))
            self.assertEqual(len(store.get()['articles']),1)

    def test_readonly_api_uses_only_manifest_urls(self):
        from flask import Flask
        from sleepy_app.blog.friend_feeds import register_friend_feeds
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'friend-feeds.json').write_text(json.dumps({'schema':1,'feeds':[{'rss':FEED}]}))
            app=Flask(__name__)
            with patch.dict('os.environ', {'SLEEPY_FRIEND_FEEDS_MANIFEST':str(root/'friend-feeds.json'),'SLEEPY_FRIEND_FEEDS_CACHE':str(root/'cache.json')}):
                register_friend_feeds(app,SimpleNamespace(article_manifest=root/'comments.json',data_file=root/'data.json'))
            service=app.extensions['friend_feeds']
            service.background=False
            service.clock=lambda:NOW
            service.fetcher=Mock(return_value=rss())
            response=app.test_client().get('/blog/friend-feeds?url=http://127.0.0.1/private')
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json['articles'][0]['feedUrl'],FEED)
            service.fetcher.assert_called_once_with(FEED)
            self.assertEqual(app.test_client().post('/blog/friend-feeds').status_code,405)

if __name__ == '__main__':unittest.main()
