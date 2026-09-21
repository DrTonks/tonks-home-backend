"""Validated holiday-cn data with disk cache and non-blocking stale refresh."""
from datetime import date, timedelta
from pathlib import Path
import json
import os
import tempfile
import threading
import time
import urllib.request

SOURCE = 'https://raw.githubusercontent.com/NateScarlet/holiday-cn/master/{}.json'
TTL = 86400
RETRY = 3600


def validate_document(value, year):
    if not isinstance(value, dict) or type(value.get('year')) is not int or value['year'] != year:
        raise ValueError('Invalid holiday year')
    days, papers = value.get('days'), value.get('papers')
    if not isinstance(days, list) or len(days) > 400 or not isinstance(papers, list):
        raise ValueError('Invalid holiday dataset')
    if any(not isinstance(p, str) or not p.startswith('https://www.gov.cn/') for p in papers):
        raise ValueError('Invalid holiday sources')
    seen = set()
    clean = []
    for item in days:
        if not isinstance(item, dict):
            raise ValueError('Invalid holiday day')
        text, name, off = item.get('date'), item.get('name'), item.get('isOffDay')
        parsed = date.fromisoformat(text)
        if parsed.isoformat() != text or parsed.year not in (year - 1, year) or text in seen:
            raise ValueError('Invalid holiday date')
        if not isinstance(name, str) or not name.strip() or len(name) > 60 or type(off) is not bool:
            raise ValueError('Invalid holiday fields')
        seen.add(text)
        clean.append({'date': text, 'name': name.strip(), 'isOffDay': off})
    if days and not papers:
        raise ValueError('Holiday data requires published sources')
    return {'year': year, 'papers': papers, 'days': sorted(clean, key=lambda d: d['date'])}


def normalize_days(documents, year):
    # Next year's announcement may override dates in this December.
    entries = {}
    for document in sorted(documents, key=lambda d: d['year']):
        for item in document['days']:
            entries[item['date']] = dict(item, countryCode='CN')
    # Include adjacent ordinary weekends in continuous breaks, but never cross
    # an explicitly scheduled working day. Keep the derivation visible.
    for item in list(entries.values()):
        if not item['isOffDay']:
            continue
        for direction in (-1, 1):
            cursor = date.fromisoformat(item['date']) + timedelta(days=direction)
            while cursor.weekday() >= 5 and cursor.isoformat() not in entries:
                entries[cursor.isoformat()] = dict(date=cursor.isoformat(), name=item['name'], isOffDay=True, countryCode='CN', inferredWeekend=True)
                cursor += timedelta(days=direction)
    return [entries[key] for key in sorted(entries) if key.startswith(f'{year}-')]


class HolidayCalendar:
    def __init__(self, cache_dir, fetcher=None, clock=time.time, background=True):
        self.cache_dir = Path(cache_dir)
        self.fetcher = fetcher or self._download
        self.clock = clock
        self.background = background
        self.lock = threading.RLock()
        self.cache = {}
        self.attempts = {}
        self.running = set()

    @staticmethod
    def _download(year):
        req = urllib.request.Request(SOURCE.format(year), headers={'User-Agent': 'TonksCalendar/1.0'})
        with urllib.request.urlopen(req, timeout=3) as response:
            raw = response.read(256001)
        if len(raw) > 256000:
            raise ValueError('Holiday response too large')
        return json.loads(raw)

    def _load(self, year):
        if year in self.cache:
            return self.cache[year]
        paths = [(self.cache_dir / f'{year}.json', True), (Path(__file__).with_name('holiday_data') / f'{year}.json', False)]
        for path, cached in paths:
            try:
                value = json.loads(path.read_text(encoding='utf-8'))
                doc = validate_document(value['data'] if cached else value, year)
                stamp = float(value['fetchedAt']) if cached else 0
                if not 0 <= stamp <= self.clock() + 300:
                    raise ValueError('Invalid cache timestamp')
                self.cache[year] = (doc, stamp)
                return self.cache[year]
            except (OSError, ValueError, TypeError, KeyError):
                continue
        return None

    def _refresh(self, year):
        try:
            doc = validate_document(self.fetcher(year), year)
            with self.lock:
                old = self.cache.get(year)
                if old and old[0]['days'] and not doc['days']:
                    raise ValueError('Refusing empty replacement of published data')
            stamp = self.clock()
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            fd, temp = tempfile.mkstemp(prefix='holiday-', suffix='.tmp', dir=self.cache_dir)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    json.dump({'fetchedAt': stamp, 'data': doc}, stream, ensure_ascii=False)
                os.replace(temp, self.cache_dir / f'{year}.json')
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
            with self.lock:
                self.cache[year] = (doc, stamp)
        except (OSError, ValueError, TypeError, KeyError):
            # Keep the last valid disk/memory snapshot. Never fabricate dates.
            pass
        finally:
            with self.lock:
                self.running.discard(year)

    def _get(self, year):
        with self.lock:
            entry = self._load(year)
            now = self.clock()
            if entry and now - entry[1] < TTL:
                return entry
            if year in self.running or now - self.attempts.get(year, -TTL) < RETRY:
                return entry
            self.running.add(year)
            self.attempts[year] = now
        if entry and self.background:
            threading.Thread(target=self._refresh, args=(year,), name=f'holiday-{year}', daemon=True).start()
            return entry
        self._refresh(year)
        with self.lock:
            return self.cache.get(year)

    def get(self, year):
        if type(year) is not int or not 2007 <= year <= date.today().year + 1:
            raise ValueError('Unsupported holiday year')
        primary = self._get(year)
        following = self._get(year + 1) if year < date.today().year + 1 else None
        entries = [e for e in (primary, following) if e]
        documents = [e[0] for e in entries]
        days = normalize_days(documents, year)
        status = 'available' if primary and primary[0]['days'] else 'unpublished' if primary else 'unavailable'
        return {'publicHolidays': [d for d in days if d['isOffDay']],
                'workdays': [d for d in days if not d['isOffDay']],
                'holidayStatus': status, 'holidaySource': 'NateScarlet/holiday-cn',
                'holidayStale': any(self.clock() - e[1] >= TTL for e in entries),
                'holidayPapers': sorted({p for d in documents for p in d['papers']})}
