import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
import onboarding as o
import bootstrap_policy as b

# The authenticated account onboarding runs as (the shape authorize() returns).
VIEWER={'login':'example-owner','type':'User','id':1001}

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
            self.assertEqual(o.find_or_create_bootstrap('org/demo',1,VIEWER),7);gh.assert_not_called()
    def test_closed_bootstrap_refuses(self):
        issue={'number':7,'body':'<!-- autocontroller-bootstrap:v1 -->','state':'closed','author_association':'MEMBER'}
        with patch.object(o.c,'gh_api_pages',return_value=[issue]),self.assertRaises(ValueError):o.find_or_create_bootstrap('org/demo',1,VIEWER)
    def test_outsider_marker_issue_is_not_adopted_as_bootstrap(self):
        # B1 regression: an outsider's copy of the marker must not be adopted (it
        # would then never be imported and onboarding would fail forever).
        outsider={'number':7,'body':'<!-- autocontroller-bootstrap:v1 -->','state':'open','author_association':'NONE'}
        created={'number':8}
        with patch.object(o.c,'gh_api_pages',return_value=[outsider]),patch.object(o,'gh',return_value=created) as gh:
            self.assertEqual(o.find_or_create_bootstrap('org/demo',1,VIEWER),8)
        gh.assert_called_once()
        self.assertEqual(gh.call_args.args[0:2],('api','--method'))
    def test_outsider_marker_does_not_cause_multiple_bootstrap_refusal(self):
        issues=[{'number':7,'body':'<!-- autocontroller-bootstrap:v1 -->','state':'open','author_association':'NONE'},
                {'number':9,'body':'<!-- autocontroller-bootstrap:v1 -->','state':'closed','author_association':'CONTRIBUTOR'},
                {'number':11,'body':'<!-- autocontroller-bootstrap:v1 -->','state':'open','author_association':'OWNER'}]
        with patch.object(o.c,'gh_api_pages',return_value=issues),patch.object(o,'gh') as gh:
            self.assertEqual(o.find_or_create_bootstrap('org/demo',1,VIEWER),11)
        gh.assert_not_called()
    def test_own_marker_issue_adopted_by_account_id_when_association_untrusted(self):
        # B-1 regression: a non-trusted authenticated account must re-find the bootstrap it created.
        own={'number':7,'body':'<!-- autocontroller-bootstrap:v1 -->','state':'open','author_association':'CONTRIBUTOR',
             'user':{'login':'example-owner','type':'User','id':1001}}
        with patch.object(o.c,'gh_api_pages',return_value=[own]),patch.object(o,'gh') as gh:
            self.assertEqual(o.find_or_create_bootstrap('org/demo',1,VIEWER),7)
        gh.assert_not_called()
    def test_untrusted_marker_by_other_account_not_adopted_by_id(self):
        outsider={'number':7,'body':'<!-- autocontroller-bootstrap:v1 -->','state':'open','author_association':'CONTRIBUTOR',
                  'user':{'login':'example-outsider','type':'User','id':2002}}
        with patch.object(o.c,'gh_api_pages',return_value=[outsider]),patch.object(o,'gh',return_value={'number':8}) as gh:
            self.assertEqual(o.find_or_create_bootstrap('org/demo',1,VIEWER),8)
        gh.assert_called_once()
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

CLASSIC_EVERYONE={'protected':True,'protection':{'enabled':True,'required_status_checks':{'enforcement_level':'everyone','contexts':['required'],'checks':[]}}}


def _rule(ruleset_id, context='required'):
    return {'type':'required_status_checks','ruleset_id':ruleset_id,'parameters':{'required_status_checks':[{'context':context}]}}


class ProtectionStateTests(unittest.TestCase):
    """Read-only classification of the default branch. Fake GitHub responses are deterministic dicts."""

    def state(self, summary, rules=(), docs=None, admin=False):
        docs = docs or {}
        def gh(*args):
            path = args[1]
            if path.startswith('repos/org/demo/branches/'):
                return summary
            if path.startswith('repos/org/demo/rulesets/'):
                return docs[int(path.rsplit('/', 1)[1])]
            raise AssertionError(f'unexpected gh call {args}')
        with patch.object(o,'gh',side_effect=gh) as g, patch.object(o.c,'gh_api_pages',return_value=list(rules)):
            return o.protection_state('org/demo','main','required',admin), g

    def test_classic_everyone_with_required_is_enforced(self):
        self.assertEqual(self.state(CLASSIC_EVERYONE)[0],'enforced')

    def test_classic_non_admins_enforces_only_for_non_admin(self):
        summary={'protected':True,'protection':{'required_status_checks':{'enforcement_level':'non_admins','contexts':['required'],'checks':[]}}}
        self.assertEqual(self.state(summary,admin=True)[0],'missing')
        self.assertEqual(self.state(summary,admin=False)[0],'enforced')

    def test_classic_checks_array_with_required_counts(self):
        summary={'protected':True,'protection':{'required_status_checks':{'enforcement_level':'everyone','contexts':[],'checks':[{'context':'required','app_id':15368}]}}}
        self.assertEqual(self.state(summary)[0],'enforced')

    def test_classic_without_required_context_is_missing(self):
        summary={'protected':True,'protection':{'required_status_checks':{'enforcement_level':'everyone','contexts':['lint'],'checks':[{'context':'build'}]}}}
        self.assertEqual(self.state(summary)[0],'missing')

    def test_unprotected_branch_is_missing(self):
        summary={'protected':False,'protection':{'enabled':False,'required_status_checks':{'enforcement_level':'off','contexts':[],'checks':[]}}}
        self.assertEqual(self.state(summary)[0],'missing')

    def test_protected_without_protection_object_is_ambiguous(self):
        for summary in ({'protected':True}, {'protected':True,'protection':{}}, {'protected':True,'protection':{'required_status_checks':None}}):
            with self.subTest(summary=summary), self.assertRaises(ValueError):
                self.state(summary)

    def test_malformed_classic_fields_are_ambiguous(self):
        bad = [
            {'enforcement_level':'everyone','contexts':'required','checks':[]},
            {'enforcement_level':'everyone','contexts':['required'],'checks':'x'},
            {'enforcement_level':None,'contexts':['required'],'checks':[]},
            {'enforcement_level':'everyone','contexts':['required'],'checks':[{'app_id':1}]},
        ]
        for rsc in bad:
            with self.subTest(rsc=rsc), self.assertRaises(ValueError):
                self.state({'protected':True,'protection':{'required_status_checks':rsc}})

    def test_non_bool_protected_is_ambiguous(self):
        with self.assertRaises(ValueError):
            self.state({'protected':'yes'})

    def test_active_ruleset_never_bypass_is_enforced(self):
        docs={7:{'enforcement':'active','current_user_can_bypass':'never'}}
        self.assertEqual(self.state({'protected':False},rules=[_rule(7)],docs=docs)[0],'enforced')

    def test_ruleset_with_bypass_does_not_enforce_for_this_account(self):
        for bypass in ('always','pull_requests_only','exempt'):
            with self.subTest(bypass=bypass):
                docs={7:{'enforcement':'active','current_user_can_bypass':bypass}}
                self.assertEqual(self.state({'protected':False},rules=[_rule(7)],docs=docs)[0],'missing')

    def test_non_returned_rulesets_do_not_enforce(self):
        # The rules endpoint lists only active rulesets; evaluate/disabled ones never appear there.
        self.assertEqual(self.state({'protected':False},rules=[])[0],'missing')

    def test_missing_bypass_field_is_ambiguous(self):
        docs={7:{'enforcement':'active'}}
        with self.assertRaises(ValueError):
            self.state({'protected':False},rules=[_rule(7)],docs=docs)

    def test_rule_without_type_is_ambiguous(self):
        with self.assertRaises(ValueError):
            self.state({'protected':False},rules=[{'parameters':{}}])

    def test_required_rule_without_ruleset_id_is_ambiguous(self):
        rule={'type':'required_status_checks','parameters':{'required_status_checks':[{'context':'required'}]}}
        with self.assertRaises(ValueError):
            self.state({'protected':False},rules=[rule])

    def test_over_limit_rules_collection_refuses_truncated_selection(self):
        pages=[[{'type':'other'}]*1001]
        with patch.object(o,'gh',return_value={'protected':False}), patch.object(o.c,'gh_json',return_value=pages):
            with self.assertRaises(ValueError) as caught:
                o.protection_state('org/demo','main','required',False)
        self.assertIn('Owner action',str(caught.exception))
        self.assertIsInstance(caught.exception.__cause__,o.c.ControllerError)

    def test_non_list_page_refuses(self):
        with patch.object(o,'gh',return_value={'protected':False}), patch.object(o.c,'gh_json',return_value=[{'type':'x'}]):
            with self.assertRaises(ValueError) as caught:
                o.protection_state('org/demo','main','required',False)
        self.assertIn('Owner action',str(caught.exception))

    def test_classic_enforced_is_not_blocked_by_broken_rules_response(self):
        # Item 5: classic enforcement is decided first, so an unrelated ambiguous rules response cannot block it.
        with patch.object(o,'gh',return_value=CLASSIC_EVERYONE), patch.object(o.c,'gh_api_pages',side_effect=AssertionError('rules must not be read')):
            self.assertEqual(o.protection_state('org/demo','main','required',False),'enforced')
        broken=[{'parameters':{}}]
        with patch.object(o,'gh',return_value=CLASSIC_EVERYONE), patch.object(o.c,'gh_api_pages',return_value=broken):
            self.assertEqual(o.protection_state('org/demo','main','required',False),'enforced')

    def test_ambiguous_rules_without_classic_still_refuse(self):
        with self.assertRaises(ValueError):
            self.state({'protected':False},rules=[{'parameters':{}}])

    def test_rules_controller_error_becomes_owner_action_value_error(self):
        with patch.object(o,'gh',return_value={'protected':False}), patch.object(o.c,'gh_api_pages',side_effect=o.c.ControllerError('rc=1: API error')):
            with self.assertRaises(ValueError) as caught:
                o.protection_state('org/demo','main','required',False)
        self.assertIn('Owner action',str(caught.exception))
        self.assertIn('not changed',str(caught.exception))
        self.assertIsInstance(caught.exception.__cause__,o.c.ControllerError)

    def test_ruleset_by_id_read_error_becomes_owner_action_value_error(self):
        # Item C: a failed repos/{repo}/rulesets/{id} read is wrapped like the other governance reads.
        rule=_rule(7)
        def gh(*args):
            if args[1].startswith('repos/org/demo/branches/'): return {'protected':False}
            if args[1]=='repos/org/demo/rulesets/7': raise o.c.ControllerError('rc=1: ruleset read failed')
            raise AssertionError(args)
        with patch.object(o,'gh',side_effect=gh), patch.object(o.c,'gh_api_pages',return_value=[rule]):
            with self.assertRaises(ValueError) as caught:
                o.protection_state('org/demo','main','required',False)
        self.assertIn('Owner action',str(caught.exception))
        self.assertIn('ruleset read failed',str(caught.exception))
        self.assertIsInstance(caught.exception.__cause__,o.c.ControllerError)

    def test_branch_summary_read_is_read_only(self):
        _, g = self.state(CLASSIC_EVERYONE)
        self.assertEqual([c.args[0] for c in g.call_args_list], ['api'])
        self.assertTrue(all('--method' not in c.args for c in g.call_args_list))


class ResumeWarningTests(unittest.TestCase):
    """Read-only warning for an enrolled repository on the resume path. Never prompts or writes."""

    ENTRY={'repo':'org/demo','base_branch':'trunk','required_check':'required'}

    def warn(self, gh_side_effect, pages=()):
        import io
        from contextlib import redirect_stderr
        err=io.StringIO()
        with patch.object(o,'gh',side_effect=gh_side_effect), patch.object(o.c,'gh_api_pages',return_value=list(pages)), \
             patch.object(o.subprocess,'run') as run, redirect_stderr(err):
            o.warn_if_not_enforced(self.ENTRY)
        run.assert_not_called()
        return err.getvalue()

    @staticmethod
    def responder(summary, admin=True):
        def gh(*args):
            if args[1]=='repos/org/demo': return {'permissions':{'admin':admin}}
            if args[1].startswith('repos/org/demo/branches/trunk'): return summary
            raise AssertionError(args)
        return gh

    def test_enforced_prints_no_warning(self):
        self.assertEqual(self.warn(self.responder(CLASSIC_EVERYONE)),'')

    def test_protection_removed_prints_warning(self):
        text=self.warn(self.responder({'protected':False}))
        self.assertIn('no longer requires status check',text)
        self.assertIn('will not add protection',text)

    def test_read_error_prints_warning_and_does_not_raise(self):
        def gh(*args):
            raise o.c.ControllerError('rc=1: boom')
        self.assertIn('could not verify',self.warn(gh))

    def test_non_dict_repo_response_warns_instead_of_raising(self):
        # Item A: a malformed repos/{repo} body must not escape as AttributeError.
        def gh(*args):
            if args[1]=='repos/org/demo': return [{'permissions':{'admin':True}}]
            raise AssertionError(args)
        text=self.warn(gh)
        self.assertIn('could not verify',text)
        self.assertIn('WARNING',text)


class NoProtectionMutationSourceTests(unittest.TestCase):
    def test_onboarding_source_never_writes_protection_or_rulesets_except_one_post(self):
        import re
        source=Path(o.__file__).read_text(encoding='utf-8')
        for line in source.splitlines():
            if re.search(r"(PUT|PATCH|DELETE)",line) and re.search(r"/protection|/rulesets",line):
                self.fail(f'onboarding must not write protection or rulesets: {line.strip()}')
        posts=[l for l in source.splitlines() if "'POST'" in l and "rulesets" in l]
        self.assertEqual(len(posts),1)


class EnsureRequiredCheckTests(unittest.TestCase):
    """Consent and mutation policy. The POST is observed at subprocess.run; nothing reaches GitHub."""

    UNPROTECTED={'protected':False}

    def run_ensure(self, *, admin, ask, summaries, owned=(), post_rc=0, rules=(), docs=None):
        """summaries: successive branch summaries returned by the read path (pre and post verification)."""
        summaries=list(summaries)
        def gh(*args):
            if args[1].startswith('repos/org/demo/branches/'):
                return summaries.pop(0) if len(summaries)>1 else summaries[0]
            if args[1].startswith('repos/org/demo/rulesets/'):
                return (docs or {})[int(args[1].rsplit('/',1)[1])]
            raise AssertionError(args)
        def pages(path,max_items=1000):
            if '/rulesets?' in path: return list(owned)
            return list(rules)
        proc=Mock(returncode=post_rc,stderr='plan does not support rulesets')
        # The consent path is an interactive terminal path; clear any agent markers the test host sets.
        clean={k:v for k,v in o.os.environ.items() if k not in o.lifecycle.agent_markers(o.os.environ)}
        with patch.object(o,'gh',side_effect=gh), patch.object(o.c,'gh_api_pages',side_effect=pages), \
             patch.object(o.subprocess,'run',return_value=proc) as run, patch.dict(o.os.environ,clean,clear=True):
            try:
                o.ensure_required_check('org/demo','main','required',{'admin':admin},ask=ask)
                error=None
            except (ValueError,o.lifecycle.LifecycleError) as exc:
                error=exc
        return error, run

    def test_missing_admin_fails_closed_with_owner_action_and_no_post(self):
        error,run=self.run_ensure(admin=False,ask=Mock(),summaries=[self.UNPROTECTED])
        self.assertIsNotNone(error)
        self.assertIn('ask a repository admin',str(error))
        run.assert_not_called()

    def test_admin_consent_yes_posts_exactly_one_additive_ruleset(self):
        ask=Mock(return_value='y')
        # Verification after the POST sees the enforced classic protection (simulated acceptance).
        error,run=self.run_ensure(admin=True,ask=ask,summaries=[self.UNPROTECTED,CLASSIC_EVERYONE])
        self.assertIsNone(error)
        ask.assert_called_once()
        self.assertIn('AutoController required check',ask.call_args.args[0])
        posts=[c for c in run.call_args_list if c.args[0][:4]==['gh','api','--method','POST']]
        self.assertEqual(len(posts),1)
        self.assertEqual(posts[0].args[0],['gh','api','--method','POST','repos/org/demo/rulesets','--input','-'])
        body=json.loads(posts[0].kwargs['input'])
        self.assertEqual(body['name'],'AutoController required check')
        self.assertEqual(body['target'],'branch')
        self.assertEqual(body['enforcement'],'active')
        self.assertEqual(body['bypass_actors'],[])
        self.assertEqual(body['conditions'],{'ref_name':{'include':['refs/heads/main'],'exclude':[]}})
        self.assertEqual(body['rules'][0]['type'],'required_status_checks')
        self.assertEqual(body['rules'][0]['parameters']['required_status_checks'],[{'context':'required','integration_id':15368}])
        self.assertEqual(body['rules'][0]['parameters']['strict_required_status_checks_policy'],False)
        self.assertEqual(body['rules'][0]['parameters']['do_not_enforce_on_create'],False)

    def test_decline_and_eof_refuse_without_post(self):
        for ask in (Mock(return_value='n'), Mock(side_effect=EOFError), Mock(return_value='')):
            with self.subTest(ask=ask):
                error,run=self.run_ensure(admin=True,ask=ask,summaries=[self.UNPROTECTED])
                self.assertIsNotNone(error)
                self.assertIn('owner declined',str(error))
                self.assertIn('Branch protection and rulesets were not changed',str(error))
                run.assert_not_called()

    def test_post_failure_surfaces_stderr_and_owner_action(self):
        error,run=self.run_ensure(admin=True,ask=Mock(return_value='y'),summaries=[self.UNPROTECTED],post_rc=1)
        self.assertIsNotNone(error)
        self.assertIn('plan does not support rulesets',str(error))
        self.assertIn('owner action',str(error))
        self.assertIn('lost response',str(error))

    def test_owned_same_named_ruleset_not_enforcing_refuses_without_post(self):
        owned=[{'id':9,'name':'AutoController required check','enforcement':'disabled'}]
        ask=Mock(return_value='y')
        error,run=self.run_ensure(admin=True,ask=ask,summaries=[self.UNPROTECTED],owned=owned)
        self.assertIsNotNone(error)
        self.assertIn('will not modify it',str(error))
        ask.assert_not_called()
        run.assert_not_called()

    def test_post_then_unverified_result_refuses(self):
        error,run=self.run_ensure(admin=True,ask=Mock(return_value='y'),summaries=[self.UNPROTECTED,self.UNPROTECTED])
        self.assertIsNotNone(error)
        self.assertIn('does not show',str(error))
        self.assertEqual(len([c for c in run.call_args_list if '--method' in c.args[0]]),1)

    def test_post_timeout_becomes_owner_action_with_lost_response_hint(self):
        ask=Mock(return_value='y')
        summaries=[self.UNPROTECTED]
        def gh(*args):
            return summaries[0]
        with patch.object(o,'gh',side_effect=gh), patch.object(o.c,'gh_api_pages',return_value=[]), \
             patch.dict(o.os.environ,{},clear=True), \
             patch.object(o.subprocess,'run',side_effect=o.subprocess.TimeoutExpired(['gh'],120)):
            with self.assertRaises(ValueError) as caught:
                o.ensure_required_check('org/demo','main','required',{'admin':True},ask=ask)
        self.assertIn('lost response',str(caught.exception))
        self.assertIn('owner action',str(caught.exception))

    def test_owned_ruleset_listing_error_becomes_owner_action_value_error(self):
        ask=Mock(return_value='y')
        def gh(*args):
            return self.UNPROTECTED
        def pages(path,max_items=1000):
            if '/rulesets?' in path: raise o.c.ControllerError('rc=1: listing failed')
            return []
        with patch.object(o,'gh',side_effect=gh), patch.object(o.c,'gh_api_pages',side_effect=pages), patch.object(o.subprocess,'run') as run, \
             patch.dict(o.os.environ,{},clear=True):
            with self.assertRaises(ValueError) as caught:
                o.ensure_required_check('org/demo','main','required',{'admin':True},ask=ask)
        self.assertIn('Owner action',str(caught.exception))
        self.assertIn('not changed',str(caught.exception))
        ask.assert_not_called()
        run.assert_not_called()

    def test_agent_session_refused_before_consent_prompt_and_post(self):
        # Item 1: an OpenCode session may read protection but must never reach the consent prompt or a POST.
        ask=Mock(return_value='y')
        def gh(*args):
            return self.UNPROTECTED
        with patch.object(o,'gh',side_effect=gh), patch.object(o.c,'gh_api_pages',return_value=[]), \
             patch.object(o.subprocess,'run') as run, \
             patch.dict(o.os.environ,{'OPENCODE_SESSION_ID':'ses_test'}):
            with self.assertRaises(o.lifecycle.LifecycleError) as caught:
                o.ensure_required_check('org/demo','main','required',{'admin':True},ask=ask)
        self.assertIn('adding a protection ruleset',str(caught.exception))
        ask.assert_not_called()
        run.assert_not_called()

    def test_already_enforced_is_read_only(self):
        error,run=self.run_ensure(admin=True,ask=Mock(),summaries=[CLASSIC_EVERYONE])
        self.assertIsNone(error)
        run.assert_not_called()


if __name__=='__main__':unittest.main()
