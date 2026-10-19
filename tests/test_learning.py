"""Public P/K/Ledger delivery and recovery; all task facts are synthetic."""
import base64
import copy
import json
import subprocess
import sys
import unittest
from unittest.mock import patch

from control_production_plane import ControlError, open_store
from control_production_plane.knowledge import encoded
from control_production_plane.learning import LearningAdapter, source
from test_core import command
import test_knowledge as fixtures


@unittest.skipUnless(fixtures.HAS_K, 'optional K and Ledger packages required')
class LearningTests(unittest.TestCase):
    setUp = fixtures.KnowledgeTests.setUp
    op = fixtures.KnowledgeTests.op

    def candidate(self, identity='learning:example'):
        state = self.p.get_run('example')
        body = dict(run_id='example', attempt_id=state['attempt']['attempt_id'], candidate_id=identity,
                    explanation='区分先前观察与后来采用知识的效果。', evidence_refs=['synthetic:observation'], artifact_versions=[])
        self.p.execute(command('learning_candidate', body))
        return body

    def proposal(self):
        return dict(kind='attribution-guidance', content='Record observed order before attributing a result to guidance.',
                    context={'project':'example'}, conditions={'project':'example'},
                    gaps=['Synthetic observation; no real execution.'], material_ids=[])

    def adapter(self):
        return LearningAdapter(self.p, self.config)

    def material_text(self, result):
        value = self.k.read_material(result['receipts']['publish_material']['result']['material_id'])
        self.assertTrue(value['content_verified'])
        return json.loads(base64.b64decode(value['entries'][0]['data'], validate=True))

    def test_public_delivery_preserves_production_and_does_not_admit(self):
        body = self.candidate(); before = self.p.get_run('example')
        result = self.adapter().deliver(body['candidate_id'], self.proposal())
        self.assertEqual((result['delivery'], result['admission']), ('RECEIVED','NOT_REQUESTED'))
        received = self.k.get('candidate', result['receipts']['propose']['result']['candidate_id'])['record']
        self.assertEqual(received['state'], 'PENDING')
        self.assertEqual(self.k.query('attribution-guidance', self.context)['versions'], [])
        text = self.material_text(result)
        self.assertEqual(text['learning_candidate'], source(self.p.get_candidate(body['candidate_id'])))
        self.assertFalse(text['production']['submission_recorded'])
        self.assertEqual(self.p.get_run('example'), before)
        self.p = open_store(self.root/'production')
        self.assertEqual(self.adapter().resume_delivery(body['candidate_id']), result)
        self.p.execute(command('learning_candidate', body))
        self.assertEqual(self.p.get_candidate(body['candidate_id'])['delivery'], 'RECEIVED')

    def test_each_lost_k_response_recovers_without_duplicate_writes(self):
        for failed_op in ('publish_material', 'propose'):
            with self.subTest(operation=failed_op):
                cid='learning:'+failed_op; self.candidate(cid)
                adapter=self.adapter(); original=self.k.execute
                def lost(raw):
                    result=original(raw)
                    if json.loads(raw)['operation']==failed_op:raise OSError('Lost committed response')
                    return result
                with patch.object(adapter,'_open',return_value=self.k),patch.object(self.k,'execute',side_effect=lost):
                    with self.assertRaises(OSError):adapter.deliver(cid,self.proposal())
                before=self.p.get_run('example')
                with patch.object(adapter,'_open',return_value=self.k),patch.object(self.k,'execute',wraps=original) as writes:
                    result=adapter.resume_delivery(cid)
                self.assertEqual(result['delivery'],'RECEIVED')
                self.assertEqual(writes.call_count,1 if failed_op=='publish_material' else 0)
                self.assertEqual(self.p.get_run('example'),before)

    def test_partial_ledger_publication_resumes_same_pending_material(self):
        from domain_operational_knowledge_core import KnowledgeError
        cid=self.candidate()['candidate_id'];adapter=self.adapter();original=self.k.ledger.publish
        def lost(*args):
            original(*args)
            raise KnowledgeError('IO_ERROR','Lost publication response','unknown')
        with patch.object(adapter,'_open',return_value=self.k),patch.object(self.k.ledger,'publish',side_effect=lost):
            with self.assertRaises(KnowledgeError):adapter.deliver(cid,self.proposal())
        req=self.p.get_candidate(cid)['delivery_record']['requests']['publish_material']
        self.assertEqual(self.k.pending(req['scope'],req['request_id'])['state'],'PENDING')
        self.assertEqual(adapter.resume_delivery(cid)['delivery'],'RECEIVED')
        self.assertEqual(self.k.pending(req['scope'],req['request_id'])['state'],'COMMITTED')

    def test_crash_after_each_k_commit_before_p_ack(self):
        script='''import json,os,sys
from control_production_plane import open_store
from control_production_plane.learning import LearningAdapter
v=json.load(sys.stdin);s=open_store(v['store']);a=LearningAdapter(s,v['config']);original=s.execute
def execute(req,**kw):
    if req['operation']=='learning_ack' and req['body']['operation']==v['op']:os._exit(87)
    return original(req,**kw)
s.execute=execute
a.deliver(v['candidate'],v['proposal'])
'''
        for op in ('publish_material','propose'):
            with self.subTest(operation=op):
                cid='learning:crash-'+op;self.candidate(cid)
                payload=dict(store=str(self.root/'production'),config=self.config,proposal=self.proposal(),candidate=cid,op=op)
                child=subprocess.run([sys.executable,'-c',script],input=encoded(payload),capture_output=True,timeout=15)
                self.assertEqual(child.returncode,87,child.stderr)
                result=self.adapter().resume_delivery(cid)
                self.assertEqual(result['delivery'],'RECEIVED')
                self.assertEqual(self.adapter().resume_delivery(cid),result)

    def test_snapshot_survives_cancel_revision_and_resume(self):
        cid=self.candidate()['candidate_id'];adapter=self.adapter()
        with patch.object(adapter,'_open',return_value=self.k),patch.object(self.k,'execute',side_effect=OSError('Before write')):
            with self.assertRaises(OSError):adapter.deliver(cid,self.proposal())
        saved=copy.deepcopy(self.p.get_candidate(cid)['delivery_record'])
        self.p.execute(command('revise',dict(run_id='example',expected_revision=1,plan=self.spec['plan'])))
        self.p.execute(command('cancel',dict(run_id='example',reason='Later cancellation')))
        before=self.p.get_run('example');result=adapter.resume_delivery(cid)
        text=self.material_text(result)
        self.assertEqual(text['production']['revision'],1)
        self.assertIsNone(text['production']['cancel'])
        self.assertEqual(self.p.get_candidate(cid)['delivery_record']['requests'],saved['requests'])
        self.assertEqual(self.p.get_run('example'),before)

    def test_changed_spec_or_destination_rejected_without_writes(self):
        cid=self.candidate()['candidate_id'];result=self.adapter().deliver(cid,self.proposal())
        changed=self.proposal();changed['content']='Different claim'
        with self.assertRaises(ControlError):self.adapter().deliver(cid,changed)
        with self.assertRaises(ControlError):LearningAdapter(self.p,dict(self.config,scope='different:scope')).resume_delivery(cid)
        self.assertEqual(self.adapter().resume_delivery(cid),result)

    def test_missing_confirmed_receipt_is_not_recreated(self):
        from domain_operational_knowledge_core import KnowledgeError
        cid=self.candidate()['candidate_id'];adapter=self.adapter();adapter.deliver(cid,self.proposal());original=self.k.get_operation
        def missing(scope,operation,request_id):
            if operation=='propose':raise KnowledgeError('NOT_FOUND','Older K snapshot','not_applicable')
            return original(scope,operation,request_id)
        with patch.object(adapter,'_open',return_value=self.k),patch.object(self.k,'get_operation',side_effect=missing),patch.object(self.k,'execute',side_effect=AssertionError('No recreation')):
            with self.assertRaises(ControlError) as raised:adapter.resume_delivery(cid)
        self.assertEqual(raised.exception.code,'IDENTITY_CONFLICT')

    def test_temporary_read_failure_does_not_issue_write_or_lock_recovery(self):
        from domain_operational_knowledge_core import KnowledgeError
        cid=self.candidate()['candidate_id'];adapter=self.adapter()
        with patch.object(adapter,'_open',return_value=self.k),patch.object(self.k,'get_operation',side_effect=KnowledgeError('IO_ERROR','Temporary read','not_applicable')),patch.object(self.k,'execute',side_effect=AssertionError('Do not write')):
            with self.assertRaises(KnowledgeError):adapter.deliver(cid,self.proposal())
        self.assertEqual(adapter.resume_delivery(cid)['delivery'],'RECEIVED')

    def test_wrong_receipt_leaves_pending_then_recovers_original(self):
        cid=self.candidate()['candidate_id'];adapter=self.adapter();original=self.k.execute
        def wrong(raw):
            result=original(raw);result['receipt']['fingerprint']='0'*64;return result
        with patch.object(adapter,'_open',return_value=self.k),patch.object(self.k,'execute',side_effect=wrong):
            with self.assertRaises(ControlError):adapter.deliver(cid,self.proposal())
        self.assertEqual(self.p.get_candidate(cid)['delivery'],'PENDING')
        self.assertEqual(adapter.resume_delivery(cid)['delivery'],'RECEIVED')

    def test_bad_input_and_unbound_artifact_do_not_publish(self):
        cid=self.candidate()['candidate_id'];cases=[]
        for field,value in (('conditions',{'illegal-key':'x'}),('content','x\x00'),('material_ids',['a']),('content','\u0001'*65536)):
            bad=self.proposal();bad[field]=value;cases.append(bad)
        for spec in cases:
            with self.subTest(spec_size=len(str(spec))):
                with self.assertRaises(ControlError):self.adapter().deliver(cid,spec)
                self.assertNotIn('delivery_record',self.p.get_candidate(cid))
        body=self.candidate('learning:unbound');body['candidate_id']='learning:wrong-artifact'
        body['artifact_versions']=[dict(artifact_id='artifact:other',version_id='version:other')]
        self.p.execute(command('learning_candidate',body))
        with self.assertRaises(ControlError):self.adapter().deliver(body['candidate_id'],self.proposal())
        self.assertNotIn('delivery_record',self.p.get_candidate(body['candidate_id']))

    def test_missing_extra_material_preserves_publication_and_can_resume(self):
        from domain_operational_knowledge_core import KnowledgeError
        cid=self.candidate()['candidate_id'];adapter=self.adapter();spec=self.proposal()
        spec['material_ids']=['material:later']
        with self.assertRaises(KnowledgeError):adapter.deliver(cid,spec)
        saved=self.p.get_candidate(cid)['delivery_record']
        self.assertIsNotNone(saved['receipts']['publish_material'])
        self.assertIsNone(saved['receipts']['propose'])
        self.k.execute(encoded(dict(schema='operational-knowledge-v1',scope='example:reviewer',
            operation='publish_material',request_id='request:later',body=dict(material_id='material:later',
            source_ref='example:source',task_ref='example:later',context={},claimed_outcome='UNKNOWN',gaps=[],text='Synthetic later evidence.'))))
        result=adapter.resume_delivery(cid)
        self.assertEqual(result['receipts']['publish_material'],saved['receipts']['publish_material'])
        self.assertEqual(result['delivery'],'RECEIVED')

    def test_candidate_digest_in_receipt_must_bind_exact_claim(self):
        cid=self.candidate()['candidate_id'];adapter=self.adapter();original=self.k.execute
        def wrong(raw):
            result=original(raw)
            if json.loads(raw)['operation']=='propose':result['receipt']['result']['candidate_sha256']='0'*64
            return result
        with patch.object(adapter,'_open',return_value=self.k),patch.object(self.k,'execute',side_effect=wrong):
            with self.assertRaises(ControlError):adapter.deliver(cid,self.proposal())
        self.assertEqual(self.p.get_candidate(cid)['delivery'],'PENDING')
        self.assertEqual(adapter.resume_delivery(cid)['delivery'],'RECEIVED')

    def test_cli_delivery_and_resume(self):
        cid=self.candidate()['candidate_id']
        config=self.root/'adapter.json';config.write_bytes(encoded(self.config));config.chmod(0o600)
        proposal=self.root/'proposal.json';proposal.write_bytes(encoded(self.proposal()));proposal.chmod(0o600)
        base=[sys.executable,'-m','control_production_plane.cli','--store',str(self.root/'production')];results=[]
        for args in (['deliver-learning','--adapter',str(config),'--proposal',str(proposal)],['resume-learning','--adapter',str(config)]):
            run=subprocess.run(base+args+['--candidate-id',cid],capture_output=True,timeout=15)
            self.assertEqual(run.returncode,0,run.stderr);results.append(json.loads(run.stdout)['data'])
        self.assertEqual(results[0],results[1])
        shown=subprocess.run(base+['show-learning','--candidate-id',cid],capture_output=True,timeout=15)
        self.assertEqual(json.loads(shown.stdout)['data']['delivery'],'RECEIVED')

    def test_executor_exclusion_and_cancellation_remain_independent(self):
        cid=self.candidate()['candidate_id']
        with self.p.executor():
            with self.assertRaises(ControlError) as raised:self.adapter().deliver(cid,self.proposal())
            self.assertEqual(raised.exception.code,'BUSY')
            self.p.execute(command('cancel',dict(run_id='example',reason='Cancel production independently')))
        self.assertEqual(self.adapter().deliver(cid,self.proposal())['delivery'],'RECEIVED')
