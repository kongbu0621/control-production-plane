"""Explicit, independently proved rejection after a real failed transport call."""
import json
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch

from control_production_plane import ControlError
import test_core as core
command = core.command


class NonAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = core.CoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        self.fixture.prepare()
        self.action = self.fixture.queue('submit')
        self.response = dict(remote_error='IO_UNCERTAIN', call_id=self.action, response_sha256='4'*64)
        self.store.perform(self.action, self.fixture.spec['binding'],
            lambda payload: dict(response=self.response, call_closed=True))
        state = self.store.get_run('example')
        self.proof = dict(action_id=self.action, operation_id=state['attempt']['operation_id'],
            request_digest=state['attempt']['prepared_request']['request_digest'], evidence_ref='demo:independent-nonacceptance-proof')

    def prove(self, key=None, **changes):
        return self.store.execute(command('confirm_not_accepted', dict(self.proof, **changes), key))

    def test_failed_call_is_preserved_and_only_business_reservation_is_released(self):
        before=self.store.get_run('example');old=self.store.get_action(self.action)
        self.prove('proof:first')
        after=self.store.get_run('example');action=self.store.get_action(self.action)
        self.assertEqual(action['response'],old['response'])
        self.assertEqual(action['state'],'RESPONDED')
        self.assertTrue(action['call_closed'])
        self.assertEqual(action['non_acceptance'],self.proof)
        self.assertEqual(after['counters'],dict(before['counters'],attempts=0))
        self.assertFalse(after['attempt']['submission_recorded'])
        self.assertEqual(after['attempt']['prepared_request'],before['attempt']['prepared_request'])
        retry=self.fixture.queue('submit');effects=[]
        self.store.perform(retry,self.fixture.spec['binding'],lambda payload:
            (effects.append(payload['operation_id']) or dict(response={'accepted':True},call_closed=True)))
        current=self.store.get_run('example')
        self.prove('proof:repeated')
        self.assertTrue(self.prove('proof:first')['replayed'])
        self.assertEqual(self.store.get_run('example'),current)
        self.assertEqual(effects,[self.proof['operation_id']])
        with self.assertRaises(ControlError):self.prove(evidence_ref='demo:different-proof')
        with self.assertRaises(ControlError):self.fixture.queue('submit')

    def test_exact_binding_closed_call_and_error_response_are_required(self):
        before=self.store.get_run('example')
        for changes in ({'operation_id':'wrong:task'},{'request_digest':'0'*64},{'evidence_ref':''}):
            with self.subTest(changes=changes),self.assertRaises(ControlError):self.prove(**changes)
            self.assertEqual(self.store.get_run('example'),before)
        query=self.fixture.queue('query')
        self.store.perform(query,self.fixture.spec['binding'],lambda payload:dict(response={'remote_error':'NOT_FOUND'},call_closed=True))
        with self.assertRaises(ControlError):self.prove(action_id=query)
        # An actual normal response cannot be turned into a rejected submission.
        self.prove();retry=self.fixture.queue('submit')
        self.store.perform(retry,self.fixture.spec['binding'],lambda payload:dict(response={'accepted':True},call_closed=True))
        with self.assertRaises(ControlError):self.prove(action_id=retry)

    def test_positive_history_cannot_be_erased_by_later_not_found(self):
        positive=self.fixture.observation(outcome='UNKNOWN',exited=False,remote_state='RUNNING')
        self.store.execute(command('observe',positive))
        self.store.execute(command('observe',dict(positive,remote_state='NOT_FOUND',evidence_state='UNKNOWN')))
        before=self.store.get_run('example')
        with self.assertRaises(ControlError):self.prove()
        self.assertEqual(self.store.get_run('example'),before)
        self.assertNotIn('non_acceptance',self.store.get_action(self.action))

    def test_pending_calls_block_proof_and_cancellation_still_blocks_retry(self):
        query=self.fixture.queue('query')
        self.store.perform(query,self.fixture.spec['binding'],lambda payload:dict(response={'remote_error':'NOT_FOUND'},call_closed=False))
        with self.assertRaises(ControlError):self.prove()
        self.store.execute(command('close_call',dict(action_id=query,evidence_ref='demo:closed')))
        self.store.execute(command('cancel',dict(run_id='example',reason='Stop production')))
        self.prove()
        with self.assertRaises(ControlError):self.fixture.queue('submit')

    def test_cli_proof_and_transaction_rollback_preserve_original_response(self):
        original=self.store.get_action(self.action);before=self.store.get_run('example')
        with patch.object(self.store,'_save',side_effect=sqlite3.OperationalError('Synthetic transaction failure')):
            with self.assertRaises(ControlError):self.prove('proof:transaction')
        self.assertEqual(self.store.get_run('example'),before)
        self.assertEqual(self.store.get_action(self.action),original)
        path=self.fixture.root/'proof.json';path.write_text(json.dumps(command('confirm_not_accepted',self.proof,'proof:transaction')))
        result=subprocess.run([sys.executable,'-m','control_production_plane.cli','--store',str(self.fixture.path),
            'write','--request',str(path)],capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'],'OK')
        self.assertEqual(self.store.get_action(self.action)['response'],original['response'])
        self.assertTrue(self.prove('proof:transaction')['replayed'])

    def test_committed_proof_with_lost_response_is_not_applied_twice(self):
        connect=sqlite3.connect
        class Connection(sqlite3.Connection):
            def commit(self):
                super().commit()
                raise sqlite3.OperationalError('Synthetic lost commit response')
        with patch('control_production_plane.store.sqlite3.connect',
                   side_effect=lambda *a,**k:connect(*a,**k,factory=Connection)):
            with self.assertRaises(ControlError) as error:self.prove('proof:lost')
        self.assertEqual(error.exception.commit_state,'unknown')
        self.assertTrue(self.prove('proof:lost')['replayed'])
        self.assertEqual(self.store.get_run('example')['counters']['attempts'],0)
        self.assertEqual(self.store.get_action(self.action)['response'],self.response)

    def test_submission_without_exit_and_eof_proof_is_not_released(self):
        other=core.CoreTests();other.setUp();self.addCleanup(other.doCleanups)
        other.prepare();action=other.queue('submit')
        other.store.perform(action,other.spec['binding'],lambda payload:dict(response={'remote_error':'IO_UNCERTAIN'},call_closed=False))
        state=other.store.get_run('example');attempt=state['attempt']
        proof=dict(action_id=action,operation_id=attempt['operation_id'],request_digest=attempt['prepared_request']['request_digest'],evidence_ref='demo:proof')
        with self.assertRaises(ControlError):other.store.execute(command('confirm_not_accepted',proof))
        self.assertEqual(other.store.get_run('example'),state)

    def test_mixed_or_malformed_error_envelopes_cannot_release_submission(self):
        for response in ({'remote_error':'IO_UNCERTAIN','accepted':True},
                         {'remote_error':''}, {'remote_error':'IO_UNCERTAIN','call_id':'x','response_sha256':'bad'}):
            with self.subTest(response=response):
                other=core.CoreTests();other.setUp();self.addCleanup(other.doCleanups)
                other.prepare();action=other.queue('submit')
                other.store.perform(action,other.spec['binding'],lambda payload:dict(response=response,call_closed=True))
                state=other.store.get_run('example');attempt=state['attempt']
                proof=dict(action_id=action,operation_id=attempt['operation_id'],request_digest=attempt['prepared_request']['request_digest'],evidence_ref='demo:proof')
                with self.assertRaises(ControlError):other.store.execute(command('confirm_not_accepted',proof))
                self.assertEqual(other.store.get_run('example'),state)
