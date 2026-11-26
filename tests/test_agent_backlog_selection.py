"""Agent-owned backlog selection; no Python priority/dependency ranking."""
import importlib.util
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location('selection_controller',str(ROOT/'autonomy-controller.py'))
m=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


def issue(n,title=None,status='Todo',priority='P3'):
    return {'item_id':f'ITEM{n}','number':n,'title':title or f'Task {n}',
            'url':f'https://github.com/org/repo/issues/{n}',
            'status':status,'priority':priority}


class AgentBacklogSelection(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.worker=m.ProjectWorker({'name':'demo','repo':'org/repo',
                    'directory':'/home/example/repositories/repo','project_owner':'org',
                    'project_number':2},
                    {'state_dir':temp.name,'poll_seconds':300,
                     'wrapper':'/tmp/guard','opencode':'/tmp/opencode'},
                     threading.Event(),threading.Lock())
        w=self.worker
        for name in ('ensure_unsafe_group_gone','checkout_neutral'):
            mock=patch.object(w,name,return_value=True)
            mock.start(); self.addCleanup(mock.stop)
        mock=patch.object(w,'quota_until',return_value=0)
        mock.start();self.addCleanup(mock.stop)

    def run_one(self,cards):
        w=self.worker
        with patch.object(m,'renovate_prs',return_value=[]), \
             patch.object(w.project,'items',return_value=cards), \
             patch.object(w,'check_maintenance',return_value=None):
            return w.one_iteration()

    def test_orchestrator_can_choose_lower_priority_higher_number(self):
        w=self.worker
        cards=[issue(10,'High priority',status='In Progress',priority='P0'),
               issue(80,'Actual prerequisite',priority='P3')]
        with patch.object(w,'run_wrapper',return_value=(0,'AUTOCONTROLLER_SELECT_ISSUE #80')) as run, \
             patch.object(m,'issue_state',return_value='open'), \
             patch.object(w.project,'set_status') as status:
            self.run_one(cards)
        self.assertEqual(m.json_read(w.cycle_state)['number'],80)
        self.assertEqual(m.json_read(w.cycle_state)['phase'],'code')
        self.assertEqual(status.call_args.args[0]['number'],80)
        self.assertEqual(status.call_args.args[1],'In Progress')
        prompt=run.call_args.args[0]
        self.assertIn('Actual prerequisite',prompt)
        self.assertIn('High priority',prompt)
        self.assertIn('implicit dependencies',prompt)
        self.assertIn('READ-ONLY TRIAGE',prompt)

    def test_selected_task_resumes_after_restart_without_reselecting(self):
        w=self.worker
        with patch.object(w,'run_wrapper',return_value=(0,'AUTOCONTROLLER_SELECT_ISSUE #80')), \
             patch.object(m,'issue_state',return_value='open'), \
             patch.object(w.project,'set_status'):
            self.run_one([issue(80)])
        with patch.object(w,'_cycle') as cycle,patch.object(w,'select_next_issue') as selector:
            self.run_one([issue(10),issue(80)])
        cycle.assert_called_once_with('issue',80,issue(80)['url'],None)
        selector.assert_not_called()

    def test_invalid_selection_never_claims_unlisted_issue(self):
        w=self.worker
        with patch.object(w,'run_wrapper',return_value=(0,'AUTOCONTROLLER_SELECT_ISSUE #999')), \
             patch.object(w.project,'set_status') as status:
            self.run_one([issue(10)])
        status.assert_not_called()
        self.assertEqual(m.json_read(w.cycle_state)['kind'],'selection')
        self.assertIn('selection:0',w.retry_map())

    def test_closed_issue_cannot_be_selected_even_if_card_stays_backlog(self):
        w=self.worker
        with patch.object(w,'run_wrapper',return_value=(0,'AUTOCONTROLLER_SELECT_ISSUE #8')), \
             patch.object(m,'issue_state',return_value='closed'), \
             patch.object(w.project,'set_status') as status:
            self.run_one([issue(8)])
        status.assert_not_called()
        self.assertEqual(m.json_read(w.cycle_state)['kind'],'selection')

    def test_no_action_returns_to_idle_without_model_poll_spam(self):
        w=self.worker
        with patch.object(w,'run_wrapper',return_value=(0,'Nothing ready\nAUTOCONTROLLER_NO_ACTION')) as run, \
             patch.object(w,'selector_signature',return_value='unchanged') as signature:
            self.run_one([issue(10)])
            self.assertFalse(w.cycle_state.exists())
            self.assertEqual(m.json_read(w.selector_skip)['signature'],'unchanged')
            self.assertEqual(self.run_one([issue(10)]),300)
        run.assert_called_once()
        self.assertEqual(signature.call_count,2)

    def test_change_to_backlog_wakes_another_agent_decision(self):
        w=self.worker
        with patch.object(w,'run_wrapper',return_value=(0,'AUTOCONTROLLER_NO_ACTION')) as run, \
             patch.object(m,'gh_json',return_value=[]):
            self.run_one([issue(10)])
            self.run_one([issue(10)])  # Same Project and issues, no new AI.
            self.run_one([issue(10),issue(11)])  # New issue needs judgment.
        self.assertEqual(run.call_count,2)

    def test_existing_pending_selection_session_recovers_without_new_assignment(self):
        w=self.worker
        m.json_atomic(w.cycle_state,{'kind':'selection','number':0,'phase':'selection',
                                    'url':'https://github.com/org/repo'})
        m.json_atomic(w.pending,{'status':'resuming','session_id':'ses-demo'})
        with patch.object(w,'select_next_issue') as select,patch.object(m,'renovate_prs') as prs, \
             patch.object(w.project,'items',return_value=[issue(77)]):
            w.one_iteration()
        self.assertEqual(select.call_args.kwargs['checkpoint']['kind'],'selection')
        prs.assert_not_called()

    def test_selector_failure_cooldown_and_checkpoint_survive_restart(self):
        w=self.worker
        with patch.object(w,'run_wrapper',return_value=(78,'Agent not found')):
            self.run_one([issue(19)])
        self.assertEqual(m.json_read(w.cycle_state)['kind'],'selection')
        self.assertGreater(w.retry_map()['selection:0']['until'],time.time()+300)
        with patch.object(w,'run_wrapper') as run:
            self.run_one([issue(19)])
        run.assert_not_called()

    def test_no_work_no_agent(self):
        w=self.worker
        with patch.object(w,'run_wrapper') as run:
            self.assertEqual(self.run_one([]),300)
            self.assertEqual(self.run_one([issue(10,status='Completed')]),300)
        run.assert_not_called()

    def test_readonly_selection_if_dirty_refuses_and_preserves_checkpoint(self):
        w=self.worker
        with patch.object(w,'checkout_neutral',return_value=False),patch.object(w,'run_wrapper') as run:
            with self.assertRaises(m.ControllerError):
                w.select_next_issue([issue(10)])
        run.assert_not_called()
        self.assertEqual(m.json_read(w.cycle_state)['kind'],'selection')

    def test_selection_is_scoped_to_release_enabled_state(self):
        w=self.worker
        with patch.object(w,'run_wrapper') as run:
            self.assertEqual(self.run_one([issue(12,'Release v5.0.0')]),300)
        run.assert_not_called()

    def test_feature_release_metadata_survives_selection(self):
        w=self.worker
        w.release_cfg={'enabled':True}
        with patch.object(w,'run_wrapper',return_value=(0,'AUTOCONTROLLER_SELECT_ISSUE #12')), \
             patch.object(m,'issue_state',return_value='open'), \
             patch.object(w.project,'set_status'):
            self.run_one([issue(12,'Release v5.0.0')])
        self.assertEqual(m.json_read(w.cycle_state)['release_tag'],'v5.0.0')

    def test_issue_edit_wakes_triage_without_status_change(self):
        # A real content change (label) wakes triage even when the Project card and
        # status are untouched. A bare updated_at bump is NOT a wake (see outsider tests).
        w=self.worker
        board=[dict(issue(15),body_sha256='a'*64)]
        first=[{'number':15,'updated_at':'2026-11-25T08:00:00Z','state':'open','labels':[]}]
        second=[{'number':15,'updated_at':'2026-11-25T12:00:00Z','state':'open','labels':[{'name':'bug'}]}]
        with patch.object(m,'gh_json',side_effect=[first,second]):
            before=w.selector_signature(board)
            after=w.selector_signature(board)
        self.assertTrue(m.selector_wake(before,after)[0])

    def test_project_custom_status_options_are_allowed(self):
        gp=m.GitHubProject('org/repo','org',2)
        with patch.object(m,'gh_json',side_effect=[{'id':'PROJECT'},
             {'fields':[{'name':'Status','id':'STATUS',
                        'options':[{'name':'Planned','id':'A'},{'name':'Working','id':'B'}]}]}]):
            gp.ensure_metadata()
        with patch.object(m,'command') as cmd:
            gp.set_status(issue(1,'Task',status='Planned'),'In Progress')
        cmd.assert_not_called()  # custom status doesn't block the whole worker


if __name__=='__main__':
    unittest.main()

def activity_row(n, *, updated, comments=0, labels=(), state='open', body_digest=None):
    """One GitHub issues-list row. Comments and updated_at are the attacker-controllable fields."""
    return {'number':n,'updated_at':updated,'comments':comments,'state':state,
            'labels':[{'name':x} for x in labels],'title':f'Task {n}',
            'body':'' if body_digest is None else 'body-'+body_digest}


class OutsiderCommentWake(unittest.TestCase):
    """B3: after an OMO NO_ACTION decision, only real content changes may start a new AI session."""

    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.worker=m.ProjectWorker({'name':'demo','repo':'org/repo',
                    'directory':'/home/example/repositories/repo','project_owner':'org',
                    'project_number':2},
                    {'state_dir':temp.name,'poll_seconds':300,'wrapper':'/tmp/guard','opencode':'/tmp/opencode'},
                    threading.Event(),threading.Lock())
        w=self.worker
        for name in ('ensure_unsafe_group_gone','checkout_neutral'):
            p=patch.object(w,name,return_value=True);p.start();self.addCleanup(p.stop)
        p=patch.object(w,'quota_until',return_value=0);p.start();self.addCleanup(p.stop)
        p=patch.object(m,'renovate_prs',return_value=[]);p.start();self.addCleanup(p.stop)
        p=patch.object(w,'check_maintenance',return_value=None);p.start();self.addCleanup(p.stop)

    def board(self,body='a'*64,status='Todo'):
        return [dict(issue(10,status=status),body_sha256=body)]

    def run_with_activity(self,rows,board):
        with patch.object(m,'gh_json',return_value=rows):
            return self.worker.one_iteration()

    def test_outsider_comment_after_no_action_does_not_start_ai(self):
        w=self.worker
        base=[activity_row(10,updated='2026-11-25T08:00:00Z')]
        with patch.object(w,'run_wrapper',return_value=(0,'AUTOCONTROLLER_NO_ACTION')) as run, \
             patch.object(w.project,'items',return_value=self.board()):
            self.run_with_activity(base,None)
            self.assertEqual(run.call_count,1)
            # Outsider comment: updated_at and comments bump; no content change.
            self.run_with_activity([activity_row(10,updated='2026-11-26T09:00:00Z',comments=7)],None)
        self.assertEqual(run.call_count,1)

    def test_legacy_string_snapshot_wakes_once_then_outsider_comments_do_not(self):
        # A legacy plain-string skip file compares by equality with the new dict
        # snapshot, so the first iteration after upgrade is one deliberate
        # re-selection. The snapshot is then stored as a dict and outsider
        # comments no longer wake anything.
        w=self.worker
        m.json_atomic(w.selector_skip,{'signature':'legacy-string','review_after':time.time()+86400})
        with patch.object(w,'run_wrapper',return_value=(0,'AUTOCONTROLLER_NO_ACTION')) as run, \
             patch.object(w.project,'items',return_value=self.board()):
            self.run_with_activity([activity_row(10,updated='2026-11-26T08:00:00Z')],None)
            self.assertEqual(run.call_count,1)
            self.run_with_activity([activity_row(10,updated='2026-11-26T09:00:00Z',comments=3)],None)
        self.assertEqual(run.call_count,1)
        self.assertIsInstance(m.json_read(w.selector_skip)['signature'],dict)

    def test_body_edit_wakes_after_no_action(self):
        w=self.worker
        with patch.object(w,'run_wrapper',return_value=(0,'AUTOCONTROLLER_NO_ACTION')) as run:
            with patch.object(w.project,'items',return_value=self.board(body='a'*64)):
                self.run_with_activity([activity_row(10,updated='2026-11-25T08:00:00Z')],None)
            with patch.object(w.project,'items',return_value=self.board(body='b'*64)):
                self.run_with_activity([activity_row(10,updated='2026-11-25T08:00:00Z')],None)
        self.assertEqual(run.call_count,2)

    def test_label_change_wakes_after_no_action(self):
        w=self.worker
        with patch.object(w,'run_wrapper',return_value=(0,'AUTOCONTROLLER_NO_ACTION')) as run, \
             patch.object(w.project,'items',return_value=self.board()):
            self.run_with_activity([activity_row(10,updated='2026-11-25T08:00:00Z')],None)
            self.run_with_activity([activity_row(10,updated='2026-11-25T08:00:00Z',labels=('bug',))],None)
        self.assertEqual(run.call_count,2)

    def test_status_change_wakes_after_no_action(self):
        w=self.worker
        with patch.object(w,'run_wrapper',return_value=(0,'AUTOCONTROLLER_NO_ACTION')) as run:
            with patch.object(w.project,'items',return_value=self.board(status='Todo')):
                self.run_with_activity([activity_row(10,updated='2026-11-25T08:00:00Z')],None)
            with patch.object(w.project,'items',return_value=self.board(status='In Progress')):
                self.run_with_activity([activity_row(10,updated='2026-11-25T08:00:00Z')],None)
        self.assertEqual(run.call_count,2)

    def test_newly_active_issue_in_window_does_not_wake_and_is_merged(self):
        # An outsider comment can pull an admitted issue into the 100-item window
        # that was previously absent. That alone must not wake a paid session.
        w=self.worker
        board=[dict(issue(10),body_sha256='a'*64),dict(issue(11),body_sha256='c'*64)]
        with patch.object(w,'run_wrapper',return_value=(0,'AUTOCONTROLLER_NO_ACTION')) as run, \
             patch.object(w.project,'items',return_value=board):
            self.run_with_activity([activity_row(10,updated='2026-11-25T08:00:00Z')],None)
            self.run_with_activity([activity_row(10,updated='2026-11-25T08:00:00Z'),
                                    activity_row(11,updated='2026-11-26T09:00:00Z',comments=4)],None)
        self.assertEqual(run.call_count,1)
        stored=m.json_read(w.selector_skip)['signature']
        self.assertIn('11',stored['issues'])

    def test_released_board_membership_change_still_wakes(self):
        w=self.worker
        with patch.object(w,'run_wrapper',return_value=(0,'AUTOCONTROLLER_NO_ACTION')) as run:
            with patch.object(w.project,'items',return_value=self.board()):
                self.run_with_activity([activity_row(10,updated='2026-11-25T08:00:00Z')],None)
            with patch.object(w.project,'items',return_value=self.board()+[dict(issue(12),body_sha256='e'*64)]):
                self.run_with_activity([activity_row(10,updated='2026-11-25T08:00:00Z')],None)
        self.assertEqual(run.call_count,2)

    def test_selector_wake_pure_decider(self):
        stored={'board':'b1','issues':{'10':'x'}}
        self.assertEqual(m.selector_wake(stored,{'board':'b1','issues':{'10':'x'}})[0],False)
        self.assertEqual(m.selector_wake(stored,{'board':'b2','issues':{'10':'x'}})[0],True)
        self.assertEqual(m.selector_wake(stored,{'board':'b1','issues':{'10':'y'}})[0],True)
        self.assertEqual(m.selector_wake(stored,{'board':'b1','issues':{'10':'x','11':'z'}})[0],False)
        self.assertEqual(m.selector_wake('old','old'),(False,'old'))
        self.assertEqual(m.selector_wake('old','new')[0],True)
        self.assertEqual(m.selector_wake('old',{'board':'b','issues':{}})[0],True)


class SelectionReconcile(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.w=m.ProjectWorker({'name':'demo','repo':'org/repo',
                'directory':'/home/example/repositories/repo','project_owner':'org','project_number':2},
                {'state_dir':temp.name,'wrapper':'/tmp/guard','opencode':'/tmp/opencode'},
                threading.Event(),threading.Lock())

    def test_selected_issue_retries_github_without_new_ai_call(self):
        w=self.w
        with patch.object(w,'checkout_neutral',return_value=True), \
             patch.object(w,'run_wrapper',return_value=(0,'AUTOCONTROLLER_SELECT_ISSUE #18')) as model, \
             patch.object(w.project,'items',side_effect=[RuntimeError('temporary GitHub failure'),[issue(18)]]), \
             patch.object(m,'issue_state',return_value='open'), \
             patch.object(w.project,'set_status'):
            with self.assertRaises(RuntimeError):
                w.select_next_issue([issue(18)])
            checkpoint=m.json_read(w.cycle_state)
            self.assertEqual(checkpoint['decision'],'issue')
            self.assertEqual(checkpoint['chosen_issue'],18)
            w.select_next_issue([issue(18)],checkpoint=checkpoint)
        model.assert_called_once()
        self.assertEqual(m.json_read(w.cycle_state)['number'],18)

    def test_noaction_retries_github_without_new_ai_call(self):
        w=self.w
        with patch.object(w,'checkout_neutral',return_value=True), \
             patch.object(w,'run_wrapper',return_value=(0,'AUTOCONTROLLER_NO_ACTION')) as model, \
             patch.object(w,'selector_signature',side_effect=[RuntimeError('api failed'), 'signature']):
            with self.assertRaises(RuntimeError):
                w.select_next_issue([issue(18)])
            cp=m.json_read(w.cycle_state)
            self.assertEqual(cp['decision'],'none')
            w.select_next_issue([issue(18)],checkpoint=cp)
        model.assert_called_once()
        self.assertFalse(w.cycle_state.exists())
        self.assertEqual(m.json_read(w.selector_skip)['signature'],'signature')
