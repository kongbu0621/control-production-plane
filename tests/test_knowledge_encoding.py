"""Unicode storage bounds and recovery of earlier complete feedback snapshots."""
import base64
import copy
import hashlib
import json
import unittest
from unittest.mock import patch

from control_production_plane import ControlError,model,open_store
from control_production_plane.knowledge import KnowledgeAdapter,encoded,feedback_material,key
from test_core import command
import test_knowledge as fixtures
HAS_K=fixtures.HAS_K


@unittest.skipUnless(HAS_K, 'optional K and Ledger packages required')
class KnowledgeEncodingTests(unittest.TestCase):
    setUp=fixtures.KnowledgeTests.setUp
    op=fixtures.KnowledgeTests.op
    queue=fixtures.KnowledgeTests.queue
    submit=fixtures.KnowledgeTests.submit

    def accepted_conditions(self, text):
        from control_production_plane import initialize
        self.p=initialize(self.root/'large-production')
        self.spec['goal']['acceptance']=[str(i)+':'+text for i in range(32)]
        self.p.execute(command('create',self.spec))
        self.a=KnowledgeAdapter(self.p,self.config)
        self.a.adopt('example',self.context)
        self.submit()
        state=self.p.get_run('example');attempt=state['attempt'];plan=state['plan']
        self.p.execute(command('observe',dict(run_id='example',attempt_id=attempt['attempt_id'],operation_id=attempt['operation_id'],
            request_digest=attempt['prepared_request']['request_digest'],remote_state='TERMINAL',exited=True,outcome='SUCCEEDED',evidence_state='SEALED',evidence_ref='synthetic:evidence')))
        source=dict(schema_version='lh-artifact-source-v1',producer=state['binding']['producer'],operation_id=attempt['operation_id'],
            request_digest=attempt['prepared_request']['request_digest'],seal_id='synthetic:seal',seal_sha256='a'*64,status_sha256='b'*64,
            workspace=dict(schema_version='lh-workspace-edit-v1',source_ref=plan['source_ref'],source_commit=plan['source_commit'],relative_path=plan['relative_path'],
                validation_ref=plan['validation_ref'],before_sha256=plan['expected_sha256'],after_sha256=hashlib.sha256(plan['content'].encode()).hexdigest(),
                scope='TASK_OWNED_COPY',original_modified=False,read_back_verified=True,validation_succeeded=True,edited_file_unchanged_by_validation=True))
        self.p.execute(command('candidate',dict(run_id='example',attempt_id=attempt['attempt_id'],artifact_id='synthetic:artifact',version_id='synthetic:version',source=source,readback_ref='synthetic:readback',receipt_sha256='c'*64)))
        candidate=self.p.get_run('example')['attempt']['candidate']
        decision=dict(run_id='example',attempt_id=attempt['attempt_id'],candidate_digest=model.digest(candidate),actor='example:reviewer',decision='ACCEPT',reason='Synthetic bounded acceptance',
            checks=[dict(condition=c,passed=True,evidence_ref='synthetic:evidence') for c in state['goal']['acceptance']])
        self.p.execute(command('decide',decision))
        return self.p.get_run('example'), candidate, decision

    @staticmethod
    def full_material(store_id, state, usage, notes):
        material=feedback_material(store_id,state,usage,notes)
        facts=json.loads(material['text'])
        if facts.pop('snapshot_format',None) is not None:
            facts.pop('full_snapshot_sha256')
            facts['candidate']=state['attempt']['candidate']
            facts['decision']=state['attempt']['decision']
            material['text']=encoded(facts).decode()
        return material

    def check_unicode_feedback(self, text):
        state,candidate,decision=self.accepted_conditions(text)
        use=state['knowledge_calls'][state['attempt']['knowledge_use']]['request']['body']
        notes='Unicode acceptance snapshot'
        full_material=self.full_material(self.p.identity['store_id'],state,use,notes)
        self.assertLessEqual(len(full_material['text'].encode()),65536)
        self.assertGreater(len(model.canonical(json.loads(full_material['text']))),65536)
        self.a.feedback('example',1,notes)
        state=self.p.get_run('example')
        call=state['knowledge_calls'][key(self.p.identity['store_id'],state,'complete_use')]
        material=call['request']['body']['material']
        self.assertLessEqual(len(material['text'].encode()),65536)
        read=self.k.read_material(call['receipt']['result']['material_id'])
        self.assertTrue(read['content_verified'])
        self.assertEqual(base64.b64decode(read['entries'][0]['data']),material['text'].encode())
        self.assertEqual(call['state'],'COMMITTED')
        self.assertLessEqual(len(model.canonical(state)),model.MAX_JSON)
        facts=json.loads(material['text'])
        self.assertEqual(facts['decision']['decision'],'ACCEPT')
        self.assertEqual(facts['decision']['document_sha256'],model.digest(decision))
        self.assertEqual(len(facts['decision']['checks']),32)
        self.assertEqual([x['condition_sha256'] for x in facts['decision']['checks']],
                         [hashlib.sha256(x['condition'].encode()).hexdigest() for x in decision['checks']])
        self.assertEqual(state['attempt']['decision'],decision)
        self.assertEqual(state['attempt']['candidate'],candidate)
        full=copy.deepcopy(facts);digest=full.pop('full_snapshot_sha256');full.pop('snapshot_format')
        full['candidate']=candidate;full['decision']=decision
        self.assertEqual(hashlib.sha256(encoded(full)).hexdigest(),digest)

    def test_chinese_feedback_fits_durable_p_record(self):
        self.check_unicode_feedback('界'*500)

    def test_non_bmp_feedback_fits_durable_p_record(self):
        self.check_unicode_feedback('🧪'*250)

    def legacy_feedback(self, lost_ack):
        state,_,_=self.accepted_conditions('界'*400)
        use=state['knowledge_calls'][state['attempt']['knowledge_use']]['request']['body']
        notes='Previously retained complete UTF-8 snapshot'
        material=self.full_material(self.p.identity['store_id'],state,use,notes)
        original=self.p.execute
        def execute(request, **kw):
            if request['operation']=='knowledge_ack':
                raise OSError('synthetic P acknowledgement failure')
            return original(request,**kw)
        # Retain the earlier full-text format through the public P/K commands.
        with patch('control_production_plane.knowledge.feedback_material',return_value=material):
            if lost_ack:
                with patch.object(self.p,'execute',side_effect=execute):
                    with self.assertRaises(OSError):self.a.feedback('example',1,notes)
            else:
                with patch.object(self.a,'_open',return_value=self.k),patch.object(self.k,'execute',side_effect=OSError('synthetic pre-write failure')):
                    with self.assertRaises(OSError):self.a.feedback('example',1,notes)
        cid=key(self.p.identity['store_id'],state,'complete_use')
        pending=self.p.get_run('example')['knowledge_calls'][cid]
        self.assertEqual(pending['state'],'PENDING')
        self.assertNotIn('snapshot_format',json.loads(pending['request']['body']['material']['text']))
        reopened=KnowledgeAdapter(open_store(self.p.root),self.config)
        if lost_ack:
            with patch.object(reopened,'_open',return_value=self.k),patch.object(self.k,'execute',side_effect=AssertionError('must recover original K receipt')):
                reopened.feedback('example',1,notes)
        else:
            reopened.feedback('example',1,notes)
        committed=self.p.get_run('example')['knowledge_calls'][cid]
        self.assertEqual(committed['state'],'COMMITTED')
        self.assertEqual(committed['request'],pending['request'])
        read=self.k.read_material(committed['receipt']['result']['material_id'])
        self.assertTrue(read['content_verified'])
        self.assertEqual(base64.b64decode(read['entries'][0]['data']),material['text'].encode())
        receipt=self.k.get_operation(committed['request']['scope'],'complete_use',cid)['receipt']
        self.assertEqual(receipt,committed['receipt'])

    def test_legacy_full_snapshot_prewrite_failure_resumes_original_bytes(self):
        self.legacy_feedback(False)

    def test_legacy_full_snapshot_lost_ack_reads_original_receipt(self):
        self.legacy_feedback(True)

    def test_invalid_query_context_rejected_before_opening_k(self):
        with patch.object(self.a,'_open',side_effect=AssertionError('invalid input must be rejected locally')):
            with self.assertRaises(ControlError):self.a.query('guidance',{'Invalid':'key'})
