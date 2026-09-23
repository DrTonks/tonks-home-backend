import copy
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from flask import Flask
from sleepy_app.community.store import CommunityStore, CommunityValidationError
from sleepy_app.community.polls import PollStore, register_polls


class PollTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.public = self.root / 'polls.json'
        self.community = CommunityStore(str(self.root / 'community.sqlite3'))
        self.poll = dict(id='quiz-1', version='a'*64, title='Question', options=[dict(id='a', label='One'), dict(id='b', label='Two')], answer='b', explanation='Private explanation')
        self.store = PollStore(self.community, self.public)
        self.store.sync(dict(schema=1, polls=[self.poll]))
        self.publish([self.poll])

    def tearDown(self):
        self.tmp.cleanup()

    def publish(self, polls):
        self.public.write_text(json.dumps(dict(schema=1, polls=[dict(id=p['id'], version=p['version']) for p in polls])), encoding='utf8')

    def state(self, owner='alice', option=None, version=None):
        return self.store.state(self.poll['id'], version or self.poll['version'], owner, option)

    def test_results_and_answer_are_hidden_until_that_identity_votes(self):
        self.state(option='a')
        for owner in ('', 'bob'):
            state = self.state(owner)
            self.assertEqual(set(state), {'success', 'voted', 'version'})
            self.assertFalse(state['voted'])
        state = self.state()
        self.assertEqual(state['choice'], 'a')
        self.assertEqual(state['answer'], 'b')
        self.assertEqual(state['counts'], {'a': 1, 'b': 0})
        self.assertEqual(state['explanation'], 'Private explanation')

    def test_repeated_and_concurrent_votes_are_immutable(self):
        with ThreadPoolExecutor(max_workers=12) as pool:
            states = list(pool.map(lambda i: self.state(option=('a' if i % 2 else 'b')), range(36)))
        self.assertEqual(len({s['choice'] for s in states}), 1)
        self.assertTrue(all(s['total'] == 1 for s in states))
        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(lambda i: self.state(owner='other'+str(i), option='b'), range(24)))
        self.assertEqual(self.state()['total'], 25)

    def test_invalid_input_never_creates_vote(self):
        for owner, option in [('', 'a'), ('alice', 'forged'), ('alice', []), ('alice', 1)]:
            with self.assertRaises(CommunityValidationError):
                self.state(owner, option)
        self.assertFalse(self.state()['voted'])

    def test_definition_sync_preserves_vote_semantics_but_allows_editorial_changes(self):
        second = {**self.poll, 'id': 'second'}
        changed = {**self.poll, 'answer': 'a', 'version': 'b'*64}
        with self.assertRaises(ValueError):
            self.store.sync(dict(schema=1, polls=[second, changed]))
        with self.community._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM article_poll_definitions').fetchone()[0], 1)
        for mutation in [dict(answer='a'), dict(options=[dict(id='a', label='Changed'), dict(id='b', label='Two')])]:
            with self.assertRaises(ValueError):
                self.store.sync(dict(schema=1, polls=[{**self.poll, **mutation, 'version': 'c'*64}]))
        editorial = {**self.poll, 'title': 'Updated question',
                     'explanation': 'Updated explanation', 'version': 'd'*64}
        self.store.sync(dict(schema=1, polls=[editorial]))
        with self.community._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM article_poll_definitions').fetchone()[0], 2)

    def test_new_version_is_inactive_until_publication_and_rollback_preserves_votes(self):
        self.state(option='a')
        newer = {**self.poll, 'version': 'b'*64, 'options': list(reversed(self.poll['options']))}
        self.store.sync(dict(schema=1, polls=[newer]))
        with self.assertRaises(CommunityValidationError):
            self.state(version=newer['version'])
        self.publish([newer])
        with self.assertRaises(CommunityValidationError):
            self.state()
        self.assertEqual(self.state(version=newer['version'])['total'], 1)
        self.publish([self.poll])
        self.assertEqual(self.state()['explanation'], 'Private explanation')
        self.publish([])
        with self.assertRaises(CommunityValidationError):
            self.state(option='b')
        self.public.unlink()
        with self.assertRaises(CommunityValidationError):
            self.state()

    def test_invalid_manifests_and_version_collision_rejected(self):
        for data in [None, {}, dict(schema=2, polls=[]), dict(schema=1, polls=[self.poll, self.poll]), dict(schema=1, polls=[{**self.poll, 'answer': 'x'}]), dict(schema=1, polls=[{**self.poll, 'options': []}]), dict(schema=1, polls=[{**self.poll, 'explanation': 'Changed but same version'}])]:
            with self.assertRaises(ValueError):
                self.store.sync(data)
        self.public.write_text('{broken', encoding='utf8')
        with self.assertRaises(CommunityValidationError):
            self.state()

    def test_api_no_cache_validation_and_comment_limiter_isolation(self):
        app = Flask(__name__)
        security = SimpleNamespace(get_community_owner_hash=lambda req: req.headers.get('X-Community-Identity', ''), get_community_rate_limit_keys=lambda req: ('ip', 'client'))
        runtime = SimpleNamespace(community_store=self.community, common_security=security, community_comment_limiter=Mock())
        with patch.dict('os.environ', {'SLEEPY_POLL_PUBLIC_MANIFEST': str(self.public)}):
            register_polls(app, runtime, SimpleNamespace(article_manifest=self.public))
        client = app.test_client()
        url = '/blog/community/polls/quiz-1?version=' + self.poll['version']
        for payload in [None, [], {}, {'option': 2}, {'option': 'a'}]:
            response = client.post(url, json=payload)
            self.assertEqual(response.status_code, 400)
            self.assertIn('no-store', response.headers['Cache-Control'])
        response = client.post(url, json={'option': 'a'}, headers={'X-Community-Identity': 'alice'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['total'], 1)
        response = client.get(url)
        self.assertNotIn('answer', response.json)
        self.assertNotIn('counts', response.json)
        self.assertIn('no-store', response.headers['Cache-Control'])
        runtime.community_comment_limiter.check.assert_not_called()
        for _ in range(30):
            response = client.post(url, json={'option': 'a'}, headers={'X-Community-Identity': 'alice'})
        self.assertEqual(response.status_code, 429)


if __name__ == '__main__':
    unittest.main()
