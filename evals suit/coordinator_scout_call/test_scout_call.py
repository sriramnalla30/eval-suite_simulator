"""Offline tests for eval 4: no Langfuse, no judge model. Run with the app's Python:

    cd "evals suit/coordinator_scout_call" && python -m unittest test_scout_call -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import scout_call_eval as ev  # noqa: E402

TOOLS = [{'role': 'tool', 'content': {'type': 'function', 'function': {'name': 'ask_user'}}}, {'role': 'tool', 'content': {'type': 'function', 'function': {'name': 'scout_agent'}}}]
SYSTEM = {'role': 'system', 'content': 'You are the Coordinator'}


def user(text):
    return {'role': 'user', 'content': text}


def scout_call(request):
    return {'role': 'assistant', 'content': '', 'tool_calls': [{'name': 'scout_agent', 'args': {'request': request}}]}


def ask_call(question):
    return {'role': 'assistant', 'content': '', 'tool_calls': [{'name': 'ask_user', 'args': {'question': question}}]}


def report(status, **more):
    return {'role': 'tool', 'name': 'scout_agent', 'content': json.dumps({'status': status, **more})}


def answer(text):
    return {'role': 'tool', 'name': 'ask_user', 'content': text}


def out_call(name, **args):
    return {'role': 'assistant', 'content': '', 'tool_calls': [{'name': name, 'args': args}]}


def out_text(text):
    return {'role': 'assistant', 'content': text}


class Reading(unittest.TestCase):
    def test_what_the_coordinator_reacted_to(self):
        self.assertEqual(ev.trigger_of([SYSTEM, user('dentist near me')] + TOOLS), ('person_message', 'dentist near me'))
        chat = [SYSTEM, user('gyms'), scout_call('gyms'), report('needs_input'), ask_call('Which area?'), answer('Koramangala')]
        self.assertEqual(ev.trigger_of(chat + TOOLS), ('person_answer', 'Koramangala'))
        self.assertEqual(ev.trigger_of(chat[:4] + TOOLS), ('scout_report', 'needs_input'))
        self.assertEqual(ev.trigger_of([SYSTEM, user('x'), scout_call('x'), report('ready')] + TOOLS), ('scout_report', 'ready'))
        self.assertEqual(ev.trigger_of([SYSTEM, user('x'), scout_call('x')]), ('other', None))
        self.assertEqual(ev.trigger_of([]), ('other', None))

    def test_tool_definitions_listed_after_the_chat_are_not_a_message(self):
        """Langfuse lists the tool definitions after the conversation: reading the last item blindly would see a tool, not the person."""
        self.assertEqual(ev.trigger_of([SYSTEM, user('hello')] + TOOLS)[0], 'person_message')

    def test_what_the_coordinator_did(self):
        self.assertEqual(ev.action_of(out_call('scout_agent', request='gyms in Indiranagar')), (True, 'Called the scout agent: "gyms in Indiranagar"'))
        self.assertEqual(ev.action_of(out_call('ask_user', question='Which city?')), (False, 'Asked the person: "Which city?"'))
        self.assertEqual(ev.action_of(out_text('All details are prepared; the search tool will run next.')),
                         (False, 'Replied to the person in text: "All details are prepared; the search tool will run next."'))
        self.assertEqual(ev.action_of(out_text('  ')), (False, 'Said nothing (an empty reply, no tool call)'))
        self.assertEqual(ev.action_of(out_call('geocode', area='x'))[0], False)

    def test_a_scout_call_next_to_another_tool_still_counts(self):
        both = {'role': 'assistant', 'content': '', 'tool_calls': [{'name': 'ask_user', 'args': {'question': 'q'}}, {'name': 'scout_agent', 'args': {'request': 'r'}}]}
        self.assertTrue(ev.action_of(both)[0])


class Verdicts(unittest.TestCase):
    def test_all_four_combinations(self):
        self.assertEqual(ev.verdict_of('yes', True), ('pass', 'none'))
        self.assertEqual(ev.verdict_of('yes', False), ('fail', 'not_called'))
        self.assertEqual(ev.verdict_of('no', False), ('pass', 'none'))
        self.assertEqual(ev.verdict_of('no', True), ('fail', 'called_wrongly'))
        self.assertEqual(ev.verdict_of(None, True), ('unknown', None))

    def test_a_session_passes_only_if_every_call_passes(self):
        p, f, u = {'verdict': 'pass'}, {'verdict': 'fail'}, {'verdict': 'unknown'}
        self.assertEqual(ev.session_verdict([p, p, p]), 'pass')
        self.assertEqual(ev.session_verdict([p, f, p]), 'fail')
        self.assertEqual(ev.session_verdict([p, u]), 'unknown')
        self.assertEqual(ev.session_verdict([u, f]), 'fail')  # one failure decides, whatever else is unclear
        self.assertEqual(ev.session_verdict([]), 'no coordinator call')


class FakePrompt:
    version = 7

    def compile(self, **values):
        return json.dumps(values)


class Judging(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(ev, 'judge_prompt', return_value=FakePrompt())
        patch.start()
        self.addCleanup(patch.stop)

    def call(self, trigger, latest, called):
        return {'trigger': trigger, 'latest': latest, 'memory': f'Person: {latest}', 'called_scout': called, 'did': 'x'}

    def test_after_a_scout_report_no_model_is_asked_and_no_call_is_expected(self):
        asked = []
        result = ev.judge(self.call('scout_report', 'needs_input', False), ask=lambda p: asked.append(p))
        self.assertEqual((result['expected'], result['source'], result['verdict']), ('no', 'rule', 'pass'))
        self.assertEqual(asked, [])  # decided by rule
        self.assertEqual(ev.judge(self.call('scout_report', 'ready', True), ask=lambda p: asked.append(p))['failure_type'], 'called_wrongly')

    def test_the_person_s_words_go_to_the_judge_with_the_conversation_and_never_the_coordinators_choice(self):
        seen = []

        def ask(prompt):
            seen.append(json.loads(prompt))
            return {'critique': 'a place search', 'should_call_scout': 'yes'}
        result = ev.judge(self.call('person_answer', 'Vijayawada', False), ask=ask)
        self.assertEqual((result['expected'], result['verdict'], result['failure_type']), ('yes', 'fail', 'not_called'))
        self.assertEqual(seen[0], {'memory': 'Person: Vijayawada', 'latest': 'Vijayawada', 'trigger': 'person_answer'})
        self.assertIn('prompt v7', result['source'])
        self.assertEqual(result['critique'], 'a place search')

    def test_a_judge_that_cannot_answer_leaves_the_call_unknown(self):
        for reply in (None, {}, {'should_call_scout': 'maybe'}):
            result = ev.judge(self.call('person_message', 'hmm', True), ask=lambda p, r=reply: r)
            self.assertEqual((result['expected'], result['verdict']), (None, 'unknown'), reply)

    def test_a_non_search_that_was_sent_to_the_scout_agent_fails(self):
        result = ev.judge(self.call('person_message', 'hi', True), ask=lambda p: {'critique': 'greeting', 'should_call_scout': 'no'})
        self.assertEqual((result['verdict'], result['failure_type']), ('fail', 'called_wrongly'))


def generation(call_id, time, messages, output, session='sess1'):
    return {'id': call_id, 'sessionId': session, 'startTime': time, 'input': json.dumps(messages), 'output': json.dumps(output) if output is not None else None}


class Pulling(unittest.TestCase):
    """The session from the bug report (427742b3...): the person answered three times and the coordinator never called the scout agent again."""

    def session(self):
        m1 = [SYSTEM, user('dentist near me')]
        m2 = m1 + [scout_call('dentist near me'), report('needs_input', missing=['area', 'city'])]
        m3 = m2 + [ask_call('Which area and city?'), answer('satyanarayanapuram vijayawada')]
        m4 = m3 + [scout_call('dentist near me in Satyanarayanapuram, Vijayawada'), report('needs_input', missing=['area'])]
        m5 = m4 + [ask_call('A different spelling?'), answer('Vijayawada')]
        m6 = m5 + [ask_call('Which neighbourhood?'), answer('satyanarayanapuram')]
        m7 = m6 + [ask_call('Confirm the spelling?'), answer('Satyanarayana Puram, within Vijayawada.')]
        rows = [generation('g1', '2026-11-18T08:38:31Z', m1 + TOOLS, out_call('scout_agent', request='dentist near me')),
                generation('g2', '2026-11-18T08:38:32Z', m2 + TOOLS, out_call('ask_user', question='Which area and city?')),
                generation('g3', '2026-11-18T08:38:48Z', m3 + TOOLS, out_call('scout_agent', request='dentist near me in Satyanarayanapuram, Vijayawada')),
                generation('g4', '2026-11-18T08:39:35Z', m4 + TOOLS, out_call('ask_user', question='A different spelling?')),
                generation('g5', '2026-11-18T08:42:03Z', m5 + TOOLS, out_call('ask_user', question='Which neighbourhood?')),
                generation('g6', '2026-11-18T08:42:16Z', m6 + TOOLS, out_call('ask_user', question='Confirm the spelling?')),
                generation('g7', '2026-11-18T08:42:35Z', m7 + TOOLS, out_text('I still need the exact neighbourhood (area) name.')),
                generation('gx', '2026-11-18T08:42:36Z', m7 + TOOLS, None)]  # a model call that failed and was retried: no output, no decision
        return list(reversed(rows))  # Langfuse does not promise an order

    def test_every_model_call_with_an_output_is_read_oldest_first(self):
        with mock.patch.object(ev.eval_core, '_paged', return_value=self.session()) as paged:
            calls = ev.pull_calls('sess1')
        self.assertEqual([c['call_id'] for c in calls], ['g1', 'g2', 'g3', 'g4', 'g5', 'g6', 'g7'])  # gx made no decision
        flt = paged.call_args[0][0]
        self.assertIn({'type': 'string', 'column': 'name', 'operator': '=', 'value': 'Coordinator: model call'}, flt)
        self.assertIn({'type': 'string', 'column': 'sessionId', 'operator': '=', 'value': 'sess1'}, flt)
        self.assertEqual([(c['trigger'], c['called_scout']) for c in calls],
                         [('person_message', True), ('scout_report', False), ('person_answer', True), ('scout_report', False),
                          ('person_answer', False), ('person_answer', False), ('person_answer', False)])
        self.assertIn('Person answered: Vijayawada', calls[4]['memory'])
        self.assertNotIn('"type": "function"', calls[4]['memory'])  # the tool definitions are not part of the chat

    def test_the_session_fails_on_the_three_answers_it_ignored(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(ev, 'HERE', Path(folder)), \
                mock.patch.object(ev, 'judge_prompt', return_value=FakePrompt()), \
                mock.patch.object(ev.eval_core, '_paged', return_value=self.session()), \
                mock.patch.object(ev.eval_core, '_ask', return_value={'critique': 'continues the search', 'should_call_scout': 'yes'}):
            results = ev.run_session('sess1')
            self.assertEqual([r['verdict'] for r in results], ['pass', 'pass', 'pass', 'pass', 'fail', 'fail', 'fail'])
            self.assertEqual({r['failure_type'] for r in results if r['verdict'] == 'fail'}, {'not_called'})
            saved = json.loads((Path(folder) / 'scout_call_sessions.json').read_text(encoding='utf-8'))
            self.assertEqual((saved['sess1']['verdict'], saved['sess1']['calls'], saved['sess1']['failed_calls']), ('fail', 7, 3))
            self.assertEqual(len(json.loads((Path(folder) / 'scout_call_results.json').read_text(encoding='utf-8'))), 7)

    def test_a_session_where_every_step_was_right_passes(self):
        m1 = [SYSTEM, user('gyms in Koramangala Bangalore')]
        rows = [generation('a1', '2026-11-18T09:00:00Z', m1 + TOOLS, out_call('scout_agent', request='gyms in Koramangala Bangalore')),
                generation('a2', '2026-11-18T09:00:30Z', m1 + [scout_call('gyms in Koramangala Bangalore'), report('ready')] + TOOLS, out_text('Found 20 gyms.'))]
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(ev, 'HERE', Path(folder)), mock.patch.object(ev, 'judge_prompt', return_value=FakePrompt()), \
                mock.patch.object(ev.eval_core, '_paged', return_value=rows), mock.patch.object(ev.eval_core, '_ask', return_value={'critique': 'a search', 'should_call_scout': 'yes'}):
            self.assertEqual([r['verdict'] for r in ev.run_session('sess1')], ['pass', 'pass'])
            self.assertEqual(json.loads((Path(folder) / 'scout_call_sessions.json').read_text(encoding='utf-8'))['sess1']['verdict'], 'pass')


class Limits(unittest.TestCase):
    def test_a_run_over_many_sessions_stops_cleanly_when_the_judge_model_hits_its_limit(self):
        def fake(sid, ask=None):
            if sid == 's2':
                raise RuntimeError('judge model limit reached, try again in 14 min')
            return [{'verdict': 'pass', 'failure_type': 'none', 'trigger': 'person_message', 'expected': 'yes', 'did': 'x', 'memory': '', 'source': 'rule', 'critique': ''}]
        with mock.patch.object(ev, 'run_session', fake):
            summary = ev.run(['s1', 's2', 's3'])
        self.assertEqual(summary, {'s1': 'pass'})  # what was done is kept, the rest waits for the next run (--missing)


class Cases(unittest.TestCase):
    """The labelled cases the judge is calibrated on (scout_call_cases.json) and the prompt they are kept apart from."""

    def setUp(self):
        self.cases = json.loads((HERE / 'scout_call_cases.json').read_text(encoding='utf-8'))
        self.prompt = (HERE / 'prompts' / 'scout-call-judge.md').read_text(encoding='utf-8')

    def test_cases_are_well_formed(self):
        ids = [c['id'] for c in self.cases]
        self.assertEqual(len(ids), len(set(ids)))
        for c in self.cases:
            self.assertIn(c['split'], ('train', 'dev', 'test'))
            self.assertIn(c['label'], ('yes', 'no'))
            self.assertIn(c['trigger'], ('person_message', 'person_answer'))
            self.assertTrue(c['memory'].strip().endswith(c['latest'].strip()[-30:]), c['id'])  # the memory ends with what the person said last

    def test_the_examples_in_the_prompt_are_the_train_split_and_nothing_else(self):
        """An example shown to the judge must not be scored (leakage), and every example shown must be a train case."""
        for c in self.cases:
            line = f'LATEST ({c["trigger"]}): {c["latest"]}\n'
            if c['split'] == 'train':
                self.assertIn(line, self.prompt, c['id'])
            else:
                self.assertNotIn(line, self.prompt, f'{c["id"]} is in the prompt but is scored')
        shown = self.prompt.count('LATEST (') - 1  # the last one is the placeholder line
        self.assertEqual(shown, sum(c['split'] == 'train' for c in self.cases))

    def test_dev_and_test_have_both_labels_and_the_prompt_has_the_placeholders(self):
        for split in ('dev', 'test'):
            labels = {c['label'] for c in self.cases if c['split'] == split}
            self.assertEqual(labels, {'yes', 'no'}, split)
        for placeholder in ('{{memory}}', '{{latest}}', '{{trigger}}'):
            self.assertIn(placeholder, self.prompt)


if __name__ == '__main__':
    unittest.main()
