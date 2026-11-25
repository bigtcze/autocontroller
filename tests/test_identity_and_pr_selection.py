"""Offline regressions: Renovate identity, outsider fork PRs and PR selection.

All GitHub responses are fixtures shaped like live REST/GraphQL data.
"""
import importlib.util
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('identity_controller',str(Path(__file__).resolve().parents[1]/'autonomy-controller.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

REPO='own/repo'
SHA='a'*40
BOT={'login':'renovate[bot]','type':'Bot','id':29139614}
URL='https://github.com/own/repo/pull/8'


def renovate_pr(number=8,**kw):
    pr={'number':number,'state':'open','merged_at':None,'draft':False,'user':BOT,
        'head':{'ref':f'renovate/pkg-{number}','sha':SHA,'repo':{'full_name':REPO}},
        'base':{'ref':'main','repo':{'full_name':REPO}},'html_url':f'https://github.com/own/repo/pull/{number}'}
    pr.update(kw);return pr


class RenovateCycleRefusalTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        root=Path(tmp.name)
        (root/'checkout').mkdir()
        cfg={'name':'demo','repo':REPO,'directory':str(root/'checkout'),'project_owner':'own','project_number':1}
        self.w=m.ProjectWorker(cfg,{'state_dir':str(root/'state'),'opencode':'oc','wrapper':'wrapper'},
                               threading.Event(),threading.Lock())

    def test_unverified_renovate_pr_refused_without_agent_call(self):
        # The live PR is a fork PR authored by the genuine bot: identity holds, repository does not.
        fork=renovate_pr(8,head={'ref':'renovate/pkg-8','sha':SHA,'repo':{'full_name':'attacker/repo'}})
        with patch.object(self.w,'related_pr',return_value=fork),patch.object(self.w,'run_wrapper') as run, \
             patch.object(self.w,'quota_clear') as clear,patch.object(m,'pr_data') as pr_data:
            self.w._cycle('renovate',8,URL,None)
        run.assert_not_called();clear.assert_not_called()
        self.assertFalse(self.w.cycle_state.exists())
        self.assertIn('renovate:8',self.w.retry_map())
        self.assertGreater(self.w.retry_map()['renovate:8']['until'],0)
        pr_data.assert_not_called()

    def test_branch_prefix_only_pr_refused_without_agent_call(self):
        spoofed=renovate_pr(9,user={'login':'attacker','type':'User','id':7})
        with patch.object(self.w,'related_pr',return_value=spoofed),patch.object(self.w,'run_wrapper') as run:
            self.w._cycle('renovate',9,URL,None)
        run.assert_not_called()
        self.assertFalse(self.w.cycle_state.exists())

    def test_verified_renovate_pr_still_reaches_agent_phase(self):
        with patch.object(self.w,'related_pr',return_value=renovate_pr(8)),patch.object(self.w,'run_wrapper',return_value=(0,'')) as run, \
             patch.object(self.w,'quota_clear'):
            self.w._cycle('renovate',8,URL,None)
        run.assert_called_once()
        self.assertEqual(m.json_read(self.w.cycle_state)['phase'],'ci')

    def test_renovate_refusal_respects_backoff_no_retry_storm(self):
        spoofed=renovate_pr(9,user={'login':'attacker','type':'User','id':7})
        with patch.object(self.w,'related_pr',return_value=spoofed),patch.object(self.w,'run_wrapper') as run:
            self.w._cycle('renovate',9,URL,None)
        run.assert_not_called()
        # Backoff is in force: the selector must not pick it again.
        with patch.object(self.w,'ensure_unsafe_group_gone'),patch.object(self.w,'quota_until',return_value=0), \
             patch.object(self.w,'checkout_neutral',return_value=True),patch.object(self.w,'bootstrap_gate',return_value=False), \
             patch.object(m,'renovate_prs',return_value=[spoofed]),patch.object(self.w.project,'items',return_value=[]), \
             patch.object(self.w,'_cycle') as cycle:
            self.w.one_iteration()
        cycle.assert_not_called()


class LinkedIssuePrTests(unittest.TestCase):
    def _graph(self,nodes):
        return {'data':{'repository':{'issue':{'closedByPullRequestsReferences':{
            'nodes':nodes,'pageInfo':{'hasNextPage':False}}}}}}

    def _node(self,number,state='OPEN',cross=False,head=REPO):
        return {'number':number,'url':f'https://github.com/own/repo/pull/{number}','state':state,
                'headRefOid':SHA,'baseRefName':'main','isCrossRepository':cross,
                'headRepository':{'nameWithOwner':head} if head else None,
                'mergeCommit':None}

    def test_fork_closing_pr_ignored_and_same_repo_selected(self):
        nodes=[self._node(50,cross=True,head='attacker/repo'),self._node(51)]
        with patch.object(m,'gh_json',return_value=self._graph(nodes)):
            self.assertEqual(m.linked_issue_pr(REPO,7)['number'],51)

    def test_fork_closing_pr_alone_yields_no_link(self):
        with patch.object(m,'gh_json',return_value=self._graph([self._node(50,cross=True,head='attacker/repo')])):
            self.assertIsNone(m.linked_issue_pr(REPO,7))

    def test_fork_open_pr_does_not_block_same_repo_pr(self):
        # An outsider fork PR must not turn a single real PR into an ambiguity error.
        nodes=[self._node(50,cross=True,head='attacker/repo'),self._node(51,state='MERGED')]
        with patch.object(m,'gh_json',return_value=self._graph(nodes)):
            self.assertEqual(m.linked_issue_pr(REPO,7)['number'],51)

    def test_two_same_repo_open_prs_still_error(self):
        nodes=[self._node(51),self._node(52)]
        with patch.object(m,'gh_json',return_value=self._graph(nodes)):
            with self.assertRaises(m.ControllerError):m.linked_issue_pr(REPO,7)

    def test_missing_head_repository_ignored(self):
        nodes=[self._node(50,head=None),self._node(51)]
        with patch.object(m,'gh_json',return_value=self._graph(nodes)):
            self.assertEqual(m.linked_issue_pr(REPO,7)['number'],51)


class MarkerPrSelectionTests(unittest.TestCase):
    def test_repair_pr_fork_with_marker_ignored(self):
        fork={'number':60,'state':'open','body':'Autocontroller-Repair-For: #21',
              'head':{'ref':'x','repo':{'full_name':'attacker/repo'}},'base':{'ref':'main','repo':{'full_name':REPO}}}
        own={'number':61,'state':'open','body':'Autocontroller-Repair-For: #21',
             'head':{'ref':'fix','repo':{'full_name':REPO}},'base':{'ref':'main','repo':{'full_name':REPO}}}
        with patch.object(m,'gh_api_pages',return_value=[fork,own]):
            self.assertEqual(m.repair_pr(REPO,21)['number'],61)
        with patch.object(m,'gh_api_pages',return_value=[fork]):
            self.assertIsNone(m.repair_pr(REPO,21))

    def test_repair_pr_fork_does_not_block_same_repo_repair(self):
        fork={'number':60,'state':'open','body':'Autocontroller-Repair-For: #21',
              'head':{'ref':'x','repo':{'full_name':'attacker/repo'}}}
        own={'number':61,'state':'open','body':'Autocontroller-Repair-For: #21',
             'head':{'ref':'fix','repo':{'full_name':REPO}}}
        with patch.object(m,'gh_api_pages',return_value=[fork,own]):
            self.assertEqual(m.repair_pr(REPO,21)['number'],61)

    def test_repair_pr_missing_head_repo_ignored(self):
        headless={'number':62,'state':'open','body':'Autocontroller-Repair-For: #21','head':{'ref':'x','repo':None}}
        with patch.object(m,'gh_api_pages',return_value=[headless]):
            self.assertIsNone(m.repair_pr(REPO,21))

    def test_release_preparation_fork_with_marker_ignored(self):
        fork={'number':70,'state':'open','body':'Autocontroller-Release: v1.2.5',
              'head':{'ref':'x','repo':{'full_name':'attacker/repo'}},'base':{'ref':'main','repo':{'full_name':REPO}}}
        own={'number':71,'state':'open','body':'Autocontroller-Release: v1.2.5',
             'head':{'ref':'release','repo':{'full_name':REPO}},'base':{'ref':'main','repo':{'full_name':REPO}}}
        with patch.object(m,'gh_api_pages',return_value=[fork,own]):
            self.assertEqual(m.release_preparation_pr(REPO,'v1.2.5')['number'],71)
        with patch.object(m,'gh_api_pages',return_value=[fork]):
            self.assertIsNone(m.release_preparation_pr(REPO,'v1.2.5'))

    def test_release_preparation_two_same_repo_still_error(self):
        a={'number':71,'state':'open','body':'Autocontroller-Release: v1.2.5','head':{'ref':'a','repo':{'full_name':REPO}}}
        b={'number':72,'state':'open','body':'Autocontroller-Release: v1.2.5','head':{'ref':'b','repo':{'full_name':REPO}}}
        with patch.object(m,'gh_api_pages',return_value=[a,b]):
            with self.assertRaises(m.ControllerError):m.release_preparation_pr(REPO,'v1.2.5')


if __name__=='__main__':unittest.main()
