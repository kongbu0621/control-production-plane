"""Real P/K/Ledger public APIs, synthetic knowledge and observed task results."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from control_production_plane import initialize, open_store, ControlError
from control_production_plane.knowledge import KnowledgeAdapter, encoded, key
from test_core import sample, command, ready
try:
    from infra_artifact_ledger import initialize as init_ledger
    from domain_operational_knowledge_core import initialize as init_k, KnowledgeError
    HAS_K = True
except ImportError:
    HAS_K = False


@unittest.skipUnless(HAS_K, "optional K and Ledger packages required")
class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        with init_ledger(self.root/'materials.sqlite') as ledger:
            ledger.execute(encoded(dict(profile="bounded-local-v0.1", contract_version="0.1.0", operation_kind="create_artifact",
                idempotency_scope_ref="example:anchor", idempotency_key="anchor", body=dict(artifact=dict(
                    artifact_id="artifact:anchor", namespace_ref="synthetic:knowledge",
                    type_ref=dict(namespace="example", type_id="store-anchor", type_version="1"))))))
        self.k = init_k(self.root/'knowledge', ledger_path=self.root/'materials.sqlite', anchor_artifact_id="artifact:anchor")
        self.op('publish_material', dict(material_id='material:example', source_ref='example:source', task_ref='example:task',
            context={'project':'example'}, claimed_outcome='FAILED', gaps=[], text='Synthetic: inspect original result before repeating work.'))
        candidate = self.op('propose', dict(proposal_id='proposal:example', kind='guidance', content='核对原结果，再继续。',
            conditions={'project':'example'}, material_ids=['material:example']))['receipt']['result']
        anchor = self.op('review', dict(candidate_id=candidate['candidate_id'], candidate_sha256=candidate['candidate_sha256'],
            decision='ACCEPT', reviewer='example:reviewer', reason='Explicit synthetic scope review', evidence_material_ids=['material:example']))['receipt']
        self.version = anchor['result']['version_id']
        self.config = dict(store_path=str(self.root/'knowledge'), store_id=anchor['store_id'], scope='example:p',
                           anchor={x:anchor[x] for x in ('scope','operation','request_id','fingerprint')})
        self.p = initialize(self.root/'production')
        self.a = KnowledgeAdapter(self.p, self.config)
        self.context = {'project':'example'}
        self.spec = sample()
        self.spec['plan']['knowledge'] = dict(required=True, status='SELECTED', reason='核对旧结果后修改合成目标',
            items=[self.a.selection(self.version, self.context)['item']])
        self.p.execute(command('create', self.spec))

    def op(self, op, body):
        return self.k.execute(encoded(dict(schema='operational-knowledge-v1', scope='example:reviewer', operation=op, request_id='request:'+op, body=body)))

    def queue(self, kind):
        state=self.p.get_run('example'); aid=str(uuid.uuid4())
        self.p.execute(command('queue',dict(run_id='example',attempt_id=state['attempt']['attempt_id'],action_id=aid,kind=kind)))
        return aid

    def submit(self):
        self.p.perform(self.queue('prepare'),self.spec['binding'],ready)
        self.p.perform(self.queue('submit'),self.spec['binding'],lambda _:dict(response=dict(accepted=True),call_closed=True))

    def test_query_selection_adoption_submission_feedback_reopen(self):
        self.assertFalse(self.a.query('guidance',self.context)['usage_recorded'])
        self.a.adopt('example',self.context)
        self.submit()
        first=self.a.feedback('example',1,'执行结果还未知，不能声称成功。')
        call=self.p.get_run('example')['knowledge_calls'][key(self.p.identity['store_id'], self.p.get_run('example'),'complete_use')]
        material=json.loads(call['request']['body']['material']['text'])
        self.assertTrue(material['submission_recorded']);self.assertIsNone(material['observation'])
        self.assertEqual(call['request']['body']['material']['claimed_outcome'],'UNKNOWN')
        self.assertTrue(self.k.read_material(call['receipt']['result']['material_id'])['content_verified'])
        reopened=KnowledgeAdapter(open_store(self.root/'production'),self.config)
        self.assertEqual(first['receipt'],reopened.feedback('example',1,'执行结果还未知，不能声称成功。')['receipt'])
        self.assertEqual(self.p.recover('example')['pending_knowledge_calls'],[])

    def test_lost_k_adoption_reply_recovers_one_usage_even_after_retirement(self):
        original=self.k.execute
        def lost(raw):
            original(raw)
            raise OSError('lost K acknowledgement')
        with patch.object(self.a,'_open',return_value=self.k),patch.object(self.k,'execute',side_effect=lost):
            with self.assertRaises(OSError):self.a.adopt('example',self.context)
        self.op('retire',dict(expected_version_id=self.version,reviewer='example:reviewer',reason='No new uses',evidence_material_ids=['material:example']))
        self.a.adopt('example',self.context)
        self.assertEqual(len(self.p.get_run('example')['knowledge_calls']),1)
        self.submit()

    def test_query_then_retirement_rejects_new_use_without_spending(self):
        self.op('retire',dict(expected_version_id=self.version,reviewer='example:reviewer',reason='No new uses',evidence_material_ids=['material:example']))
        with self.assertRaises(ControlError):self.a.adopt('example',self.context)
        self.assertEqual(self.p.get_run('example')['counters']['attempts'],0)
        self.assertNotIn('knowledge_calls',self.p.get_run('example'))

    def test_wrong_destination_or_hash_or_context_writes_no_usage(self):
        for change in ('store','hash','context'):
            with self.subTest(change=change):
                if change=='store':
                    config=dict(self.config,store_id='wrong:store')
                    with self.assertRaises(ControlError):KnowledgeAdapter(self.p,config).adopt('example',self.context)
                elif change=='hash':
                    spec=copy.deepcopy(self.spec);spec['plan']['knowledge']['items'][0]['content_sha256']='a'*64
                    self.p.execute(command('revise',dict(run_id='example',expected_revision=1,plan=spec['plan'])))
                    with self.assertRaises(ControlError):self.a.adopt('example',self.context)
                else:
                    with self.assertRaises(ControlError):self.a.adopt('example',{'project':'other'})
                self.assertNotIn('knowledge_calls',self.p.get_run('example'))

    def test_revision_needs_new_use_and_late_feedback_keeps_original_task(self):
        self.a.adopt('example',self.context)
        first=self.p.get_run('example');old_id=first['attempt']['operation_id']
        self.p.execute(command('revise',dict(run_id='example',expected_revision=1,plan=self.spec['plan'])))
        self.p.perform(self.queue('prepare'),self.spec['binding'],ready)
        with self.assertRaises(ControlError):self.p.perform(self.queue('submit'),self.spec['binding'],lambda _:self.fail('old usage must not authorize new attempt'))
        self.a.feedback('example',1,'Old attempt was revised before dispatch.')
        current=self.p.get_run('example');call=current['knowledge_calls'][key(self.p.identity['store_id'],first,'complete_use')]
        self.assertEqual(call['request']['body']['material']['task_ref'],'p-task:'+old_id)
        self.a.adopt('example',self.context)
        self.assertEqual(len(self.p.get_run('example')['knowledge_calls']),3)

    def test_cancel_before_uncommitted_use_blocks_retry_but_preserves_intent(self):
        with patch.object(self.a,'_open',return_value=self.k),patch.object(self.k,'execute',side_effect=OSError('pre-write failure')):
            with self.assertRaises(OSError):self.a.adopt('example',self.context)
        self.p.execute(command('cancel',dict(run_id='example',reason='Stop this work')))
        with self.assertRaises(ControlError):self.a.adopt('example',self.context)
        self.assertEqual(len(self.p.recover('example')['pending_knowledge_calls']),1)
        self.assertEqual(self.p.get_run('example')['counters']['attempts'],0)

    def test_lost_p_acknowledgement_recovers_without_another_k_write(self):
        original=self.p.execute
        def lost(request,**kwargs):
            if request['operation']=='knowledge_ack':raise OSError('P disk temporarily unavailable')
            return original(request,**kwargs)
        with patch.object(self.p,'execute',side_effect=lost):
            with self.assertRaises(OSError):self.a.adopt('example',self.context)
        with patch.object(self.a,'_open',return_value=self.k),patch.object(self.k,'execute',side_effect=AssertionError('must read receipt')):
            self.a.adopt('example',self.context)

    def test_feedback_failure_retries_original_payload_after_observation_changes(self):
        self.a.adopt('example',self.context)
        with patch.object(self.a,'_open',return_value=self.k),patch.object(self.k,'execute',side_effect=OSError('before K write')):
            with self.assertRaises(OSError):self.a.feedback('example',1,'Initial unknown result')
        self.p.execute(command('cancel',dict(run_id='example',reason='Later cancellation')))
        self.a.feedback('example',1,'Initial unknown result')
        call=self.p.get_run('example')['knowledge_calls'][key(self.p.identity['store_id'],self.p.get_run('example'),'complete_use')]
        self.assertIsNone(json.loads(call['request']['body']['material']['text'])['cancel'])
        with self.assertRaises(ControlError):self.a.feedback('example',1,'Changed feedback')

    def test_changed_receipt_is_rejected_and_does_not_authorize_submit(self):
        original=self.k.execute
        def wrong(raw):
            result=original(raw);result['receipt']['fingerprint']='0'*64;return result
        with patch.object(self.a,'_open',return_value=self.k),patch.object(self.k,'execute',side_effect=wrong):
            with self.assertRaises(ControlError):self.a.adopt('example',self.context)
        self.assertEqual(len(self.p.recover('example')['pending_knowledge_calls']),1)
        self.a.adopt('example',self.context)

    def test_cli_uses_optional_adapter_and_records_usage(self):
        import subprocess,sys,os
        config=self.root/'adapter.json';config.write_bytes(encoded(self.config));config.chmod(0o600)
        context=self.root/'context.json';context.write_bytes(encoded(self.context));context.chmod(0o600)
        base=[sys.executable,'-m','control_production_plane.cli','--store',str(self.root/'production')]
        selected=subprocess.run(base+['select-knowledge','--adapter',str(config),'--context',str(context),'--version-id',self.version],capture_output=True)
        self.assertEqual(selected.returncode,0,selected.stderr)
        self.assertEqual(json.loads(selected.stdout)['data']['item'],self.spec['plan']['knowledge']['items'][0])
        adopted=subprocess.run(base+['adopt-knowledge','--adapter',str(config),'--context',str(context),'--run-id','example'],capture_output=True)
        self.assertEqual(adopted.returncode,0,adopted.stderr)
        self.assertEqual(json.loads(adopted.stdout)['data']['receipt']['result']['state'],'COMMITTED')

    def test_crashed_process_after_k_commit_resumes_same_usage(self):
        import subprocess,sys
        payload=dict(config=self.config,store=str(self.root/'production'),context=self.context)
        script='''import json,os,sys
from control_production_plane import open_store
from control_production_plane.knowledge import KnowledgeAdapter
v=json.load(sys.stdin);s=open_store(v['store']);a=KnowledgeAdapter(s,v['config']);original=s.execute
def execute(request,**kw):
    if request['operation']=='knowledge_ack':os._exit(87)
    return original(request,**kw)
s.execute=execute
a.adopt('example',v['context'])
'''
        proc=subprocess.run([sys.executable,'-c',script],input=encoded(payload),capture_output=True,timeout=15)
        self.assertEqual(proc.returncode,87,proc.stderr)
        self.assertEqual(len(self.p.recover('example')['pending_knowledge_calls']),1)
        self.a.adopt('example',self.context)
        self.assertEqual(self.p.recover('example')['pending_knowledge_calls'],[])

    def test_missing_previously_committed_k_receipt_does_not_recreate_it(self):
        self.a.adopt('example',self.context)
        original=self.k.get_operation
        def missing(scope,operation,request_id):
            if operation=='start_use':raise KnowledgeError('NOT_FOUND','simulated rolled back K store','not_applicable')
            return original(scope,operation,request_id)
        with patch.object(self.a,'_open',return_value=self.k),patch.object(self.k,'get_operation',side_effect=missing),patch.object(self.k,'execute',side_effect=AssertionError('no recreation')):
            with self.assertRaises(ControlError):self.a.adopt('example',self.context)

    def test_second_caller_cannot_adopt_while_executor_held(self):
        with self.p.executor():
            with self.assertRaises(ControlError):self.a.adopt('example',self.context)
        self.assertNotIn('knowledge_calls',self.p.get_run('example'))
