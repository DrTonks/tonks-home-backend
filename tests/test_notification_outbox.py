import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from sleepy_app.notifications.outbox import Outbox, enqueue, RETRY_WINDOW
from sleepy_app.notifications.worker import Settings, process_one
from sleepy_app.notifications.delivery import DeliveryError


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'community.sqlite3'
        self.queue = Outbox(self.path)
        self.env = patch.dict(os.environ, {'SLEEPY_NOTIFICATIONS_ENABLED': '1'}, clear=True)
        self.env.start()
        self.settings = Settings('test-key', 'Tonks <notify@mail.tonks.top>', ('admin@example.test',))
        self.add(1)

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def add(self, ident):
        db = sqlite3.connect(self.path)
        try:
            enqueue(db, kind='comment', entity_id=ident,
                    payload={'content': 'Hello', 'source': '博客', 'status': 'published', 'url': 'https://blog.tonks.top/about/'})
            db.commit()
        finally:
            db.close()

    def test_two_workers_claim_one_event_once(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.queue.claim(), range(2)))
        self.assertEqual(sum(r is not None for r in results), 1)

    def test_crash_recovery_keeps_key_and_rejects_stale_ack(self):
        first = self.queue.claim(now=time.time())
        self.queue.save_envelope(first, {'immutable': True})
        second = self.queue.claim(now=first['lease_until'] + 1)
        self.assertEqual(first['event_key'], second['event_key'])
        self.assertEqual(json.loads(second['envelope']), {'immutable': True})
        self.queue.finish(first, provider_id='stale')
        self.assertEqual(self.queue.status()['counts'], {'sending': 1})
        self.queue.finish(second, provider_id='accepted')
        self.assertEqual(self.queue.status()['counts'], {'sent': 1})

    def test_timeout_then_retry_preserves_mail_despite_config_change(self):
        calls = []
        class Sender:
            def __init__(self, key, sender, recipients):
                self.sender, self.recipients = sender, recipients
            def send(self, key, rendered):
                calls.append((key, rendered, self.sender, self.recipients))
                if len(calls) == 1:
                    raise DeliveryError('timeout', retryable=True)
                return 'accepted'
        process_one(self.queue, self.settings, Sender)
        with self.queue.connect() as db:
            db.execute('UPDATE notification_outbox SET available_at=0')
        changed = Settings('new-key', 'Other <other@example.test>', ('different@example.test',))
        process_one(self.queue, changed, Sender)
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(self.queue.status()['counts'], {'sent': 1})

    def test_expired_uncertain_delivery_is_not_resent(self):
        now = time.time()
        row = self.queue.claim(now=now)
        self.assertIsNone(self.queue.claim(now=now + RETRY_WINDOW + 1))
        self.assertEqual(self.queue.status()['counts'], {'failed': 1})
        self.assertFalse(self.queue.retry(row['id'], now=now + RETRY_WINDOW + 1))

    def test_retry_after_and_terminal_error(self):
        now = time.time();row = self.queue.claim(now=now)
        self.queue.finish(row, error='rate_limit', retryable=True, retry_after=500, now=now)
        self.assertIsNone(self.queue.claim(now=now + 499))
        row = self.queue.claim(now=now + 501)
        self.queue.finish(row, error='unauthorized', retryable=False, now=now + 501)
        self.assertEqual(self.queue.status()['counts'], {'failed': 1})

    def test_no_database_is_created_by_idle_worker(self):
        path = Path(self.temp.name) / 'missing.sqlite3'
        self.assertIsNone(Outbox(path).claim())
        self.assertFalse(path.exists())

    def test_finished_payload_cleanup_keeps_deduplication(self):
        now = time.time();row = self.queue.claim(now=now)
        self.queue.finish(row, provider_id='id', now=now)
        self.queue.cleanup(now=now + 31 * 86400)
        self.add(1)
        with self.queue.connect() as db:
            rows = db.execute('SELECT payload,envelope FROM notification_outbox').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['payload'], '{}')

    def test_mailbox_validation_and_secret_never_in_error(self):
        with patch.dict(os.environ, {'RESEND_API_KEY': 'private-secret', 'SLEEPY_NOTIFY_TO': 'admin@example.test'}):
            self.assertEqual(Settings.from_env().recipients, ('admin@example.test',))
            os.environ['SLEEPY_NOTIFY_TO'] = 'admin@example.test\nBcc: other@example.test'
            with self.assertRaisesRegex(ValueError, 'mailbox'):
                Settings.from_env()

    def test_deleted_internal_record_does_not_enqueue(self):
        with self.queue.connect() as db:
            enqueue(db,kind='comment',entity_id=2,payload={'status':'deleted'})
        self.assertEqual(self.queue.status()['counts'], {'pending': 1})

    def test_cleanup_error_does_not_stop_worker(self):
        from sleepy_app.notifications.worker import run
        with patch('sleepy_app.notifications.worker.queues', return_value={'community': self.queue}), \
             patch('sleepy_app.notifications.worker.process_one', return_value=False), \
             patch.object(self.queue, 'cleanup', side_effect=sqlite3.OperationalError('locked')):
            run(self.settings, once=True)

    def test_retry_missing_database_is_safe(self):
        self.assertFalse(Outbox(Path(self.temp.name) / 'missing.sqlite3').retry(1))
