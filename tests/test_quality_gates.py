"""Deterministic GitHub quality gates; independent review belongs to OMO."""
from contextlib import ExitStack
from pathlib import Path
import importlib.util
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('quality_controller',str(Path(__file__).resolve().parents[1]/'autonomy-controller.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
SHA='a'*40
MERGE='b'*40
URL='https://github.com/org/demo/issues/11'

class Gates(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        root=Path(tmp.name)
        cfg={'name':'demo','repo':'org/demo','directory':str(root/'checkout'),
             'project_owner':'org','project_number':1}
        (root/'checkout').mkdir()
        self.w=m.ProjectWorker(cfg,{'state_dir':str(root/'state'),'opencode':'oc','wrapper':'wrapper'},
                               threading.Event(),threading.Lock())
        self.pr={'number':21,'state':'open','merged_at':None,'draft':False,
                 'head':{'sha':SHA,'repo':{'full_name':'org/demo'}},
                 'base':{'ref':'main'},'merge_commit_sha':None}
        self.item={'number':11,'status':'In Progress','item_id':'item','url':URL}
        self.agent_calls=[]
        self.merge_calls=[]
        self.merge_rejects=None  # optional callable(pr): may mutate self.pr, then raise
        self.agent_output=(0,'Orchestrator: fixer completed; oracle reviewed, no blockers')
        # Independent review evidence, as the gate would return it for the current head. Tests that
        # exercise the gate itself set self.review = None (absence) or a specific candidate.
        self.review='default'

    def stage(self, phase, **extra):
        cp={'kind':'issue','number':11,'url':URL,'phase':phase,
            'pr_number':21,'retry_same_task':True,**extra}
        # A checkpoint that carries a ready_sha past CI holds the review evidence the gate stored for it.
        ready=cp.get('ready_sha')
        if ready and 'review_evidence' not in cp and phase in ('merge','postmerge'):
            cp['review_evidence']={'sha':ready,'tree':'t'*40,'session':'ses_oracle','root':'ses_root'}
        self.w.save_checkpoint(cp)

    def patches(self,ci='passed',main='passed',real_related=False):
        stack=ExitStack();self.addCleanup(stack.close)
        if not real_related:
            stack.enter_context(patch.object(self.w,'related_pr',side_effect=lambda *a:dict(self.pr)))
        stack.enter_context(patch.object(m,'pr_data',side_effect=lambda *a:dict(self.pr)))
        def fake_agent(prompt,kind,number,**kw):
            self.agent_calls.append((prompt,kw))
            return self.agent_output
        stack.enter_context(patch.object(self.w,'run_wrapper',side_effect=fake_agent))
        stack.enter_context(patch.object(m,'pr_ci_state',return_value=ci))
        stack.enter_context(patch.object(m,'main_ci_state',return_value=main))
        def fake_command(argv,**kwargs):
            if argv[:3]==['gh','pr','merge']:
                self.merge_calls.append(argv)
                if self.merge_rejects:
                    self.merge_rejects(self.pr)
                self.pr.update(state='closed',merged_at='2026-11-24T10:00:00Z',merge_commit_sha=MERGE)
            return ''
        stack.enter_context(patch.object(m,'command',side_effect=fake_command))
        stack.enter_context(patch.object(self.w,'checkout_state',return_value=('main',False)))
        stack.enter_context(patch.object(self.w,'cleanup_checkout'))
        stack.enter_context(patch.object(self.w.project,'set_status'))
        stack.enter_context(patch.object(self.w.project,'items',return_value=[self.item]))
        stack.enter_context(patch.object(m,'issue_state',return_value='closed'))
        stack.enter_context(patch.object(self.w,'review_evidence',side_effect=self._review_for))
        return stack

    def _review_for(self,kind,number,head_sha):
        """Stand-in for the wrapper listing + git tree lookup: a trusted candidate for this head, or none."""
        if self.review is None:
            return None,'no completed, idle, inactive oracle session with a complete snapshot (test: absent)'
        return {'sha':head_sha,'tree':'t'*40,'session':'ses_oracle','root':'ses_root'},''

    def test_full_flow_one_omo_session_and_real_github_gates(self):
        self.patches()
        for phase in ('ci','merge','postmerge'):
            self.w._cycle('issue',11,URL,self.item)
            self.assertEqual(m.json_read(self.w.cycle_state)['phase'],phase)
            self.w.project.set_status.assert_not_called()
        self.w._cycle('issue',11,URL,self.item)
        self.assertFalse(self.w.cycle_state.exists())
        self.w.project.set_status.assert_called_once_with(self.item,'Done')
        self.assertEqual(len(self.agent_calls),1)
        self.assertIn('Native OMO Slim',self.agent_calls[0][0])
        self.assertIn('`oracle`',self.agent_calls[0][0])
        self.assertEqual(self.merge_calls[0][-1],SHA)
        self.assertIn('--match-head-commit',self.merge_calls[0])

    def test_ci_pending_never_spawns_ai_or_merges(self):
        self.stage('ci');self.patches(ci='pending')
        self.w._cycle('issue',11,URL,None)
        self.assertEqual(m.json_read(self.w.cycle_state)['phase'],'ci')
        self.assertEqual(self.agent_calls,[])
        self.assertEqual(self.merge_calls,[])

    def test_ci_failed_returns_to_omo_fixer_and_oracle(self):
        self.stage('ci');self.patches(ci='failed')
        self.w._cycle('issue',11,URL,None)
        cp=m.json_read(self.w.cycle_state)
        self.assertEqual(cp['phase'],'code')
        self.assertIn('CI failed',cp['review_feedback'])
        self.assertIn('issue:11',self.w.retry_map())
        self.assertEqual(self.agent_calls,[])

    def test_exact_head_change_restarts_omo_review(self):
        self.stage('merge',ready_sha='d'*40);self.patches()
        self.w._cycle('issue',11,URL,None)
        cp=m.json_read(self.w.cycle_state)
        self.assertEqual(cp['phase'],'code')
        self.assertIn('head changed',cp['review_feedback'])
        self.assertFalse(self.merge_calls)

    def test_ci_rechecked_before_merge_pending(self):
        self.stage('merge',ready_sha=SHA);self.patches(ci='pending')
        self.w._cycle('issue',11,URL,None)
        self.assertEqual(m.json_read(self.w.cycle_state)['phase'],'ci')
        self.assertFalse(self.merge_calls)

    def test_ci_rechecked_before_merge_failed(self):
        self.stage('merge',ready_sha=SHA);self.patches(ci='failed')
        self.w._cycle('issue',11,URL,None)
        self.assertEqual(m.json_read(self.w.cycle_state)['phase'],'code')
        self.assertFalse(self.merge_calls)

    def test_green_main_pending_not_done(self):
        self.stage('postmerge',ready_sha=SHA,merge_commit=MERGE)
        self.pr.update(state='closed',merged_at='now',merge_commit_sha=MERGE)
        self.patches(main='pending')
        self.w._cycle('issue',11,URL,None)
        self.assertTrue(self.w.cycle_state.exists())
        self.w.project.set_status.assert_not_called()

    def test_red_main_enters_protected_repair(self):
        self.stage('postmerge',ready_sha=SHA,merge_commit=MERGE)
        self.pr.update(state='closed',merged_at='now',merge_commit_sha=MERGE)
        self.patches(main='failed')
        self.w._cycle('issue',11,URL,None)
        self.assertEqual(m.json_read(self.w.cycle_state)['phase'],'repair')
        self.w.project.set_status.assert_not_called()

    def test_crash_after_merge_before_persist_recovers_without_remerge(self):
        self.stage('merge',ready_sha=SHA)
        self.pr.update(state='closed',merged_at='now',merge_commit_sha=MERGE)
        self.patches()
        self.w._cycle('issue',11,URL,None)
        cp=m.json_read(self.w.cycle_state)
        self.assertEqual((cp['phase'],cp['merge_commit']),('postmerge',MERGE))
        self.assertEqual(self.merge_calls,[])
        self.assertEqual(self.agent_calls,[])

    def test_unexpected_merge_before_ci_gate_is_not_done(self):
        self.stage('ci')
        self.pr.update(state='closed',merged_at='now',merge_commit_sha=MERGE)
        self.patches()
        with self.assertRaises(m.ControllerError):self.w._cycle('issue',11,URL,None)
        self.w.project.set_status.assert_not_called()

    def test_code_not_allowed_to_retroactively_accept_merged_pr(self):
        self.stage('code')
        self.pr.update(state='closed',merged_at='now',merge_commit_sha=MERGE)
        self.patches()
        with self.assertRaises(m.ControllerError):self.w._cycle('issue',11,URL,None)

    def test_draft_pr_after_agent_cannot_enter_ci(self):
        self.pr['draft']=True
        self.patches()
        self.w._cycle('issue',11,URL,None)
        self.assertEqual(m.json_read(self.w.cycle_state)['phase'],'code')
        self.assertIn('issue:11',self.w.retry_map())
        self.assertEqual(self.agent_calls.__len__(),1)
        self.assertFalse(self.merge_calls)

    def test_draft_pr_in_ci_returns_to_agent(self):
        self.stage('ci');self.pr['draft']=True;self.patches()
        self.w._cycle('issue',11,URL,None)
        self.assertEqual(m.json_read(self.w.cycle_state)['phase'],'code')
        self.assertFalse(self.merge_calls)

    def test_draft_pr_in_merge_cannot_merge(self):
        self.stage('merge',ready_sha=SHA);self.pr['draft']=True;self.patches()
        self.w._cycle('issue',11,URL,None)
        self.assertEqual(m.json_read(self.w.cycle_state)['phase'],'code')
        self.assertFalse(self.merge_calls)

    def test_dirty_checkout_returns_to_agent_without_merging(self):
        self.stage('ci');self.patches()
        with patch.object(self.w,'checkout_state',return_value=('feature',True)):
            self.w._cycle('issue',11,URL,None)
        cp=m.json_read(self.w.cycle_state)
        self.assertEqual(cp['phase'],'code')
        self.assertIn('Checkout is dirty',cp['review_feedback'])

    def test_repair_pr_uses_ci_without_standalone_review_phase(self):
        self.stage('repair',review_feedback='Fix failing main CI')
        self.pr.update(state='closed',merged_at='now',merge_commit_sha=MERGE)
        self.patches()
        repair={'number':22,'state':'open','head':{'sha':SHA},'base':{'ref':'main'}}
        with patch.object(m,'repair_pr',return_value=repair):
            self.w._cycle('issue',11,URL,None)
        cp=m.json_read(self.w.cycle_state)
        self.assertEqual((cp['phase'],cp['pr_number']),('ci',22))
        self.assertIn('Autocontroller-Repair-For: #21',self.agent_calls[0][0])
        self.assertIn('`oracle`',self.agent_calls[0][0])

    def test_reopened_issue_repair_checkpoint_stays_on_exact_repair_linkage(self):
        # PR #21 merged and main CI failed; the Issue was reopened afterwards. The
        # repair phase must keep resolving the merged PR #21 by its exact number, not
        # treat it as historical (which would leave the repair gate with no PR to find).
        self.pr.update(number=21,state='closed',merged_at='2026-11-26T21:11:46Z',merge_commit_sha=MERGE)
        nodes=[self._closing_node(21,'MERGED','2026-11-26T21:11:46Z')]
        repair={'number':22,'state':'open','merged_at':None,'draft':False,
                'head':{'sha':'e'*40,'repo':{'full_name':'org/demo'}},
                'base':{'ref':'main'},'merge_commit_sha':None,'mergeable_state':'clean'}
        self.w.save_checkpoint({'kind':'issue','number':11,'url':URL,'phase':'repair',
                                'pr_number':21,'review_feedback':'Fix failing main CI','retry_same_task':True})
        self.patches(real_related=True)
        with self._linked_graph(nodes,reopened_at='2026-11-26T21:28:31Z'), \
             patch.object(m,'issue_state',return_value='open'), \
             patch.object(m,'repair_pr',return_value=repair), \
             patch.object(m,'pr_data',side_effect=lambda repo,n:dict(repair if n==22 else self.pr)):
            self.w._cycle('issue',11,URL,None)
        self.assertEqual(len(self.agent_calls),1)
        self.assertIn('Autocontroller-Repair-For: #21',self.agent_calls[0][0])
        cp=m.json_read(self.w.cycle_state)
        self.assertEqual((cp['phase'],cp['pr_number'],cp['repairing']),('ci',22,True))

    def test_old_completed_review_merge_checkpoint_without_evidence_fails_closed(self):
        # Before issue #48 a completed `reviewed_sha` merge checkpoint merged without stored evidence.
        # Such a checkpoint can no longer prove an oracle reviewed the head tree, so it must not merge.
        self.stage('merge',reviewed_sha=SHA)
        self.patches()
        self.w._cycle('issue',11,URL,None)
        cp=m.json_read(self.w.cycle_state)
        self.assertEqual(cp['phase'],'code')
        self.assertNotIn('ready_sha',cp)
        self.assertIn('evidence',cp['review_feedback'])
        self.assertEqual(self.merge_calls,[])

    def test_closed_unmerged_renovate_not_false_positive(self):
        self.pr.update(state='closed',merged_at=None,user={'login':'renovate[bot]','type':'Bot','id':29139614},
                       head={'ref':'renovate/pkg-21','sha':SHA,'repo':{'full_name':'org/demo'}},
                       base={'ref':'main','repo':{'full_name':'org/demo'}})
        self.patches()
        with patch.object(self.w,'related_pr',side_effect=lambda *a:dict(self.pr)):
            self.w._cycle('renovate',21,'https://github.com/org/demo/pull/21',None)
        self.assertFalse(self.w.cycle_state.exists())

    def test_terminal_project_status_is_not_candidate(self):
        self.assertIn('done',m.TERMINAL_PROJECT_STATUSES)
        self.assertIn('completed',m.TERMINAL_PROJECT_STATUSES)

    def test_required_aggregate_check_cannot_be_substituted(self):
        class Result:
            returncode=0;stderr='';stdout=''
        def run(rows):
            p=Result();p.stdout=json.dumps(rows);return p
        with patch.object(m.subprocess,'run',return_value=run([{'name':'other','bucket':'pass'}])):
            self.assertEqual(m.pr_ci_state('org/demo',21),'pending')
        with patch.object(m.subprocess,'run',return_value=run([{'name':'required','bucket':'pass'},{'name':'lint','bucket':'fail'}])):
            self.assertEqual(m.pr_ci_state('org/demo',21),'failed')
        with patch.object(m.subprocess,'run',return_value=run([{'name':'required','bucket':'pass'},{'name':'lint','bucket':'pass'}])):
            self.assertEqual(m.pr_ci_state('org/demo',21),'passed')

    def test_merge_behind_base_returns_to_omo_reconciliation_not_retry_loop(self):
        self.stage('merge',ready_sha=SHA);self.pr['mergeable_state']='behind';self.patches()
        self.w._cycle('issue',11,URL,None)
        cp=m.json_read(self.w.cycle_state)
        self.assertEqual(cp['phase'],'code')
        self.assertNotIn('ready_sha',cp)
        self.assertEqual(cp['diverged_head'],SHA)
        self.assertIn('behind',cp['review_feedback'])
        self.assertIn('origin/main',cp['review_feedback'])
        self.assertIn('`oracle`',cp['review_feedback'])
        self.assertIn('Never force-push',cp['review_feedback'])
        self.assertFalse(self.merge_calls)
        self.assertEqual(self.agent_calls,[])
        self.assertNotIn('issue:11',self.w.retry_map())

    def test_ci_phase_conflicting_pr_returns_to_code_instead_of_pending(self):
        self.stage('ci');self.pr['mergeable_state']='dirty';self.patches(ci='pending')
        self.w._cycle('issue',11,URL,None)
        cp=m.json_read(self.w.cycle_state)
        self.assertEqual(cp['phase'],'code')
        self.assertIn('merge conflict',cp['review_feedback'])
        self.assertNotIn('ready_sha',cp)
        self.assertFalse(self.merge_calls)
        self.assertEqual(self.agent_calls,[])

    def test_merge_race_behind_after_clean_precheck_routes_to_code(self):
        def reject(pr):
            pr['mergeable_state']='behind'
            raise m.ControllerError('Pull request is not mergeable: base branch was modified')
        self.stage('merge',ready_sha=SHA);self.pr['mergeable_state']='clean'
        self.merge_rejects=reject;self.patches()
        self.w._cycle('issue',11,URL,None)  # must not raise
        cp=m.json_read(self.w.cycle_state)
        self.assertEqual(cp['phase'],'code')
        self.assertNotIn('ready_sha',cp)
        self.assertIn('behind',cp['review_feedback'])
        self.assertEqual(len(self.merge_calls),1)

    def test_merge_race_without_divergence_keeps_ordinary_backoff(self):
        for state in (None,'unknown','blocked'):
            with self.subTest(state=state):
                self.merge_calls.clear()
                self.stage('merge',ready_sha=SHA);self.pr['mergeable_state']='clean'
                def reject(pr,state=state):
                    pr['mergeable_state']=state
                    raise m.ControllerError('transient GitHub API failure')
                self.merge_rejects=reject;self.patches()
                with self.assertRaises(m.ControllerError) as ctx:
                    self.w._cycle('issue',11,URL,None)
                if state=='blocked':
                    self.assertIn('not branch divergence',str(ctx.exception))
                cp=m.json_read(self.w.cycle_state)
                self.assertEqual((cp['phase'],cp['ready_sha']),('merge',SHA))
                self.assertNotIn('diverged_head',cp)
                self.assertEqual(self.agent_calls,[])

    def test_recovered_new_head_requires_fresh_agent_review_and_ci(self):
        new='c'*40
        self.stage('merge',ready_sha=SHA);self.pr['mergeable_state']='behind';self.patches(ci='pending')
        self.w._cycle('issue',11,URL,None)
        self.assertEqual(m.json_read(self.w.cycle_state)['phase'],'code')
        # The agent pushes a reconciled head; GitHub now reports it clean.
        self.pr.update(head={'sha':new,'repo':{'full_name':'org/demo'}},mergeable_state='clean')
        self.w._cycle('issue',11,URL,None)
        self.assertEqual(len(self.agent_calls),1)
        self.assertEqual(m.json_read(self.w.cycle_state)['phase'],'ci')
        self.w._cycle('issue',11,URL,None)  # CI pending on the new head: no merge
        self.assertEqual(m.json_read(self.w.cycle_state)['phase'],'ci')
        self.assertEqual(self.merge_calls,[])
        with patch.object(m,'pr_ci_state',return_value='passed'):
            self.w._cycle('issue',11,URL,None)
            self.assertEqual(m.json_read(self.w.cycle_state)['ready_sha'],new)
            self.w._cycle('issue',11,URL,None)
        self.assertEqual(self.merge_calls[0][-1],new)
        self.assertIn('--match-head-commit',self.merge_calls[0])

    def test_repair_pr_divergence_keeps_repair_linkage_not_merged_original(self):
        repair={'number':22,'state':'open','merged_at':None,'draft':False,
                'head':{'sha':'e'*40,'repo':{'full_name':'org/demo'}},
                'base':{'ref':'main'},'merge_commit_sha':None,'mergeable_state':'dirty'}
        self.pr.update(state='closed',merged_at='2026-11-24T10:00:00Z',merge_commit_sha=MERGE)
        self.w.save_checkpoint({'kind':'issue','number':11,'url':URL,'phase':'ci',
                                'pr_number':22,'repairing':True,'retry_same_task':True})
        self.patches(real_related=True)
        with patch.object(m,'pr_data',side_effect=lambda repo,n:dict(repair if n==22 else self.pr)):
            with patch.object(m,'linked_issue_pr',return_value={'number':21}):
                self.w._cycle('issue',11,URL,None)  # ci: dirty repair PR -> code
                self.assertEqual(m.json_read(self.w.cycle_state)['phase'],'code')
                self.w._cycle('issue',11,URL,None)  # code: agent works the repair PR, not PR #21
        cp=m.json_read(self.w.cycle_state)
        self.assertEqual((cp['phase'],cp['pr_number']),('ci',22))
        self.assertEqual(len(self.agent_calls),1)
        self.assertFalse(self.merge_calls)

    def test_closed_unmerged_repair_pr_keeps_main_is_red_checkpoint(self):
        closed_repair={'number':22,'state':'closed','merged_at':None,'draft':False,
                       'head':{'sha':'e'*40,'repo':{'full_name':'org/demo'}},
                       'base':{'ref':'main'},'merge_commit_sha':None}
        self.pr.update(state='closed',merged_at='2026-11-24T10:00:00Z',merge_commit_sha=MERGE)
        self.w.save_checkpoint({'kind':'issue','number':11,'url':URL,'phase':'ci',
                                'pr_number':22,'repairing':True,'retry_same_task':True})
        self.patches(real_related=True)
        with patch.object(m,'pr_data',side_effect=lambda repo,n:dict(closed_repair if n==22 else self.pr)):
            with patch.object(m,'linked_issue_pr',return_value={'number':21}):
                self.w._cycle('issue',11,URL,None)  # ci: closed repair PR -> code, reopen feedback
                cp=m.json_read(self.w.cycle_state)
                self.assertEqual(cp['phase'],'code')
                self.assertIn('Repair PR #22 was closed without merge; reopen it',cp['review_feedback'])
                self.w._cycle('issue',11,URL,None)  # code: agent runs; PR still closed
        self.assertEqual(len(self.agent_calls),1)
        cp=m.json_read(self.w.cycle_state)
        self.assertIsNotNone(cp,'main-is-red repair obligation must survive a closed repair PR')
        self.assertEqual((cp['pr_number'],cp['repairing']),(22,True))
        self.assertIn('issue:11',self.w.retry_map())
        self.assertFalse(self.merge_calls)

    def test_repair_agent_failure_keeps_main_is_red_checkpoint(self):
        repair={'number':22,'state':'open','merged_at':None,'draft':False,
                'head':{'sha':'e'*40,'repo':{'full_name':'org/demo'}},
                'base':{'ref':'main'},'merge_commit_sha':None}
        self.pr.update(state='closed',merged_at='2026-11-24T10:00:00Z',merge_commit_sha=MERGE)
        self.w.save_checkpoint({'kind':'issue','number':11,'url':URL,'phase':'code',
                                'pr_number':22,'repairing':True,'retry_same_task':True})
        self.patches(real_related=True)
        self.agent_output=(1,'boom')
        with patch.object(m,'pr_data',side_effect=lambda repo,n:dict(repair if n==22 else self.pr)):
            with patch.object(m,'linked_issue_pr',return_value={'number':21}):
                self.w._cycle('issue',11,URL,None)
        self.assertEqual(len(self.agent_calls),1)
        cp=m.json_read(self.w.cycle_state)
        self.assertIsNotNone(cp,'failed repair agent run must not discard the main-is-red obligation')
        self.assertEqual((cp['pr_number'],cp['repairing']),(22,True))
        self.assertIn('issue:11',self.w.retry_map())
        self.assertFalse(self.merge_calls)

    def _linked_graph(self, nodes, reopened_at=None):
        """GraphQL linkage for Issue #11 with the real parser (only gh_json is faked)."""
        timeline={'totalCount':0,'nodes':[]}
        if reopened_at:
            timeline={'totalCount':1,'nodes':[{'createdAt':reopened_at}]}
        graph={'data':{'repository':{'issue':{'timelineItems':timeline,
               'closedByPullRequestsReferences':{'nodes':nodes,'pageInfo':{'hasNextPage':False}}}}}}
        return patch.object(m,'gh_json',side_effect=lambda *a,**k: graph if 'graphql' in a else {})

    @staticmethod
    def _closing_node(number,state,merged_at=None):
        return {'number':number,'url':f'https://github.com/org/demo/pull/{number}','state':state,
                'mergedAt':merged_at,'headRefOid':SHA,'baseRefName':'main','isCrossRepository':False,
                'headRepository':{'nameWithOwner':'org/demo'},'mergeCommit':None}

    def test_reopened_issue_historical_merged_pr_runs_fresh_agent(self):
        # Issue #19 / PR #26 merged, Issue reopened later, revert #27 not linked: a new attempt.
        self.pr.update(number=26,state='closed',merged_at='2026-11-26T21:11:46Z',merge_commit_sha=MERGE)
        nodes=[self._closing_node(26,'MERGED','2026-11-26T21:11:46Z')]
        self.patches(real_related=True)
        with self._linked_graph(nodes,reopened_at='2026-11-26T21:28:31Z'), \
             patch.object(m,'issue_state',return_value='open'):
            self.w._cycle('issue',11,URL,None)
        self.assertEqual(len(self.agent_calls),1)
        self.assertIn('Native OMO Slim',self.agent_calls[0][0])
        # The fake agent opens no PR, so the ordinary post-agent check (not the historical merge error) applies.
        self.assertIn('No open linked PR',self.w.retry_map()['issue:11']['reason'])
        self.assertFalse(self.merge_calls)

    def test_reopened_issue_replacement_pr_goes_through_normal_ci_gate(self):
        replacement={'number':40,'state':'open','merged_at':None,'draft':False,
                     'head':{'sha':'e'*40,'repo':{'full_name':'org/demo'}},
                     'base':{'ref':'main'},'merge_commit_sha':None,'mergeable_state':'clean'}
        nodes=[self._closing_node(26,'MERGED','2026-11-26T21:11:46Z'),self._closing_node(40,'OPEN')]
        self.patches(real_related=True)
        with self._linked_graph(nodes,reopened_at='2026-11-26T21:28:31Z'), \
             patch.object(m,'issue_state',return_value='open'), \
             patch.object(m,'pr_data',side_effect=lambda repo,n:dict(replacement if n==40 else self.pr)):
            self.w._cycle('issue',11,URL,None)
        cp=m.json_read(self.w.cycle_state)
        self.assertEqual((cp['phase'],cp['pr_number']),('ci',40))
        self.assertEqual(len(self.agent_calls),1)
        self.assertFalse(self.merge_calls)

    def test_reopened_issue_restart_from_persisted_code_checkpoint_runs_agent(self):
        # A checkpoint persisted by earlier failed attempts must resume a normal agent run.
        self.pr.update(number=26,state='closed',merged_at='2026-11-26T21:11:46Z',merge_commit_sha=MERGE)
        self.w.save_checkpoint({'kind':'issue','number':11,'url':URL,'phase':'code',
                                'started':0,'review_feedback':'previous attempt failed'})
        nodes=[self._closing_node(26,'MERGED','2026-11-26T21:11:46Z')]
        self.patches(real_related=True)
        with self._linked_graph(nodes,reopened_at='2026-11-26T21:28:31Z'), \
             patch.object(m,'issue_state',return_value='open'):
            self.w._cycle('issue',11,URL,None)
        self.assertEqual(len(self.agent_calls),1)
        self.assertIn('No open linked PR',self.w.retry_map()['issue:11']['reason'])

    def test_external_merge_during_current_attempt_still_raises(self):
        # Merged AFTER the latest reopen: never retrofit a merge that bypassed the CI gate.
        self.pr.update(number=26,state='closed',merged_at='2026-11-26T22:05:00Z',merge_commit_sha=MERGE)
        nodes=[self._closing_node(26,'MERGED','2026-11-26T22:05:00Z')]
        self.patches(real_related=True)
        with self._linked_graph(nodes,reopened_at='2026-11-26T21:28:31Z'):
            with self.assertRaisesRegex(m.ControllerError,'already merged before controller'):
                self.w._cycle('issue',11,URL,None)
        self.assertEqual(self.agent_calls,[])

    def test_never_reopened_merged_pr_still_raises(self):
        self.pr.update(number=26,state='closed',merged_at='2026-11-26T21:11:46Z',merge_commit_sha=MERGE)
        nodes=[self._closing_node(26,'MERGED','2026-11-26T21:11:46Z')]
        self.patches(real_related=True)
        with self._linked_graph(nodes):
            with self.assertRaisesRegex(m.ControllerError,'already merged before controller'):
                self.w._cycle('issue',11,URL,None)
        self.assertEqual(self.agent_calls,[])

    def test_merge_race_reread_failure_reports_both_errors(self):
        def reject(pr):
            raise m.ControllerError('merge rejected by GitHub')
        self.stage('merge',ready_sha=SHA);self.pr['mergeable_state']='clean'
        self.merge_rejects=reject;self.patches()
        with patch.object(m,'pr_data',side_effect=m.ControllerError('re-read unavailable')):
            with self.assertRaises(m.ControllerError) as ctx:
                self.w._cycle('issue',11,URL,None)
        self.assertIn('merge rejected by GitHub',str(ctx.exception))
        self.assertIn('re-read unavailable',str(ctx.exception))
        self.assertIn('merge rejected by GitHub',str(ctx.exception.__cause__))
        self.assertEqual(m.json_read(self.w.cycle_state)['phase'],'merge')

    def test_repeated_divergence_at_same_head_blocks_and_backs_off(self):
        self.stage('merge',ready_sha=SHA,diverged_head=SHA)
        self.pr['mergeable_state']='behind';self.patches()
        before=time.time()
        self.w._cycle('issue',11,URL,None)
        cp=m.json_read(self.w.cycle_state)
        self.assertEqual(cp['phase'],'code')
        self.assertEqual(self.agent_calls,[])
        self.assertFalse(self.merge_calls)
        self.assertGreaterEqual(self.w.retry_map()['issue:11']['until'],before+3500)
        notice=m.json_read(self.w.notices_file)['diverged-unresolved:issue:11']['message']
        self.assertIn('BLOCKER',notice)
        self.assertIn('still behind',notice)

class BranchDivergenceTests(unittest.TestCase):
    def test_only_explicit_behind_or_dirty_is_divergence(self):
        self.assertEqual(m.branch_divergence({'mergeable_state':'behind'}),'behind')
        self.assertEqual(m.branch_divergence({'mergeable_state':'DIRTY'}),'dirty')
        for state in (None,'unknown','blocked','unstable','clean','has_hooks','draft'):
            with self.subTest(state=state):
                self.assertIsNone(m.branch_divergence({'mergeable_state':state}))
        self.assertIsNone(m.branch_divergence({}))

if __name__=='__main__':unittest.main()
