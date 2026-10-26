import copy
import io
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sleepy_app.blog.dates import latest_dated_entry
from sleepy_app.blog.service import BlogService


class BlogDateTests(unittest.TestCase):
    def test_unpadded_month_does_not_hide_september_project(self):
        projects = [
            {'id': 'dshtui-2026', 'startDate': '2026-8-17'},
            {'id': 'deepseedian', 'startDate': '2026-09-10'},
        ]
        self.assertEqual(latest_dated_entry(projects)['id'], 'deepseedian')

    def test_calendar_order_across_days_months_and_years(self):
        for earlier, later in [
            ('2026-9-9', '2026-09-10'),
            ('2026-9-30', '2026-10-01'),
            ('2025-12-31', '2026-1-1'),
            ('2024-2-28', '2024-02-29'),
        ]:
            with self.subTest(earlier=earlier, later=later):
                entries = [{'startDate': earlier}, {'startDate': later}]
                self.assertIs(latest_dated_entry(entries), entries[1])

    def test_invalid_rows_and_dates_do_not_hide_valid_entry(self):
        valid = {'id': 'valid', 'startDate': '2026-09-10'}
        invalid = [None, [], 'invalid', {}, {'startDate': None}, {'startDate': 20260911},
                   {'startDate': 'unknown'}, {'startDate': ''}, {'startDate': '2026-13-01'},
                   {'startDate': '2026-02-29'}, {'startDate': '2026-04-31'}]
        self.assertIs(latest_dated_entry(invalid + [valid]), valid)

    def test_no_valid_date_returns_none(self):
        for entries in (None, {}, 'invalid', [], [None, {}, {'startDate': 'bad'}]):
            with self.subTest(entries=entries):
                self.assertIsNone(latest_dated_entry(entries))

    def test_equal_dates_preserve_order_without_mutating_source(self):
        entries = [{'id': 'first', 'startDate': '2026-9-10'},
                   {'id': 'second', 'startDate': '2026-09-10'}]
        before = copy.deepcopy(entries)
        self.assertIs(latest_dated_entry(entries), entries[0])
        self.assertEqual(entries, before)


class BlogExtraDateTests(unittest.TestCase):
    def setUp(self):
        self.service = BlogService(SimpleNamespace(d=SimpleNamespace(data={})))
        env = patch.dict(os.environ, {
            'SLEEPY_BLOG_BASE_URL': 'https://blog.test',
            'SLEEPY_BLOG_DATA_URL': 'https://blog.test/data',
        })
        env.start()
        self.addCleanup(env.stop)

    def fetch(self, projects, timeline):
        responses = [io.BytesIO(json.dumps(entries).encode()) for entries in (projects, timeline)]
        with patch('sleepy_app.blog.service.urllib.request.urlopen', side_effect=responses) as fetch:
            result = self.service.fetch_blog_extra()
        self.assertEqual([call.args[0].full_url for call in fetch.call_args_list], [
            'https://blog.test/data/projects.json', 'https://blog.test/data/timeline.json'])
        self.assertTrue(all(call.kwargs == {'timeout': 10} for call in fetch.call_args_list))
        return result

    def test_project_and_timeline_share_date_order_preserving_display_fields(self):
        projects = [
            {'id': 'older', 'startDate': '2026-8-17', 'featured': True},
            {'id': 'newest', 'startDate': '2026-09-10', 'image': '/images/projects/new.png',
             'sourceCode': 'https://github.com/example/project', 'tags': ['DSH']},
        ]
        timeline = [
            {'id': 'older', 'startDate': '2026-9-9'},
            {'id': 'newest', 'startDate': '2026-09-10', 'image': ['/images/timeline/new.jpg'],
             'links': [{'name': 'link', 'url': 'https://example.com', 'type': 'website'}]},
        ]
        result = self.fetch(projects, timeline)
        project = result['featuredProject']
        self.assertEqual(project['id'], 'newest')
        self.assertEqual(project['startDate'], '2026-09-10')
        self.assertEqual(project['image'], 'https://blog.test/images/projects/new.png')
        self.assertEqual(project['tags'], ['DSH'])
        self.assertEqual(project['links'][0]['url'], projects[1]['sourceCode'])
        self.assertEqual(result['featuredTimeline']['id'], 'newest')
        self.assertEqual(result['featuredTimeline']['image'], ['https://blog.test/images/timeline/new.jpg'])
        self.assertEqual(result['featuredTimeline']['links'], timeline[1]['links'])

    def test_invalid_project_dates_do_not_block_valid_timeline(self):
        result = self.fetch([{'startDate': 'invalid'}, None], [{'id': 'timeline', 'startDate': '2026-9-1'}])
        self.assertIsNone(result['featuredProject'])
        self.assertEqual(result['featuredTimeline']['id'], 'timeline')

    def test_malformed_payloads_keep_response_shape(self):
        self.assertEqual(self.fetch({'error': 'unavailable'}, []), {
            'featuredProject': None, 'featuredTimeline': None})


if __name__ == '__main__':
    unittest.main()
