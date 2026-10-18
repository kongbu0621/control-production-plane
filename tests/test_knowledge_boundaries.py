"""Boundary regressions using retained P intents and the public K contract."""
import copy
import json
import unittest
from unittest.mock import patch

from control_production_plane import ControlError,model
from control_production_plane.knowledge import KnowledgeAdapter,encoded,feedback_material,key
from test_core import command
import test_knowledge as fixtures
HAS_K=fixtures.HAS_K


@unittest.skipUnless(HAS_K, 'optional K and Ledger packages required')
class KnowledgeBoundaryTests(unittest.TestCase):
    setUp=fixtures.KnowledgeTests.setUp
    op=fixtures.KnowledgeTests.op
    queue=fixtures.KnowledgeTests.queue
    submit=fixtures.KnowledgeTests.submit

    def test_invalid_context_rejected_before_intent_then_same_attempt_can_continue(self):
        attempt=self.p.get_run('example')['attempt']['attempt_id']
        bad=dict(self.context, Invalid='locally invalid key')
        with self.assertRaises(ControlError):self.a.adopt('example',bad)
        self.assertNotIn('knowledge_calls',self.p.get_run('example'))
        self.a.adopt('example',self.context)
        self.assertEqual(self.p.get_run('example')['attempt']['attempt_id'],attempt)

    def test_cancel_during_receipt_lookup_blocks_new_adoption(self):
        original=self.k.get_operation
        def changed(scope,operation,request_id):
            if operation=='start_use':
                self.p.execute(command('cancel',dict(run_id='example',reason='Cancel while read-only lookup was pending')))
            return original(scope,operation,request_id)
        with patch.object(self.a,'_open',return_value=self.k),patch.object(self.k,'get_operation',side_effect=changed):
            with self.assertRaises(ControlError):self.a.adopt('example',self.context)
        call=next(iter(self.p.get_run('example')['knowledge_calls'].values()))
        with self.assertRaises(fixtures.KnowledgeError) as absent:
            original(call['request']['scope'],'start_use',call['request']['request_id'])
        self.assertEqual(absent.exception.code,'NOT_FOUND')

    def test_revision_during_receipt_lookup_keeps_old_use_unsent(self):
        original=self.k.get_operation
        def changed(scope,operation,request_id):
            if operation=='start_use':
                self.p.execute(command('revise',dict(run_id='example',expected_revision=1,plan=self.spec['plan'])))
            return original(scope,operation,request_id)
        with patch.object(self.a,'_open',return_value=self.k),patch.object(self.k,'get_operation',side_effect=changed):
            with self.assertRaises(ControlError):self.a.adopt('example',self.context)
        call=next(iter(self.p.get_run('example')['knowledge_calls'].values()))
        with self.assertRaises(fixtures.KnowledgeError) as absent:
            original(call['request']['scope'],'start_use',call['request']['request_id'])
        self.assertEqual(absent.exception.code,'NOT_FOUND')
        self.a.adopt('example',self.context)
        self.assertEqual(self.p.get_run('example')['revision'],2)

    def test_feedback_bound_covers_maximum_acceptance_conditions(self):
        import hashlib
        from control_production_plane import initialize
        self.p=initialize(self.root/'large-production')
        self.spec['goal']['acceptance']=[str(i)+':'+'x'*2040 for i in range(32)]
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
        self.a.feedback('example',1,'Maximum-size synthetic acceptance snapshot')
        state=self.p.get_run('example')
        call=state['knowledge_calls'][key(self.p.identity['store_id'],state,'complete_use')]
        material=call['request']['body']['material']
        self.assertLessEqual(len(material['text'].encode()),65536)
        read=self.k.read_material(call['receipt']['result']['material_id'])
        self.assertTrue(read['content_verified'])
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

    def test_context_shape_limits_and_unicode_reject_before_any_lookup(self):
        cases=[None,[],{'project':'example','bad':True},{'project':'example','bad':''},
               {'project':'example','bad':'\x00'}, {'project':'example','bad':'界'*86},
               {str(i):'x' for i in range(17)}, {'project':'example','bad':'\ud800'}]
        for context in cases:
            with self.subTest(context=repr(context)),patch.object(self.a,'_open',side_effect=AssertionError('must reject locally')):
                with self.assertRaises(ControlError):self.a.adopt('example',context)
        self.assertNotIn('knowledge_calls',self.p.get_run('example'))

    def test_maximum_k_conditions_fit_p_selection_and_keep_exact_binding(self):
        conditions={'project':'example',**{'field_'+str(i):'x'*256 for i in range(15)}}
        proposed=self.k.execute(encoded(dict(schema='operational-knowledge-v1',scope='example:reviewer',operation='propose',request_id='request:wide-propose',
            body=dict(proposal_id='proposal:wide',kind='guidance',content='Wide condition fixture',conditions=conditions,material_ids=['material:example']))))['receipt']['result']
        version=self.k.execute(encoded(dict(schema='operational-knowledge-v1',scope='example:reviewer',operation='review',request_id='request:wide-review',
            body=dict(candidate_id=proposed['candidate_id'],candidate_sha256=proposed['candidate_sha256'],decision='ACCEPT',reviewer='example:reviewer',reason='Bounded condition fixture',evidence_material_ids=['material:example']))))['receipt']['result']['version_id']
        selected=self.a.selection(version,conditions)
        self.assertEqual(selected['conditions'],conditions)
        plan=copy.deepcopy(self.spec['plan']);plan['knowledge']['items']=[selected['item']]
        self.p.execute(command('revise',dict(run_id='example',expected_revision=1,plan=plan)))
        self.a.adopt('example',conditions)
        state=self.p.get_run('example');use=state['knowledge_calls'][state['attempt']['knowledge_use']]
        self.assertEqual(use['request']['body']['context'],conditions)
        self.assertTrue(selected['item']['scope'].startswith('conditions-sha256:'))

    def test_cancel_after_lookup_before_k_ack_preserves_receipt_but_blocks_submission(self):
        original=self.k.execute
        def execute(raw):
            self.p.execute(command('cancel',dict(run_id='example',reason='Cancel concurrent with in-flight K registration')))
            return original(raw)
        with patch.object(self.a,'_open',return_value=self.k),patch.object(self.k,'execute',side_effect=execute):
            self.a.adopt('example',self.context)
        state=self.p.get_run('example');call=state['knowledge_calls'][state['attempt']['knowledge_use']]
        self.assertEqual(call['state'],'COMMITTED')
        self.assertTrue(state['cancel'])
        with self.assertRaises(ControlError):self.queue('prepare')
        self.assertEqual(state['counters']['attempts'],0)
        self.a.feedback('example',1,'Registered use was cancelled before execution')
        state=self.p.get_run('example');feedback=state['knowledge_calls'][key(self.p.identity['store_id'],state,'complete_use')]
        self.assertEqual(feedback['request']['body']['material']['claimed_outcome'],'UNKNOWN')
        self.assertFalse(json.loads(feedback['request']['body']['material']['text'])['submission_recorded'])

    def test_small_feedback_remains_byte_compatible_with_previous_contract(self):
        self.a.adopt('example',self.context)
        state=self.p.get_run('example');a=state['attempt'];use=state['knowledge_calls'][a['knowledge_use']]['request']['body']
        expected=dict(run_id=state['run_id'],revision=state['revision'],attempt_id=a['attempt_id'],operation_id=a['operation_id'],
            goal_digest=state['goal_digest'],plan_sha256=model.digest(state['plan']),submission_recorded=a['submission_recorded'],
            observation=a['observation'],candidate=a.get('candidate'),decision=a.get('decision'),cancel=state['cancel'],maintenance_stop=a.get('maintenance_stop'),notes='same snapshot')
        self.assertEqual(feedback_material(self.p.identity['store_id'],state,use,'same snapshot')['text'].encode(),encoded(expected))
