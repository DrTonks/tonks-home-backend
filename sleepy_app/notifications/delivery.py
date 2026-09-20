"""Bounded Resend HTTPS delivery; never logs credentials or provider bodies."""
import hashlib
import http.client
import json
import socket
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

MAX_RETRY_AFTER = 3600
IDEMPOTENCY_WINDOW_SECONDS = 24 * 3600


class DeliveryError(Exception):
    def __init__(self, message='Email delivery failed', *, retryable=False, retry_after=None):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


def _retry_after(value):
    if not value:
        return None
    try:
        seconds = int(value)
    except (ValueError, TypeError):
        try:
            moment = parsedate_to_datetime(value)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            seconds = (moment - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0, min(MAX_RETRY_AFTER, seconds))


class ResendSender:
    def __init__(self, api_key, from_address, recipients, timeout=10):
        self.api_key = str(api_key or '').strip()
        self.from_address = str(from_address or '').strip()
        self.recipients = [recipients] if isinstance(recipients, str) else list(recipients or [])
        self.timeout = max(1, min(30, float(timeout)))

    def send(self, event_key, rendered):
        if not self.api_key or not self.from_address or not self.recipients:
            raise DeliveryError('Email delivery configuration is incomplete')
        if any(ord(char) < 33 or ord(char) > 126 for char in self.api_key):
            raise DeliveryError('Email API credential format is invalid')
        if not event_key:
            raise DeliveryError('Notification event key is missing')
        payload = {'from': self.from_address, 'to': self.recipients,
                   **{key: rendered[key] for key in ('subject', 'text', 'html')}}
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        key = 'sleepy-' + hashlib.sha256(str(event_key).encode('utf-8')).hexdigest()
        connection = http.client.HTTPSConnection('api.resend.com', timeout=self.timeout)
        try:
            connection.request('POST', '/emails', body=body, headers={
                'Authorization': 'Bearer ' + self.api_key,
                'Content-Type': 'application/json', 'Idempotency-Key': key,
                'User-Agent': 'sleepy-notifications/1.0'})
            response = connection.getresponse()
            raw = response.read(65537)
            try:
                data = json.loads(raw) if len(raw) <= 65536 else {}
            except (ValueError, UnicodeError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            if 200 <= response.status < 300:
                provider_id = data.get('id')
                if isinstance(provider_id, str) and provider_id and len(provider_id) <= 256:
                    return provider_id
                raise DeliveryError('Resend returned an invalid acknowledgement', retryable=True)
            retryable = response.status == 429 or response.status >= 500 or (
                response.status == 409 and data.get('name') == 'concurrent_idempotent_requests')
            raise DeliveryError('Resend HTTP ' + str(response.status), retryable=retryable,
                                retry_after=_retry_after(response.getheader('Retry-After')) if retryable else None)
        except (OSError, socket.timeout, http.client.HTTPException):
            raise DeliveryError('Resend connection failed', retryable=True) from None
        finally:
            connection.close()
