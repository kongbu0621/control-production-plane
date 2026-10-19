"""Knowledge observations cannot consume the declared response byte budget."""
import unittest
from unittest.mock import Mock

from control_production_plane import ControlError, initialize, model as m
from control_production_plane.knowledge import KnowledgeAdapter
from test_core import command, ready
import test_knowledge as fixtures


@unittest.skipUnless(fixtures.HAS_K, 'K and Ledger required')
class ExecutionCheckCapacityTests(unittest.TestCase):
    setUp = fixtures.KnowledgeTests.setUp
    op = fixtures.KnowledgeTests.op
    queue = fixtures.KnowledgeTests.queue

    def prepared(self, name, response_budget):
        self.spec['plan']['content'] = 'x' * 16384
        self.spec['budget']['max_response_bytes'] = response_budget
        self.p = initialize(self.root / name)
        self.p.execute(command('create', self.spec))
        self.a = KnowledgeAdapter(self.p, self.config)
        self.a.adopt('example', self.context)
        self.p.perform(self.queue('prepare'), self.spec['binding'], ready)
        return self.queue('submit')

    def response(self, bytes_count):
        empty = {'text': ''}
        return {'text': 'r' * (bytes_count - len(m.canonical(empty)))}

    def test_new_check_rejects_before_business_when_declared_response_will_not_fit(self):
        aid = self.prepared('oversized', 250000)
        action = self.p.get_action(aid)
        # This legal reply filled the pre-check action exactly. Its standalone
        # value and response command are both within the public JSON limit.
        old_size = len(m.canonical(dict(action, state='RESPONDED', call_closed=True, response=None)))
        reply = self.response(m.MAX_JSON - old_size + 4)
        m.validate_tree(reply)
        self.assertLessEqual(len(m.canonical(reply)), 250000)
        invoke = Mock(return_value=dict(response=reply, call_closed=True))
        with self.assertRaises(ControlError) as error:
            self.p.perform(aid, self.spec['binding'], invoke, knowledge=self.a)
        self.assertEqual(error.exception.code, 'LIMIT')
        self.assertEqual(invoke.call_count, 0)
        state = self.p.get_run('example')
        self.assertFalse(state['attempt']['submission_recorded'])
        self.assertEqual(state['counters']['attempts'], 0)
        self.assertEqual(self.p.get_action(aid)['state'], 'QUEUED')
        self.assertNotIn('knowledge_check', self.p.get_action(aid))
        self.assertEqual(len(state['knowledge_calls']), 1)
        self.p.execute(command('cancel', dict(run_id='example', reason='Stop the oversized plan')))
        self.assertIsNotNone(self.p.get_run('example')['cancel'])

    def test_exact_response_capacity_survives_recording_reopen_and_close(self):
        aid = self.prepared('sizing', 250000)
        action = self.p.get_action(aid)
        checked = dict(action, knowledge_check=self.a.check_execution(self.p.get_run('example')),
                       state='RESPONDED', call_closed=False, response=None)
        available = m.MAX_JSON - len(m.canonical(checked)) + 4
        aid = self.prepared('exact', available)
        reply = self.response(available)
        self.p.perform(aid, self.spec['binding'], lambda _: dict(response=reply, call_closed=False), knowledge=self.a)
        self.assertEqual(len(m.canonical(self.p.get_action(aid))), m.MAX_JSON)
        from control_production_plane import open_store
        reopened = open_store(self.p.root)
        self.assertEqual(reopened.get_action(aid)['response'], reply)
        reopened.execute(command('close_call', dict(action_id=aid, evidence_ref='synthetic:closed')))
        self.assertTrue(reopened.get_action(aid)['call_closed'])
        self.assertEqual(reopened.get_action(aid)['response'], reply)

        aid = self.prepared('one-byte-over', available + 1)
        invoke = Mock(return_value=dict(response={}, call_closed=True))
        with self.assertRaises(ControlError) as error:
            self.p.perform(aid, self.spec['binding'], invoke, knowledge=self.a)
        self.assertEqual(error.exception.code, 'LIMIT')
        self.assertEqual(invoke.call_count, 0)
        self.assertEqual(self.p.get_action(aid)['state'], 'QUEUED')
