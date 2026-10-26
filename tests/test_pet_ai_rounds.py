"""Bounded adaptive search protocol for every user-answer question."""
import json
import unittest
from test_pet_ai import config, FakeProvider, FakeSearch
from pet_ai.service import PetAIService
from pet_ai.validation import validate_payload, load_questions, PetAIRequestError


def call(*queries):
    return {'tool_calls': [{'id': 'upstream-id', 'function': {
        'name': 'web_search', 'arguments': json.dumps({'query': q})}} for q in queries]}


def request(question='q_food', pet='static'):
    return validate_payload({'question_id': question, 'pet_id': pet, 'answer': '测试回答'})


class RoundsTests(unittest.TestCase):
    def test_birthday_is_not_an_ai_endpoint_question(self):
        with self.assertRaises(PetAIRequestError) as error:
            request('q_birthday')
        self.assertEqual(error.exception.code, 'unknown_question')

    def run_reply(self, responses, search=None, req=None):
        provider = FakeProvider(responses)
        search = search or FakeSearch()
        events = list(PetAIService(config(), provider=provider, search=search).events(req or request()))
        return provider, search, events

    def test_all_input_questions_and_both_pets_have_tool_but_no_forced_search(self):
        for pet in ('static', 'live2d'):
            for question in load_questions():
                provider, search, events = self.run_reply([{'content': '记下来了。'}], req=request(question, pet))
                self.assertIsNotNone(provider.calls[0]['tools'])
                self.assertEqual(search.queries, [])
                self.assertEqual(events[-1]['type'], 'result')

    def test_three_adaptive_searches_then_final_without_tools(self):
        provider, search, events = self.run_reply([
            call('资料一'), call('补充二'), call('核实三'), {'content': '已经核实好了。'}])
        self.assertEqual(search.queries, ['资料一', '补充二', '核实三'])
        self.assertEqual(len(provider.calls), 4)
        self.assertIsNone(provider.calls[-1]['tools'])
        messages = provider.calls[-1]['messages']
        ids = [m['tool_call_id'] for m in messages if m['role'] == 'tool']
        self.assertEqual(ids, ['web_search_1', 'web_search_2', 'web_search_3'])
        self.assertEqual(sum(e.get('stage') == 'searching' for e in events), 3)

    def test_batch_is_clipped_to_three_and_protocol_ids_all_match(self):
        provider, search, _ = self.run_reply([call('一', '二', '三', '四'), {'content': '已核实。'}])
        self.assertEqual(len(search.queries), 3)
        messages = provider.calls[-1]['messages']
        assistant = next(m for m in messages if m['role'] == 'assistant')
        self.assertEqual([c['id'] for c in assistant['tool_calls']],
                         [m['tool_call_id'] for m in messages if m['role'] == 'tool'])
        self.assertIsNone(provider.calls[-1]['tools'])

    def test_duplicates_consume_call_budget_but_not_paid_requests(self):
        provider, search, _ = self.run_reply([call('Same'), call('same'), call(' same '), {'content': '好了。'}])
        self.assertEqual(search.queries, ['Same'])
        self.assertEqual(len(provider.calls), 4)

    def test_error_stops_batch_and_next_model_call_has_no_tools(self):
        provider, search, events = self.run_reply([call('一', '二'), {'content': '先不下结论。'}], search=FakeSearch(error=True))
        self.assertEqual(search.queries, ['一'])
        self.assertIsNone(provider.calls[-1]['tools'])
        self.assertEqual(len(provider.calls[-1]['messages'][-2]['tool_calls']), 1)
        self.assertEqual(events[-1]['type'], 'result')

    def test_empty_results_allow_refined_query(self):
        search = FakeSearch()
        search.results = []
        _, search, _ = self.run_reply([call('一'), call('二'), {'content': '没找到可靠资料。'}], search=search)
        self.assertEqual(search.queries, ['一', '二'])

    def test_uncertainty_does_not_send_personal_answer_as_fallback_query(self):
        for question in ('q_name', 'q_mood', 'q_city_life'):
            _, search, _ = self.run_reply([{'content': '我不了解具体情况，但会陪着你。'}], req=request(question))
            self.assertEqual(search.queries, [])

    def test_ignoring_final_tool_limit_cannot_trigger_fourth_search(self):
        from pet_ai.provider import ProviderError
        provider = FakeProvider([call('一'), call('二'), call('三'), call('四')])
        search = FakeSearch()
        with self.assertRaises(ProviderError):
            list(PetAIService(config(), provider=provider, search=search).events(request()))
        self.assertEqual(len(search.queries), 3)
        self.assertEqual(len(provider.calls), 4)

    def test_reasoning_is_preserved_for_each_tool_exchange(self):
        initial = {**call('一'), 'reasoning_content': 'internal reasoning'}
        provider, _, events = self.run_reply([initial, {'content': '已核实。'}])
        self.assertEqual(provider.calls[-1]['messages'][-2]['reasoning_content'], 'internal reasoning')
        self.assertNotIn('internal reasoning', str(events))
