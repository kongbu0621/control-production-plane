"""Isolated P/K/Ledger recovery exercise using installed public interfaces.

Only fresh synthetic directories are accepted. This is an acceptance program,
not a backup, migration, production activation or cross-machine fencing API.
All original files and failed attempts remain under the printed work directory.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

import control_production_plane as p_api
from control_production_plane.knowledge import KnowledgeAdapter
import domain_operational_knowledge_core as k_api
import infra_artifact_ledger as ledger_api
from infra_artifact_ledger import recovery


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def encode(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode()


def save(path, value):
    # Never replace an earlier result, including an earlier failure report.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(encode(value) + b'\n')
        stream.flush()
        os.fsync(stream.fileno())


def hashes(directory):
    return {str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(directory.rglob('*')) if path.is_file()}


def command(operation, body):
    return dict(schema='production-plane-v1', request_id=str(uuid.uuid4()), operation=operation, body=body)


def expect_error(code, function):
    try:
        function()
    except (p_api.ControlError, k_api.KnowledgeError) as exc:
        require(exc.code == code, f'Expected {code}, got {exc.code}')
        return exc
    raise RuntimeError(f'Expected {code}, operation unexpectedly succeeded')


class Fixture:
    """One private synthetic data domain; only this harness creates writers."""
    def __init__(self, root, ledger_commit):
        self.root, self.ledger_commit = root, ledger_commit
        root.mkdir(mode=0o700)
        self.ledger_dir = root / 'bound-ledger'
        self.ledger_dir.mkdir(mode=0o700)
        self.snapshots = root / 'snapshots'; self.snapshots.mkdir(mode=0o700)
        self.scratch = root / 'scratch'; self.scratch.mkdir(mode=0o700)
        self.db = self.ledger_dir / 'ledger.sqlite'
        with ledger_api.initialize(self.db) as ledger:
            ledger.execute(encode(dict(profile='bounded-local-v0.1', contract_version='0.1.0',
                operation_kind='create_artifact', idempotency_scope_ref='synthetic:recovery', idempotency_key='anchor',
                body=dict(artifact=dict(artifact_id='artifact:anchor', namespace_ref='synthetic:recovery',
                type_ref=dict(namespace='example', type_id='store-anchor', type_version='1'))))))
        self.early = self.snapshot('anchor-only')
        self.k = k_api.initialize(root / 'original-k', ledger_path=self.db, anchor_artifact_id='artifact:anchor')
        self.kop('publish_material', dict(material_id='material:example', source_ref='synthetic:source',
            task_ref='synthetic:task', context={'project': 'example'}, claimed_outcome='FAILED', gaps=[],
            text='Synthetic recovery material: retain the original task before retrying.'))
        proposal = self.kop('propose', dict(proposal_id='proposal:example', kind='guidance',
            content='Read the original task before retrying.', conditions={'project': 'example'},
            material_ids=['material:example']))['receipt']['result']
        reviewed = self.kop('review', dict(candidate_id=proposal['candidate_id'],
            candidate_sha256=proposal['candidate_sha256'], decision='ACCEPT', reviewer='synthetic:reviewer',
            reason='Synthetic scoped review', evidence_material_ids=['material:example']))['receipt']
        self.version = reviewed['result']['version_id']
        self.config = dict(store_path=str(root / 'original-k'), store_id=reviewed['store_id'],
            scope='synthetic:production', anchor={k: reviewed[k] for k in ('scope', 'operation', 'request_id', 'fingerprint')})
        self.p = p_api.initialize(root / 'original-p')
        self.adapter = KnowledgeAdapter(self.p, self.config)
        self.context = {'project': 'example'}
        self.binding = dict(executor_id='synthetic:executor', authority_id='synthetic:authority',
            principal_id='synthetic:caller', profile_ref='synthetic:profile', tool_schema_digest='1' * 64,
            archive_id='synthetic:archive', producer='synthetic:producer')
        plan = dict(source_ref='synthetic:source', source_commit='2' * 40, relative_path='example.txt',
            expected_sha256='3' * 64, content='Synthetic replacement.\n', validation_ref='synthetic:validation',
            knowledge=dict(required=True, status='SELECTED', reason='Inspect the original result before retrying',
                items=[self.adapter.selection(self.version, self.context)['item']]))
        self.p.execute(command('create', dict(run_id='example', goal=dict(text='Synthetic recovery exercise',
            acceptance=['Retain original identities and uncertain outcomes']), binding=self.binding, plan=plan,
            budget=dict(max_attempts=2, max_actions=20, max_queries=20, max_cancellations=20,
                        deadline_utc=int(time.time()) + 3600, max_response_bytes=32768))))

    def kop(self, operation, body):
        return self.k.execute(encode(dict(schema='operational-knowledge-v1', scope='synthetic:reviewer',
            operation=operation, request_id='request:' + operation, body=body)))

    def snapshot(self, name):
        result = recovery.create(db=self.db, output_root=self.snapshots, snapshot_id=uuid.uuid4().hex,
                                 source_commit=self.ledger_commit)
        save(self.root / (name + '-snapshot.json'), result)
        return result['data']

    def restore_materials(self, snapshot, retained_name):
        # Every connection/writer created by this fixture has stopped. K binds
        # the absolute Ledger path: retain the whole original directory, then
        # restore to a NEW directory at that same synthetic binding. Never edit
        # K's private identity or database to redirect the reference.
        self.ledger_dir.rename(self.root / retained_name)
        result = recovery.restore(snapshot=snapshot['snapshot_path'], target_dir=self.ledger_dir,
            expected_manifest_sha256=snapshot['manifest_sha256'], scratch_parent=self.scratch)
        save(self.root / (retained_name + '-restore.json'), result)
        require(result['data']['database_sha256'] == snapshot['database_sha256'], 'Restored Ledger digest changed')

    def clone(self, name='recovered', k_source=None):
        # Cold copies are specific to these closed synthetic stores, not a
        # claim that P/K provide online or arbitrary crash-consistent backups.
        destination = self.root / name; destination.mkdir(mode=0o700)
        shutil.copytree(self.root / 'original-p', destination / 'p')
        shutil.copytree(k_source or self.root / 'original-k', destination / 'k')
        store = p_api.open_store(destination / 'p')
        adapter = KnowledgeAdapter(store, dict(self.config, store_path=str(destination / 'k')))
        return store, adapter, k_api.open_store(destination / 'k')

    def queue(self, kind):
        aid = str(uuid.uuid4())
        self.p.execute(command('queue', dict(run_id='example', attempt_id=self.p.get_run('example')['attempt']['attempt_id'],
                                            action_id=aid, kind=kind)))
        return aid

    def prepare(self):
        self.adapter.adopt('example', self.context)
        def ready(payload):
            request = dict(payload, expires_at=int(time.time()) + 600, expected={}, schema_version='synthetic-recovery-v1')
            request['request_digest'] = hashlib.sha256(encode(request)).hexdigest()
            return dict(response=dict(state='READY', request=request), call_closed=True)
        self.p.perform(self.queue('prepare'), self.binding, ready)
        return self.queue('submit')


def lost_adoption_reply(f):
    config = f.root / 'adapter.json'; save(config, f.config)
    # A real child exits after K commits and before P acknowledges that receipt.
    code = '''
import json, os, sys
from control_production_plane import open_store
from control_production_plane.knowledge import KnowledgeAdapter
p = open_store(sys.argv[1]); execute = p.execute
def stop(request, **kwargs):
    if request['operation'] == 'knowledge_ack': os._exit(23)
    return execute(request, **kwargs)
p.execute = stop
KnowledgeAdapter(p, json.load(open(sys.argv[2]))).adopt('example', {'project':'example'})
'''
    child = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(f.p.root), str(config)],
                           capture_output=True, timeout=30)
    require(child.returncode == 23 and not child.stdout and not child.stderr, 'Child did not stop at the acknowledgement gap')
    state = f.p.get_run('example'); cid = state['attempt']['knowledge_use']
    call = state['knowledge_calls'][cid]
    require(call['state'] == 'PENDING', 'P did not retain the original pending call')
    req = call['request']
    receipt = f.k.get_operation(req['scope'], req['operation'], req['request_id'])['receipt']
    save(f.root / 'known-adoption-receipt.json', receipt)
    snapshot = f.snapshot('with-materials')
    original_p, original_k = hashes(f.root / 'original-p'), hashes(f.root / 'original-k')
    p, adapter, _ = f.clone()
    f.restore_materials(snapshot, 'retained-original-ledger')
    k_before, ledger_before = hashes(f.root / 'recovered/k'), hashes(f.ledger_dir)
    adapter.resume('example', cid)
    restored = p.get_run('example')
    require(restored['knowledge_calls'][cid]['receipt'] == receipt, 'A different K receipt was adopted')
    require(restored['attempt']['operation_id'] == state['attempt']['operation_id']
            and restored['counters'] == state['counters'], 'Recovery changed execution identity or budget')
    p_after = hashes(p.root)
    adapter.resume('example', cid)
    require(hashes(p.root) == p_after, 'Repeated receipt recovery wrote new P records')
    require(hashes(f.root / 'recovered/k') == k_before and hashes(f.ledger_dir) == ledger_before,
            'Receipt recovery wrote K or Ledger')
    require(hashes(f.root / 'original-p') == original_p and hashes(f.root / 'original-k') == original_k,
            'Recovery changed original P/K files')
    return dict(result='ORIGINAL_RECEIPT_RECOVERED', child_exit=23, business_submissions=0,
                k_files_unchanged=True, ledger_files_unchanged=True, repeated_recovery_unchanged=True)


def older_material_snapshot(f):
    aid = f.prepare(); current = f.snapshot('with-materials')
    p, adapter, k = f.clone()
    f.restore_materials(f.early, 'retained-complete-ledger')
    before = hashes(p.root)
    material = k.get('version', f.version)['materials']
    require(any(item['status'] != 'VERIFIED' for item in material), 'Old Ledger unexpectedly contains later material')
    calls = []
    invoke = lambda _: calls.append('business') or dict(response={}, call_closed=True)
    expect_error('KNOWLEDGE_UNAVAILABLE', lambda: p.perform(aid, f.binding, invoke, knowledge=adapter))
    require(not calls and p.get_action(aid)['state'] == 'QUEUED', 'Missing material allowed a submission')
    require(p.get_run('example')['counters']['attempts'] == 0, 'Missing material consumed business budget')
    require(hashes(p.root) != before, 'Expected the failed preflight event to be preserved')
    # Restore correct materials, then READ the same task/version again. Do not
    # use an isolated acceptance run to authorize live execution of this copy.
    f.restore_materials(current, 'retained-older-ledger')
    observation = adapter.check_execution(p.get_run('example'))
    require(all(item['status'] == 'VERIFIED' for item in observation['materials']), 'Correct material restoration did not recover reads')
    require(not calls, 'The read check unexpectedly submitted business')
    return dict(result='MISSING_MATERIAL_BLOCKED', business_submissions=0, corrected_materials_readable=True)


def known_later_retirement(f):
    f.prepare()
    old_k = f.root / 'older-k'; shutil.copytree(f.root / 'original-k', old_k)
    retired = f.kop('retire', dict(expected_version_id=f.version, reviewer='synthetic:reviewer',
        reason='Synthetic later retirement', evidence_material_ids=['material:example']))['receipt']
    # An independently retained LATER decision, not a checksum that comes only
    # from the older snapshot. In production its provenance/freshness must be
    # established before copying or choosing the restored active instance.
    checkpoint = f.root / 'known-later-retirement.json'; save(checkpoint, retired)
    checkpoint_sha = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
    current = f.snapshot('with-materials')
    p, adapter, k = f.clone(k_source=old_k)
    f.restore_materials(current, 'retained-original-ledger')
    before = hashes(f.root / 'recovered')
    stale = adapter.check_execution(p.get_run('example'))
    require(stale['state'] == 'ACTIVE', 'Fixture did not model a self-consistent stale K snapshot')
    require(hashlib.sha256(checkpoint.read_bytes()).hexdigest() == checkpoint_sha, 'Known later decision changed')
    expected = json.loads(checkpoint.read_bytes())
    expect_error('NOT_FOUND', lambda: k.get_operation(expected['scope'], expected['operation'], expected['request_id']))
    require(hashes(f.root / 'recovered') == before, 'Historical inspection modified the restored files')
    # Never call perform on the stale copy. It lacks the known later decision.
    # A separately retained complete copy retains RETIRED and blocks submit.
    correct_p, correct_adapter, correct_k = f.clone(name='complete-copy')
    observed = correct_k.get_operation(expected['scope'], expected['operation'], expected['request_id'])
    require(observed['receipt'] == expected and observed['observation']['record_status'] == 'AVAILABLE',
            'Complete copy did not retain the original retirement')
    expect_error('NOT_READY', lambda: correct_adapter.check_execution(correct_p.get_run('example')))
    return dict(result='STALE_K_DETECTED_BY_KNOWN_LATER_DECISION', stale_check_state=stale['state'],
                restored_copy_use='HISTORICAL_READ_ONLY', complete_copy_state='RETIRED', business_submissions=0)


def unknown_submission(f):
    aid = f.prepare(); effects = []
    def lost(_):
        effects.append('synthetic-effect')
        raise OSError('Synthetic response lost after effect')
    expect_error('CALL_UNKNOWN', lambda: f.p.perform(aid, f.binding, lost, knowledge=f.adapter))
    snapshot = f.snapshot('with-materials')
    p, adapter, _ = f.clone()
    f.restore_materials(snapshot, 'retained-original-ledger')
    before = hashes(p.root)
    require(p.recover('example')['next_step'] == 'QUERY_ORIGINAL_TASK', 'Recovery advice lost the unknown original submission')
    expect_error('PENDING', lambda: p.perform(aid, f.binding, lost, knowledge=adapter))
    require(len(effects) == 1 and hashes(p.root) == before, 'Recovery replayed or rewrote unknown business')
    return dict(result='UNKNOWN_SUBMISSION_RETAINED', synthetic_effects=1, recovery_effects=0)


def original_writer(f):
    # Deliberately demonstrate that locks in two cold copies are independent.
    # Copy BEFORE starting the writer: never take an online P/K file copy.
    p, _, _ = f.clone()
    code = '''
import sys
from control_production_plane import open_store
with open_store(sys.argv[1]).executor():
    print('held', flush=True)
    sys.stdin.buffer.read(1)
'''
    child = subprocess.Popen([sys.executable, '-I', '-B', '-c', code, str(f.p.root)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            require(bool(selector.select(10)), 'Original writer did not become ready')
        require(child.stdout.readline() == b'held\n' and child.poll() is None, 'Original writer did not hold its lease')
        def original_lock():
            with f.p.executor():
                raise RuntimeError('Original writer lock was bypassed')
        expect_error('BUSY', original_lock)
        with p.executor():
            require(child.poll() is None, 'Fixture writer exited unexpectedly')
            # Getting the copy's lock does NOT establish source writer exit.
            blocked = child.poll() is None
        require(blocked, 'A live source writer was treated as isolated')
        out, err = child.communicate(b'x', timeout=10)
        require(child.returncode == 0 and not out and not err, 'Original writer exit/streams not confirmed')
    finally:
        if child.poll() is None:
            child.kill(); child.communicate(timeout=10)
    require(p.get_run('example')['counters']['attempts'] == 0, 'Writer probe submitted a task')
    return dict(result='COPY_LOCK_IS_NOT_WRITER_FENCING', live_original_blocked=True,
                original_child_exit=0, business_submissions=0, restored_copy_use='HISTORICAL_READ_ONLY')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scratch-parent', required=True, type=Path)
    parser.add_argument('--ledger-source-commit', required=True)
    args = parser.parse_args()
    require(sys.flags.isolated and sys.prefix != sys.base_prefix, 'Use an installed venv Python with -I')
    require(re.fullmatch('[0-9a-f]{40}', args.ledger_source_commit), 'Expected full Ledger source commit')
    for module in (p_api, k_api, ledger_api):
        require(Path(sys.prefix).resolve() in Path(module.__file__).resolve().parents, 'Use independently installed packages')
    root = Path(tempfile.mkdtemp(prefix='combined-recovery-', dir=args.scratch_parent.resolve(strict=True)))
    print(json.dumps({'work_directory': str(root)}), flush=True)
    report = dict(schema='synthetic-combined-recovery-v1', status='RUNNING', cases={},
                  packages={name: importlib.metadata.version(name) for name in
                            ('control-production-plane', 'domain-operational-knowledge-core', 'infra-artifact-ledger')},
                  implementation_sha256={module.__name__: {
                      str(path.relative_to(Path(module.__file__).parent)): hashlib.sha256(path.read_bytes()).hexdigest()
                      for path in sorted(Path(module.__file__).parent.rglob('*.py'))}
                      for module in (p_api, k_api, ledger_api)},
                  exercise_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  ledger_source_commit=args.ledger_source_commit, activation_performed=False)
    try:
        for name, case in (('lost-adoption-reply', lost_adoption_reply), ('older-materials', older_material_snapshot),
                           ('later-retirement', known_later_retirement), ('unknown-submission', unknown_submission),
                           ('original-writer', original_writer)):
            result = case(Fixture(root / name, args.ledger_source_commit))
            report['cases'][name] = result
            save(root / (name + '-result.json'), result)
            print(json.dumps({'case': name, 'status': 'PASS', **result}), flush=True)
        report['status'] = 'PASS'
    except Exception as exc:
        report.update(status='FAIL', error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        save(root / 'report.json', report)


if __name__ == '__main__':
    main()
