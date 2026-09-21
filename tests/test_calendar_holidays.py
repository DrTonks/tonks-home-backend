import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from sleepy_app.integrations.calendar import HolidayCalendar, normalize_days, validate_document, TTL


def document(year=2026, days=None):
    return {'year': year, 'papers': ['https://www.gov.cn/test'], 'days': days or []}


class CalendarTests(unittest.TestCase):
    def test_validation_rejects_wrong_year_dates_types_duplicates(self):
        for days in [[{'date': '2026-02-30', 'name': '节日', 'isOffDay': True}],
                     [{'date': '2026-01-01', 'name': '节日', 'isOffDay': 'false'}],
                     [{'date': '2026-01-01', 'name': '节日', 'isOffDay': True}] * 2]:
            with self.assertRaises((ValueError, TypeError)):
                validate_document(document(days=days), 2026)
        with self.assertRaises(ValueError):
            validate_document(document(), 2025)

    def test_weekend_extension_stops_at_makeup_workday(self):
        days = [{'date': '2026-06-19', 'name': '端午节', 'isOffDay': True},
                {'date': '2026-06-21', 'name': '端午节', 'isOffDay': False}]
        result = normalize_days([document(days=days)], 2026)
        self.assertEqual([(d['date'], d['isOffDay']) for d in result], [('2026-06-19', True), ('2026-06-20', True), ('2026-06-21', False)])
        self.assertTrue(result[1]['inferredWeekend'])

    def test_next_year_overrides_december(self):
        old = document(days=[{'date': '2026-12-31', 'name': '旧', 'isOffDay': True}])
        new = document(2027, [{'date': '2026-12-31', 'name': '元旦', 'isOffDay': False}])
        self.assertFalse(normalize_days([new, old], 2026)[0]['isOffDay'])

    def test_seed_has_correct_ranges_and_no_invented_next_year(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HolidayCalendar(directory, fetcher=Mock(side_effect=OSError()), background=False)
            result = store.get(2026)
            days = {d['date']: d for d in result['publicHolidays']}
            self.assertIn('2026-02-15', days)
            self.assertIn('2026-04-04', days)
            self.assertNotIn('2026-04-07', days)
            self.assertIn('2026-09-20', [d['date'] for d in result['workdays']])
            self.assertEqual(store.get(2027)['holidayStatus'], 'unpublished')

    def test_persistent_cache_refresh_and_stale_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = Mock(return_value=2_000_000_000)
            fetch = Mock(side_effect=lambda year: document(year, [{'date': f'{year}-01-01', 'name': '元旦', 'isOffDay': True}]))
            store = HolidayCalendar(directory, fetcher=fetch, clock=clock, background=False)
            result = store.get(2026)
            self.assertFalse(result['holidayStale'])
            calls = fetch.call_count
            store.get(2026)
            self.assertEqual(fetch.call_count, calls)
            restarted = HolidayCalendar(directory, fetcher=Mock(side_effect=OSError()), clock=clock, background=False)
            self.assertEqual(restarted.get(2026)['publicHolidays'], result['publicHolidays'])
            clock.return_value += TTL + 1
            stale = restarted.get(2026)
            self.assertTrue(stale['holidayStale'])
            self.assertEqual(stale['publicHolidays'], result['publicHolidays'])
            self.assertEqual(list(Path(directory).glob('*.tmp')), [])

    def test_empty_upstream_cannot_wipe_published_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HolidayCalendar(directory, fetcher=lambda year: document(year), background=False)
            self.assertGreater(len(store.get(2026)['publicHolidays']), 20)

    def test_bad_disk_cache_falls_back_to_seed_and_throttles_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / '2026.json').write_text('{bad')
            fetch = Mock(side_effect=OSError())
            store = HolidayCalendar(directory, fetcher=fetch, background=False)
            self.assertEqual(store.get(2026)['holidayStatus'], 'available')
            calls = fetch.call_count
            store.get(2026)
            self.assertEqual(fetch.call_count, calls)
            self.assertEqual(store.get(2007)['holidayStatus'], 'unavailable')

    def test_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            fetch = Mock()
            store = HolidayCalendar(directory, fetcher=fetch)
            for year in [True, 1, 2006, 99999]:
                with self.assertRaises(ValueError): store.get(year)
            fetch.assert_not_called()

if __name__ == '__main__': unittest.main()
