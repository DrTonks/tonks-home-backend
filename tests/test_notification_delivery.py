import io
import json
import unittest
from unittest.mock import MagicMock, patch

from sleepy_app.notifications.delivery import DeliveryError, ResendSender, _retry_after
from sleepy_app.notifications.templates import render_notification


class NotificationDeliveryTests(unittest.TestCase):
    def sender(self):
        return ResendSender('secret-test-key', '通知 <notify@mail.tonks.top>', ['admin@example.com'])

    def response(self, connection, status=200, data=None, retry=None):
        response = connection.return_value.getresponse.return_value
        response.status = status
        response.read.return_value = json.dumps(data or {'id': 'provider-id'}).encode()
        response.getheader.return_value = retry
        return response

    @patch('sleepy_app.notifications.delivery.http.client.HTTPSConnection')
    def test_send_uses_stable_idempotency_and_json(self, connection):
        self.response(connection)
        rendered = render_notification({'kind': 'test'})
        sender = self.sender()
        self.assertEqual(sender.send('comment:1', rendered), 'provider-id')
        first = connection.return_value.request.call_args.kwargs
        sender.send('comment:1', rendered)
        second = connection.return_value.request.call_args.kwargs
        self.assertEqual(first['headers']['Idempotency-Key'], second['headers']['Idempotency-Key'])
        self.assertEqual(json.loads(first['body'])['to'], ['admin@example.com'])
        self.assertEqual(first['headers']['Authorization'], 'Bearer secret-test-key')
        connection.assert_called_with('api.resend.com', timeout=10)
        self.assertEqual(connection.return_value.close.call_count, 2)

    @patch('sleepy_app.notifications.delivery.http.client.HTTPSConnection')
    def test_http_classification_and_no_secret_echo(self, connection):
        for status, name, retryable in [(400, '', False), (401, '', False), (403, '', False),
                (429, '', True), (500, '', True), (503, '', True),
                (409, 'invalid_idempotent_request', False), (409, 'concurrent_idempotent_requests', True)]:
            with self.subTest(status=status, name=name):
                self.response(connection, status, {'name': name, 'message': 'secret-test-key private-body'}, '99999')
                with self.assertRaises(DeliveryError) as raised:
                    self.sender().send('key', render_notification({}))
                self.assertEqual(raised.exception.retryable, retryable)
                self.assertEqual(raised.exception.retry_after, 3600 if retryable else None)
                self.assertNotIn('secret-test-key', str(raised.exception))
                self.assertNotIn('private-body', str(raised.exception))

    @patch('sleepy_app.notifications.delivery.http.client.HTTPSConnection')
    def test_timeout_retries_without_leaking_exception(self, connection):
        connection.return_value.request.side_effect = TimeoutError('secret-test-key')
        with self.assertRaises(DeliveryError) as raised:
            self.sender().send('key', render_notification({}))
        self.assertTrue(raised.exception.retryable)
        self.assertNotIn('secret-test-key', str(raised.exception))
        connection.return_value.close.assert_called_once()

    @patch('sleepy_app.notifications.delivery.http.client.HTTPSConnection')
    def test_bad_success_ack_retries(self, connection):
        response = self.response(connection)
        response.read.return_value = b'not-json'
        with self.assertRaises(DeliveryError) as raised:
            self.sender().send('key', render_notification({}))
        self.assertTrue(raised.exception.retryable)

    def test_invalid_credential_does_not_leak(self):
        with self.assertRaises(DeliveryError) as raised:
            ResendSender('secret\nheader', 'from@example.com', ['to@example.com']).send('key', render_notification({}))
        self.assertFalse(raised.exception.retryable)
        self.assertNotIn('secret', str(raised.exception))

    def test_retry_after_values(self):
        self.assertEqual(_retry_after('-1'), 0)
        self.assertEqual(_retry_after('30'), 30)
        self.assertIsNone(_retry_after('invalid'))
        self.assertEqual(_retry_after('Wed, 21 Oct 2015 07:28:00 GMT'), 0)

    def test_template_escapes_markup_and_truncates(self):
        mail = render_notification({'source': 'blog', 'kind': 'comment', 'status': 'approved',
            'nickname': '<script>x</script>', 'content': '<img src=x>' + 'a' * 1000,
            'url': 'https://blog.tonks.top/about?admin_key=secret#comments', 'created_at': '2026', 'entity_id': 1})
        self.assertNotIn('<script>', mail['html'])
        self.assertNotIn('<img', mail['html'])
        self.assertIn('&lt;script&gt;', mail['html'])
        self.assertNotIn('admin_key', mail['html'])
        self.assertNotIn('a' * 401, mail['text'])
        self.assertIn('博客', mail['subject'])

    def test_rejected_omits_all_untrusted_content_urls(self):
        mail = render_notification({'kind': 'comment', 'status': 'rejected',
            'author': 'bad https://spam.example/x', 'content': 'spam https://spam.example',
            'reason': 'https://spam.example/x', 'title': 'https://spam.example/x',
            'url': 'https://blog.tonks.top/about'})
        self.assertNotIn('spam.example', mail['text'])
        self.assertIn('自动拒绝', mail['subject'])

    def test_actual_event_source_and_kind_labels(self):
        for source in ('博客', '主页', '博客/主页（共享）', '博客/主页'):
            for kind, expected in [('friend_application', '友链申请'), ('article_comment', '新评论')]:
                mail = render_notification({'source': source, 'kind': kind, 'status': 'published'})
                self.assertIn(source, mail['subject'])
                self.assertIn(expected, mail['subject'])
                if kind == 'article_comment':
                    self.assertIn('自动通过', mail['subject'])
        for kind in ('recommendation',):
            mail = render_notification({'kind': kind, 'source': '主页', 'status': 'published'})
            self.assertNotIn('自动通过', mail['subject'])
            self.assertNotIn('状态：', mail['text'])

    def test_feedback_review_states_and_management_guidance(self):
        for status, label in [('published', '自动通过'), ('pending', '待人工审核'), ('rejected', '自动拒绝')]:
            mail = render_notification({'kind': 'feedback', 'status': status})
            self.assertIn(label, mail['subject'])
            self.assertIn('反馈群聊', mail['text'])
        self.assertIn('管理模式查看友链申请', render_notification({'kind': 'friend_application'})['text'])

    def test_rejected_reason_is_bounded_and_escaped(self):
        mail = render_notification({'kind': 'comment', 'status': 'rejected',
            'reason': '<script>bad</script> HTTPS://spam.example/path ' + 'x' * 500})
        self.assertNotIn('spam.example', mail['text'])
        self.assertNotIn('<script>', mail['html'])
        self.assertIn('&lt;script&gt;', mail['html'])
        self.assertNotIn('x' * 301, mail['text'])
        self.assertNotIn('原因，请进入管理页面查看', mail['text'])

    def test_non_ascii_credential_is_rejected(self):
        with self.assertRaises(DeliveryError) as raised:
            ResendSender('密钥', 'from@example.com', ['to@example.com']).send('key', render_notification({}))
        self.assertFalse(raised.exception.retryable)
        self.assertNotIn('密钥', str(raised.exception))

    def test_only_allowed_https_links(self):
        for url in ['https://evil.example/', 'javascript:alert(1)', 'https://blog.tonks.top.evil/',
                    'https://user:pass@blog.tonks.top/', 'http://blog.tonks.top/', 'https://blog.tonks.top:444/']:
            with self.subTest(url=url):
                self.assertNotIn('<a href=', render_notification({'url': url})['html'])


if __name__ == '__main__':
    unittest.main()
