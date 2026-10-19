"""Execution eligibility is fresh; an original adoption remains historical."""
from unittest.mock import Mock, patch
import unittest

from control_production_plane import ControlError, open_store
from control_production_plane.knowledge import KnowledgeAdapter
from test_core import command, ready
import test_knowledge as fixtures


@unittest.skipUnless(fixtures.HAS_K, 'K and Ledger required')
class ExecutionKnowledgeTests(unittest.TestCase):
    setUp = fixtures.KnowledgeTests.setUp
    op = fixtures.KnowledgeTests.op
    queue = fixtures.KnowledgeTests.queue

    def prepared(self):
        self.a.adopt('example', self.context)
        self.p.perform(self.queue('prepare'), self.spec['binding'], ready)
        return self.queue('submit')

    def retire(self):
        self.op('retire', dict(expected_version_id=self.version, reviewer='example:reviewer',
                reason='No new execution', evidence_material_ids=['material:example']))

    def test_historical_receipt_cannot_directly_dispatch_after_retirement(self):
        aid = self.prepared()
        self.retire()
        with self.p.executor() as lease:
            with self.assertRaises(ControlError):
                self.p.execute(command('dispatch', dict(action_id=aid, observed_binding=self.spec['binding'])), lease=lease)
        self.assertEqual(self.p.get_action(aid)['state'], 'QUEUED')
        self.assertEqual(self.p.get_run('example')['counters']['attempts'], 0)

    def perform_submit(self, aid, **kwargs):
        invoke = Mock(return_value=dict(response=dict(accepted=True), call_closed=True))
        result = self.p.perform(aid, self.spec['binding'], invoke, knowledge=self.a, **kwargs)
        invoke.assert_called_once()
        return result

    def assert_unsubmitted(self, aid):
        state = self.p.get_run('example')
        self.assertEqual(self.p.get_action(aid)['state'], 'QUEUED')
        self.assertFalse(state['attempt']['submission_recorded'])
        self.assertEqual(state['counters']['attempts'], 0)

    def test_success_retains_check_with_original_exact_inputs(self):
        aid = self.prepared()
        self.perform_submit(aid)
        action = open_store(self.root/'production').get_action(aid)
        check = action['knowledge_check']
        self.assertEqual(check['state'], 'ACTIVE')
        self.assertEqual(check['item'], self.spec['plan']['knowledge']['items'][0])
        self.assertEqual(check['knowledge_store_id'], self.config['store_id'])
        self.assertTrue(all(x['status'] == 'VERIFIED' for x in check['materials']))
        self.assertEqual(len(self.p.get_run('example')['knowledge_calls']), 1)

    def test_missing_adapter_never_invokes_or_spends(self):
        aid = self.prepared()
        invoke = Mock()
        with self.assertRaises(ControlError):
            self.p.perform(aid, self.spec['binding'], invoke)
        invoke.assert_not_called()
        self.assert_unsubmitted(aid)
        self.perform_submit(aid)

    def test_retirement_after_executor_preflight_blocks_and_history_survives(self):
        aid = self.prepared()
        invoke = Mock()
        with self.assertRaises(ControlError):
            self.p.perform(aid, self.spec['binding'], invoke, knowledge=self.a, preflight=lambda _: self.retire())
        invoke.assert_not_called()
        self.assert_unsubmitted(aid)
        self.assertEqual(self.k.get('version', self.version)['record']['state'], 'RETIRED')
        self.a.feedback('example', 1, 'Not submitted after retirement.')
        self.assertEqual(len(self.p.get_run('example')['knowledge_calls']), 2)

    def test_cancel_during_k_read_is_not_blocked_and_prevents_dispatch(self):
        aid = self.prepared()
        original = self.k.get
        def cancel(kind, identity):
            result = original(kind, identity)
            if kind == 'version':
                # Separate P handle, so a held write transaction would fail.
                open_store(self.root/'production').execute(command('cancel', dict(run_id='example', reason='Stop during K read')))
            return result
        with patch.object(self.a, '_open', return_value=self.k), patch.object(self.k, 'get', side_effect=cancel):
            with self.assertRaises(ControlError):
                self.p.perform(aid, self.spec['binding'], Mock(side_effect=AssertionError('no submit')), knowledge=self.a)
        self.assert_unsubmitted(aid)
        self.assertIsNotNone(self.p.get_run('example')['cancel'])

    def test_k_read_error_can_retry_same_action_with_fresh_read(self):
        aid = self.prepared()
        with patch.object(self.a, '_open', side_effect=OSError('transient read')):
            with self.assertRaises(ControlError) as error:
                self.perform_submit(aid)
        self.assertEqual(error.exception.code, 'KNOWLEDGE_UNAVAILABLE')
        self.assert_unsubmitted(aid)
        self.perform_submit(aid)

    def test_material_store_missing_rejects_then_recovers_without_k_write(self):
        aid = self.prepared()
        path = self.root/'materials.sqlite'
        saved = path.with_suffix('.saved')
        path.rename(saved)
        try:
            with self.assertRaises(ControlError) as error: self.perform_submit(aid)
            self.assertEqual(error.exception.code, 'KNOWLEDGE_UNAVAILABLE')
            self.assert_unsubmitted(aid)
        finally:
            saved.rename(path)
        with patch.object(self.k, 'execute', side_effect=AssertionError('read check only')):
            with patch.object(self.a, '_open', return_value=self.k): self.perform_submit(aid)

    def test_reopened_p_still_requires_current_k_state(self):
        aid = self.prepared()
        self.retire()
        self.p = open_store(self.root/'production')
        self.a = KnowledgeAdapter(self.p, self.config)
        with self.assertRaises(ControlError): self.perform_submit(aid)
        self.assert_unsubmitted(aid)

    def test_unrelated_k_scope_rejected_even_with_same_store(self):
        aid = self.prepared()
        self.a = KnowledgeAdapter(self.p, dict(self.config, scope='another:consumer'))
        with self.assertRaises(ControlError) as error: self.perform_submit(aid)
        self.assertEqual(error.exception.code, 'IDENTITY_CONFLICT')
        self.assert_unsubmitted(aid)

    def test_missing_committed_receipt_never_recreates_use(self):
        from domain_operational_knowledge_core import KnowledgeError
        aid = self.prepared()
        original = self.k.get_operation
        def missing(scope, operation, request_id):
            if operation == 'start_use': raise KnowledgeError('NOT_FOUND', 'simulated restored older K', 'not_applicable')
            return original(scope, operation, request_id)
        with patch.object(self.a, '_open', return_value=self.k), patch.object(self.k, 'get_operation', side_effect=missing), \
                patch.object(self.k, 'execute', side_effect=AssertionError('never re-adopt')):
            with self.assertRaises(ControlError): self.perform_submit(aid)
        self.assert_unsubmitted(aid)

    def test_completed_dispatch_not_replayed_when_k_goes_away(self):
        aid = self.prepared()
        self.perform_submit(aid)
        self.retire()
        with patch.object(self.a, '_open', side_effect=AssertionError('no K needed for old result')):
            with self.assertRaises(ControlError) as error: self.perform_submit(aid)
            self.assertEqual(error.exception.code, 'PENDING')
            for kind in ('query', 'cancel'):
                if kind == 'cancel': self.p.execute(command('cancel', dict(run_id='example', reason='Stop original')))
                self.p.perform(self.queue(kind), self.spec['binding'], lambda _: dict(response={}, call_closed=True), knowledge=self.a)
        self.assertEqual(self.p.get_run('example')['counters']['attempts'], 1)

    def test_selected_optional_knowledge_also_needs_fresh_check(self):
        plan = self.spec['plan']
        plan['knowledge']['required'] = False
        self.p.execute(command('revise', dict(run_id='example', expected_revision=1, plan=plan)))
        aid = self.prepared()
        self.retire()
        with self.assertRaises(ControlError): self.perform_submit(aid)
        self.assert_unsubmitted(aid)

    def test_superseded_version_is_not_silently_replaced(self):
        import uuid
        from control_production_plane.knowledge import encoded
        aid = self.prepared()
        self.a.feedback('example', 1, 'New synthetic observation before execution.')
        current = self.p.get_run('example')
        material_id = next(c['receipt']['result']['material_id'] for c in current['knowledge_calls'].values() if c['request']['operation'] == 'complete_use')
        proposal = self.op('propose_revision', dict(proposal_id='revision:example', expected_version_id=self.version,
                            kind='guidance', content='New guidance', conditions=self.context,
                            material_ids=['material:example', material_id]))['receipt']['result']
        self.k.execute(encoded(dict(schema='operational-knowledge-v1', scope='example:reviewer', operation='review',
                request_id=str(uuid.uuid4()), body=dict(candidate_id=proposal['candidate_id'],
                candidate_sha256=proposal['candidate_sha256'], decision='ACCEPT', reviewer='example:reviewer',
                reason='New version', evidence_material_ids=['material:example']))))
        with self.assertRaises(ControlError): self.perform_submit(aid)
        self.assert_unsubmitted(aid)
        self.assertEqual(self.p.get_run('example')['plan']['knowledge']['items'][0]['version_id'], self.version)

    def test_k_snapshot_before_usage_cannot_authorize_committed_p(self):
        import shutil
        # Both K calls are closed. Copy only synthetic fixture data to model
        # a consistent older snapshot containing the anchor but not the usage.
        older = self.root/'older-knowledge'
        shutil.copytree(self.root/'knowledge', older)
        aid = self.prepared()
        self.a = KnowledgeAdapter(self.p, dict(self.config, store_path=str(older)))
        with self.assertRaises(ControlError): self.perform_submit(aid)
        self.assert_unsubmitted(aid)
        self.a = KnowledgeAdapter(self.p, self.config)
        self.perform_submit(aid)

    def test_process_exit_before_and_after_dispatch_preserves_boundaries(self):
        import json
        import subprocess
        import sys
        import time
        aid = self.prepared()
        code = '''
import json, sys, time
from pathlib import Path
from control_production_plane import open_store
from control_production_plane.knowledge import KnowledgeAdapter
store = open_store(sys.argv[1]); config = json.loads(sys.argv[2]); phase = sys.argv[3]
marker = Path(sys.argv[4]); aid = sys.argv[5]
adapter = KnowledgeAdapter(store, config)
check = adapter.check_execution
def pause():
    marker.write_text('reached')
    time.sleep(30)
def checked(state):
    observation = check(state)
    if phase == 'before': pause()
    return observation
def invoked(payload):
    pause()
adapter.check_execution = checked
store.perform(aid, store.get_run('example')['binding'], invoked, knowledge=adapter)
'''
        for phase in ('before', 'after'):
            with self.subTest(phase=phase):
                marker = self.root/phase
                child = subprocess.Popen([sys.executable, '-B', '-c', code, str(self.p.root),
                                          json.dumps(self.config), phase, str(marker), aid],
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    until = time.monotonic() + 10
                    while not marker.exists() and child.poll() is None and time.monotonic() < until:
                        time.sleep(.02)
                    self.assertTrue(marker.exists(), 'child did not reach the requested boundary')
                    child.kill()
                    out, err = child.communicate(timeout=3)
                    self.assertEqual(child.returncode, -9, err)
                finally:
                    if child.poll() is None:
                        child.kill(); child.communicate(timeout=3)
                if phase == 'before':
                    self.assert_unsubmitted(aid)
                    self.assertNotIn('knowledge_check', self.p.get_action(aid))
                else:
                    action = self.p.get_action(aid)
                    self.assertEqual(action['state'], 'MAY_HAVE_RUN')
                    self.assertEqual(action['knowledge_check']['item']['version_id'], self.version)
                    with self.assertRaises(ControlError): self.perform_submit(aid)
                    self.assertEqual(self.p.get_run('example')['counters']['attempts'], 1)
