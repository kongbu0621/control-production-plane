"""Receipt capacity is reserved before K writes, including concurrent updates."""
import copy
import sqlite3
import unittest
from unittest.mock import patch

from control_production_plane import ControlError, model, open_store
from control_production_plane.knowledge import KnowledgeAdapter, key
from test_core import command
import test_knowledge as fixtures


@unittest.skipUnless(fixtures.HAS_K, 'optional K and Ledger packages required')
class KnowledgeCapacityTests(unittest.TestCase):
    setUp = fixtures.KnowledgeTests.setUp
    queue = fixtures.KnowledgeTests.queue
    op = fixtures.KnowledgeTests.op

    def pending(self, operation='start_use', *, k_committed=False):
        if operation == 'complete_use':
            self.a.adopt('example', self.context)
        action = (lambda: self.a.adopt('example', self.context)) if operation == 'start_use' else (
            lambda: self.a.feedback('example', 1, 'Synthetic original snapshot'))
        original = self.p.execute
        def lost(request, **kw):
            if request['operation'] == 'knowledge_ack':
                raise OSError('synthetic local acknowledgement failure')
            return original(request, **kw)
        if k_committed:
            with patch.object(self.p, 'execute', side_effect=lost):
                with self.assertRaises(OSError): action()
        else:
            with patch.object(self.a, '_open', return_value=self.k), patch.object(self.k, 'execute', side_effect=OSError('synthetic pre-write failure')):
                with self.assertRaises(OSError): action()
        state = self.p.get_run('example')
        cid = key(self.p.identity['store_id'], state, operation)
        return state, cid

    def legacy(self, state, cid):
        # Version fixture: earlier P stores have the same request/receipt fields,
        # but lack the new capacity marker. Only this synthetic P row is changed.
        state = copy.deepcopy(state)
        state['knowledge_calls'][cid].pop('receipt_capacity_version', None)
        with sqlite3.connect(self.p.root / 'runs.sqlite') as db:
            db.execute('UPDATE runs SET document=? WHERE run_id=?', (model.canonical(state).decode(), 'example'))
        return self.p.get_run('example')

    def test_actual_record_limit_rejects_before_any_k_write(self):
        for revision in range(1, 40):
            self.a.adopt('example', self.context)
            self.a.feedback('example', revision, 'n' * 4096)
            state = self.p.get_run('example')
            if model.MAX_JSON - len(model.canonical(state)) < 15000:
                break
            self.p.execute(command('revise', dict(run_id='example', expected_revision=revision, plan=self.spec['plan'])))
        else:
            self.fail('fixture did not approach the real 256 KiB record limit')
        self.p.execute(command('revise', dict(run_id='example', expected_revision=revision, plan=self.spec['plan'])))
        state = self.p.get_run('example')
        padding = model.MAX_JSON - len(model.canonical(state)) - 1000
        self.assertTrue(0 < padding <= 16384)
        plan = copy.deepcopy(self.spec['plan']); plan['content'] = 'p' * padding
        self.p.execute(command('revise', dict(run_id='example', expected_revision=state['revision'], plan=plan)))
        before = self.p.get_run('example')
        cid = key(self.p.identity['store_id'], before, 'start_use')
        with patch.object(self.a, '_open', return_value=self.k), patch.object(self.k, 'execute', side_effect=AssertionError('no K write without receipt capacity')):
            with self.assertRaises(ControlError) as error: self.a.adopt('example', self.context)
        self.assertEqual(error.exception.code, 'LIMIT')
        self.assertEqual(self.p.get_run('example'), before)
        with self.assertRaises(fixtures.KnowledgeError) as absent:
            self.k.get_operation(self.config['scope'], 'start_use', cid)
        self.assertEqual(absent.exception.code, 'NOT_FOUND')
        self.assertEqual(self.p.recover('example')['pending_knowledge_calls'], [])
        self.assertEqual(self.p.get_run('example')['counters']['attempts'], 0)

    def test_revision_cannot_spend_pending_receipt_capacity(self):
        state, cid = self.pending()
        plan = copy.deepcopy(self.spec['plan']); plan['content'] = 'p' * 12000
        with patch.object(model, 'MAX_JSON', len(model.canonical(state)) + 13000):
            with self.assertRaises(ControlError) as error:
                self.p.execute(command('revise', dict(run_id='example', expected_revision=1, plan=plan)))
            self.assertEqual(error.exception.code, 'LIMIT')
            self.assertEqual(self.p.get_run('example'), state)
            self.a.resume('example', cid)
        self.assertEqual(self.p.get_run('example')['knowledge_calls'][cid]['state'], 'COMMITTED')

    def cancel_during_write(self, operation):
        state, cid = self.pending(operation)
        original = self.k.execute
        def execute(raw):
            self.p.execute(command('cancel', dict(run_id='example', reason='\x00' * 2048)))
            return original(raw)
        # A smaller synthetic ceiling makes the capacity boundary exact without
        # spending extra disk on repeated historical calls in every race case.
        with patch.object(model, 'MAX_JSON', len(model.canonical(state)) + 13500):
            with patch.object(self.a, '_open', return_value=self.k), patch.object(self.k, 'execute', side_effect=execute):
                self.a.resume('example', cid)
            current = self.p.get_run('example')
            self.assertEqual(current['knowledge_calls'][cid]['state'], 'COMMITTED')
            self.assertEqual(current['cancel']['reason'], '\x00' * 2048)
            self.assertNotIn('receipt_capacity_version', current['knowledge_calls'][cid])
            with self.assertRaises(ControlError): self.queue('prepare')
        self.assertEqual(current['counters']['attempts'], 0)

    def test_long_escaped_cancellation_and_adoption_receipt_both_fit(self):
        self.cancel_during_write('start_use')

    def test_long_escaped_cancellation_and_feedback_receipt_both_fit(self):
        self.cancel_during_write('complete_use')

    def test_legacy_unsent_call_reserves_before_writing_and_can_retry(self):
        state, cid = self.pending()
        state = self.legacy(state, cid)
        reopened = KnowledgeAdapter(open_store(self.p.root), self.config)
        with patch.object(model, 'MAX_JSON', len(model.canonical(state)) + 1000):
            with patch.object(reopened, '_open', return_value=self.k), patch.object(self.k, 'execute', side_effect=AssertionError('legacy call needs room before retry')):
                with self.assertRaises(ControlError) as error: reopened.resume('example', cid)
            self.assertEqual(error.exception.code, 'LIMIT')
            self.assertEqual(self.p.get_run('example'), state)
        reopened.resume('example', cid)
        self.assertEqual(self.p.get_run('example')['knowledge_calls'][cid]['state'], 'COMMITTED')

    def test_legacy_committed_k_receipt_can_be_acknowledged_without_new_reservation(self):
        state, cid = self.pending(k_committed=True)
        state = self.legacy(state, cid)
        reopened = KnowledgeAdapter(open_store(self.p.root), self.config)
        with patch.object(model, 'MAX_JSON', len(model.canonical(state)) + 1000):
            with patch.object(reopened, '_open', return_value=self.k), patch.object(self.k, 'execute', side_effect=AssertionError('read existing receipt only')):
                reopened.resume('example', cid)
        self.assertEqual(self.p.get_run('example')['knowledge_calls'][cid]['state'], 'COMMITTED')

    def test_malformed_receipt_does_not_spend_reserved_capacity(self):
        original = self.k.execute
        def extra(raw):
            result = original(raw)
            result['receipt']['result']['unexpected'] = 'x' * 3000
            return result
        with patch.object(self.a, '_open', return_value=self.k), patch.object(self.k, 'execute', side_effect=extra):
            with self.assertRaises(ControlError): self.a.adopt('example', self.context)
        state = self.p.get_run('example'); cid = state['attempt']['knowledge_use']
        self.assertEqual(state['knowledge_calls'][cid]['state'], 'PENDING')
        with patch.object(self.a, '_open', return_value=self.k), patch.object(self.k, 'execute', side_effect=AssertionError('must use original receipt')):
            self.a.resume('example', cid)
        self.assertEqual(self.p.get_run('example')['knowledge_calls'][cid]['state'], 'COMMITTED')

    def test_cancelled_legacy_unsent_call_does_not_gain_a_new_reservation(self):
        state, cid = self.pending()
        self.legacy(state, cid)
        self.p.execute(command('cancel', dict(run_id='example', reason='Stop original attempt')))
        before = self.p.get_run('example')
        with patch.object(self.a, '_open', return_value=self.k), patch.object(self.k, 'execute', side_effect=AssertionError('cancelled use must stay unsent')):
            with self.assertRaises(ControlError) as error: self.a.resume('example', cid)
        self.assertEqual(error.exception.code, 'NOT_READY')
        self.assertEqual(self.p.get_run('example'), before)

    def test_unsupported_reservation_cannot_authorize_a_k_write(self):
        state, cid = self.pending()
        state['knowledge_calls'][cid]['receipt_capacity_version'] = 2
        with sqlite3.connect(self.p.root / 'runs.sqlite') as db:
            db.execute('UPDATE runs SET document=? WHERE run_id=?', (model.canonical(state).decode(), 'example'))
        with patch.object(self.a, '_open', return_value=self.k), patch.object(self.k, 'execute', side_effect=AssertionError('unknown reservation must not authorize a write')):
            with self.assertRaises(ControlError) as error: self.a.resume('example', cid)
        self.assertEqual(error.exception.code, 'STORE_INVALID')
