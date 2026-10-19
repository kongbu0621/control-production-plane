"""Combined scope, delivery, recovery and record-capacity boundaries; synthetic records."""
import copy
import hashlib
import json
import unittest
from unittest.mock import patch

from control_production_plane import ControlError, open_store
from control_production_plane.learning import LearningAdapter
from control_production_plane.knowledge import KnowledgeAdapter
from test_core import command
from test_knowledge import HAS_K
import test_learning as fixtures


class BeforeWrite(Exception):
    pass


@unittest.skipUnless(HAS_K, "optional K and Ledger packages required")
class LearningBoundaryTests(unittest.TestCase):
    setUp = fixtures.LearningTests.setUp
    op = fixtures.LearningTests.op
    candidate = fixtures.LearningTests.candidate
    proposal = fixtures.LearningTests.proposal
    adapter = fixtures.LearningTests.adapter
    material_text = fixtures.LearningTests.material_text

    def hashes(self):
        return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (
            self.root/'production/runs.sqlite', self.root/'knowledge/knowledge.sqlite', self.root/'materials.sqlite')}

    def lost_commit_cancellation_revision_and_reopen(self, failed):
        self.config['scope'] = 'p' * 256
        cid = self.candidate('learning:lost-' + failed)['candidate_id']
        adapter = self.adapter(); original = self.k.execute
        def lost(raw):
            result = original(raw)
            if json.loads(raw)['operation'] == failed:
                raise OSError('Synthetic loss after K commit')
            return result
        with patch.object(adapter, '_open', return_value=self.k), patch.object(self.k, 'execute', side_effect=lost):
            with self.assertRaises(OSError): adapter.deliver(cid, self.proposal())
        saved = copy.deepcopy(self.p.get_candidate(cid)['delivery_record'])
        state = self.p.get_run('example')
        self.p.execute(command('revise', dict(run_id='example', expected_revision=state['revision'], plan=self.spec['plan'])))
        self.p.execute(command('cancel', dict(run_id='example', reason='Synthetic stop after learning write')))
        before = self.p.get_run('example')
        self.p = open_store(self.root/'production')
        result = self.adapter().resume_delivery(cid)
        self.assertEqual(result['delivery'], 'RECEIVED')
        self.assertEqual(self.p.get_candidate(cid)['delivery_record']['requests'], saved['requests'])
        self.assertEqual(self.p.get_run('example'), before)
        self.assertEqual(self.material_text(result)['production']['revision'], state['revision'])
        hashes = self.hashes()
        self.assertEqual(self.adapter().resume_delivery(cid), result)
        self.assertEqual(self.hashes(), hashes)

    def test_256_scope_lost_publication_cancellation_revision_and_reopen(self):
        self.lost_commit_cancellation_revision_and_reopen('publish_material')

    def test_256_scope_lost_proposal_cancellation_revision_and_reopen(self):
        self.lost_commit_cancellation_revision_and_reopen('propose')

    def test_long_unicode_proposal_capacity_precedes_k_writes(self):
        self.config['scope'] = 'p' * 256
        # Probe the real 256 KiB P record boundary; accepted intents are retained,
        # but K writing is intercepted before it starts during threshold search.
        low, high, index, best = 0, 21846, 0, None
        while high-low > 1:
            count = (low+high)//2
            cid = self.candidate('learning:capacity-%02d' % index)['candidate_id']; index += 1
            spec = self.proposal(); spec['content'] = '界' * count
            adapter = self.adapter(); before = self.hashes()
            with patch.object(adapter, '_open', return_value=self.k), patch.object(self.k, 'execute', side_effect=BeforeWrite) as writes:
                try:
                    adapter.deliver(cid, spec)
                except BeforeWrite:
                    self.assertEqual(writes.call_count, 1)
                    low, best = count, cid
                except ControlError as exc:
                    self.assertEqual(exc.code, 'LIMIT')
                    self.assertEqual(writes.call_count, 0)
                    self.assertEqual(self.hashes(), before)
                    high = count
                else:
                    self.fail('Expected pre-write interception or capacity rejection')
        self.assertIsNotNone(best)
        result = self.adapter().resume_delivery(best)
        self.assertEqual(result['delivery'], 'RECEIVED')
        retained = self.p.get_candidate(best)
        self.assertEqual(retained['delivery_record']['proposal']['content'], '界' * low)
        candidate = self.k.get('candidate', result['receipts']['propose']['result']['candidate_id'])['record']
        self.assertEqual(candidate['content'], '界' * low)
        hashes = self.hashes()
        self.assertEqual(LearningAdapter(open_store(self.root/'production'), self.config).resume_delivery(best), result)
        self.assertEqual(self.hashes(), hashes)
        self.assertEqual(high, low + 1)
        self.assertLessEqual(len(('界' * high).encode()), 65536)

    def test_invalid_scope_configuration_never_changes_any_store(self):
        before = self.hashes()
        for scope in (None, True, 256, [], {}, '', '-bad', 'a b', 'a\n', 'a\x00', '界', '\ud800', 'a'*257):
            for cls in (KnowledgeAdapter, LearningAdapter):
                with self.subTest(scope=repr(scope), adapter=cls.__name__):
                    with self.assertRaises(ControlError) as exc: cls(self.p, dict(self.config, scope=scope))
                    self.assertEqual(exc.exception.code, 'INVALID_INPUT')
                    self.assertEqual(self.hashes(), before)


if __name__ == '__main__':
    unittest.main(verbosity=2)
