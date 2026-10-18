"""Real local CLI/SQLite/Ledger integration with a synthetic Unix service."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from control_production_plane import initialize, ControlError, CallNotStarted
from control_production_plane import model
from control_production_plane.calls import CliCalls, response_json, retained, publish
from control_production_plane.local_hand import LocalHandAdapter
from control_production_plane.artifacts import ArtifactAdapter, verify_readback
from test_core import sample, command

HAS_DEPENDENCIES = all(importlib.util.find_spec(name) for name in ('local_hand_jobs', 'infra_artifact_ledger'))


class CallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.calls = CliCalls(self.root/'calls', timeout=1, max_bytes=1024)

    def test_finite_response_timestamps_and_strict_decoding(self):
        self.assertEqual(response_json(b'{"observed_at":1.5}'), {'observed_at':1.5})
        for raw in (b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":1e999}', b'[]', b'{"a":9007199254740992}'):
            with self.subTest(raw=raw), self.assertRaises(ControlError):
                response_json(raw)

    def test_both_streams_exit_and_no_automatic_redelivery(self):
        argv = [sys.executable, '-c', 'import sys;print("ok");print("error",file=sys.stderr)']
        result = self.calls.run(argv, call_id='one')
        self.assertTrue(result['call_closed'])
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(self.calls.read('one'), b'ok\n')
        self.assertEqual(self.calls.read('one','stderr'), b'error\n')
        with self.assertRaises(ControlError) as raised:
            self.calls.run(argv,call_id='one')
        self.assertEqual(raised.exception.code,'PENDING')

    def test_start_failure_timeout_and_output_limit_are_distinct(self):
        with self.assertRaises(CallNotStarted):
            self.calls.run(['/nonexistent/production-test-program'],call_id='absent')
        self.assertTrue((self.calls.root/'absent/not-started.json').exists())
        timeout = self.calls.run([sys.executable,'-c','import time;time.sleep(20)'])
        self.assertEqual(timeout['reason'],'TIMEOUT')
        self.assertTrue(timeout['call_closed'])
        limit = self.calls.run([sys.executable,'-c','import os;os.write(1,b"x"*4096)'])
        self.assertEqual(limit['reason'],'OUTPUT_LIMIT')
        self.assertEqual(len(self.calls.read(limit['call_id'])),1024)

    def test_retained_output_rewrite_is_rejected(self):
        result = self.calls.run([sys.executable,'-c','print("one")'])
        (self.calls.root/result['call_id']/'stdout').write_bytes(b'two\n')
        with self.assertRaises(ControlError):
            self.calls.read(result['call_id'])

    def test_missing_call_read_does_not_create_an_unresolved_call(self):
        with self.assertRaises((OSError, ControlError)):
            self.calls.read('never-started')
        self.assertFalse((self.calls.root/'never-started').exists())
        with self.calls.serial():
            result = self.calls.run([sys.executable, '-c', 'print("usable")'])
        self.assertEqual(self.calls.read(result['call_id']), b'usable\n')

    def test_retained_fifo_is_rejected_without_waiting_for_a_writer(self):
        path = self.root/'invalid-retained-file'
        os.mkfifo(path, 0o600)
        code = '''import sys
from control_production_plane.calls import retained
from control_production_plane import ControlError
try:
    retained(sys.argv[1])
except ControlError as error:
    assert error.code == "STORE_INVALID", error.code
else:
    raise AssertionError("FIFO accepted")
'''
        result = subprocess.run([sys.executable, '-c', code, str(path)],
                                capture_output=True, timeout=2)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_orphan_child_blocks_archive_until_actual_exit(self):
        # Real detached child identity; releasing the owner lock proves nothing.
        child = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'])
        try:
            directory = self.calls.root/'orphan'; directory.mkdir(mode=0o700)
            ticks = Path(f'/proc/{child.pid}/stat').read_text().rsplit(')',1)[1].split()[19]
            publish(directory/'process.json', model.canonical(dict(pid=child.pid,start_ticks=ticks,
                boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip())))
            with self.assertRaises(ControlError) as error:
                with self.calls.serial(): pass
            self.assertEqual(error.exception.code, 'PENDING')
            child.kill(); child.wait(timeout=2)
            with self.calls.serial(): pass
            proof = response_json(retained(directory/'exit-observed.json'))
            self.assertTrue(proof['child_stopped'])
            self.assertFalse(proof['output_complete'])
        finally:
            if child.poll() is None: child.kill(); child.wait(timeout=2)



@unittest.skipUnless(HAS_DEPENDENCIES, 'Install verified Local Hand and Ledger wheels for adapter integration')
class AdapterTests(unittest.TestCase):
    def setUp(self):
        from local_hand_jobs.contract import TOOL_SCHEMA_DIGEST
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = initialize(self.root/'control')
        self.spec = sample()
        self.original = b"print('before')\n"
        self.spec['binding'].update(profile_ref='demo-profile',tool_schema_digest=TOOL_SCHEMA_DIGEST)
        self.spec['plan'].update(source_ref='demo-source',validation_ref='core',expected_sha256=hashlib.sha256(self.original).hexdigest())
        self.store.execute(command('create',self.spec))
        self.socket = socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        self.socket.bind(str(self.root/'service.sock'));self.socket.listen(16);self.socket.settimeout(.1)
        self.stop = threading.Event();self.threads=[];self.requests=[]
        self.config = dict(schema='production-plane-local-hand-v1',binding=self.spec['binding'],
            command=[sys.executable,'-I','-B','-m','local_hand_jobs.cli'],socket=str(self.root/'service.sock'),
            server_uid=os.geteuid(),policy_digest='4'*64,catalog_version='5'*64)
        self.caps = dict(schema_version='lh-job-v1',authority_id=self.spec['binding']['authority_id'],
            tool_schema_digest=TOOL_SCHEMA_DIGEST,policy_digest='4'*64,catalog_version='5'*64,
            profiles=[dict(profile_ref='demo-profile',allowed_kinds=['workspace.edit'],source_refs=['demo-source'],validation_refs=['core'])],next_cursor=None)
        self.handler = self.respond
        self.thread=threading.Thread(target=self.serve,daemon=True);self.thread.start()
        self.addCleanup(self.shutdown)
        self.adapter = LocalHandAdapter(self.store,self.config,self.root/'executor')

    def shutdown(self):
        self.stop.set();self.thread.join(timeout=2);self.socket.close()
        for thread in self.threads: thread.join(timeout=3)

    def serve(self):
        while not self.stop.is_set():
            try: conn,_ = self.socket.accept()
            except socket.timeout: continue
            thread=threading.Thread(target=self.connection,args=(conn,),daemon=True)
            self.threads.append(thread);thread.start()

    def connection(self,conn):
        with conn:
            conn.settimeout(3)
            header=conn.recv(4)
            if not header:return  # Credential probe.
            if len(header)!=4:return
            size=struct.unpack('!I',header)[0];raw=b''
            while len(raw)<size:
                block=conn.recv(size-len(raw))
                if not block:return
                raw+=block
            request=json.loads(raw);self.requests.append(request)
            result=self.handler(request['tool'],request['arguments'])
            if result is None:return
            value=result if 'error' in result else {'result':result}
            raw=json.dumps(value,allow_nan=False).encode()
            try:conn.sendall(struct.pack('!I',len(raw))+raw)
            except BrokenPipeError:pass

    def respond(self,tool,args):
        from local_hand_jobs.contract import request_digest
        if tool=='lh_capabilities':return self.caps
        if tool=='lh_workspace_read':
            return dict(source_ref='demo-source',source_commit=self.spec['plan']['source_commit'],relative_path='greeting.py',
                        sha256=hashlib.sha256(self.original).hexdigest(),bytes=len(self.original),content=self.original.decode())
        if tool=='lh_job_prepare':
            request=dict(schema_version='lh-job-v1',operation_id=args['operation_id'],kind='workspace.edit',profile_ref=args['profile_ref'],inputs=args['inputs'],
                expires_at=int(time.time())+600,expected=dict(node_id='demo-node',install_uuid=str(uuid.uuid4()),deployment_epoch=1,
                profile_digest='a'*64,policy_digest='b'*64,registry_digest='c'*64))
            request['request_digest']=request_digest(request)
            self.request=request
            return dict(operation_id=args['operation_id'],state='READY',request=request)
        if tool in ('lh_job_submit','lh_job_status','lh_job_cancel'):
            return getattr(self,'status',dict(operation_id=args['operation_id'],request_digest=self.request['request_digest'],
                lifecycle='RUNNING',outcome='PENDING',phase='BUSINESS',evidence='STAGING',observed_at=1.25,exit_proof=None))
        if tool=='lh_evidence_manifest':return self.evidence_store.manifest(args['operation_id'],principal='reader')
        if tool=='lh_evidence_read_chunk':return self.evidence_store.read_chunk(principal='reader',**args)
        raise AssertionError(tool)

    def queue(self,kind,**extra):
        action_id=str(uuid.uuid4());state=self.store.get_run('example')
        self.store.execute(command('queue',dict(run_id='example',attempt_id=state['attempt']['attempt_id'],action_id=action_id,kind=kind,**extra)))
        return action_id

    def perform(self,kind,**extra):
        action=self.queue(kind,**extra);self.adapter.perform(action);return action

    def test_real_cli_preparation_submission_query_and_cancel_binding(self):
        self.perform('prepare');self.perform('submit');self.perform('query')
        self.store.execute(command('cancel',dict(run_id='example',reason='Synthetic cancellation')))
        self.perform('cancel')
        cancels=[r['arguments'] for r in self.requests if r['tool']=='lh_job_cancel']
        self.assertEqual(cancels,[dict(operation_id=self.request['operation_id'],expected_request_digest=self.request['request_digest'],target={'kind':'job'})])
        self.assertFalse(self.store.get_run('example')['attempt']['observation']['exited'])
        self.assertEqual(self.store.get_run('example')['attempt']['observation']['outcome'],'UNKNOWN')

    def test_capability_and_source_drift_block_before_dispatch(self):
        action=self.queue('prepare')
        for target,key,value in ((self.caps,'policy_digest','0'*64),(self.spec['plan'],'source_commit','8'*40)):
            old=target[key];target[key]=value
            with self.assertRaises(ControlError):self.adapter.perform(action)
            target[key]=old
            self.assertEqual(self.store.get_action(action)['state'],'QUEUED')
            self.assertEqual(self.store.get_run('example')['counters']['actions'],0)
        self.assertFalse(any(r['tool']=='lh_job_prepare' for r in self.requests))

    def test_failed_preparation_retries_with_same_identity_and_explicit_flag(self):
        real=self.respond
        def fail(tool,args):
            if tool=='lh_job_prepare' and not args.get('retry_failed'):
                return dict(operation_id=args['operation_id'],state='FAILED',retryable=True)
            return real(tool,args)
        self.handler=fail
        first=self.perform('prepare');second=self.perform('prepare',retry_failed=True)
        a,b=[self.store.get_action(x)['payload'] for x in (first,second)]
        self.assertEqual(a,{k:v for k,v in b.items() if k!='retry_failed'})
        self.perform('submit')
        self.assertEqual(self.store.get_run('example')['counters']['attempts'],1)

    def test_lost_response_keeps_original_task_and_query_remains_available(self):
        self.perform('prepare')
        real=self.respond
        self.handler=lambda tool,args: None if tool=='lh_job_submit' else real(tool,args)
        action=self.perform('submit')  # CLI reports IO_UNCERTAIN with confirmed local EOF.
        self.assertEqual(self.store.get_action(action)['response']['remote_error'],'IO_UNCERTAIN')
        self.perform('query')
        with self.assertRaises(ControlError):self.adapter.perform(action)
        self.assertEqual(len([r for r in self.requests if r['tool']=='lh_job_submit']),1)

    def test_response_recording_failure_reconciles_without_cli_reinvoke(self):
        action=self.queue('prepare');real=self.store.execute
        def fail(request,**kwargs):
            if request['operation']=='respond':raise ControlError('STORAGE_ERROR','synthetic write failure')
            return real(request,**kwargs)
        with patch.object(self.store,'execute',side_effect=fail), self.assertRaises(ControlError):self.adapter.perform(action)
        count=len(self.requests)
        self.adapter.reconcile(action)
        self.adapter.reconcile(action)
        self.assertEqual(len(self.requests),count)
        self.assertIsNotNone(self.store.get_run('example')['attempt']['prepared_request'])

    def sealed(self, *, extra_change=False):
        from local_hand_jobs.evidence import EvidenceStore,FrozenSnapshot,QuiescenceProof
        self.perform('prepare');self.perform('submit')
        state=self.store.get_run('example');plan=state['plan'];operation=self.request['operation_id']
        workspace=dict(schema_version='lh-workspace-edit-v1',scope='TASK_OWNED_COPY',original_modified=False,
            read_back_verified=True,validation_succeeded=True,edited_file_unchanged_by_validation=True,
            source_ref=plan['source_ref'],source_commit=plan['source_commit'],relative_path=plan['relative_path'],validation_ref=plan['validation_ref'],
            before_sha256=plan['expected_sha256'],after_sha256=hashlib.sha256(plan['content'].encode()).hexdigest(),
            git_status=' M greeting.py\n'+(' M unexpected.py\n' if extra_change else ''))
        source=self.root/'sealed-source';source.mkdir(mode=0o700);(source/'business').mkdir(mode=0o700)
        files={'workspace-before.txt':self.original,'workspace-after.txt':plan['content'].encode(),
               'workspace-result.json':model.canonical(workspace),'workspace.patch':b'Synthetic patch\n','workspace-final.patch':b'Synthetic patch\n'}
        for name,raw in files.items():p=source/'business'/name;p.write_bytes(raw);p.chmod(0o600)
        registered={}
        snapshot=FrozenSnapshot(operation,7,source,tuple('business/'+name for name in files),
            QuiescenceProof('synthetic-owned-execution',7,True,True,True,True),{'request_digest':self.request['request_digest']},
            broker_events=({'seq':7,'namespace':'job','id':operation,'kind':'EXECUTION_EXIT',
                            'observed_at':1,'data_json':'{"outcome":"SUCCEEDED"}'},))
        self.evidence_store=EvidenceStore(self.root/'sealed-store',snapshot_provider=lambda _:snapshot,
            register_seal=lambda r:registered.update({r['seal_id']:r['seal_sha256']}),
            is_registered=lambda name,digest:registered.get(name)==digest,authorize=lambda principal,op:None)
        self.evidence_store.seal(operation)
        descriptor=self.evidence_store.manifest(operation,principal='reader')['artifacts'][0]
        self.status=dict(operation_id=operation,request_digest=self.request['request_digest'],lifecycle='TERMINAL',phase='EXITED',outcome='SUCCEEDED',evidence='SEALED',observed_at=1.25,
            exit_proof=dict(future_start_blocked=True,tree_exited=True,writers_stopped=True,collectors_stopped=True,effects_checked=True),
            seal_refs=[{k:descriptor[k] for k in ('seal_id','seal_sha256')}])
        action=self.perform('query')
        evidence=self.adapter.download(action,self.root/'download')
        archive_command = getattr(self, 'archive_command', [sys.executable,'-I','-B','-m','local_hand_jobs.artifact_archive'])
        self.artifacts=ArtifactAdapter(self.store,archive_command,self.root/'archive',self.root/'artifacts')
        archived=self.artifacts.archive('example',evidence)
        return evidence,archived

    def test_actual_ledger_archive_exact_readback_and_explicit_acceptance(self):
        evidence,archived=self.sealed()
        replay=self.artifacts.archive('example',evidence)
        self.assertEqual(replay,archived)
        read=self.artifacts.read('example',artifact_id=archived['artifact_id'],version_id=archived['version_id'],output=self.root/'readback')
        candidate=self.store.get_run('example')['attempt']['candidate']
        self.assertEqual(candidate['version_id'],archived['version_id'])
        self.assertNotIn('decision',self.store.get_run('example')['attempt'])
        decision=command('decide',dict(run_id='example',attempt_id=candidate['attempt_id'],candidate_digest=model.digest(candidate),
            actor='demo:reviewer',decision='ACCEPT',reason='Synthetic acceptance after reading exact bytes',
            checks=[dict(condition=self.spec['goal']['acceptance'][0],passed=True,evidence_ref='demo:readback')]))
        self.store.execute(decision);self.assertTrue(self.store.execute(decision)['replayed'])
        self.artifacts.read('example',artifact_id=archived['artifact_id'],version_id=archived['version_id'],output=self.root/'readback-again')
        self.assertEqual(self.store.get_run('example')['attempt']['candidate'],candidate)
        self.assertEqual(self.store.get_run('example')['attempt']['decision'],decision['body'])
        with self.assertRaises(ControlError):
            self.artifacts.read('example',artifact_id=archived['artifact_id'],version_id=archived['version_id'],output=self.root/'readback')
        changed=copy.deepcopy(read);changed['files']['evidence.zip']['sha256']='0'*64
        with self.assertRaises(ControlError):verify_readback(self.store.get_run('example'),changed,self.root/'readback',evidence)
        wrong=copy.deepcopy(self.store.get_run('example'));wrong['plan']['source_commit']='9'*40
        with self.assertRaises(ControlError):verify_readback(wrong,read,self.root/'readback',evidence)
        bad=copy.deepcopy(decision);bad['request_id']=str(uuid.uuid4());bad['body']['checks'][0]['passed']=False
        with self.assertRaises(ControlError):self.store.execute(bad)

    def test_additional_validation_changes_cannot_become_candidate(self):
        _,archived=self.sealed(extra_change=True)
        with self.assertRaises(ControlError) as raised:
            self.artifacts.read('example',artifact_id=archived['artifact_id'],version_id=archived['version_id'],output=self.root/'rejected-readback')
        self.assertEqual(raised.exception.code,'CONFLICT')
        self.assertEqual(raised.exception.commit_state,'unknown')
        self.assertNotIn('candidate',self.store.get_run('example')['attempt'])
        self.assertTrue((self.root/'rejected-readback/evidence.zip').exists())

    def test_cancel_recording_and_readonly_query_during_pending_submit(self):
        self.perform('prepare')
        entered, release = threading.Event(), threading.Event()
        original = self.respond
        def waiting(tool,args):
            if tool == 'lh_job_submit':
                entered.set(); release.wait(1.5)
            return original(tool,args)
        self.handler = waiting
        action = self.queue('submit'); errors = []
        def perform():
            try: self.adapter.perform(action)
            except Exception as exc: errors.append(exc)
        worker = threading.Thread(target=perform);worker.start()
        try:
            self.assertTrue(entered.wait(3))
            self.store.execute(command('cancel',dict(run_id='example',reason='Cancel while CLI awaits response')))
            self.perform('query')
            self.perform('cancel')
            with self.assertRaises(ControlError): self.queue('submit')
        finally:
            release.set();worker.join(4)
        self.assertFalse(errors)
        self.assertEqual(sum(r['tool']=='lh_job_submit' for r in self.requests),1)
        self.assertTrue(self.store.get_run('example')['cancel'])

    def test_controller_kill_retains_submission_and_allows_original_query(self):
        self.perform('prepare')
        action = self.queue('submit'); entered = threading.Event(); original = self.respond
        def waiting(tool,args):
            if tool == 'lh_job_submit': entered.set(); time.sleep(.5)
            return original(tool,args)
        self.handler = waiting
        config=self.root/'config.json';config.write_bytes(model.canonical(self.config));config.chmod(0o600)
        worker = subprocess.Popen([sys.executable,'-m','control_production_plane.cli','--store',str(self.root/'control'),
            'execute-action','--adapter',str(config),'--data',str(self.root/'executor'),'--action-id',action],
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            self.assertTrue(entered.wait(4));worker.kill();worker.wait(timeout=3)
            self.assertFalse((self.adapter.calls.root/action/'result.json').exists())
            self.perform('query')
            with self.assertRaises(ControlError): self.adapter.perform(action)
            with self.assertRaises(ControlError): self.adapter.reconcile(action)
            self.assertEqual(sum(r['tool']=='lh_job_submit' for r in self.requests),1)
            self.assertTrue(self.store.recover('example')['pending_calls'])
        finally:
            if worker.poll() is None:worker.kill();worker.wait(timeout=3)

    def test_endpoint_loss_after_dispatch_is_proved_not_started(self):
        action=self.queue('prepare');original=self.adapter._endpoint;count=0
        def endpoint():
            nonlocal count
            count+=1
            if count==5:raise FileNotFoundError('Synthetic local endpoint loss')
            return original()
        with patch.object(self.adapter,'_endpoint',side_effect=endpoint), self.assertRaises(CallNotStarted):
            self.adapter.perform(action)
        self.assertEqual(self.store.get_action(action)['state'],'NOT_SENT')
        self.assertEqual(self.store.get_run('example')['counters']['actions'],0)
        self.assertFalse(any(r['tool']=='lh_job_prepare' for r in self.requests))
        self.adapter.reconcile(action)
        self.perform('prepare')

    def test_read_response_loss_keeps_output_and_reject_allows_revision(self):
        _,archived=self.sealed()
        original=self.artifacts.calls.read
        def lost(call_id,*args):
            if call_id.startswith('read-'):raise OSError('Synthetic response access failure')
            return original(call_id,*args)
        with patch.object(self.artifacts.calls,'read',side_effect=lost),self.assertRaises(OSError):
            self.artifacts.read('example',artifact_id=archived['artifact_id'],version_id=archived['version_id'],output=self.root/'lost-output')
        self.assertTrue((self.root/'lost-output/evidence.zip').exists())
        self.artifacts.read('example',artifact_id=archived['artifact_id'],version_id=archived['version_id'],output=self.root/'fresh-output')
        state=self.store.get_run('example');candidate=state['attempt']['candidate']
        base=dict(run_id='example',attempt_id=candidate['attempt_id'],candidate_digest=model.digest(candidate),actor='demo:reviewer',
            decision='REJECT',reason='Synthetic unmet requirement',checks=[dict(condition=c,passed=False,evidence_ref='demo:review') for c in state['goal']['acceptance']])
        for changed in (dict(base,attempt_id=str(uuid.uuid4())),dict(base,candidate_digest='0'*64),dict(base,checks=[]),dict(base,decision='ACCEPT')):
            with self.assertRaises(ControlError):self.store.execute(command('decide',changed))
        with self.assertRaises(ControlError):self.store.execute(command('revise',dict(run_id='example',expected_revision=1,plan=state['plan'])))
        self.store.execute(command('decide',base))
        revised=copy.deepcopy(state['plan']);revised['content']='Revised synthetic content'
        self.store.execute(command('revise',dict(run_id='example',expected_revision=1,plan=revised)))
        self.assertEqual(self.store.get_revision('example',1)['attempt']['decision'],base)
        self.assertNotIn('candidate',self.store.get_run('example')['attempt'])
        with self.assertRaises(ControlError):self.store.execute(command('decide',base))

    def test_archive_response_loss_resumes_original_write(self):
        # Lose the completed archive response after its actual Ledger write.
        original=ArtifactAdapter._call
        def lost(adapter,args,call_id):
            result=original(adapter,args,call_id)
            if args[0]=='archive':raise OSError('Synthetic lost receipt')
            return result
        with patch.object(ArtifactAdapter,'_call',lost),self.assertRaises(OSError):self.sealed()
        result=self.artifacts.resume('example')
        evidence=response_json(retained(self.artifacts.root/self.store.get_run('example')['attempt']['attempt_id']/'archive.json'))['evidence']
        replay=self.artifacts.archive('example',evidence)
        self.assertEqual((result['artifact_id'],result['version_id']),(replay['artifact_id'],replay['version_id']))
        self.assertEqual(sum(r['tool']=='lh_job_submit' for r in self.requests),1)

    def test_archive_nonstarted_cli_can_continue_without_replaying_business(self):
        original=CliCalls.run
        failed=False
        def missing(calls,argv,**kwargs):
            nonlocal failed
            if not failed and 'local_hand_jobs.artifact_archive' in argv:
                failed=True
                calls.not_started(kwargs['call_id'],argv,None,'SyntheticUnavailableExecutable')
            return original(calls,argv,**kwargs)
        with patch.object(CliCalls,'run',missing),self.assertRaises(CallNotStarted):self.sealed()
        state=self.store.get_run('example')
        evidence=response_json(retained(self.artifacts.root/state['attempt']['attempt_id']/'archive.json'))['evidence']
        first=self.artifacts.archive('example',evidence)
        second=self.artifacts.archive('example',evidence)
        self.assertEqual(first,second)
        self.assertEqual(sum(r['tool']=='lh_job_submit' for r in self.requests),1)

    def test_archive_completed_input_failure_can_continue_with_original_inputs(self):
        original = CliCalls.run
        def invalid_input(calls, argv, **kwargs):
            if 'local_hand_jobs.artifact_archive' not in argv:
                return original(calls, argv, **kwargs)
            path = Path(argv[argv.index('--evidence-dir') + 1])/'evidence.zip'
            saved = path.with_name('temporarily-unavailable.zip')
            path.rename(saved)
            try:
                return original(calls, argv, **kwargs)
            finally:
                saved.rename(path)
        with patch.object(CliCalls, 'run', invalid_input), self.assertRaises(ControlError) as failed:
            self.sealed()
        self.assertEqual(failed.exception.code, 'ARCHIVE_ERROR')
        state = self.store.get_run('example')
        call_id = 'archive-' + state['attempt']['attempt_id']
        directory = self.artifacts.calls.root/call_id
        retained_failure = {p.name:p.read_bytes() for p in directory.iterdir()}
        result = response_json(retained(directory/'result.json'))
        self.assertTrue(result['call_closed'])
        self.assertEqual(result['exit_code'], 1)
        self.assertFalse(Path(self.artifacts.archive_store).exists())
        evidence = response_json(retained(self.artifacts.root/state['attempt']['attempt_id']/'archive.json'))['evidence']
        recovered = self.artifacts.archive('example', evidence)
        self.assertTrue(recovered['content_verified'])
        self.assertEqual(recovered, self.artifacts.archive('example', evidence))
        self.assertEqual(retained_failure, {p.name:p.read_bytes() for p in directory.iterdir()})
        self.assertEqual(sum(r['tool']=='lh_job_submit' for r in self.requests), 1)
        read = self.artifacts.read('example', artifact_id=recovered['artifact_id'],
            version_id=recovered['version_id'], output=self.root/'recovered-read')
        self.assertEqual(read['use']['consumed_after_sha256'], hashlib.sha256(state['plan']['content'].encode()).hexdigest())

    def test_resumed_archive_receipt_requires_verified_content_and_exact_ids(self):
        _, archived = self.sealed()
        for change in ({'content_verified':False}, {'artifact_id':None}, {'version_id':''}):
            with self.subTest(change=change), patch.object(self.artifacts, '_call', return_value=dict(archived, **change)):
                with self.assertRaises(ControlError):
                    self.artifacts.resume('example')

    def unarchived_evidence(self):
        with patch.object(ArtifactAdapter, 'archive', return_value=None):
            evidence, _ = self.sealed()
        return evidence

    def test_invalid_evidence_description_cannot_freeze_first_archive_input(self):
        evidence = self.unarchived_evidence()
        cases = [None, [], dict(evidence, unexpected=True)]
        for key in evidence:
            cases.append({k:v for k,v in evidence.items() if k != key})
        cases.extend(dict(evidence, **change) for change in (
            {'directory':'relative/evidence'}, {'directory':None}, {'directory':'/bad\0path'},
            {'directory':'/bad\ud800path'},
            {'status_sha256':'invalid'}, {'seal':None}, {'seal':{}},
            {'seal':dict(evidence['seal'],seal_sha256='invalid')},
            {'seal':dict(evidence['seal'],seal_id='')},
            {'operation_id':str(uuid.uuid4())}))
        attempt = self.store.get_run('example')['attempt']['attempt_id']
        for index, bad in enumerate(cases):
            with self.subTest(index=index):
                adapter = ArtifactAdapter(self.store, self.artifacts.command, self.artifacts.archive_store,
                                          self.root/('invalid-descriptor-'+str(index)))
                with patch.object(adapter.calls, 'run', wraps=adapter.calls.run) as run:
                    with self.assertRaises(ControlError):
                        adapter.archive('example', bad)
                    run.assert_not_called()
                self.assertFalse((adapter.root/attempt/'archive.json').exists())
        # Continue on the same adapter that rejected the last malformed input.
        corrected = adapter.archive('example', evidence)
        self.assertTrue(corrected['content_verified'])
        self.assertEqual(sum(r['tool']=='lh_job_submit' for r in self.requests), 1)

    def test_status_binding_is_checked_before_first_archive_intent(self):
        evidence = self.unarchived_evidence()
        attempt = self.store.get_run('example')['attempt']['attempt_id']
        status = response_json(retained(Path(evidence['directory'])/'status.json'))
        cases = [('digest', dict(evidence,status_sha256='0'*64), None),
                 ('seal', dict(evidence,seal=dict(evidence['seal'],seal_sha256='0'*64)), None)]
        for key, value in (('operation_id',str(uuid.uuid4())), ('request_digest','0'*64),
                           ('lifecycle','RUNNING'), ('phase','BUSINESS'), ('outcome','FAILED'),
                           ('evidence','STAGING'), ('seal_refs', []), ('exit_proof', None),
                           ('exit_proof', dict(status['exit_proof'],tree_exited=False))):
            cases.append((key, evidence, dict(status, **{key:value})))
        for label, description, wrong_status in cases:
            with self.subTest(label=label):
                description = copy.deepcopy(description)
                if wrong_status is not None:
                    directory = Path(tempfile.mkdtemp(prefix='wrong-status-'+label+'-', dir=self.root))
                    raw = json.dumps(wrong_status).encode()
                    publish(directory/'status.json', raw)
                    description.update(directory=str(directory),status_sha256=hashlib.sha256(raw).hexdigest())
                adapter = ArtifactAdapter(self.store, self.artifacts.command, self.artifacts.archive_store,
                                          Path(tempfile.mkdtemp(prefix='status-check-'+label+'-', dir=self.root)))
                with patch.object(adapter.calls, 'run', wraps=adapter.calls.run) as run:
                    with self.assertRaises(ControlError):
                        adapter.archive('example', description)
                    run.assert_not_called()
                self.assertFalse((adapter.root/attempt/'archive.json').exists())
        corrected = adapter.archive('example', evidence)
        self.assertTrue(corrected['content_verified'])
        self.assertEqual(sum(r['tool']=='lh_job_submit' for r in self.requests), 1)

    def test_completed_archive_receipt_and_resume_do_not_require_download_files(self):
        evidence, archived = self.sealed()
        for name in ('status.json', 'evidence.zip', 'evidence.manifest', 'evidence.seal'):
            (Path(evidence['directory'])/name).unlink()
        with patch.object(self.artifacts.calls, 'run', wraps=self.artifacts.calls.run) as run:
            self.assertEqual(self.artifacts.archive('example', evidence), archived)
            run.assert_not_called()
            recovered = self.artifacts.resume('example')
            self.assertEqual(run.call_count, 1)
        self.assertEqual(recovered['version_id'], archived['version_id'])
        self.assertTrue(recovered['content_verified'])

    def test_archive_cli_rejects_bad_first_input_then_accepts_correction(self):
        evidence = self.unarchived_evidence()
        config = self.root/'archive-config.json'
        publish(config, model.canonical(dict(command=self.artifacts.command,archive_store=self.artifacts.archive_store)))
        supplied = self.root/'supplied-evidence.json'
        publish(supplied, model.canonical({k:v for k,v in evidence.items() if k != 'seal'}))
        argv = [sys.executable, '-m', 'control_production_plane.cli', '--store', str(self.root/'control'),
            'archive-result', '--adapter', str(config), '--data', str(self.artifacts.root),
            '--run-id', 'example', '--evidence', str(supplied)]
        failed = subprocess.run(argv, capture_output=True, timeout=15)
        self.assertEqual(failed.returncode, 2)
        self.assertEqual(json.loads(failed.stderr)['error']['code'], 'INVALID_INPUT')
        attempt = self.store.get_run('example')['attempt']['attempt_id']
        self.assertFalse((self.artifacts.root/attempt/'archive.json').exists())
        supplied.write_bytes(model.canonical(evidence))
        completed = subprocess.run(argv, capture_output=True, timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        archived = json.loads(completed.stdout)['data']
        self.assertEqual(archived['status'], 'ARCHIVED')
        self.assertTrue(archived['content_verified'])
        self.assertEqual(sum(r['tool']=='lh_job_submit' for r in self.requests), 1)

    def archive_exit_wrapper(self, *, timeout=False):
        # Actual public archive CLI commits first; alter only its final delivery.
        wrapper = self.root/'archive-cli.py'
        wrapper.write_text('''import contextlib, io, json, sys, time
from pathlib import Path
from local_hand_jobs.artifact_archive import main
output = io.StringIO()
with contextlib.redirect_stdout(output):
    code = main(sys.argv[1:])
saved = Path(__file__).with_name("committed-receipt.json")
if sys.argv[1] == "archive" and code == 0 and not saved.exists():
    saved.write_text(output.getvalue())
    if TIMEOUT:
        time.sleep(30)
    print(json.dumps(dict(status="ERROR", code="SYNTHETIC_DELIVERY_ERROR", commit_state="committed")))
    raise SystemExit(1)
print(output.getvalue(), end="")
raise SystemExit(code)
'''.replace('TIMEOUT', repr(timeout)))
        self.archive_command = [sys.executable, '-I', '-B', str(wrapper)]

    def test_archive_completed_error_after_commit_reuses_original_version(self):
        self.archive_exit_wrapper()
        with self.assertRaises(ControlError) as failed:
            self.sealed()
        self.assertEqual(failed.exception.code, 'ARCHIVE_ERROR')
        first = json.loads((self.root/'committed-receipt.json').read_bytes())
        state = self.store.get_run('example')
        evidence = response_json(retained(self.artifacts.root/state['attempt']['attempt_id']/'archive.json'))['evidence']
        changed = dict(evidence, status_sha256='0'*64)
        with patch.object(self.artifacts.calls, 'run', wraps=self.artifacts.calls.run) as run:
            with self.assertRaises(ControlError):
                self.artifacts.archive('example', changed)
            run.assert_not_called()
            recovered = self.artifacts.archive('example', evidence)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(recovered, self.artifacts.archive('example', evidence))
            self.assertEqual(run.call_count, 1)
        self.assertTrue(recovered['reused'])
        for key in ('artifact_id', 'version_id', 'operation_id', 'producer', 'files'):
            self.assertEqual(recovered[key], first[key])
        self.assertEqual(sum(r['tool']=='lh_job_submit' for r in self.requests), 1)

    def test_archive_timeout_requires_resume_instead_of_another_archive(self):
        self.archive_exit_wrapper(timeout=True)
        original = CliCalls.run
        def short_timeout(calls, argv, **kwargs):
            if 'archive' in argv:
                calls.timeout = 1
            return original(calls, argv, **kwargs)
        with patch.object(CliCalls, 'run', short_timeout), self.assertRaises(ControlError) as failed:
            self.sealed()
        self.assertEqual(failed.exception.code, 'CALL_UNKNOWN')
        first = json.loads((self.root/'committed-receipt.json').read_bytes())
        state = self.store.get_run('example')
        evidence = response_json(retained(self.artifacts.root/state['attempt']['attempt_id']/'archive.json'))['evidence']
        self.artifacts.calls.timeout = 60
        with patch.object(self.artifacts.calls, 'run', wraps=self.artifacts.calls.run) as run:
            with self.assertRaises(ControlError) as blocked:
                self.artifacts.archive('example', evidence)
            self.assertEqual(blocked.exception.code, 'CALL_UNKNOWN')
            run.assert_not_called()
            recovered = self.artifacts.resume('example')
            self.assertEqual(run.call_count, 1)
        self.assertEqual(recovered['version_id'], first['version_id'])
        self.assertEqual(sum(r['tool']=='lh_job_submit' for r in self.requests), 1)

    def test_unsupported_candidate_path_is_refused_before_preparation(self):
        plan=copy.deepcopy(self.spec['plan']);plan['relative_path']='内容.py'
        self.store.execute(command('revise',dict(run_id='example',expected_revision=1,plan=plan)))
        action=self.queue('prepare')
        with self.assertRaises(ControlError) as error:self.adapter.perform(action)
        self.assertEqual(error.exception.code,'UNSUPPORTED')
        self.assertEqual(self.store.get_action(action)['state'],'QUEUED')
        self.assertFalse(any(r['tool']=='lh_job_prepare' for r in self.requests))
