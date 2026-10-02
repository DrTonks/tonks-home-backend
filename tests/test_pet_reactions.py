"""Reaction output stays optional, persona-scoped and compatible with old clients."""
import json
import unittest
from flask import Flask
from pet_ai import create_pet_ai_blueprint
from pet_ai.reactions import choices, parse_reply
from pet_ai.service import PetAIService
from pet_ai.validation import validate_payload
from tests.test_pet_ai import config, FakeProvider, FakeSearch

class ReactionTests(unittest.TestCase):
    def test_plain_text_and_fenced_json(self):
        self.assertEqual(parse_reply('好，我记住了。', 'static'), ('好，我记住了。', None))
        self.assertEqual(parse_reply('```json\n{"reply":"好耶。","emoji_id":"quiet_happy"}\n```', 'static'), ('好耶。', 'quiet_happy'))

    def test_rejects_cross_persona_unknown_and_url_ids_without_losing_text(self):
        for value in ['u_cheer', 'unknown', '/assets/emoji/cry.jpg', 'https://evil.test/pixel.png', {}, []]:
            self.assertEqual(parse_reply(json.dumps({'reply': '正常回复。', 'emoji_id': value}), 'static'), ('正常回复。', None))
        self.assertIn('u_cheer', {i['id'] for i in choices('live2d')})
        self.assertNotIn('quiet_happy', {i['id'] for i in choices('live2d')})
        self.assertEqual(parse_reply('{"reply":"休息一下。","emoji_id":"u_drink"}', 'static'), ('休息一下。', 'u_drink'))
        self.assertEqual(parse_reply('{"reply":"想一想。","emoji_id":"u_tv_think"}', 'live2d'), ('想一想。', 'u_tv_think'))
        self.assertEqual(parse_reply('{"reply":"想一想。","emoji_id":"u_tv_think"}', 'static'), ('想一想。', None))

    def test_malformed_or_empty_structured_reply_never_leaks_json(self):
        for content in ['{"reply":"broken', '{"emoji_id":"quiet_happy"}', '{"reply":42}', '[]']:
            self.assertEqual(parse_reply(content, 'static'), ('', None))

    def test_reaction_does_not_require_an_extra_model_call(self):
        provider = FakeProvider([{'content': '{"reply":"开心就好。","emoji_id":"quiet_happy"}'}])
        service = PetAIService(config(), provider=provider)
        result = list(service.events(validate_payload({'pet_id':'static', 'question_id':'q_mood', 'answer':'开心'})))[-1]
        self.assertEqual(result, {'type':'result','reply':'开心就好。','emoji_id':'quiet_happy'})
        self.assertEqual(len(provider.calls), 1)
        system = provider.calls[0]['messages'][0]['content']
        self.assertIn('quiet_happy', system)
        self.assertNotIn('u_cheer', system)

    def test_structured_uncertainty_still_searches_and_clears_mismatched_image(self):
        provider = FakeProvider([
            {'content': '{"reply":"我没听过这首歌。","emoji_id":"quiet_happy"}'},
            {'content': '{"reply":"无法确认作品资料。","emoji_id":"quiet_happy"}'},
        ])
        class EmptySearch:
            def search(self, query): return []
        service = PetAIService(config(), provider=provider, search=EmptySearch())
        request = validate_payload({'pet_id':'static','question_id':'q_recent_music','answer':'歌名'})
        events = list(service.events(request))
        self.assertTrue(any(e.get('stage') == 'searching' for e in events))
        self.assertNotIn('emoji_id', events[-1])
        self.assertIn('可靠资料', events[-1]['reply'])

    def test_normal_json_and_sse_routes_keep_reply_and_optional_id(self):
        for stream in [False, True]:
            app = Flask(__name__)
            app.register_blueprint(create_pet_ai_blueprint())
            app.extensions['pet_ai_service'] = PetAIService(config(), provider=FakeProvider([
                {'content': '{"reply":"好耶！","emoji_id":"u_cheer"}'}]))
            response = app.test_client().post('/pet/reply', json={'pet_id':'live2d','question_id':'q_mood','answer':'开心'}, headers={'Accept':'text/event-stream' if stream else 'application/json'})
            self.assertEqual(response.status_code, 200)
            if stream:
                result = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')][-1]
            else: result = response.json
            self.assertEqual(result['reply'], '好耶！')
            self.assertEqual(result['emoji_id'], 'u_cheer')
