"""Notification hooks are atomic and leave existing business responses unchanged."""
import json
import os
from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sleepy_app.community.store import (CommunityStore, CommentSubmission,
    FeedbackSubmission, FeedbackTopicSubmission, FriendApplicationSubmission)
from sleepy_app.community.articles import ArticleCommentStore
from sleepy_app.personal.recommendation_store import RecommendationStore, RecommendationRateLimitExceeded
from sleepy_app.notifications.outbox import enqueue, initialize


class NotificationEventTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.community = CommunityStore(str(self.path / 'community.db'))
        self.comment = CommentSubmission('about', None, 'visitor', 'test@example.com', '', 'hello')
        self.feedback = FeedbackSubmission('visitor', 'test@example.com', '', 'feedback')
        self.events = []

    def capture(self, connection, **event):
        self.assertTrue(connection.in_transaction)
        self.events.append(event)

    def test_comments_include_all_moderation_outcomes_and_admin_flag(self):
        with patch('sleepy_app.community.store.enqueue', side_effect=self.capture):
            for status in ('published', 'pending', 'rejected'):
                result = self.community.create_comment(self.comment, 'actor', status=status,
                                                       moderation_reason='reason')
                event = self.events[-1]
                self.assertEqual(event['entity_id'], result['id'])
                self.assertEqual(event['payload']['status'], status)
                self.assertEqual(event['payload']['reason'], 'reason')
                self.assertNotIn('email', event['payload'])
            self.community.create_comment(self.comment, 'admin', status='published', is_admin=True)
        self.assertTrue(self.events[-1]['is_admin'])

    def test_feedback_sources_do_not_collide_or_duplicate_topic_initial_message(self):
        with patch('sleepy_app.community.store.enqueue', side_effect=self.capture):
            self.community.add_feedback_room_message(self.feedback, 'actor', status='pending')
            topic = self.community.create_feedback_topic(
                FeedbackTopicSubmission('title', 'bug', self.feedback), 'actor', status='published')
            self.community.add_feedback_message(topic['id'], self.feedback, 'actor', status='rejected')
        self.assertEqual(len(self.events), 3)
        self.assertEqual({e['entity_id'].split(':')[0] for e in self.events}, {'room', 'topic', 'message'})
        self.assertEqual(len({e['entity_id'] for e in self.events}), 3)

    def test_friend_application_uses_safe_management_destination(self):
        with patch('sleepy_app.community.store.enqueue', side_effect=self.capture):
            result = self.community.create_friend_application(
                FriendApplicationSubmission('site', 'https://untrusted.example/', '', 'description', 'test@example.com'), 'actor')
        self.assertEqual(self.events[0]['entity_id'], result['id'])
        self.assertEqual(self.events[0]['payload']['url'], 'https://tonks.top/')
        self.assertEqual(self.events[0]['payload']['status'], 'pending')

    def test_article_uses_manifest_title_slug_and_trusted_quote(self):
        manifest = self.path / 'manifest.json'
        manifest.write_text(json.dumps({'schema': 1, 'articles': [{
            'id': 'a', 'slug': 'nested/post', 'title': 'Article', 'version': 'v',
            'blocks': [{'id': 'b', 'text': 'trusted quote'}]}]}), encoding='utf-8')
        articles = ArticleCommentStore(self.community, manifest)
        context, parent = articles.context('a', {'version': 'v', 'block_id': 'b'})
        with patch('sleepy_app.community.articles.enqueue', side_effect=self.capture):
            ident = articles.create(self.comment, context, parent, 'actor', 'owner', 'pending', 'reason')
        event = self.events[0]
        self.assertEqual(event['entity_id'], ident)
        self.assertEqual(event['payload']['url'], 'https://blog.tonks.top/posts/nested/post/')
        self.assertEqual(event['payload']['quote'], 'trusted quote')
        self.assertEqual(event['payload']['title'], 'Article')

    def test_public_recommendations_only_notify_once(self):
        store = RecommendationStore(str(self.path / 'recommendations.db'))
        with patch('sleepy_app.personal.recommendation_store.enqueue', side_effect=self.capture):
            store.create('book', 'admin book', 'admin', '')
            result = store.create_public_once_per_day('book', 'book', 'visitor', '', {'actor'})
            with self.assertRaises(RecommendationRateLimitExceeded):
                store.create_public_once_per_day('book', 'again', 'visitor', '', {'actor'})
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0]['entity_id'], result['id'])

    def test_notification_failure_rolls_back_business_write(self):
        with patch('sleepy_app.community.store.enqueue', side_effect=RuntimeError('failure')):
            with self.assertRaises(RuntimeError):
                self.community.create_comment(self.comment, 'actor', status='published')
        with closing(sqlite3.connect(self.community.database_path)) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM community_comments').fetchone()[0], 0)


    def test_real_queue_disabled_admin_and_duplicate_suppression(self):
        with patch.dict(os.environ, {'SLEEPY_NOTIFICATIONS_ENABLED': '0'}):
            self.community.create_comment(self.comment, 'actor', status='published')
        with patch.dict(os.environ, {'SLEEPY_NOTIFICATIONS_ENABLED': '1', 'SLEEPY_NOTIFY_KINDS': 'comment'}):
            self.community.create_comment(self.comment, 'admin', status='published', is_admin=True)
            result = self.community.create_comment(self.comment, 'actor', status='pending')
            with self.community._connect() as db:
                enqueue(db, kind='comment', entity_id=result['id'], payload={'content': 'duplicate'})
                rows = db.execute('SELECT * FROM notification_outbox').fetchall()
                self.assertEqual(len(rows), 1)
                self.assertEqual(json.loads(rows[0]['payload'])['status'], 'pending')

    def test_real_queue_rolls_back_along_with_business(self):
        self.community.initialize()
        with self.community._connect() as db:
            initialize(db)
        def enqueue_then_fail(connection, **event):
            enqueue(connection, **event)
            raise RuntimeError('after enqueue')
        with patch.dict(os.environ, {'SLEEPY_NOTIFICATIONS_ENABLED': '1', 'SLEEPY_NOTIFY_KINDS': 'comment'}):
            with patch('sleepy_app.community.store.enqueue', side_effect=enqueue_then_fail):
                with self.assertRaises(RuntimeError):
                    self.community.create_comment(self.comment, 'actor', status='published')
        with self.community._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM community_comments').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM notification_outbox').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()


class NotificationHttpFlowTests(unittest.TestCase):
    def test_rejected_http_comment_is_persisted_and_delivered_without_public_exposure(self):
        from sleepy_app.app import create_app
        from sleepy_app.community.moderation import ModerationResult
        from sleepy_app.notifications.worker import Settings, process_one
        from sleepy_app.notifications.outbox import Outbox
        with tempfile.TemporaryDirectory() as directory:
            settings = {'SLEEPY_DATA_DIR': directory,
                        'SLEEPY_ENV_FILE': str(Path(directory) / 'absent.env'),
                        'SLEEPY_NOTIFICATIONS_ENABLED': '1'}
            with patch.dict(os.environ, settings, clear=True):
                app = create_app();runtime = app.extensions['sleepy_runtime']
                with patch.object(runtime.comment_moderator, 'moderate', return_value=ModerationResult('reject', 'spam', 'test reason')):
                    response = app.test_client().post('/blog/community/comments/about', json={
                        'nickname': 'visitor', 'email': 'visitor@example.test', 'content': 'private rejected fixture'})
                self.assertFalse(response.get_json()['success'])
                public = app.test_client().get('/blog/community/comments/about').get_data(as_text=True)
                self.assertNotIn('private rejected fixture', public)
                queue = Outbox(Path(directory) / 'community.sqlite3')
                self.assertEqual(queue.status()['counts'], {'pending': 1})
                with patch('sleepy_app.notifications.delivery.ResendSender.send', return_value='mock-provider-id') as send:
                    self.assertTrue(process_one(queue, Settings('fake', 'notify@mail.tonks.top', ('admin@example.test',))))
                    rendered = send.call_args.args[1]
                    self.assertIn('test reason', rendered['text'])
                    self.assertNotIn('private rejected fixture', rendered['text'])
                self.assertEqual(queue.status()['counts'], {'sent': 1})
