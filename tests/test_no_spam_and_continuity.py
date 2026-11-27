"""Failures/reboots must not spam, and safe independent work must continue."""
import importlib.util
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

SPEC=importlib.util.spec_from_file_location('ac_continuity',str(Path(__file__).resolve().parents[1]/'autonomy-controller.py'))
m=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


class Continuity(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name)
        self.base.joinpath('checkout').mkdir()
        self.cfg={'name':'app','repo':'org/app','directory':str(self.base/'checkout'),
                  'project_owner':'org','project_number':1}
        self.gl={'state_dir':str(self.base/'state'),'wrapper':str(self.base/'wrapper.py'),
                 'opencode':str(self.base/'opencode'),'poll_seconds':300}
        self.w=self.make_worker()

    def make_worker(self):
        w=m.ProjectWorker(self.cfg,self.gl,threading.Event(),threading.Lock())
        w.pdir.mkdir(parents=True,exist_ok=True)
        # Preserve old retry tests without actual network access. The new
        # independent PR verification is covered in test_quality_gates.py.
        w.related_pr=lambda *args: None
        return w

    def test_worker_failure_retry_is_persistent_across_restart(self):
        self.w.worker_pause(RuntimeError('GitHub offline'))
        self.assertGreater(self.make_worker().worker_ready(),90)
        self.w.worker_pause(RuntimeError('GitHub offline'))
        self.assertGreater(self.make_worker().worker_ready(),200)
        self.make_worker().worker_success()
        self.assertEqual(self.make_worker().worker_ready(),0)

    def test_same_notice_is_suppressed_across_restart(self):
        with patch.object(m.LOG,'warning') as w:
            self.w.notice('github','GitHub unavailable')
            self.w.notice('github','GitHub unavailable')
            self.make_worker().notice('github','GitHub unavailable')
            self.assertEqual(w.call_count,1)

    def test_cooldown_short_circuits_github_and_agent(self):
        self.w.quota_pause()
        with patch.object(m,'renovate_prs') as prs, patch.object(self.w,'run_wrapper') as run:
            wait=self.w.one_iteration()
            self.assertGreater(wait,1)
            prs.assert_not_called();run.assert_not_called()

    def test_neutral_checkpoint_resumes_target_before_renovate(self):
        checkpoint={'kind':'issue','number':28,'url':'https://github.com/org/app/issues/28'}
        m.json_atomic(self.w.cycle_state,checkpoint)
        with patch.object(self.w,'checkout_neutral',return_value=True), \
             patch.object(self.w,'quota_until',return_value=0), \
             patch.object(m,'renovate_prs') as prs, patch.object(self.w,'_cycle') as cycle:
            self.w.one_iteration()
            cycle.assert_called_once_with('issue',28,checkpoint['url'],None)
            prs.assert_not_called()

    def test_clean_incomplete_task_retries_later_but_does_not_stall_project(self):
        url='https://github.com/org/app/issues/19'
        with patch.object(self.w,'checkout_neutral',return_value=True), \
             patch.object(self.w,'run_wrapper',return_value=(0,'ok')), \
             patch.object(m,'issue_state',return_value='open'):
            self.w._cycle('issue',19,url,None)
        self.assertFalse(self.w.cycle_state.exists())
        self.assertGreater(self.w.retry_map()['issue:19']['until'],time.time())
        with patch.object(self.w,'checkout_neutral',return_value=True), \
             patch.object(self.w,'quota_until',return_value=0), \
             patch.object(m,'renovate_prs',return_value=[]), \
             patch.object(self.w.project,'items',return_value=[
                  {'number':19,'status':'In Progress','url':url,'item_id':'X'},
                  {'number':20,'status':'Backlog','url':'https://github.com/org/app/issues/20','item_id':'Y'}]), \
             patch.object(self.w,'select_next_issue') as select:
            self.w.one_iteration()
            select.assert_called_once()
            self.assertEqual([x['number'] for x in select.call_args.args[0]],[20])

    def test_dirty_incomplete_task_never_advances(self):
        with patch.object(self.w,'checkout_neutral',return_value=False), \
             patch.object(self.w,'run_wrapper',return_value=(0,'ok')), \
             patch.object(m,'issue_state',return_value='open'):
            self.w._cycle('issue',19,'https://github.com/org/app/issues/19',None)
        self.assertTrue(self.w.cycle_state.exists())
        with patch.object(self.w,'checkout_neutral',return_value=False), \
             patch.object(self.w,'quota_until',return_value=0), \
             patch.object(m,'renovate_prs') as prs, patch.object(self.w,'_cycle') as cycle:
            # The retry is active so there is no new agent call or unrelated PR.
            self.w.one_iteration()
            prs.assert_not_called(); cycle.assert_not_called()

    def test_quota_error_before_session_remembers_original_task_across_restart(self):
        url='https://github.com/org/app/issues/23'
        with patch.object(self.w,'checkout_neutral',return_value=True), \
             patch.object(self.w,'run_wrapper',return_value=(78,'429 rate limit exceeded')), \
             patch.object(m,'issue_state',return_value='open'):
            self.w._cycle('issue',23,url,None)
        self.assertEqual(m.json_read(self.w.cycle_state)['number'],23)
        self.assertTrue(m.json_read(self.w.cycle_state)['retry_same_task'])
        second=self.make_worker()
        with patch.object(second,'quota_until',return_value=0), \
             patch.object(second,'checkout_neutral',return_value=True), \
             patch.object(second,'_cycle') as cycle, patch.object(m,'renovate_prs') as prs:
            second.one_iteration()
            cycle.assert_called_once_with('issue',23,url,None)
            prs.assert_not_called()

    def test_successful_cli_mentioning_http_429_is_not_ai_quota(self):
        # OAuth rate-limiting tests and source output legitimately mention 429.
        # A successful coding cycle must proceed to GitHub CI, not enter an AI
        # provider cooldown simply because its text contains that status code.
        url='https://github.com/org/app/issues/23'
        pr={'number':99,'state':'open','merged_at':None,'base':{'ref':'main'},'draft':False}
        with patch.object(self.w,'checkout_neutral',return_value=True), \
             patch.object(self.w,'related_pr',return_value=pr), \
             patch.object(self.w,'run_wrapper',return_value=(0,'HTTP 429 Too Many Requests. Rate limit exceeded in OAuth regression test.')), \
             patch.object(self.w,'quota_pause') as pause:
            self.w._cycle('issue',23,url,None)
        self.assertEqual(m.json_read(self.w.cycle_state)['phase'],'ci')
        self.assertFalse((self.w.state_home/m.SHARED_QUOTA_FILE).exists())
        pause.assert_not_called()

    def test_closed_issue_without_pr_evidence_never_spends_model_tokens(self):
        url='https://github.com/org/app/issues/23'
        m.json_atomic(self.w.cycle_state,{'kind':'issue','number':23,'url':url})
        with patch.object(self.w,'checkout_neutral',return_value=True), \
             patch.object(m,'issue_state',return_value='closed'), \
             patch.object(self.w,'run_wrapper') as run:
            self.w._cycle('issue',23,url,None)
        run.assert_not_called()
        self.assertFalse(self.w.cycle_state.exists())
        self.assertIn('issue:23',self.w.retry_map())

    def test_project_name_cannot_escape_state_directory(self):
        cfg={'projects':[{'name':'../../repositories/example-project', 'repo':'example-owner/example-project',
            'directory':'/home/example/repositories/example-project', 'project_owner':'example-owner',
            'project_number':1}]}
        with self.assertRaises(m.ControllerError):m.validate_config(cfg)

    def test_failed_clean_task_backoffs_and_releases_checkpoint(self):
        with patch.object(self.w,'checkout_neutral',return_value=True), \
             patch.object(self.w,'run_wrapper',return_value=(78,'Agent not found')), \
             patch.object(m,'issue_state',return_value='open'):
            self.w._cycle('issue',10,'https://github.com/org/app/issues/10',None)
        self.assertFalse(self.w.cycle_state.exists())
        self.assertGreater(self.w.retry_map()['issue:10']['until'],time.time())

    def test_pre_session_marker_salvage_is_retried_after_outage(self):
        url='https://github.com/org/app/issues/9'
        m.json_atomic(self.w.cycle_state,{'kind':'issue','number':9,'url':url})
        m.json_atomic(self.w.pending,{'status':'starting','title':'unique-guard-title'})
        calls=[]
        def fake_command(argv,**kwargs):
            calls.append(argv)
            self.w.pending.unlink()
            return ''
        with patch.object(self.w,'quota_until',return_value=0), \
             patch.object(m,'command',side_effect=fake_command), \
             patch.object(self.w,'_cycle') as cycle:
            self.w.one_iteration()
        self.assertFalse(self.w.pending.exists())
        self.assertTrue(calls)
        cycle.assert_called_once_with('issue',9,url,None)

    def test_unverified_marker_is_never_deleted(self):
        m.json_atomic(self.w.pending,{'status':'starting','title':'unique'})
        with patch.object(m,'command',side_effect=m.ControllerError('API unavailable')):
            self.assertFalse(self.w.clear_verified_unstarted_marker())
        self.assertTrue(self.w.pending.exists())

    def test_unresolved_real_session_is_not_deleted(self):
        m.json_atomic(self.w.pending,{'status':'resuming','session_id':'ses01'})
        with patch.object(m,'command') as cmd:
            self.assertFalse(self.w.clear_verified_unstarted_marker())
            cmd.assert_not_called()
        self.assertTrue(self.w.pending.exists())

    def test_unsafe_process_group_blocks_tasks_until_gone(self):
        boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        m.json_atomic(self.w.unsafe_file,{'pgid':999999,'boot_id':boot})
        with patch.object(self.w,'group_alive',return_value=True):
            with self.assertRaises(m.FatalWorkerError):self.w.ensure_unsafe_group_gone()
        with patch.object(self.w,'group_alive',return_value=False):
            self.w.ensure_unsafe_group_gone()
        self.assertFalse(self.w.unsafe_file.exists())

    def test_installed_default_paths_stay_outside_all_repos(self):
        self.assertEqual(str(m.INSTALL_ROOT),str(Path.home()/'scripts/autocontroller'))
        self.assertEqual(str(m.DEFAULT_STATE),str(m.INSTALL_ROOT/'state'))
        from json import loads
        cfg=loads((Path(__file__).resolve().parents[1]/'config.example.json').read_text())
        self.assertEqual(cfg['projects'], [])
        self.assertNotIn('state_dir',cfg)
        self.assertNotIn('wrapper',cfg)


if __name__=='__main__':unittest.main()
