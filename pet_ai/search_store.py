"""SQLite search cache, cross-worker leases and conservative paid-call budgets.

Only result excerpts and hashed cache keys are stored, never raw queries or keys.
A reservation is retained even if the caller crashes or the upstream times out:
we cannot know whether an ambiguous request was billed by the provider.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid


class SearchStateError(RuntimeError):
    pass


@dataclass(frozen=True)
class Claim:
    kind: str
    results: list | None = None
    token: str = ""
    day: str = ""


class SearchStore:
    def __init__(self, path: str, max_entries: int = 1024):
        self.path = Path(path)
        self.max_entries = max_entries
        self._ready = False
        self._lock = threading.Lock()

    @contextmanager
    def _db(self):
        with self._lock:
            if not self._ready:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
                os.close(fd)
                deadline = time.monotonic() + 2
                while True:
                    try:
                        with closing(sqlite3.connect(self.path, timeout=.1)) as db:
                            db.execute("PRAGMA journal_mode=WAL")
                            db.executescript("""
                        CREATE TABLE IF NOT EXISTS search_cache (
                            key TEXT PRIMARY KEY, payload TEXT NOT NULL,
                            expires REAL NOT NULL, touched REAL NOT NULL);
                        CREATE TABLE IF NOT EXISTS search_leases (
                            key TEXT PRIMARY KEY, token TEXT NOT NULL, expires REAL NOT NULL);
                        CREATE TABLE IF NOT EXISTS search_days (
                            day TEXT PRIMARY KEY, attempts INTEGER NOT NULL DEFAULT 0,
                            successes INTEGER NOT NULL DEFAULT 0, empty INTEGER NOT NULL DEFAULT 0,
                            errors INTEGER NOT NULL DEFAULT 0, cache_hits INTEGER NOT NULL DEFAULT 0,
                            blocked INTEGER NOT NULL DEFAULT 0);
                        CREATE TABLE IF NOT EXISTS search_cooldown (
                            id INTEGER PRIMARY KEY CHECK(id=1), until REAL NOT NULL);
                            """)
                        break
                    except sqlite3.OperationalError as exc:
                        # WAL's first activation can return SQLITE_BUSY without
                        # honoring busy_timeout when several workers start together.
                        if not any(word in str(exc).lower() for word in ('locked', 'busy')) or time.monotonic() >= deadline:
                            raise
                        time.sleep(.025)
                self._ready = True
        db = sqlite3.connect(self.path, timeout=2, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    def claim(self, key: str, now: float, limit: int, lease_seconds: float) -> Claim:
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        oldest = datetime.fromtimestamp(now - 31 * 86400, timezone.utc).date().isoformat()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM search_cache WHERE expires<=?", (now,))
            db.execute("DELETE FROM search_leases WHERE expires<=?", (now,))
            db.execute("DELETE FROM search_days WHERE day<?", (oldest,))
            db.execute("INSERT OR IGNORE INTO search_days(day) VALUES (?)", (day,))
            cached = db.execute("SELECT payload FROM search_cache WHERE key=?", (key,)).fetchone()
            if cached:
                try:
                    results = json.loads(cached['payload'])
                    if not isinstance(results, list):
                        raise ValueError('invalid cache')
                except (ValueError, TypeError):
                    db.execute("DELETE FROM search_cache WHERE key=?", (key,))
                else:
                    db.execute("UPDATE search_cache SET touched=? WHERE key=?", (now, key))
                    db.execute("UPDATE search_days SET cache_hits=cache_hits+1 WHERE day=?", (day,))
                    db.commit()
                    return Claim('cache', results=results)
            cooldown = db.execute("SELECT until FROM search_cooldown WHERE id=1").fetchone()
            if cooldown and cooldown['until'] > now:
                db.execute("UPDATE search_days SET blocked=blocked+1 WHERE day=?", (day,))
                db.commit()
                raise SearchStateError('search_cooldown')
            if db.execute("SELECT 1 FROM search_leases WHERE key=?", (key,)).fetchone():
                db.commit()
                return Claim('wait')
            attempts = db.execute("SELECT attempts FROM search_days WHERE day=?", (day,)).fetchone()[0]
            if attempts >= limit:
                db.execute("UPDATE search_days SET blocked=blocked+1 WHERE day=?", (day,))
                db.commit()
                raise SearchStateError('search_daily_limit')
            token = uuid.uuid4().hex
            db.execute("INSERT INTO search_leases VALUES (?,?,?)", (key, token, now + lease_seconds))
            db.execute("UPDATE search_days SET attempts=attempts+1 WHERE day=?", (day,))
            db.commit()
            return Claim('owner', token=token, day=day)

    def finish(self, key: str, claim: Claim, now: float, *, results: list | None,
               ttl: int = 0, cooldown: int = 0) -> None:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            owned = db.execute("DELETE FROM search_leases WHERE key=? AND token=?", (key, claim.token)).rowcount
            # Expired owners must not overwrite a newer result or its lease.
            if owned and results is not None:
                db.execute("INSERT OR REPLACE INTO search_cache VALUES (?,?,?,?)",
                           (key, json.dumps(results, ensure_ascii=False), now + ttl, now))
                db.execute("DELETE FROM search_cache WHERE key IN (SELECT key FROM search_cache "
                           "ORDER BY touched DESC, key LIMIT -1 OFFSET ?)", (self.max_entries,))
            column = 'errors' if results is None else ('successes' if results else 'empty')
            db.execute(f"UPDATE search_days SET {column}={column}+1 WHERE day=?", (claim.day,))
            if cooldown:
                db.execute("INSERT INTO search_cooldown VALUES (1,?) ON CONFLICT(id) DO UPDATE "
                           "SET until=MAX(until,excluded.until)", (now + cooldown,))
            db.commit()

    def stats(self) -> list[dict]:
        with self._db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM search_days ORDER BY day DESC LIMIT 31")]
