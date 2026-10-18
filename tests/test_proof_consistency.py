"""Proofs must not erase retained execution facts across separate transactions."""
import unittest

from control_production_plane import ControlError, model
import test_core as core


class ProofConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = core.CoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        self.fixture.prepare()

    def failed_submit(self, *, transport=False):
        action = self.fixture.queue('submit')
        def invoke(payload):
            if transport:
                raise RuntimeError('Synthetic transport uncertainty')
            return dict(response={'remote_error': 'IO_UNCERTAIN'}, call_closed=True)
        if transport:
            with self.assertRaises(ControlError):
                self.store.perform(action, self.fixture.spec['binding'], invoke)
        else:
            self.store.perform(action, self.fixture.spec['binding'], invoke)
        return action

    def prove(self, action, *, transport=False):
        attempt = self.store.get_run('example')['attempt']
        body = dict(action_id=action, evidence_ref='demo:independent-proof')
        if not transport:
            body.update(operation_id=attempt['operation_id'],
                        request_digest=attempt['prepared_request']['request_digest'])
        return self.store.execute(core.command('not_started' if transport else 'confirm_not_accepted', body))

    def stop(self):
        state = self.store.get_run('example'); attempt = state['attempt']
        body = dict(run_id='example', attempt_id=attempt['attempt_id'], operation_id=attempt['operation_id'],
                    request_digest=attempt['prepared_request']['request_digest'], binding_digest=model.digest(state['binding']),
                    future_start_blocked=True, tree_exited=True, writers_stopped=True,
                    effect_scope='TASK_OWNED_COPY', evidence_ref='demo:maintenance-exit-proof')
        self.store.execute(core.command('confirm_stopped', body))

    def test_saved_running_response_blocks_non_acceptance_before_observe_commit(self):
        action = self.failed_submit()
        query = self.fixture.queue('query')
        observation = self.fixture.observation(remote_state='RUNNING', outcome='UNKNOWN', exited=False)
        self.store.perform(query, self.fixture.spec['binding'],
                           lambda p: dict(response={'observation': observation}, call_closed=True))
        # Model a process exit between respond and the adapter's observe command.
        self.assertIsNone(self.store.get_run('example')['attempt']['observation'])
        before = self.store.get_run('example'); original = self.store.get_action(action)
        with self.assertRaises(ControlError):
            self.prove(action)
        self.assertEqual(self.store.get_run('example'), before)
        self.assertEqual(self.store.get_action(action), original)

    def test_saved_running_response_blocks_no_start_even_after_later_not_found(self):
        action = self.failed_submit(transport=True)
        query = self.fixture.queue('query')
        observation = self.fixture.observation(remote_state='RUNNING', outcome='UNKNOWN', exited=False)
        self.store.perform(query, self.fixture.spec['binding'],
                           lambda p: dict(response={'observation': observation}, call_closed=True))
        self.store.execute(core.command('observe', dict(observation, remote_state='NOT_FOUND', evidence_state='UNKNOWN')))
        before = self.store.get_run('example')
        with self.assertRaises(ControlError):
            self.prove(action, transport=True)
        self.assertEqual(self.store.get_run('example'), before)

    def test_stop_proof_cannot_be_combined_with_non_acceptance_to_reopen_task(self):
        action = self.failed_submit(); self.stop()
        before = self.store.get_run('example'); original = self.store.get_action(action)
        with self.assertRaises(ControlError):
            self.prove(action)
        self.assertEqual(self.store.get_run('example'), before)
        self.assertEqual(self.store.get_action(action), original)
        with self.assertRaises(ControlError):
            self.fixture.queue('submit')

    def test_stop_proof_cannot_be_combined_with_no_start_to_reopen_task(self):
        action = self.failed_submit(transport=True)
        self.store.execute(core.command('close_call', dict(action_id=action, evidence_ref='demo:closed')))
        self.stop(); before = self.store.get_run('example')
        with self.assertRaises(ControlError):
            self.prove(action, transport=True)
        self.assertEqual(self.store.get_run('example'), before)

    def test_legacy_released_stopped_record_cannot_queue_dispatch_or_revise_unknown(self):
        waiting = self.fixture.queue('prepare')
        action = self.failed_submit(); self.stop()
        # Model a record saved by the previous release, before this proof guard.
        from unittest.mock import patch
        with patch.object(self.store, '_require_no_execution_observed', return_value=None):
            self.prove(action)
        before = self.store.get_run('example')
        self.assertFalse(before['attempt']['submission_recorded'])
        self.assertIn('maintenance_stop', before['attempt'])
        for kind in ('prepare', 'submit'):
            with self.subTest(kind=kind), self.assertRaises(ControlError):
                self.fixture.queue(kind)
        calls = []
        with self.assertRaises(ControlError):
            self.store.perform(waiting, self.fixture.spec['binding'], lambda p: calls.append(p))
        self.assertEqual(calls, [])
        with self.assertRaises(ControlError):
            self.store.execute(core.command('revise', dict(run_id='example', expected_revision=1, plan=before['plan'])))
        self.assertEqual(self.store.get_run('example'), before)

    def test_retained_not_found_response_does_not_permanently_block_independent_proof(self):
        action = self.failed_submit()
        query = self.fixture.queue('query')
        observation = self.fixture.observation(remote_state='NOT_FOUND', outcome='UNKNOWN', exited=False)
        observation['evidence_state'] = 'UNKNOWN'
        self.store.perform(query, self.fixture.spec['binding'],
                           lambda p: dict(response={'observation': observation}, call_closed=True))
        self.prove(action)
        retry = self.fixture.queue('submit'); calls = []
        self.store.perform(retry, self.fixture.spec['binding'],
                           lambda p: (calls.append(p) or dict(response={'accepted': True}, call_closed=True)))
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.store.get_run('example')['counters']['attempts'], 1)

    def test_old_attempt_response_does_not_block_new_attempt_proof(self):
        action = self.fixture.queue('submit')
        observation = self.fixture.observation(remote_state='TERMINAL', outcome='FAILED', exited=True)
        self.store.perform(action, self.fixture.spec['binding'],
                           lambda p: dict(response={'observation': observation}, call_closed=True))
        self.store.execute(core.command('observe', observation))
        self.store.execute(core.command('revise', dict(run_id='example', expected_revision=1, plan=self.fixture.spec['plan'])))
        self.fixture.prepare(); failed = self.failed_submit(); self.prove(failed)
        current = self.store.get_run('example')
        self.assertEqual(current['revision'], 2)
        self.assertEqual(current['counters']['attempts'], 1)
        self.assertFalse(current['attempt']['submission_recorded'])
        self.assertEqual(self.store.get_action(action)['response']['observation'], observation)

    def test_stopped_attempt_with_later_confirmed_failure_can_revise_without_replaying_it(self):
        self.failed_submit(); self.stop()
        self.store.execute(core.command('observe', self.fixture.observation()))
        original = self.store.get_run('example')
        self.store.execute(core.command('revise', dict(run_id='example', expected_revision=1, plan=self.fixture.spec['plan'])))
        self.fixture.prepare(); action = self.fixture.queue('submit'); calls = []
        self.store.perform(action, self.fixture.spec['binding'],
                           lambda p: (calls.append(p['operation_id']) or dict(response={'accepted': True}, call_closed=True)))
        self.assertEqual(len(calls), 1)
        self.assertNotEqual(calls[0], original['attempt']['operation_id'])
        self.assertEqual(self.store.get_revision('example', 1), original)
        self.assertNotIn('maintenance_stop', self.store.get_run('example')['attempt'])

