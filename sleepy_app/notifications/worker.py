"""Independent notification process. No Flask app or production client is started."""
import argparse
import json
import logging
import os
import re
import signal
import threading
import time
import uuid
from dataclasses import dataclass
from email.utils import parseaddr
from pathlib import Path

from sleepy_app.common.environment import load_env_file
from sleepy_app.config import Paths
from .outbox import Outbox, enabled
from .delivery import DeliveryError, ResendSender
from .templates import render_notification

logger = logging.getLogger('sleepy.notifications')


@dataclass(frozen=True)
class Settings:
    api_key: str
    sender: str
    recipients: tuple
    interval: float = 2

    @classmethod
    def from_env(cls):
        key = os.environ.get('RESEND_API_KEY', '').strip()
        sender = os.environ.get('SLEEPY_NOTIFY_FROM', 'Tonks 网站通知 <notify@mail.tonks.top>').strip()
        to = tuple(dict.fromkeys(s.strip() for s in os.environ.get('SLEEPY_NOTIFY_TO', '').split(',') if s.strip()))
        if not key or any(ord(c) < 33 or ord(c) > 126 for c in key):
            raise ValueError('RESEND_API_KEY is missing or malformed')
        if not to or len(to) > 10:
            raise ValueError('Configure 1-10 administrator recipients in SLEEPY_NOTIFY_TO')
        for address in (sender, *to):
            if '\r' in address or '\n' in address or not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+', parseaddr(address)[1]):
                raise ValueError('Notification mailbox configuration is invalid')
        # Recipient values are bare mailboxes, not display-name header lists.
        if any(parseaddr(address)[1] != address for address in to):
            raise ValueError('Use bare recipient addresses separated by commas')
        try:
            interval = max(2, min(3600, float(os.environ.get('SLEEPY_NOTIFY_INTERVAL_SECONDS', '2'))))
        except ValueError:
            raise ValueError('SLEEPY_NOTIFY_INTERVAL_SECONDS must be numeric') from None
        return cls(key, sender, to, interval)


def process_one(queue, settings, sender_factory=ResendSender):
    row = queue.claim()
    if row is None:
        return False
    try:
        if row['envelope']:
            envelope = json.loads(row['envelope'])
        else:
            envelope = {'from': settings.sender, 'to': list(settings.recipients),
                        'rendered': render_notification(json.loads(row['payload']))}
            if not queue.save_envelope(row, envelope):
                return True  # Lease lost; another worker owns recovery.
        sender = sender_factory(settings.api_key, envelope['from'], envelope['to'])
        ident = sender.send(row['event_key'], envelope['rendered'])
        queue.finish(row, provider_id=ident)
        logger.info('notification_sent queue=%s id=%s', queue.path.name, row['id'])
    except DeliveryError as error:
        queue.finish(row, error=str(error), retryable=error.retryable, retry_after=error.retry_after)
        logger.warning('notification_delivery_failed queue=%s id=%s retryable=%s', queue.path.name, row['id'], error.retryable)
    except Exception as error:
        # Do not leak payload, secret, provider body or recipient addresses into logs.
        queue.finish(row, error='worker_' + type(error).__name__, retryable=False)
        logger.error('notification_worker_failed queue=%s id=%s type=%s', queue.path.name, row['id'], type(error).__name__)
    return True


def queues():
    paths = Paths.from_env()
    return {'community': Outbox(paths.community_db), 'recommendations': Outbox(paths.recommendations_db)}


def run(settings, *, once=False, stop=None):
    stop = stop or threading.Event()
    targets = list(queues().values())
    last_cleanup = 0
    while not stop.is_set():
        if not enabled():
            logger.info('notifications_disabled')
            return
        for queue in targets:
            if stop.is_set():
                break
            try:
                attempted = process_one(queue, settings)
            except Exception as error:
                logger.error('notification_queue_unavailable type=%s', type(error).__name__)
                attempted = False
            if attempted and not once:
                stop.wait(settings.interval)
        if time.time() - last_cleanup > 3600:
            for queue in targets:
                try:
                    queue.cleanup()
                except Exception as error:
                    logger.error('notification_cleanup_failed type=%s', type(error).__name__)
            last_cleanup = time.time()
        if once:
            return
        stop.wait(settings.interval)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Sleepy administrator notification worker')
    sub = parser.add_subparsers(dest='command', required=True)
    runner = sub.add_parser('run'); runner.add_argument('--once', action='store_true', help='Process at most one event per database')
    sub.add_parser('status')
    retry = sub.add_parser('retry'); retry.add_argument('--database', choices=['community', 'recommendations'], required=True); retry.add_argument('--id', type=int, required=True)
    sub.add_parser('preview')
    test = sub.add_parser('test'); test.add_argument('--confirm-send', action='store_true', help='Send one real test email to configured administrators')
    args = parser.parse_args(argv)
    load_env_file(os.environ.get('SLEEPY_ENV_FILE') or None)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        if args.command == 'status':
            print(json.dumps({name: queue.status() for name, queue in queues().items()}, ensure_ascii=False, indent=2))
            return 0
        if args.command == 'retry':
            if not queues()[args.database].retry(args.id):
                print('Task not eligible: missing, not failed, or beyond the 20-hour retry window.')
                return 1
            print('Task scheduled with its original delivery identity and content.')
            return 0
        event = {'kind': 'test', 'source': '博客/主页', 'title': '管理员通知测试',
                 'content': '这是一封连接测试邮件。实际通知会包含推荐、友链申请或评论审核结果。',
                 'url': 'https://tonks.top/'}
        if args.command == 'preview':
            print(render_notification(event)['text'])
            return 0
        if args.command == 'test' and not args.confirm_send:
            parser.error('test requires --confirm-send; preview does not send email')
        settings = Settings.from_env()
        if args.command == 'test':
            sender = ResendSender(settings.api_key, settings.sender, settings.recipients)
            sender.send('test-' + uuid.uuid4().hex, render_notification(event))
            print('Test email accepted by provider; check inbox and spam folder.')
            return 0
        stop = threading.Event()
        for name in (signal.SIGINT, signal.SIGTERM):
            signal.signal(name, lambda *_: stop.set())
        run(settings, once=args.once, stop=stop)
        return 0
    except (ValueError, DeliveryError) as error:
        print(str(error))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
