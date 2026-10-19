"""Actual P CLI -> K/Ledger reads -> Local Hand CLI, using a synthetic socket."""
import json
import os
import subprocess
import sys
import unittest

import test_adapters as executor_fixtures
import test_knowledge as knowledge_fixtures
from control_production_plane import ControlError
from control_production_plane.knowledge import KnowledgeAdapter
from test_core import command


@unittest.skipUnless(executor_fixtures.HAS_DEPENDENCIES and knowledge_fixtures.HAS_K,
                     'Verified K/Ledger/Local Hand packages required')
class ExecutionKnowledgeCliTests(unittest.TestCase):
    def setUp(self):
        k = knowledge_fixtures.KnowledgeTests()
        self.addCleanup(k.doCleanups)
        k.setUp()
        h = executor_fixtures.AdapterTests()
        self.addCleanup(h.doCleanups)
        h.setUp()
        self.k, self.h = k, h
        h.spec['plan']['knowledge'] = k.spec['plan']['knowledge']
        h.store.execute(command('revise', dict(run_id='example', expected_revision=1, plan=h.spec['plan'])))
        self.a = KnowledgeAdapter(h.store, k.config)
        self.a.adopt('example', k.context)
        self.config_path = h.root/'hand.json'
        self.knowledge_path = h.root/'knowledge.json'
        for path, value in ((self.config_path, h.config), (self.knowledge_path, k.config)):
            path.write_text(json.dumps(value)); os.chmod(path, 0o600)
        h.perform('prepare')
        self.aid = h.queue('submit')

    def cli(self, action, *, with_k=True):
        args = [sys.executable, '-B', '-m', 'control_production_plane.cli', '--store', str(self.h.store.root),
                'execute-action', '--action-id', action, '--adapter', str(self.config_path),
                '--data', str(self.h.root/'executor')]
        if with_k: args += ['--knowledge-adapter', str(self.knowledge_path)]
        return subprocess.run(args, capture_output=True, timeout=15)

    def retire(self):
        self.k.op('retire', dict(expected_version_id=self.k.version, reviewer='example:reviewer',
                  reason='Block new submission', evidence_material_ids=['material:example']))

    def submissions(self):
        return [r for r in self.h.requests if r['tool'] == 'lh_job_submit']

    def test_cli_requires_config_then_submits_once_and_queries_without_k(self):
        denied = self.cli(self.aid, with_k=False)
        self.assertEqual(denied.returncode, 2, denied.stderr)
        self.assertEqual(self.submissions(), [])
        passed = self.cli(self.aid)
        self.assertEqual(passed.returncode, 0, passed.stderr)
        self.assertEqual(len(self.submissions()), 1)
        self.assertEqual(self.h.store.get_action(self.aid)['knowledge_check']['item']['version_id'], self.k.version)
        self.retire()
        self.knowledge_path.unlink()
        # Even an explicitly supplied unavailable K config cannot block query.
        queried = self.cli(self.h.queue('query'))
        self.assertEqual(queried.returncode, 0, queried.stderr)
        self.assertEqual(len(self.submissions()), 1)

    def test_cli_retirement_after_adoption_leaves_no_submit_and_no_attempt_cost(self):
        self.retire()
        blocked = self.cli(self.aid)
        self.assertEqual(blocked.returncode, 2, blocked.stderr)
        self.assertEqual(self.submissions(), [])
        self.assertEqual(self.h.store.get_action(self.aid)['state'], 'QUEUED')
        self.assertEqual(self.h.store.get_run('example')['counters']['attempts'], 0)

    def test_local_hand_adapter_checks_after_real_source_preflight(self):
        original = self.h.handler
        def retire_during_read(tool, arguments):
            result = original(tool, arguments)
            if tool == 'lh_workspace_read': self.retire()
            return result
        self.h.handler = retire_during_read
        with self.assertRaises(ControlError): self.h.adapter.perform(self.aid, knowledge=self.a)
        self.assertEqual(self.submissions(), [])
