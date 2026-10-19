"""Widen request scopes without widening task IDs or changing stored requests."""
import hashlib
import json
import subprocess
import sys
import unittest
from unittest.mock import patch

from control_production_plane import ControlError, open_store
from control_production_plane.knowledge import KnowledgeAdapter, encoded, key
from control_production_plane.learning import LearningAdapter
from test_core import command
import test_knowledge as fixtures


@unittest.skipUnless(fixtures.HAS_K, 'optional K and Ledger packages required')
class ScopeTests(unittest.TestCase):
    setUp = fixtures.KnowledgeTests.setUp
    op = fixtures.KnowledgeTests.op
    queue = fixtures.KnowledgeTests.queue

    def hashes(self):
        return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in
                (self.root/'production/runs.sqlite', self.root/'knowledge/knowledge.sqlite', self.root/'materials.sqlite')}

    def test_long_scope_adoption_feedback_recovery_and_long_anchor(self):
        scope = 'p' * 256
        value = dict(schema='operational-knowledge-v1', scope='k' * 256, operation='publish_material',
                     request_id='request:anchor-long', body=dict(material_id='material:anchor-long',
                     source_ref='example:source', task_ref='task:anchor-long', context={},
                     claimed_outcome='UNKNOWN', gaps=[], text='Synthetic long-scope anchor.'))
        anchor = self.k.execute(encoded(value))['receipt']
        config = dict(self.config, scope=scope, anchor={k: anchor[k] for k in ('scope', 'operation', 'request_id', 'fingerprint')})
        adapter = KnowledgeAdapter(self.p, config)
        adapter.adopt('example', self.context)
        self.a = adapter
        fixtures.KnowledgeTests.submit(self)  # Synthetic executor checks the long-scope adoption.
        original = self.k.execute
        def lost(raw):
            result = original(raw)
            if json.loads(raw)['operation'] == 'complete_use':
                raise OSError('Synthetic response lost after K commit')
            return result
        with patch.object(adapter, '_open', return_value=self.k), patch.object(self.k, 'execute', side_effect=lost):
            with self.assertRaises(OSError):
                adapter.feedback('example', 1, 'Synthetic executor observation; no real business was executed.')
        self.p = open_store(self.root/'production')
        state = self.p.get_run('example')
        call_id = key(self.p.identity['store_id'], state, 'complete_use')
        config_file = self.root/'config.json'; config_file.write_bytes(encoded(config)); config_file.chmod(0o600)
        cli = subprocess.run([sys.executable, '-m', 'control_production_plane.cli', '--store', str(self.root/'production'),
            'resume-knowledge', '--run-id', 'example', '--call-id', call_id, '--adapter', str(config_file)],
            capture_output=True, timeout=15)
        self.assertEqual(cli.returncode, 0, cli.stderr)
        final = self.p.get_run('example')
        call = final['knowledge_calls'][call_id]
        self.assertEqual(call['state'], 'COMMITTED')
        self.assertEqual(call['receipt']['scope'], scope)
        feedback = self.k.get('feedback', call['receipt']['result']['feedback_id'])['record']
        self.assertEqual(feedback['scope'], scope)
        self.assertTrue(final['attempt']['submission_recorded'])
        before = self.hashes()
        KnowledgeAdapter(self.p, config).resume('example', call_id)
        self.assertEqual(self.hashes(), before)

    def test_257_scope_rejected_before_intent_and_other_ids_unchanged(self):
        before = self.hashes()
        for cls in (KnowledgeAdapter, LearningAdapter):
            with self.assertRaises(ControlError) as error:
                cls(self.p, dict(self.config, scope='p' * 257))
            self.assertEqual(error.exception.code, 'INVALID_INPUT')
        state = self.p.get_run('example')
        body = dict(run_id='example', attempt_id=state['attempt']['attempt_id'], candidate_id='c' * 129,
                    explanation='Synthetic source.', evidence_refs=['source:one'], artifact_versions=[])
        with self.assertRaises(ControlError) as error:
            self.p.execute(command('learning_candidate', body))
        self.assertEqual(error.exception.code, 'INVALID_INPUT')
        self.assertEqual(self.hashes(), before)
