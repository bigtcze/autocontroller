import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
import onboarding as o
import bootstrap_policy as b

class OnboardingTests(unittest.TestCase):
    def test_preflight_checks_omo_roles_without_model_request(self):
        import json
        with patch.object(o.c,'command',side_effect=[json.dumps({'data':{'version':'2.0.0'}})]+[json.dumps({'data':{'id':role}}) for role in ('orchestrator','fixer','oracle')]) as cmd:
            o.runtime_preflight('/bin/opencode')
            self.assertEqual([x.args[0][-1] for x in cmd.call_args_list],['/api/info','/api/agent/orchestrator','/api/agent/fixer','/api/agent/oracle'])
    def test_missing_bootstrap_checks_do_not_stall_forever(self):
        from subprocess import CompletedProcess
        p=CompletedProcess([],0,stdout='[]',stderr='')
        with patch.object(o.c.subprocess,'run',return_value=p):
            self.assertEqual(o.c.pr_ci_state('org/demo',1),'pending')
            self.assertEqual(o.c.pr_ci_state('org/demo',1,missing_is_failed=True),'failed')
        with patch.object(o.c,'gh_json',return_value={'workflow_runs':[]}):
            self.assertEqual(o.c.main_ci_state('org/demo','a'*40),'pending')
            self.assertEqual(o.c.main_ci_state('org/demo','a'*40,missing_is_failed=True),'failed')
    def test_url_rejects_non_github_and_paths(self):
        self.assertEqual(o.repo_url('https://github.com/Owner/project.git'),'Owner/project')
        self.assertEqual(o.repo_url('https://github.com/Owner/project/'),'Owner/project')
        for url in ('http://github.com/a/b','https://github.com.evil.test/a/b','https://github.com/a/b/issues','https://github.com/a/..','https://github.com/a/b?x=1','https://user@github.com/a/b'):
            with self.subTest(url=url), self.assertRaises(ValueError):o.repo_url(url)
    def test_name_unique_and_stable(self):
        self.assertEqual(o.repo_key('Owner/project'),o.repo_key('owner/PROJECT'))
        self.assertNotEqual(o.repo_key('owner/project'),o.repo_key('other/project'))
        self.assertRegex(o.repo_key('owner/_.project'),'^[a-z0-9][a-z0-9_-]{0,63}$')
    def test_merge_method(self):
        info={'full_name':'org/demo','default_branch':'trunk','has_issues':True,'permissions':{'push':True},'allow_merge_commit':True}
        with patch.object(o,'gh',return_value=info):
            self.assertEqual(o.repo_details('org/demo')[1:],('org/demo','trunk','merge'))
    def test_read_only_repo_rejected(self):
        with patch.object(o,'gh',return_value={'permissions':{'push':False}}), self.assertRaises(ValueError):o.repo_details('org/demo')
    def test_bootstrap_issue_reuse(self):
        issue={'number':7,'body':'<!-- autocontroller-bootstrap:v1 -->','state':'open','author_association':'OWNER'}
        with patch.object(o.c,'gh_api_pages',return_value=[issue]),patch.object(o,'gh') as gh:
            self.assertEqual(o.find_or_create_bootstrap('org/demo',1),7);gh.assert_not_called()
    def test_closed_bootstrap_refuses(self):
        issue={'number':7,'body':'<!-- autocontroller-bootstrap:v1 -->','state':'closed','author_association':'MEMBER'}
        with patch.object(o.c,'gh_api_pages',return_value=[issue]),self.assertRaises(ValueError):o.find_or_create_bootstrap('org/demo',1)
    def test_outsider_marker_issue_is_not_adopted_as_bootstrap(self):
        # B1 regression: an outsider's copy of the marker must not be adopted (it
        # would then never be imported and onboarding would fail forever).
        outsider={'number':7,'body':'<!-- autocontroller-bootstrap:v1 -->','state':'open','author_association':'NONE'}
        created={'number':8}
        with patch.object(o.c,'gh_api_pages',return_value=[outsider]),patch.object(o,'gh',return_value=created) as gh:
            self.assertEqual(o.find_or_create_bootstrap('org/demo',1),8)
        gh.assert_called_once()
        self.assertEqual(gh.call_args.args[0:2],('api','--method'))
    def test_outsider_marker_does_not_cause_multiple_bootstrap_refusal(self):
        issues=[{'number':7,'body':'<!-- autocontroller-bootstrap:v1 -->','state':'open','author_association':'NONE'},
                {'number':9,'body':'<!-- autocontroller-bootstrap:v1 -->','state':'closed','author_association':'CONTRIBUTOR'},
                {'number':11,'body':'<!-- autocontroller-bootstrap:v1 -->','state':'open','author_association':'OWNER'}]
        with patch.object(o.c,'gh_api_pages',return_value=issues),patch.object(o,'gh') as gh:
            self.assertEqual(o.find_or_create_bootstrap('org/demo',1),11)
        gh.assert_not_called()
    def test_import_skips_pr_and_existing_and_renovate(self):
        project=Mock(repo='org/demo',number=1,owner='org');project.items.return_value=[{'number':1}]
        bot={'login':'renovate[bot]','type':'Bot','id':29139614}
        issues=[{'number':1,'author_association':'OWNER'},
                {'number':2,'pull_request':{'url':'x'},'html_url':'https://github.com/org/demo/pull/2','author_association':'OWNER'},
                {'number':3,'user':bot,'author_association':'NONE'},
                {'number':4,'html_url':'https://github.com/org/demo/issues/4','author_association':'MEMBER'}]
        with patch.object(o.c,'gh_api_pages',return_value=issues),patch.object(o,'run') as run:
            o.import_issues(project);run.assert_called_once_with('gh','project','item-add','1','--owner','org','--url','https://github.com/org/demo/issues/4')
    def test_import_never_adds_outsider_or_unverified_issues(self):
        project=Mock(repo='org/demo',number=1,owner='org');project.items.return_value=[]
        fake_bot={'login':'renovate[bot]','type':'User','id':1}
        issues=[{'number':10,'html_url':'https://github.com/org/demo/issues/10','author_association':'NONE'},
                {'number':11,'html_url':'https://github.com/org/demo/issues/11','author_association':'CONTRIBUTOR'},
                {'number':12,'html_url':'https://github.com/org/demo/issues/12'},
                {'number':13,'html_url':'https://github.com/org/demo/issues/13','author_association':''},
                # Not a verified Renovate identity (type User), so it is judged by association only.
                {'number':14,'html_url':'https://github.com/org/demo/issues/14','user':fake_bot,'author_association':'NONE'},
                {'number':15,'html_url':'https://github.com/org/demo/issues/15','author_association':'COLLABORATOR'}]
        with patch.object(o.c,'gh_api_pages',return_value=issues),patch.object(o,'run') as run:
            o.import_issues(project)
        self.assertEqual([c.args[-1] for c in run.call_args_list],['https://github.com/org/demo/issues/15'])
    def test_project_refuses_shared_config(self):
        data={'projects':[{'title':'AutoController · org/demo','number':5}]}
        with patch.object(o,'gh',return_value=data),patch.object(o,'run') as run,self.assertRaises(ValueError):
            o.project_for('org/demo','org',{'projects':[{'project_owner':'ORG','project_number':5}]})
        run.assert_not_called()
    def test_project_creation_resumes_by_exact_title(self):
        data={'projects':[{'title':'AutoController · org/demo','number':5,'url':'https://github.com/orgs/org/projects/5'}]}
        with patch.object(o,'gh',side_effect=[data,{'items':[]}]) as gh,patch.object(o,'run') as run,patch.object(o.c.GitHubProject,'ensure_metadata'):
            project,url=o.project_for('org/demo','org',{'projects':[]})
            self.assertEqual(project.number,5); self.assertEqual(gh.call_count,2)
            run.assert_called_once_with('gh','project','link','5','--owner','org','--repo','org/demo')
    def test_standard_requires_meaningful_files_and_marker(self):
        paths=set(b.REQUIRED)|{'mise.toml','renovate.json'}
        self.assertEqual(b.missing(paths),[])
        self.assertIn('AGENTS.md',b.missing(paths-{'AGENTS.md'}))
        tree={'tree':[{'path':p,'type':'blob','mode':'100644','size':1} for p in paths]}
        gh=Mock(side_effect=[tree,{'encoding':'base64','content':base64.b64encode(b'1\n').decode()}])
        self.assertEqual(b.remote_errors(gh,'org/demo','a'*40,'1'),[])
    def test_remote_truncated_tree_never_passes(self):
        with self.assertRaises(ValueError):b.remote_errors(Mock(return_value={'truncated':True,'tree':[]}), 'org/demo','a'*40,'1')
    def test_bootstrap_gates_before_renovate_and_selector(self):
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            cfg={'name':'demo','repo':'org/demo','directory':'/tmp/demo','project_owner':'org','project_number':1,'bootstrap_issue':42,'standard_version':'1'}
            w=o.c.ProjectWorker(cfg,{'state_dir':tmp,'wrapper':'/tmp/wrapper','opencode':'/tmp/opencode'},threading.Event(),threading.Lock())
            item={'number':42,'status':'Todo','url':'https://github.com/org/demo/issues/42','item_id':'1'}
            with patch.object(w,'checkout_neutral',return_value=True),patch.object(w,'ensure_unsafe_group_gone'),patch.object(w.project,'items',return_value=[item]),patch.object(o.c,'issue_state',return_value='open'),patch.object(w,'_cycle') as cycle,patch.object(o.c,'renovate_prs') as renovate:
                w.one_iteration();cycle.assert_called_once_with('issue',42,item['url'],item);renovate.assert_not_called()
            with patch.object(w.project,'items',return_value=[]),patch.object(w,'_cycle') as cycle:
                self.assertTrue(w.bootstrap_gate());cycle.assert_not_called()
            o.c.json_atomic(w.bootstrap_done,{'issue':42,'version':'1','commit':'a'*40})
            self.assertFalse(w.bootstrap_gate())

if __name__=='__main__':unittest.main()
