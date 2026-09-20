"""Per-business-database outbox. Enqueue uses the caller's transaction, never network I/O."""
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

KINDS = frozenset({'comment', 'article_comment', 'friend_application', 'feedback', 'recommendation'})
LEASE_SECONDS = 90
RETRY_WINDOW = 20 * 3600  # Resend keeps idempotency keys for 24 hours.
MAX_ATTEMPTS = 8


def enabled():
    return os.environ.get('SLEEPY_NOTIFICATIONS_ENABLED', '').lower() in {'1', 'true', 'yes'}


def initialize(connection):
    # executescript would commit the surrounding business transaction; do not use it here.
    connection.execute("""CREATE TABLE IF NOT EXISTS notification_outbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_key TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        payload TEXT NOT NULL,
        envelope TEXT,
        state TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        available_at REAL NOT NULL,
        first_attempt REAL,
        lease_until REAL NOT NULL DEFAULT 0,
        lease_token TEXT,
        finished_at REAL,
        provider_id TEXT,
        last_error TEXT,
        UNIQUE(kind, entity_id)
    )""")
    connection.execute('CREATE INDEX IF NOT EXISTS idx_notification_due ON notification_outbox(state, available_at, lease_until)')


def enqueue(connection, *, kind, entity_id, payload, is_admin=False):
    if not enabled() or is_admin or payload.get('status') == 'deleted':
        return
    allowed = {s.strip() for s in os.environ.get('SLEEPY_NOTIFY_KINDS', ','.join(sorted(KINDS))).split(',')}
    if kind not in KINDS or kind not in allowed:
        return
    if payload.get('status') == 'rejected' and os.environ.get('SLEEPY_NOTIFY_REJECTED', '1').lower() in {'0', 'false', 'no'}:
        return
    # Store a bounded, private-data-free snapshot. Never copy request headers or email addresses.
    fields = {'source', 'page', 'title', 'nickname', 'content', 'status', 'reason', 'url', 'quote', 'created_at'}
    event = {k: str(v)[:1200] for k, v in payload.items() if k in fields and v is not None}
    event.update(kind=kind, entity_id=str(entity_id))
    now = time.time()
    initialize(connection)
    connection.execute("""INSERT INTO notification_outbox
        (event_key,kind,entity_id,payload,created_at,available_at)
        VALUES (?,?,?,?,?,?) ON CONFLICT(kind,entity_id) DO NOTHING""",
        ('sleepy-' + uuid.uuid4().hex, kind, str(entity_id), json.dumps(event, ensure_ascii=False), now, now))


class Outbox:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def connect(self):
        # Do not create a business database merely by starting the worker.
        db = sqlite3.connect(self.path.resolve().as_uri() + '?mode=rw', uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def exists(db):
        return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='notification_outbox'").fetchone() is not None

    def claim(self, now=None):
        if not self.path.is_file():
            return None
        now = time.time() if now is None else now
        with self.connect() as db:
            if not self.exists(db):
                return None
            db.execute('BEGIN IMMEDIATE')
            db.execute("""UPDATE notification_outbox SET state='failed', last_error='retry_window_expired', finished_at=?
                WHERE state IN ('pending','sending') AND lease_until<=? AND first_attempt IS NOT NULL AND first_attempt<=?""",
                (now, now, now - RETRY_WINDOW))
            db.execute("""UPDATE notification_outbox SET state='failed', last_error='attempts_exhausted', finished_at=?
                WHERE state IN ('pending','sending') AND lease_until<=? AND attempts>=?""", (now, now, MAX_ATTEMPTS))
            row = db.execute("""SELECT * FROM notification_outbox
                WHERE (state='pending' AND available_at<=?) OR (state='sending' AND lease_until<=?)
                ORDER BY available_at,id LIMIT 1""", (now, now)).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            db.execute("""UPDATE notification_outbox SET state='sending', attempts=attempts+1,
                first_attempt=COALESCE(first_attempt,?),lease_until=?,lease_token=? WHERE id=?""",
                (now, now + LEASE_SECONDS, token, row['id']))
            return dict(db.execute('SELECT * FROM notification_outbox WHERE id=?', (row['id'],)).fetchone())

    def save_envelope(self, row, envelope):
        with self.connect() as db:
            cursor = db.execute("""UPDATE notification_outbox SET envelope=?
                WHERE id=? AND lease_token=? AND state='sending'""",
                (json.dumps(envelope, ensure_ascii=False), row['id'], row['lease_token']))
            return cursor.rowcount == 1

    def finish(self, row, *, provider_id=None, error=None, retryable=False, retry_after=None, now=None):
        now = time.time() if now is None else now
        delay = max(min(float(retry_after or 0), 3600), min(30 * 2 ** (row['attempts'] - 1), 3600))
        retry = (retryable and row['attempts'] < MAX_ATTEMPTS
                 and now + delay < row['first_attempt'] + RETRY_WINDOW)
        state = 'sent' if provider_id else ('pending' if retry else 'failed')
        with self.connect() as db:
            db.execute("""UPDATE notification_outbox SET state=?,available_at=?,lease_until=0,lease_token=NULL,
                finished_at=?,provider_id=?,last_error=? WHERE id=? AND lease_token=? AND state='sending'""",
                (state, now + delay, None if retry else now, provider_id, error,
                 row['id'], row['lease_token']))

    def status(self):
        if not self.path.is_file():
            return {'counts': {}, 'recent': []}
        with self.connect() as db:
            if not self.exists(db):
                return {'counts': {}, 'recent': []}
            return {'counts': {r['state']: r['n'] for r in db.execute('SELECT state,COUNT(*) n FROM notification_outbox GROUP BY state')},
                    'recent': [dict(r) for r in db.execute('SELECT id,kind,entity_id,state,attempts,last_error FROM notification_outbox ORDER BY id DESC LIMIT 20')]}

    def retry(self, ident, now=None):
        if not self.path.is_file():
            return False
        now = time.time() if now is None else now
        with self.connect() as db:
            if not self.exists(db):
                return False
            # Keep the original key and payload; never blindly resend beyond the provider's dedupe window.
            cursor = db.execute("""UPDATE notification_outbox SET state='pending',available_at=?,attempts=0,finished_at=NULL
                WHERE id=? AND state='failed' AND (first_attempt IS NULL OR first_attempt>?)""",
                (now, ident, now - RETRY_WINDOW))
            return cursor.rowcount == 1

    def cleanup(self, now=None):
        if not self.path.is_file():
            return
        now = time.time() if now is None else now
        with self.connect() as db:
            if self.exists(db):
                # Retain unique event keys to prevent replay; erase content after 30 days.
                db.execute("""UPDATE notification_outbox SET payload='{}',envelope=NULL
                    WHERE state IN ('sent','failed') AND finished_at<? AND payload!='{}'""", (now - 30 * 86400,))
