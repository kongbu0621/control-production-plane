import copy,json,sqlite3,subprocess,sys,unittest,uuid
from unittest.mock import patch
from control_production_plane import ControlError,model
import test_core as core

class MaintenanceStopTests(unittest.TestCase):
    def setUp(self):
        self.fixture=core.CoreTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.store=self.fixture.store;self.fixture.prepare();self.action=self.fixture.queue('submit')
        self.store.perform(self.action,self.fixture.spec['binding'],lambda p:dict(response={'accepted':True},call_closed=True))
        self.store.execute(core.command('observe',self.fixture.observation(remote_state='RUNNING',outcome='UNKNOWN',exited=False)))
        state=self.store.get_run('example');attempt=state['attempt']
        self.proof=dict(run_id='example',attempt_id=attempt['attempt_id'],operation_id=attempt['operation_id'],
            request_digest=attempt['prepared_request']['request_digest'],binding_digest=model.digest(state['binding']),
            evidence_ref='demo:independent-stop-proof',future_start_blocked=True,tree_exited=True,writers_stopped=True,effect_scope='TASK_OWNED_COPY')
    def prove(self,key=None,**changes):return self.store.execute(core.command('confirm_stopped',dict(self.proof,**changes),key))
    def other(self):
        spec=core.sample('replacement');self.store.execute(core.command('create',spec));state=self.store.get_run('replacement');action=str(uuid.uuid4())
        self.store.execute(core.command('queue',dict(run_id='replacement',attempt_id=state['attempt']['attempt_id'],action_id=action,kind='prepare')))
        return spec,action
    def test_proof_allows_separate_run_without_rewriting_unknown_or_refunding(self):
        before=self.store.get_run('example');oldaction=self.store.get_action(self.action);spec,action=self.other();calls=[]
        with self.assertRaises(ControlError):self.store.perform(action,spec['binding'],lambda p:calls.append(p))
        self.assertEqual(calls,[]);self.prove('stop:first')
        after=self.store.get_run('example');expected=copy.deepcopy(before);expected['attempt']['maintenance_stop']=self.proof
        self.assertEqual(after,expected);self.assertEqual(self.store.get_action(self.action),oldaction)
        self.store.perform(action,spec['binding'],core.ready)
        current=self.store.get_run('replacement');self.prove('stop:again');self.assertTrue(self.prove('stop:first')['replayed'])
        self.assertEqual(current,self.store.get_run('replacement'))
        with self.assertRaises(ControlError):self.fixture.queue('submit')
        with self.assertRaises(ControlError):self.store.execute(core.command('revise',dict(run_id='example',expected_revision=1,plan=before['plan'])))
        self.assertEqual(self.store.recover('example')['next_step'],'REVIEW_STOPPED_RESULT_GAPS')
    def test_exact_binding_and_complete_proof_are_required(self):
        before=self.store.get_run('example')
        for changes in ({'operation_id':str(uuid.uuid4())},{'attempt_id':str(uuid.uuid4())},{'request_digest':'0'*64},
                        {'binding_digest':'0'*64},{'effect_scope':'EXTERNAL'},{'writers_stopped':False},{'tree_exited':1},
                        {'future_start_blocked':False},{'evidence_ref':''}):
            with self.subTest(changes=changes),self.assertRaises(ControlError):self.prove(**changes)
            self.assertEqual(self.store.get_run('example'),before)
        self.prove()
        with self.assertRaises(ControlError):self.prove(evidence_ref='demo:other')
    def test_unclosed_call_blocks_proof_until_closed(self):
        query=self.fixture.queue('query');self.store.perform(query,self.fixture.spec['binding'],lambda p:dict(response={'status':'unknown'},call_closed=False))
        with self.assertRaises(ControlError):self.prove()
        self.store.execute(core.command('close_call',dict(action_id=query,evidence_ref='demo:exit-eof')))
        self.prove();self.assertIn('maintenance_stop',self.store.get_run('example')['attempt'])
    def test_rollback_and_cli_reopen_preserve_original_facts(self):
        before=self.store.get_run('example')
        with patch.object(self.store,'_save',side_effect=sqlite3.OperationalError('Synthetic failure')):
            with self.assertRaises(ControlError):self.prove('stop:atomic')
        self.assertEqual(self.store.get_run('example'),before)
        path=self.fixture.root/'stop-command.json';path.write_text(json.dumps(core.command('confirm_stopped',self.proof,'stop:atomic')))
        p=subprocess.run([sys.executable,'-m','control_production_plane.cli','--store',str(self.fixture.path),'write','--request',str(path)],capture_output=True,timeout=10)
        self.assertEqual(p.returncode,0,p.stderr);self.assertTrue(self.prove('stop:atomic')['replayed'])
        self.assertEqual(self.store.get_run('example')['attempt']['observation'],before['attempt']['observation'])
    def test_cancellation_and_later_query_do_not_erase_proof_or_allow_original_replay(self):
        self.store.execute(core.command('cancel',dict(run_id='example',reason='Retain stopped unknown')));self.prove()
        observation=self.fixture.observation(remote_state='RUNNING',outcome='UNKNOWN',exited=False)
        self.store.execute(core.command('observe',observation))
        self.assertEqual(self.store.get_run('example')['attempt']['maintenance_stop'],self.proof)
        with self.assertRaises(ControlError):self.fixture.queue('prepare')
        spec,action=self.other();self.store.perform(action,spec['binding'],core.ready)
