"""Legacy review/checkpoint upgrade without lost work or a new review loop."""
import importlib.util
from pathlib import Path
from contextlib import ExitStack
import tempfile
import threading
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('legacy_review_controller',str(Path(__file__).resolve().parents[1]/'autonomy-controller.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
SHA='a'*40
NEW='b'*40
URL='https://github.com/org/demo/issues/68'

class ReviewTransition(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        root=Path(tmp.name)
        self.w=m.ProjectWorker({'name':'demo','repo':'org/demo','directory':str(root/'repo'),
                                'project_owner':'org','project_number':2},
                               {'state_dir':str(root/'state'),'opencode':'oc','wrapper':'wrapper'},
                               threading.Event(),threading.Lock())
        self.w.pdir.mkdir(parents=True)
        self.pr={'number':94,'state':'open','base':{'ref':'main'},
                 'head':{'sha':SHA},'body':'CI runs production Docker','draft':False}
        self.prompts=[]

    def checkpoint(self,phase='review',feedback=None):
        cp={'kind':'issue','number':68,'url':URL,'phase':phase,
            'pr_number':94,'retry_same_task':True}
        if feedback:cp['review_feedback']=feedback
        self.w.save_checkpoint(cp)
        return cp

    def patches(self):
        stack=ExitStack();self.addCleanup(stack.close)
        stack.enter_context(patch.object(self.w,'related_pr',side_effect=lambda *a:dict(self.pr)))
        stack.enter_context(patch.object(m,'pr_data',side_effect=lambda *a:dict(self.pr)))
        stack.enter_context(patch.object(self.w,'checkout_state',return_value=('main',False)))
        stack.enter_context(patch.object(m,'command',return_value=''))
        def agent(prompt,*args,**kwargs):
            self.prompts.append(prompt)
            return 0,'legacy or new OMO session completed'
        stack.enter_context(patch.object(self.w,'run_wrapper',side_effect=agent))
        return stack

    def test_old_review_checkpoint_becomes_fresh_omo_workflow(self):
        self.checkpoint();self.patches()
        self.w._cycle('issue',68,URL,None)
        cp=m.json_read(self.w.cycle_state)
        self.assertEqual(cp['phase'],'ci')
        self.assertNotIn('reviewed_sha',cp)
        self.assertNotIn('legacy_review_resume',cp)
        self.assertEqual(len(self.prompts),1)
        self.assertIn('former standalone review',self.prompts[0])
        self.assertIn('`oracle`',self.prompts[0])
        self.assertIn('`fixer`',self.prompts[0])

    def test_old_seven_failures_not_respected_after_review_migration(self):
        self.checkpoint();self.patches()
        m.json_atomic(self.w.retries_file,{
            'issue:68':{'failures':7,'until':9999999999,
                        'reason':'Independent review incomplete or rejected'},
            'issue:99':{'failures':2,'until':10000,'reason':'Other error'}})
        with patch.object(self.w,'_cycle') as cycle,patch.object(self.w,'checkout_neutral',return_value=True),patch.object(self.w,'quota_until',return_value=0):
            self.w.one_iteration()
        cycle.assert_called_once_with('issue',68,URL,None)
        self.assertEqual(m.json_read(self.w.cycle_state)['phase'],'code')
        self.assertNotIn('issue:68',self.w.retry_map())
        self.assertIn('issue:99',self.w.retry_map())

    def test_migration_preserves_non_review_retries(self):
        self.checkpoint();m.json_atomic(self.w.retries_file,{
            'issue:68':{'failures':2,'until':9999999999,'reason':'OpenCode exit 75'}})
        self.w.migrate_legacy_review(m.json_read(self.w.cycle_state))
        self.assertIn('issue:68',self.w.retry_map())

    def test_legacy_pending_review_is_drained_before_new_agent_work(self):
        self.checkpoint()
        m.json_atomic(self.w.pending,{'status':'resuming','session_id':'ses-review'})
        self.patches()
        with patch.object(self.w,'run_wrapper',side_effect=lambda prompt,*args,**kw:(self.prompts.append(prompt),self.w.pending.unlink(),(0,'Old read-only reviewer output'))[-1]):
            self.w._cycle('issue',68,URL,None)
        saved=m.json_read(self.w.cycle_state)
        self.assertEqual(saved['phase'],'code')
        self.assertNotIn('legacy_review_resume',saved)
        self.assertNotIn('ready_sha',saved)
        self.w._cycle('issue',68,URL,None)
        self.assertEqual(m.json_read(self.w.cycle_state)['phase'],'ci')
        self.assertEqual(len(self.prompts),2)

    def test_review_feedback_survives_migration_into_prompt(self):
        self.checkpoint(feedback='Missing real Docker image test')
        self.patches()
        self.w._cycle('issue',68,URL,None)
        self.assertIn('Missing real Docker image test',self.prompts[0])
        self.assertEqual(m.json_read(self.w.cycle_state)['phase'],'ci')

    def test_description_only_update_is_allowed(self):
        self.checkpoint('code',feedback='False Docker claim')
        self.patches()
        def agent(prompt,*a,**k):
            self.pr['body']='Production Docker image not built by current CI'
            return 0,'oracle reviewed wording correction'
        with patch.object(self.w,'run_wrapper',side_effect=agent):
            self.w._cycle('issue',68,URL,None)
        self.assertEqual(m.json_read(self.w.cycle_state)['phase'],'ci')

    def test_new_code_commit_is_allowed(self):
        self.checkpoint('code',feedback='Fix Docker test')
        self.patches()
        with patch.object(self.w,'run_wrapper',side_effect=lambda *a,**k:(self.pr['head'].__setitem__('sha',NEW),(0,'done'))[-1]):
            self.w._cycle('issue',68,URL,None)
        self.assertEqual(m.json_read(self.w.cycle_state)['phase'],'ci')

    def test_migration_idempotent(self):
        self.checkpoint();first=self.w.migrate_legacy_review(m.json_read(self.w.cycle_state))
        second=self.w.migrate_legacy_review(m.json_read(self.w.cycle_state))
        self.assertEqual(first,second)

    def test_no_custom_verdict_token_survives(self):
        s=(Path(__file__).resolve().parents[1]/'autonomy-controller.py').read_text()
        self.assertNotIn('AUTOCONTROLLER_REVIEW_PASS',s)
        self.assertNotIn('AUTOCONTROLLER_REVIEW_FAIL',s)

if __name__=='__main__':unittest.main()
