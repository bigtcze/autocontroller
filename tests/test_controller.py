import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC=importlib.util.spec_from_file_location("autonomy_controller",str(Path(__file__).resolve().parents[1]/"autonomy-controller.py"))
m=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


def issue(n,status="Backlog",repo="example-owner/example-project"):
    return {"number":n,"status":status,"item_id":f"PVTI_{n}","title":f"Work {n}","url":f"https://github.com/{repo}/issues/{n}"}


class SelectionTests(unittest.TestCase):
    def test_filters_foreign_project_item(self):
        self.assertIsNone(m.normalize_project_item({"content":{"type":"Issue","url":"https://github.com/other/repo/issues/5"}},"example-owner/example-project"))
    def test_filters_pr_project_item(self):
        self.assertIsNone(m.normalize_project_item({"content":{"type":"PullRequest","url":"https://github.com/example-owner/example-project/pull/5"}},"example-owner/example-project"))
    def test_keeps_only_own_issue(self):
        o=m.normalize_project_item({"id":"a","status":"Backlog","content":{"type":"Issue","url":"https://github.com/example-owner/example-project/issues/7"}},"example-owner/example-project")
        self.assertEqual(o["number"],7)
    def test_selection_is_an_agent_decision_not_status_or_priority_rank(self):
        prompt=m.selection_prompt('example-owner/example-project', [issue(7,'Review'),issue(3,'Backlog')],False)
        self.assertIn('OMO Slim', prompt)
        self.assertIn('AUTOCONTROLLER_SELECT_ISSUE', prompt)
        self.assertIn('GitHub Project Issues', prompt)
        self.assertIn('only Issues you may select', prompt)
        self.assertNotIn('p0:0',prompt)

    def test_selection_verdict_one_number_or_no_action(self):
        self.assertEqual(m.selection_verdict('Done\nAUTOCONTROLLER_SELECT_ISSUE #37'),37)
        self.assertIsNone(m.selection_verdict('Reason: all blocked\nAUTOCONTROLLER_NO_ACTION'))
        for raw in ('no marker', 'AUTOCONTROLLER_SELECT_ISSUE #1\nAUTOCONTROLLER_SELECT_ISSUE #2',
                    'AUTOCONTROLLER_NO_ACTION\nAUTOCONTROLLER_SELECT_ISSUE #1'):
            with self.subTest(raw=raw), self.assertRaises(m.ControllerError):
                m.selection_verdict(raw)

    def test_custom_project_status_is_not_silently_dropped(self):
        item=m.normalize_project_item({'status':'Development Queue',
            'content':{'type':'Issue','url':'https://github.com/example-owner/example-project/issues/7'}},'example-owner/example-project')
        self.assertEqual(item['status'],'Development Queue')

    def test_renovate_login(self):
        repo="example-owner/example-project"
        same_repo={"head":{"ref":"renovate/go-something","repo":{"full_name":repo}},
                   "base":{"ref":"main","repo":{"full_name":repo}}}
        bot={"login":"renovate[bot]","type":"Bot","id":29139614}
        self.assertTrue(m.is_renovate({"user":bot,**same_repo},repo))
        self.assertFalse(m.is_renovate({"user":{"login":"dependabot[bot]","type":"Bot","id":1},**same_repo},repo))
        # Branch prefix alone is never identity.
        self.assertFalse(m.is_renovate({"head":{"ref":"renovate/go-something"}},repo))

    def test_renovate_identity_rejects_impersonation(self):
        repo="example-owner/example-project"
        same_repo={"head":{"ref":"renovate/pkg","repo":{"full_name":repo}},
                   "base":{"ref":"main","repo":{"full_name":repo}}}
        for user in ({"login":"renovate","type":"User","id":5},             # human account named renovate
                     {"login":"renovate[bot]","type":"User","id":29139614}, # bot login, wrong type
                     {"login":"renovate[bot]","type":"Bot","id":1234},      # bot login, wrong id
                     {"login":"app/renovate","type":"Bot","id":29139614},   # no GraphQL author path
                     {"login":"renovate[bot]"}):                            # missing type/id
            with self.subTest(user=user):
                self.assertFalse(m.is_renovate({"user":user,**same_repo},repo))
                self.assertFalse(m.releases.renovate_pr({"user":user,**same_repo},repo))
        self.assertTrue(m.is_renovate({"user":{"login":"Renovate[bot]","type":"Bot","id":29139614},**same_repo},repo))

    def test_fork_renovate_pr_from_genuine_bot_rejected(self):
        repo="example-owner/example-project"
        fork={"user":{"login":"renovate[bot]","type":"Bot","id":29139614},
              "head":{"ref":"renovate/pkg","repo":{"full_name":"attacker/example-project"}},
              "base":{"ref":"main","repo":{"full_name":repo}}}
        self.assertFalse(m.is_renovate(fork,repo))
        self.assertFalse(m.releases.renovate_pr(fork,repo))

    def test_missing_head_repository_rejected(self):
        repo="example-owner/example-project"
        bot={"login":"renovate[bot]","type":"Bot","id":29139614}
        no_head={"user":bot,"head":{"ref":"renovate/pkg","repo":None},"base":{"ref":"main","repo":{"full_name":repo}}}
        self.assertFalse(m.is_renovate(no_head,repo))
        self.assertFalse(m.is_renovate({"user":bot,"head":{"ref":"renovate/pkg"}},repo))

    def test_renovate_issue_filter_uses_identity_not_login_text(self):
        self.assertTrue(m.releases.is_renovate_identity({"login":"renovate[bot]","type":"Bot","id":29139614}))
        self.assertFalse(m.releases.is_renovate_identity({"login":"renovate","type":"User","id":29139614}))
    def test_rate_limit_classification(self):
        self.assertTrue(m.QUOTA_ERROR.search("HTTP 429 rate limit exceeded"))
        self.assertFalse(m.QUOTA_ERROR.search("Agent not found: orchestrator"))
    def test_project_metadata_supports_default_todo_status(self):
        gp=m.GitHubProject("example-owner/example-project","example-owner",1)
        def fake(*args,**kwargs):
            return {"id":"PVT1"} if "view" in args else {"fields":[{"name":"Status","id":"FIELD","options":[{"name":"Todo","id":"a"},{"name":"In Progress","id":"b"},{"name":"Done","id":"c"}]}]}
        with patch.object(m,"gh_json",side_effect=fake):
            gp.ensure_metadata()
        self.assertEqual(gp.project_id,"PVT1")
        self.assertEqual(gp.options["todo"],"a")
    def test_distinct_github_projects_enforced(self):
        cfg={"projects":[
            {"name":"example-project","repo":"example-owner/example-project","directory":"/home/example/repositories/example-project","project_owner":"example-owner","project_number":1},
            {"name":"pve","repo":"example-owner/example-exporter","directory":"/home/example/repositories/example-exporter","project_owner":"example-owner","project_number":1},
        ]}
        with self.assertRaises(m.ControllerError):m.validate_config(cfg)
    def test_project_list_fails_closed_on_truncation(self):
        gp=m.GitHubProject("example-owner/example-project","example-owner",1)
        with patch.object(m,"gh_json",return_value={"items":[],"totalCount":100}):
            with self.assertRaises(m.ControllerError):gp.items()
    def test_pr_completion_url_validation(self):
        self.assertTrue(m.PR_URL.match("https://github.com/example-owner/example-project/pull/14"))
    def test_prompt_no_clone_or_new_worktree(self):
        prompt=m.work_prompt("example-owner/example-project","renovate",4,"https://github.com/example-owner/example-project/pull/4")
        self.assertIn("Do not clone",prompt)
        self.assertIn("git worktrees",prompt)
    def test_state_write_is_atomic_and_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/"a.json"
            m.json_atomic(p,{"a":1})
            self.assertEqual(m.json_read(p),{"a":1})
            self.assertEqual(p.stat().st_mode&0o777,0o600)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg={"name":"example-project","repo":"example-owner/example-project","directory":"/home/example/repositories/example-project","project_owner":"example-owner","project_number":1}
        global_cfg={"state_dir":self.tmp.name,"wrapper":"/tmp/wrapper","opencode":"/tmp/opencode"}
        import threading
        self.w=m.ProjectWorker(cfg,global_cfg,threading.Event(),threading.Lock())
        self.checkout=patch.object(self.w,"checkout_neutral",return_value=True)
        self.checkout.start();self.addCleanup(self.checkout.stop)
    def test_retry_persists_between_instances(self):
        with patch.object(m.time,"time",return_value=100):self.w.retry("issue:3","temporary",600)
        self.assertGreater(self.w.retry_map()["issue:3"]["until"],600)
        self.assertEqual(self.w.retry_map()["issue:3"]["failures"],1)
    def test_idle_does_not_spawn_opencode(self):
        with patch.object(self.w,"quota_until",return_value=0),patch.object(self.w.project,"items",return_value=[]),patch.object(m,"renovate_prs",return_value=[]),patch.object(self.w,"run_wrapper") as wrapper:
            self.assertEqual(self.w.one_iteration(),300)
            wrapper.assert_not_called()
    def test_renovate_has_priority_over_backlog(self):
        pr={"number":42,"html_url":"https://github.com/example-owner/example-project/pull/42"}
        with patch.object(self.w,"quota_until",return_value=0),patch.object(self.w.project,"items") as items,patch.object(m,"renovate_prs",return_value=[pr]),patch.object(self.w,"_cycle") as cycle:
            self.w.one_iteration()
            cycle.assert_called_once_with("renovate",42,pr["html_url"],None)
            items.assert_not_called()
    def test_pending_cycle_recovery_first(self):
        m.json_atomic(self.w.cycle_state,{"kind":"issue","number":3,"url":"https://github.com/example-owner/example-project/issues/3"})
        m.json_atomic(self.w.pending,{'status':'resuming','session_id':'ses-test'})
        with patch.object(self.w,"quota_until",return_value=0),patch.object(self.w,"_cycle") as cycle,patch.object(self.w.project,"items") as items:
            self.w.one_iteration()
            cycle.assert_called_once()
            items.assert_not_called()
    def test_quota_survives_restart(self):
        with patch.object(m.time,"time",return_value=100):self.w.quota_pause()
        self.assertGreater(self.w.quota_until(),100)
    def test_run_cycle_no_auto_done_if_issue_remains_open(self):
        with patch.object(self.w,"related_pr",return_value=None),patch.object(self.w,"run_wrapper",return_value=(0,"")),patch.object(m,"issue_state",return_value="open"),patch.object(self.w,"retry") as retry:
            self.w._cycle("issue",7,"https://github.com/example-owner/example-project/issues/7",None)
            retry.assert_called_once()
    def test_closed_issue_without_verified_pr_never_sets_done(self):
        item=issue(7,"In Progress")
        with patch.object(self.w,"related_pr",return_value=None),patch.object(self.w,"run_wrapper") as run,patch.object(m,"issue_state",return_value="closed"),patch.object(self.w.project,"set_status") as setter,patch.object(self.w,"checkout_neutral",return_value=True):
            self.w._cycle("issue",7,item["url"],item)
            run.assert_not_called()
            setter.assert_not_called()
    def test_quota_error_doesnt_mark_task_done(self):
        with patch.object(self.w,"related_pr",return_value=None),patch.object(self.w,"run_wrapper",return_value=(78,"HTTP 429 exceeded")),patch.object(self.w,"quota_pause") as quota,patch.object(m,"issue_state",return_value="open") as stat:
            self.w._cycle("issue",7,"url",None)
            quota.assert_called_once()

if __name__=="__main__":unittest.main()
