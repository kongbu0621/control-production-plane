"""Negative controls for the installed combined recovery exercise.

Inject wrong public observations into fresh synthetic fixtures, without editing
installed packages. The exercise must reject them. Retain all fixtures/reports;
this runner's zero exit means its rejection assertions passed, not activation.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid


class RejectionTests(unittest.TestCase):
    def fixture(self):
        return self.exercise.Fixture(self.root / self._testMethodName, self.ledger_commit)

    def test_wrong_failure_event_is_rejected(self):
        h = self.exercise
        fixture = self.fixture()
        execute = h.p_api.ControlStore.execute
        def wrong_event(store, request, **kwargs):
            if request['operation'] == 'preflight_failure':
                request = dict(request, body=dict(request['body'], code='WRONG_CODE'))
            return execute(store, request, **kwargs)
        with patch.object(h.p_api.ControlStore, 'execute', wrong_event):
            with self.assertRaisesRegex(RuntimeError, 'exact failed preflight'):
                h.older_material_snapshot(fixture)

    def test_wrong_recovery_identity_is_rejected(self):
        h = self.exercise
        fixture = self.fixture()
        recover = h.p_api.ControlStore.recover
        def wrong_task(store, run_id):
            value = recover(store, run_id)
            value['run']['attempt']['operation_id'] = str(uuid.uuid4())
            return value
        with patch.object(h.p_api.ControlStore, 'recover', wrong_task):
            with self.assertRaisesRegex(RuntimeError, 'unknown original submission'):
                h.unknown_submission(fixture)

    def test_unconfirmed_receipt_is_rejected(self):
        h = self.exercise
        fixture = self.fixture()
        get_run = h.p_api.ControlStore.get_run
        def pending_receipt(store, run_id):
            state = get_run(store, run_id)
            if store.root.parent.name == 'recovered':
                for call in state.get('knowledge_calls', {}).values():
                    if call['receipt'] is not None:
                        call['state'] = 'PENDING'
            return state
        with patch.object(h.p_api.ControlStore, 'get_run', pending_receipt):
            with self.assertRaisesRegex(RuntimeError, 'receipt was not committed'):
                h.lost_adoption_reply(fixture)

    def test_other_not_ready_is_not_retirement_proof(self):
        h = self.exercise
        fixture = self.fixture()
        clone = h.Fixture.clone
        def wrong_state(source, *args, **kwargs):
            p, adapter, k = clone(source, *args, **kwargs)
            if kwargs.get('name') == 'complete-copy':
                get = k.get
                def not_retired(kind, identity):
                    value = get(kind, identity)
                    if kind == 'version':
                        value['record']['state'] = 'ACTIVE'
                    return value
                k.get = not_retired
            # The adapter still reads the actual retired store and returns
            # NOT_READY. That code alone must not replace the state evidence.
            return p, adapter, k
        with patch.object(h.Fixture, 'clone', wrong_state):
            with self.assertRaisesRegex(RuntimeError, 'original version as RETIRED'):
                h.known_later_retirement(fixture)

    def run_failed_main(self, case, error):
        h = self.exercise
        before = set(self.root.glob('combined-recovery-*'))
        argv = [str(self.script), '--scratch-parent', str(self.root), '--ledger-source-commit', self.ledger_commit]
        with patch.object(sys, 'argv', argv), patch.object(h, case, side_effect=error):
            with self.assertRaises(type(error)):
                h.main()
        created, = set(self.root.glob('combined-recovery-*')) - before
        return created, json.loads((created / 'report.json').read_bytes())

    def test_interruption_records_its_case(self):
        _, report = self.run_failed_main('lost_adoption_reply', KeyboardInterrupt())
        self.assertEqual(report['status'], 'INTERRUPTED')
        self.assertEqual(report['failed_case'], 'lost-adoption-reply')
        self.assertEqual(report['error_type'], 'KeyboardInterrupt')
        self.assertEqual(report['cases'], {})

    def test_failure_retains_completed_case(self):
        created, report = self.run_failed_main('older_material_snapshot', OSError('Synthetic case failure'))
        self.assertEqual(report['status'], 'FAIL')
        self.assertEqual(report['failed_case'], 'older-materials')
        self.assertEqual(report['error_type'], 'OSError')
        self.assertEqual(set(report['cases']), {'lost-adoption-reply'})
        self.assertEqual(report['cases']['lost-adoption-reply'],
                         json.loads((created / 'lost-adoption-reply-result.json').read_bytes()))
        self.assertFalse(report['activation_performed'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scratch-parent', required=True, type=Path)
    parser.add_argument('--ledger-source-commit', required=True)
    args = parser.parse_args()
    script = Path(__file__).with_name('installed_combined_recovery.py')
    spec = importlib.util.spec_from_file_location('combined_recovery_exercise', script)
    h = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(h)
    h.require(sys.flags.isolated and sys.prefix != sys.base_prefix, 'Use an installed venv Python with -I')
    h.require(re.fullmatch('[0-9a-f]{40}', args.ledger_source_commit), 'Expected full Ledger source commit')
    for module in (h.p_api, h.k_api, h.ledger_api):
        h.require(Path(sys.prefix).resolve() in Path(module.__file__).resolve().parents, 'Use independently installed packages')
    root = Path(tempfile.mkdtemp(prefix='combined-recovery-rejections-', dir=args.scratch_parent.resolve(strict=True)))
    print(json.dumps({'negative_control_directory': str(root)}), flush=True)
    RejectionTests.exercise, RejectionTests.root = h, root
    RejectionTests.script, RejectionTests.ledger_commit = script, args.ledger_source_commit
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(RejectionTests))
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == '__main__':
    main()
