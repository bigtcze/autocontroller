"""Tests for production hazards missed by the initial 26 happy-path tests."""
import importlib.util
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import subprocess
import sys
import time

spec=importlib.util.spec_from_file_location('reviewed_controller',str(Path(__file__).resolve().parents[1]/'autonomy-controller.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


def issue(n,status='Backlog'):
    return {'number':n,'status':status,'item_id':f'pvti-{n}','title':'test','url':f'https://github.com/own/repo/issues/{n}'}


class Regressions(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.repo=Path(self.tmp.name)/'repo'
        self.repo.mkdir()
        subprocess.run(['git','init','-q','-b','main',str(self.repo)],check=True)
        cfg={'name':'example','repo':'own/repo','directory':str(self.repo),'project_owner':'own','project_number':1}
        global_cfg={'state_dir':str(Path(self.tmp.name)/'state'),'wrapper':str(Path(self.tmp.name)/'wrapper.py'),'opencode':str(Path(self.tmp.name)/'opencode')}
        self.w=m.ProjectWorker(cfg,global_cfg,threading.Event(),threading.Lock())
        self.w.pdir.mkdir(parents=True)

    def test_unset_project_status_is_automatic_backlog(self):
        item=m.normalize_project_item({'content':{'type':'Issue','url':'https://github.com/own/repo/issues/1'}},'own/repo')
        self.assertEqual(item['status'],'Backlog')

    def test_pagination_fail_closed(self):
        with patch.object(m,'gh_json',return_value={'items':[{'status':'Backlog','content':{'type':'Issue','url':'https://github.com/own/repo/issues/1'}}],'totalCount':2}):
            with self.assertRaises(m.ControllerError):self.w.project.items()

    def test_retry_item_omitted_from_agent_candidates(self):
        m.json_atomic(self.w.retries_file,{'issue:1':{'until':time.time()+500}})
        with patch.object(self.w,'quota_until',return_value=0), \
             patch.object(m,'renovate_prs',return_value=[]), \
             patch.object(self.w.project,'items',return_value=[issue(1)]), \
             patch.object(self.w,'select_next_issue') as select:
            self.w.one_iteration()
            select.assert_not_called()

    def test_pending_error_backoff_is_obeyed(self):
        m.json_atomic(self.w.cycle_state,{'kind':'issue','number':1,'url':issue(1)['url']})
        m.json_atomic(self.w.retries_file,{'issue:1':{'until':time.time()+500}})
        m.json_atomic(self.w.pending,{'status':'resuming','session_id':'ses-test'})
        with patch.object(self.w,'quota_until',return_value=0),patch.object(self.w,'_cycle') as cycle:
            value=self.w.one_iteration()
            self.assertGreater(value,100)
            cycle.assert_not_called()

    def test_pending_recovers_when_retry_due(self):
        m.json_atomic(self.w.cycle_state,{'kind':'issue','number':1,'url':issue(1)['url']})
        m.json_atomic(self.w.pending,{'status':'resuming','session_id':'ses-test'})
        with patch.object(self.w,'quota_until',return_value=0),patch.object(self.w,'_cycle') as cycle:
            self.w.one_iteration();cycle.assert_called_once()

    def test_dirty_checkout_requires_checkpoint(self):
        (self.repo/'unfinished.txt').write_text('work')
        with patch.object(self.w,'quota_until',return_value=0),patch.object(m,'renovate_prs') as prs:
            with self.assertRaises(m.ControllerError):self.w.one_iteration()
            prs.assert_not_called()

    def test_dirty_checkout_resumes_same_target_only(self):
        (self.repo/'unfinished.txt').write_text('work')
        m.json_atomic(self.w.cycle_state,{'kind':'issue','number':17,'url':issue(17)['url']})
        with patch.object(self.w,'quota_until',return_value=0),patch.object(self.w,'_cycle') as cycle,patch.object(m,'renovate_prs') as prs:
            self.w.one_iteration();cycle.assert_called_once_with('issue',17,issue(17)['url'],None)
            prs.assert_not_called()

    def test_nondefault_branch_requires_same_checkpoint(self):
        subprocess.run(['git','-C',str(self.repo),'checkout','-q','-b','feature'],check=True)
        m.json_atomic(self.w.cycle_state,{'kind':'renovate','number':2,'url':'https://github.com/own/repo/pull/2'})
        with patch.object(self.w,'quota_until',return_value=0),patch.object(self.w,'_cycle') as cycle:
            self.w.one_iteration();cycle.assert_called_once_with('renovate',2,'https://github.com/own/repo/pull/2',None)

    def test_pre_session_quota_retry_keeps_exact_target(self):
        m.json_atomic(self.w.cycle_state,{'kind':'issue','number':9,'url':issue(9)['url'],'retry_same_task':True})
        with patch.object(self.w,'quota_until',return_value=0),patch.object(self.w,'_cycle') as cycle,patch.object(m,'renovate_prs') as prs:
            self.w.one_iteration();cycle.assert_called_once_with('issue',9,issue(9)['url'],None);prs.assert_not_called()

    def test_quota_clear_must_preserve_concurrent_newer_pause(self):
        p=self.w.state_home/m.SHARED_QUOTA_FILE
        m.json_atomic(p,{'recorded_at':400,'until':1000})
        self.w.quota_clear(started=300)
        self.assertTrue(p.exists())

    def test_quota_clear_can_clear_old_pause_on_success(self):
        p=self.w.state_home/m.SHARED_QUOTA_FILE
        m.json_atomic(p,{'recorded_at':100,'until':1000})
        self.w.quota_clear(started=300)
        self.assertFalse(p.exists())

    def test_pr_open_vs_closed_unmerged_vs_merged(self):
        for response,expected in [({'state':'open','merged_at':None},'open'),({'state':'closed','merged_at':None},'closed_unmerged'),({'state':'closed','merged_at':'2026-11-23T15:00:00Z'},'merged')]:
            with self.subTest(expected=expected),patch.object(m,'gh_json',return_value=response):
                self.assertEqual(m.pull_request_state('own/repo',8),expected)

    def test_unexpected_pr_state_fails_closed(self):
        with patch.object(m,'gh_json',return_value={'state':'maybe','merged_at':None}):
            with self.assertRaises(m.ControllerError):m.pull_request_state('own/repo',7)

    def test_closed_unmerged_pr_is_dismissed_not_successfully_merged(self):
        pr={'number':8,'state':'closed','merged_at':None,'base':{'ref':'main'}}
        with patch.object(self.w,'related_pr',return_value=pr),patch.object(self.w,'run_wrapper') as run, \
             patch.object(self.w,'quota_clear') as clear,self.assertLogs('autonomous-dev',level='INFO') as logs:
            self.w._cycle('renovate',8,'https://github.com/own/repo/pull/8',None)
        self.assertIn('WITHOUT merge',' '.join(logs.output))
        self.assertFalse(self.w.cycle_state.exists())
        run.assert_not_called();clear.assert_not_called()

    def test_open_pr_proceeds_to_review_but_not_done(self):
        pr={'number':8,'state':'open','merged_at':None,'base':{'ref':'main'}}
        with patch.object(self.w,'related_pr',return_value=pr),patch.object(self.w,'run_wrapper',return_value=(0,'')), \
             patch.object(self.w,'quota_clear') as clear:
            self.w._cycle('renovate',8,'https://github.com/own/repo/pull/8',None)
        self.assertEqual(m.json_read(self.w.cycle_state)['phase'],'ci')
        clear.assert_called_once()

    def test_dirty_checkout_after_verified_merge_prevents_done(self):
        pr={'number':8,'state':'closed','merged_at':'2026-11-24T10:00:00Z',
            'merge_commit_sha':'b'*40,'base':{'ref':'main'}}
        m.json_atomic(self.w.cycle_state,{'kind':'renovate','number':8,'url':'https://github.com/own/repo/pull/8',
                                          'phase':'postmerge','pr_number':8,'merge_commit':'b'*40,'reviewed_sha':'a'*40})
        (self.repo/'temp.txt').write_text('uncommitted')
        with patch.object(self.w,'related_pr',return_value=pr),patch.object(m,'main_ci_state',return_value='passed'), \
             patch.object(self.w.project,'set_status') as status:
            with self.assertRaises(m.ControllerError):
                self.w._cycle('renovate',8,'https://github.com/own/repo/pull/8',None)
        status.assert_not_called()
        self.assertTrue(self.w.cycle_state.exists())

    def test_nonquota_error_preserves_pending_and_defers_retry(self):
        m.json_atomic(self.w.pending,{'status':'resuming','session_id':'ses-test'})
        with patch.object(self.w,'related_pr',return_value={'number':5,'state':'open','merged_at':None,'base':{'ref':'main'}}),patch.object(self.w,'run_wrapper',return_value=(78,'Agent not found: orchestrator')),patch.object(self.w,'quota_pause') as quota:
            self.w._cycle('renovate',5,'https://github.com/own/repo/pull/5',None)
            self.assertTrue(self.w.pending.exists())
            self.assertGreater(self.w.retry_map()['renovate:5']['until'],time.time()+500)
            quota.assert_not_called()

    def test_failed_before_session_quota_marker_retry_same_task(self):
        m.json_atomic(self.w.pending,{'status':'resuming','session_id':'ses-test'})
        with patch.object(self.w,'related_pr',return_value=None),patch.object(m,'issue_state',return_value='open'),patch.object(self.w,'run_wrapper',return_value=(78,'HTTP 429 rate limit exceeded')),patch.object(m,'command',return_value=''),patch.object(self.w,'checkout_neutral',return_value=True),patch.object(self.w,'quota_pause') as quota:
            self.w._cycle('issue',9,issue(9)['url'],None)
            self.assertTrue(m.json_read(self.w.cycle_state)['retry_same_task'])
            quota.assert_called_once()

    def test_process_group_liveness_ignores_ended_process(self):
        child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(0.2)'],start_new_session=True)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        self.assertTrue(self.w.group_alive(child.pid))
        child.wait(timeout=4)
        self.assertFalse(self.w.group_alive(child.pid))

    def test_controller_state_is_outside_checkout(self):
        self.assertNotIn(self.repo,self.w.pdir.parents)


if __name__=='__main__':unittest.main()

class AgentSelectionTests(unittest.TestCase):
    def test_python_priority_is_not_an_issue_selector(self):
        self.assertFalse(hasattr(m,'pick_issue'))
        selection=m.selection_prompt('own/repo',[issue(1,'In Progress'),
                             {**issue(9),'priority':'P0'}],True)
        self.assertIn('Choose based on engineering judgment',selection)
        self.assertIn('not issue numbers',selection)
