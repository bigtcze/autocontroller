"""Onboarding integration suite: real onboarding.py subprocesses against fake gh/OpenCode/systemctl.

Every test runs in a disposable World (install-root copy, PATH shims, bare
remotes, temporary HOME). Assertions use the real subprocess exit status plus
fake-GitHub, git and filesystem state, never stdout alone. The simulated GitHub
contract and its limits are documented in docs/ONBOARDING_TESTING.md.
"""
import fcntl
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import onboarding_harness  # noqa: E402
from onboarding_harness import World, OWNER, ORG, classic_protection  # noqa: E402
from onboarded_fixture import onboarded_project_entry  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FAKE_TOKEN = 'fake-oauth-token-not-real'
BOOTSTRAP_MARKER = '<!-- autocontroller-bootstrap:v1 -->'
TRUSTED = ('OWNER', 'MEMBER', 'COLLABORATOR')


class OnboardingCase(unittest.TestCase):
    def setUp(self):
        self.w = self.new_world()

    def new_world(self) -> World:
        world = World()
        self.addCleanup(world.cleanup)
        return world

    def onboard(self, repo_name_or_url, **kwargs):
        url = repo_name_or_url if repo_name_or_url.startswith('https://') else f'https://github.com/{repo_name_or_url}'
        return self.w.onboard(url, **kwargs)

    def entries(self, repo):
        return [p for p in self.w.read_config()['projects'] if p['repo'].lower() == repo.lower()]

    def project_titled(self, repo):
        return [p for p in self.w.projects() if p['title'] == f'AutoController · {repo}']

    def assert_checkout(self, repo, branch):
        self.assertEqual(self.w.checkout_git(repo, 'branch', '--show-current').strip(), branch)
        self.assertEqual(self.w.checkout_git(repo, 'status', '--porcelain').strip(), '')

    def assert_no_mutation(self):
        self.assertEqual(self.w.mutating_gh_calls(), [])
        self.assertEqual(self.w.systemctl_calls(), [])
        self.assertFalse(self.w.config_path().exists())
        self.assertEqual(self.w.projects(), [])

    def assert_success_state(self, repo, owner, branch):
        self.assertEqual(len(self.entries(repo)), 1)
        entry = self.entries(repo)[0]
        self.assertEqual(entry['base_branch'], branch)
        self.assertEqual(entry['project_owner'], owner)
        self.assertEqual(len(self.project_titled(repo)), 1)
        bootstrap = self.w.bootstrap_issues(repo)
        self.assertEqual(len(bootstrap), 1)
        self.assertEqual(entry['bootstrap_issue'], bootstrap[0]['number'])
        self.assertIn(['--user', 'enable', 'autocontroller.service'], self.w.systemctl_calls())
        self.assertIn(['--user', 'restart', 'autocontroller.service'], self.w.systemctl_calls())


class WrittenConfigShapeTests(OnboardingCase):
    """Drift guard: the entry onboarding really writes must equal the shared fixture
    that the controller-side admission tests use. Strict equality (no subset)."""

    def test_written_entry_matches_shared_admission_fixture_shape(self):
        repo = self.w.add_repo(OWNER, 'demo', default_branch='trunk')
        self.assertEqual(self.onboard(repo).rc, 0)
        written = self.entries(repo)[0]
        bootstrap = self.w.bootstrap_issues(repo)[0]['number']
        project = self.project_titled(repo)[0]
        expected = onboarded_project_entry(
            name=written['name'], repo=repo, directory=self.w.checkout_dir(repo),
            base_branch='trunk', project_owner=OWNER, project_number=project['number'],
            bootstrap_issue=bootstrap, merge_method='squash', approver=OWNER)
        self.assertEqual(written, expected)


class HappyPathTests(OnboardingCase):
    def test_existing_personal_repo_default_main(self):
        repo = self.w.add_repo(OWNER, 'demo', default_branch='main')
        real = self.w.add_issue(repo, 'Real work', association='OWNER')
        r = self.onboard(repo)
        self.assertEqual(r.rc, 0, r.stderr)
        self.assertIn('Onboarded', r.stdout)
        self.assert_success_state(repo, OWNER, 'main')
        self.assertEqual(self.entries(repo)[0]['merge_method'], 'squash')
        self.assert_checkout(repo, 'main')
        items = {int(i['url'].rsplit('/', 1)[1]) for i in self.project_titled(repo)[0]['items']}
        self.assertIn(real, items)

    def test_existing_org_repo_default_branch_trunk(self):
        repo = self.w.add_repo(ORG, 'demo', default_branch='trunk', viewer_association='MEMBER')
        r = self.onboard(repo)
        self.assertEqual(r.rc, 0, r.stderr)
        self.assert_success_state(repo, ORG, 'trunk')
        self.assertEqual(self.project_titled(repo)[0]['owner'], ORG)
        self.assert_checkout(repo, 'trunk')

    def test_empty_personal_repo_gets_exactly_one_seed_commit_on_trunk(self):
        repo = self.w.add_repo(OWNER, 'empty', default_branch='trunk', seed=False)
        r = self.onboard(repo)
        self.assertEqual(r.rc, 0, r.stderr)
        self.assertEqual(self.w.remote_commits(OWNER, 'empty', 'trunk'), 1)
        self.assertEqual(self.w.remote_branch_count(OWNER, 'empty'), 1)
        self.assert_success_state(repo, OWNER, 'trunk')
        self.assert_checkout(repo, 'trunk')

    def test_empty_org_repo_gets_exactly_one_seed_commit_on_trunk(self):
        self.w.add_repo(ORG, 'empty', default_branch='trunk', seed=False, viewer_association='MEMBER')
        r = self.onboard(f'{ORG}/empty')
        self.assertEqual(r.rc, 0, r.stderr)
        self.assertEqual(self.w.remote_commits(ORG, 'empty', 'trunk'), 1)
        self.assert_success_state(f'{ORG}/empty', ORG, 'trunk')


class RefusalTests(OnboardingCase):
    def test_merge_modes_preferred_method_and_refusal_without_methods(self):
        cases = [(('squash', 'merge', 'rebase'), 'squash'), (('merge',), 'merge'), (('rebase',), 'rebase')]
        for merge, expected in cases:
            with self.subTest(merge=merge):
                world = self.new_world()
                repo = world.add_repo(OWNER, 'demo', default_branch='trunk', merge=merge)
                self.w = world
                r = self.onboard(repo)
                self.assertEqual(r.rc, 0, r.stderr)
                self.assertEqual(self.entries(repo)[0]['merge_method'], expected)
        world = self.new_world()
        self.w = world
        repo = world.add_repo(OWNER, 'nomethod', default_branch='trunk', merge=())
        r = self.onboard(repo)
        self.assertNotEqual(r.rc, 0)
        self.assertIn('no supported merge method', r.stderr)
        self.assertFalse(world.checkout_dir(repo).exists())
        self.assert_no_mutation()

    def test_missing_push_permission_refused_before_mutation(self):
        repo = self.w.add_repo(OWNER, 'readonly', default_branch='trunk', push=False)
        r = self.onboard(repo)
        self.assertNotEqual(r.rc, 0)
        self.assertFalse(self.w.checkout_dir(repo).exists())
        self.assert_no_mutation()

    def test_issues_disabled_without_admin_refused(self):
        repo = self.w.add_repo(OWNER, 'noissues', default_branch='trunk', has_issues=False, admin=False)
        r = self.onboard(repo)
        self.assertNotEqual(r.rc, 0)
        self.assertIn('Issues are disabled', r.stderr)
        self.assertEqual([c for c in self.w.mutating_gh_calls() if 'PATCH' in c], [])
        self.assert_no_mutation()

    def test_issues_disabled_with_admin_patched_then_succeeds(self):
        repo = self.w.add_repo(OWNER, 'noissues', default_branch='trunk', has_issues=False, admin=True)
        r = self.onboard(repo)
        self.assertEqual(r.rc, 0, r.stderr)
        patch_calls = [c for c in self.w.gh_calls() if '--method' in c['argv'] and 'PATCH' in c['argv']]
        self.assertEqual(len(patch_calls), 1)
        self.assertEqual(patch_calls[0]['rc'], 0)
        self.assertTrue(self.w.gh()['repos'][repo]['has_issues'])
        self.assert_success_state(repo, OWNER, 'trunk')

    def test_org_project_creation_denied_leaves_no_project_issue_or_config(self):
        repo = self.w.add_repo(ORG, 'demo', default_branch='trunk', viewer_association='MEMBER')
        self.w.mutate_gh(lambda s: s.__setitem__('project_create_denied', [ORG]))
        r = self.onboard(repo)
        self.assertNotEqual(r.rc, 0)
        self.assertIn('denied', r.stderr)
        self.assertEqual(self.w.projects(), [])
        self.assertEqual(self.w.bootstrap_issues(repo), [])
        self.assertFalse(self.w.config_path().exists())
        self.assertEqual(self.w.systemctl_calls(), [])

    def test_personal_repo_owned_by_another_user_refused(self):
        self.w.add_owner('example-other', 'User')
        repo = self.w.add_repo('example-other', 'demo', default_branch='trunk')
        r = self.onboard(repo)
        self.assertNotEqual(r.rc, 0)
        self.assertIn('another user', r.stderr)
        self.assertEqual(self.w.projects(), [])
        self.assertEqual(self.w.bootstrap_issues(repo), [])
        self.assertFalse(self.w.config_path().exists())

    def test_missing_project_scope_triggers_refresh_and_continues(self):
        self.w.set_login(True, scopes=('repo', 'workflow'))
        repo = self.w.add_repo(OWNER, 'demo', default_branch='trunk')
        r = self.onboard(repo)
        self.assertEqual(r.rc, 0, r.stderr)
        refresh = [c['argv'] for c in self.w.gh_calls() if c['argv'][:2] == ['auth', 'refresh']]
        self.assertEqual(refresh, [['auth', 'refresh', '--hostname', 'github.com', '--scopes', 'repo,project,workflow']])
        self.assert_success_state(repo, OWNER, 'trunk')

    def test_refresh_failure_is_nonzero_without_remote_mutation(self):
        self.w.set_login(True, scopes=('repo', 'workflow'), refresh_fail=True)
        repo = self.w.add_repo(OWNER, 'demo', default_branch='trunk')
        r = self.onboard(repo)
        self.assertNotEqual(r.rc, 0)
        self.assertEqual(self.w.mutating_gh_calls(), [])
        self.assertFalse(self.w.checkout_dir(repo).exists())
        self.assertFalse(self.w.config_path().exists())


class OAuthHandoffTests(OnboardingCase):
    """The device flow itself is never run: the fake records the exact argv and never opens a browser."""

    def test_not_logged_in_runs_web_login_with_exact_arguments_then_continues(self):
        self.w.set_login(False, scopes=())
        repo = self.w.add_repo(OWNER, 'demo', default_branch='trunk')
        r = self.onboard(repo, extra_env={'BROWSER': '/bin/false'})
        self.assertEqual(r.rc, 0, r.stderr)
        logins = [c['argv'] for c in self.w.gh_calls() if c['argv'][:2] == ['auth', 'login']]
        self.assertEqual(logins, [['auth', 'login', '--hostname', 'github.com', '--git-protocol', 'https',
                                   '--web', '--scopes', 'project,workflow']])
        self.assertTrue(self.w.gh()['logged_in'])
        self.assert_success_state(repo, OWNER, 'trunk')

    def test_login_abort_is_nonzero_with_zero_remote_mutations(self):
        self.w.set_login(False, scopes=(), login_fail=True)
        repo = self.w.add_repo(OWNER, 'demo', default_branch='trunk')
        r = self.onboard(repo)
        self.assertNotEqual(r.rc, 0)
        self.assertNotIn('Onboarded', r.stdout)
        self.assertEqual(self.w.mutating_gh_calls(), [])
        self.assertFalse(self.w.checkout_dir(repo).exists())
        self.assertFalse(self.w.config_path().exists())

    def test_gh_token_in_onboarding_env_refused_before_any_gh_call(self):
        repo = self.w.add_repo(OWNER, 'demo', default_branch='trunk')
        r = self.onboard(repo, extra_env={'GH_TOKEN': FAKE_TOKEN})
        self.assertNotEqual(r.rc, 0)
        self.assertIn('Unset temporary GH_TOKEN', r.stderr)
        self.assertEqual(self.w.gh_calls(), [])
        self.assertEqual(self.w.refused_calls(), [])
        self.assertNotIn(FAKE_TOKEN, r.stdout + r.stderr)
        self.assertFalse(self.w.checkout_dir(repo).exists())

    def test_token_value_never_printed_by_successful_run(self):
        repo = self.w.add_repo(OWNER, 'demo', default_branch='trunk')
        r = self.onboard(repo)
        self.assertEqual(r.rc, 0, r.stderr)
        self.assertNotIn(FAKE_TOKEN, r.stdout + r.stderr)
        self.assertNotIn('token', self.w.config_path().read_text().lower())


def fault(match, mode='before', rc=1, skip=0):
    return {'match': match, 'mode': mode, 'rc': rc, 'skip': skip, 'once': True}


class InterruptionResumeTests(OnboardingCase):
    """Each phase fails once (nonzero, no success line), then a fault-free rerun converges."""

    def seed_issues(self, repo):
        # Trusted numbers 1, 3, 4; outsider 2. The bootstrap issue becomes 5.
        self.w.add_issue(repo, 'Owner work', association='OWNER')
        self.w.add_issue(repo, 'Outsider bug', association='NONE')
        self.w.add_issue(repo, 'Member work', association='MEMBER')
        self.w.add_issue(repo, 'Collaborator work', association='COLLABORATOR')

    def phase_setup(self, name, viewer_association='OWNER'):
        """Return (repo, extra_setup_callable) for one interruption phase, in a fresh World."""
        world = self.new_world()
        self.w = world
        if name == 'PATCH has_issues':
            repo = world.add_repo(OWNER, 'demo', default_branch='trunk', seed=False,
                                  has_issues=False, admin=True, viewer_association=viewer_association)
        else:
            repo = world.add_repo(OWNER, 'demo', default_branch='trunk', seed=False,
                                  viewer_association=viewer_association)
        self.seed_issues(repo)
        if name == 'auth login':
            world.set_login(False, scopes=())
        if name == 'git push seed':
            hooks = world.remote_path(OWNER, 'demo') / 'hooks'
            hooks.mkdir(parents=True, exist_ok=True)
            marker = world.remote_path(OWNER, 'demo') / 'reject-once'
            marker.touch()
            hook = hooks / 'pre-receive'
            hook.write_text(f'#!/bin/sh\nif [ -f "{marker}" ]; then rm -f "{marker}"; '
                            'echo "injected rejection" >&2; exit 1; fi\nexit 0\n')
            hook.chmod(0o755)
        gh_faults = {
            'auth login': [fault(['auth', 'login'])],
            'api repos/': [fault(['api', 'repos/'])],
            'PATCH has_issues': [fault(['PATCH', 'repos/'], 'after')],
            'project list': [fault(['project', 'list'])],
            'project create': [fault(['project', 'create'], 'after')],
            'project link': [fault(['project', 'link'])],
            'project field-list': [fault(['project', 'field-list'])],
            'project item-list': [fault(['project', 'item-list'])],
            'issues list': [fault(['GET', f'repos/{repo}/issues'])],
            'POST issue': [fault(['POST', 'repos/'], 'after')],
            'first item-add': [fault(['project', 'item-add'], 'after')],
            'later item-add': [fault(['project', 'item-add'], 'before', skip=1)],
            'final item-list': [fault(['project', 'item-list'], skip=2)],
        }
        if name in gh_faults:
            world.set_gh_faults(gh_faults[name])
        if name == 'systemctl enable':
            world.set_systemctl_faults([fault(['enable'])])
        if name == 'systemctl restart':
            world.set_systemctl_faults([fault(['restart'])])
        return repo

    PHASES = ('auth login', 'api repos/', 'PATCH has_issues', 'git push seed', 'project list',
              'project create', 'project link', 'project field-list', 'project item-list',
              'issues list', 'POST issue', 'first item-add', 'later item-add', 'final item-list',
              'systemctl enable', 'systemctl restart')

    def test_interrupted_at_each_phase_then_resumes_to_exact_state(self):
        for name in self.PHASES:
            with self.subTest(phase=name):
                repo = self.phase_setup(name)
                first = self.onboard(repo)
                self.assertNotEqual(first.rc, 0, f'{name}: first run must fail\n{first.stderr}')
                self.assertNotIn('Onboarded', first.stdout)
                second = self.onboard(repo)
                self.assertEqual(second.rc, 0, f'{name}: resume failed\n{second.stderr}')
                self.assert_resumed_exact(repo, name)

    def test_post_issue_interruption_with_contributor_viewer_resumes_to_one_bootstrap(self):
        # B-1 regression: a non-trusted viewer's lost POST response must not cause a second
        # bootstrap issue on resume. The marker issue is matched by the viewer's own user id.
        repo = self.phase_setup('POST issue', viewer_association='CONTRIBUTOR')
        first = self.onboard(repo)
        self.assertNotEqual(first.rc, 0, first.stderr)
        self.assertNotIn('Onboarded', first.stdout)
        self.assertEqual(len(self.w.bootstrap_issues(repo)), 1)
        second = self.onboard(repo)
        self.assertEqual(second.rc, 0, second.stderr)
        self.assert_resumed_exact(repo, 'POST issue')
        self.assertEqual(len(self.w.bootstrap_issues(repo)), 1)

    def assert_resumed_exact(self, repo, name):
        entries = [p for p in self.w.read_config()['projects'] if p['repo'].lower() == repo.lower()]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['base_branch'], 'trunk')
        titled = [p for p in self.w.projects() if p['title'] == f'AutoController · {repo}']
        self.assertEqual(len(titled), 1)
        bootstrap = self.w.bootstrap_issues(repo)
        self.assertEqual(len(bootstrap), 1)
        self.assertEqual(entries[0]['bootstrap_issue'], bootstrap[0]['number'])
        urls = [int(i['url'].rsplit('/', 1)[1]) for i in titled[0]['items']]
        self.assertEqual(sorted(urls), sorted([1, 3, 4, bootstrap[0]['number']]),
                         f'{name}: trusted issues must be present exactly once, outsider absent')
        self.assertNotIn(2, urls)
        self.assertEqual(self.w.remote_commits(OWNER, 'demo', 'trunk'), 1)
        self.assertEqual(self.w.remote_branch_count(OWNER, 'demo'), 1)
        self.assert_checkout(repo, 'trunk')
        # A resumed run either re-enters the already-configured path (enable --now)
        # or completes enable + restart. The last successful unit action must be one of these.
        successful = [c['argv'] for c in self.w.systemctl_records() if c['rc'] == 0]
        self.assertIn(successful[-1], (['--user', 'enable', '--now', 'autocontroller.service'],
                                       ['--user', 'restart', 'autocontroller.service']), name)


class DuplicateAndLockTests(OnboardingCase):
    def setUp(self):
        super().setUp()
        self.repo = self.w.add_repo(OWNER, 'demo', default_branch='trunk', seed=False)
        self.w.add_issue(self.repo, 'Owner work', association='OWNER')

    def test_second_successful_run_is_noop(self):
        self.assertEqual(self.onboard(self.repo).rc, 0)
        before = (len(self.w.mutating_gh_calls()), self.w.config_path().read_bytes())
        second = self.onboard(self.repo)
        self.assertEqual(second.rc, 0, second.stderr)
        self.assertIn('already configured', second.stdout.lower())
        self.assertEqual((len(self.w.mutating_gh_calls()), self.w.config_path().read_bytes()), before)
        self.assertIn(['--user', 'enable', '--now', 'autocontroller.service'], self.w.systemctl_calls())

    def test_lost_local_config_reuses_remote_project_bootstrap_and_items(self):
        self.assertEqual(self.onboard(self.repo).rc, 0)
        snapshot = len(self.w.gh_calls())
        self.w.config_path().unlink()
        rerun = self.onboard(self.repo)
        self.assertEqual(rerun.rc, 0, rerun.stderr)
        later = self.w.gh_calls()[snapshot:]
        self.assertFalse([c for c in later if c['argv'][:2] == ['project', 'create']])
        self.assertFalse([c for c in later if c['argv'][:2] == ['project', 'item-add']])
        self.assertFalse([c for c in later if '--method' in c['argv'] and 'POST' in c['argv']])
        self.assertEqual(len(self.w.projects()), 1)
        self.assertEqual(len(self.w.bootstrap_issues(self.repo)), 1)
        self.assertEqual(len(self.entries_for(self.repo)), 1)

    def test_lost_config_rerun_with_contributor_viewer_creates_no_second_bootstrap(self):
        # B-1 regression: with a non-trusted viewer, the rerun must adopt the marker issue
        # this account created earlier instead of posting another bootstrap issue.
        repo = self.w.add_repo(OWNER, 'contrib', default_branch='trunk', viewer_association='CONTRIBUTOR')
        self.assertEqual(self.onboard(repo).rc, 0)
        self.assertEqual(len(self.w.bootstrap_issues(repo)), 1)
        snapshot = len(self.w.gh_calls())
        self.w.config_path().unlink()
        rerun = self.onboard(repo)
        self.assertEqual(rerun.rc, 0, rerun.stderr)
        later = self.w.gh_calls()[snapshot:]
        self.assertFalse([c for c in later if '--method' in c['argv'] and 'POST' in c['argv']])
        self.assertEqual(len(self.w.bootstrap_issues(repo)), 1)
        self.assertEqual(len(self.entries_for(repo)), 1)
        self.assertEqual(self.entries_for(repo)[0]['bootstrap_issue'], self.w.bootstrap_issues(repo)[0]['number'])

    def test_checkout_with_two_origin_urls_refused_without_mutation(self):
        # Item 5: an ambiguous origin (two values) must fail closed, not be read as the first value.
        repo = self.repo
        self.assertEqual(self.onboard(repo).rc, 0)
        self.w.config_path().unlink()
        self.w.checkout_git(repo, 'config', '--add', 'remote.origin.url', 'https://example.invalid/other.git')
        snapshot = len(self.w.mutating_gh_calls())
        result = self.onboard(repo)
        self.assertNotEqual(result.rc, 0)
        self.assertIn('exactly one origin', result.stderr)
        self.assertEqual(len(self.w.mutating_gh_calls()), snapshot)
        self.assertFalse(self.w.config_path().exists())
        self.assertTrue(self.w.checkout_dir(repo).is_dir())

    def entries_for(self, repo):
        return [p for p in self.w.read_config()['projects'] if p['repo'].lower() == repo.lower()]

    def test_concurrent_run_with_held_lock_refused_without_mutation(self):
        with (self.w.install / 'onboarding.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.onboard(self.repo)
        self.assertNotEqual(result.rc, 0)
        self.assertEqual(self.w.gh_calls(), [])
        self.assertEqual(self.w.systemctl_calls(), [])
        self.assertFalse(self.w.config_path().exists())
        self.assertFalse(self.w.checkout_dir(self.repo).exists())


class ExistingStateTests(OnboardingCase):
    def test_existing_config_entries_and_settings_preserved(self):
        other = {'name': 'other', 'enabled': True, 'repo': f'{OWNER}/other',
                 'directory': str(self.w.home / 'repositories' / 'other'), 'project_owner': OWNER,
                 'project_number': 9, 'required_check': 'required', 'required_workflow': 'required.yml',
                 'base_branch': 'main', 'merge_method': 'squash'}
        original = {'agent': 'orchestrator', 'poll_seconds': 777, 'post_cycle_seconds': 15,
                    'state_dir': str(self.w.install / 'custom-state'), 'custom': {'keep': True},
                    'projects': [other]}
        self.w.write_config(original)
        repo = self.w.add_repo(OWNER, 'demo', default_branch='trunk')
        gitconfig_before = self.w.gitconfig_bytes()
        result = self.onboard(repo)
        self.assertEqual(result.rc, 0, result.stderr)
        after = self.w.read_config()
        for key, value in original.items():
            if key != 'projects':
                self.assertEqual(after[key], value, key)
        self.assertEqual(after['projects'][0], other)
        self.assertEqual(len(after['projects']), 2)
        self.assertEqual(after['projects'][1]['repo'], repo)
        self.assertEqual(after['projects'][1]['project_number'],
                         self.project_titled(repo)[0]['number'])
        self.assertEqual(self.w.config_path().stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.w.gitconfig_bytes(), gitconfig_before)
        self.assertNotIn('user.name', self.w.gitconfig_bytes().decode())

    def test_existing_checkout_with_uncommitted_work_refused_and_untouched(self):
        repo = self.w.add_repo(OWNER, 'demo', default_branch='trunk')
        self.assertEqual(self.onboard(repo).rc, 0)
        work = self.w.checkout_dir(repo) / 'scratch.txt'
        work.write_text('my unfinished work\n')
        self.w.config_path().unlink()
        snapshot = len(self.w.mutating_gh_calls())
        result = self.onboard(repo)
        self.assertNotEqual(result.rc, 0)
        self.assertIn('local work', result.stderr)
        self.assertEqual(work.read_text(), 'my unfinished work\n')
        self.assertEqual(len(self.w.mutating_gh_calls()), snapshot)

    def test_resume_with_global_insteadof_rewrite_accepts_checkout_it_cloned(self):
        # Regression: `remote get-url` applies insteadOf, so the stored https origin
        # was reported as file://… and a valid managed checkout was refused on resume.
        repo = self.w.add_repo(OWNER, 'demo', default_branch='trunk')
        self.assertEqual(self.onboard(repo).rc, 0)
        self.w.config_path().unlink()
        result = self.onboard(repo)
        self.assertEqual(result.rc, 0, result.stderr)
        self.assertNotIn('does not match', result.stderr)

    def test_existing_checkout_ahead_of_remote_refused_and_commit_kept(self):
        repo = self.w.add_repo(OWNER, 'demo', default_branch='trunk')
        self.assertEqual(self.onboard(repo).rc, 0)
        checkout = self.w.checkout_dir(repo)
        self.w.checkout_git(repo, 'commit', '--allow-empty', '-m', 'local only')
        head = self.w.checkout_git(repo, 'rev-parse', 'HEAD').strip()
        self.w.config_path().unlink()
        result = self.onboard(repo)
        self.assertNotEqual(result.rc, 0)
        self.assertIn('ahead', result.stderr)
        self.assertEqual(self.w.checkout_git(repo, 'rev-parse', 'HEAD').strip(), head)
        self.assertTrue(checkout.is_dir())


class AdmissionIsolationTests(OnboardingCase):
    def test_bootstrap_issue_admitted_explicitly_even_when_import_rule_would_skip_it(self):
        # B2 regression: the account creating the bootstrap is a non-trusted association,
        # so import_issues would never add it. Onboarding must add the issue it created.
        repo = self.w.add_repo(OWNER, 'demo', default_branch='trunk', viewer_association='CONTRIBUTOR')
        result = self.onboard(repo)
        self.assertEqual(result.rc, 0, result.stderr)
        bootstrap = self.w.bootstrap_issues(repo)[0]['number']
        urls = [i['url'] for i in self.project_titled(repo)[0]['items']]
        self.assertEqual(urls.count(f'https://github.com/{repo}/issues/{bootstrap}'), 1)
        self.assertEqual(self.entries(repo)[0]['bootstrap_issue'], bootstrap)

    def test_bootstrap_item_add_is_idempotent_on_resume(self):
        repo = self.w.add_repo(OWNER, 'demo', default_branch='trunk', viewer_association='CONTRIBUTOR')
        self.assertEqual(self.onboard(repo).rc, 0)
        bootstrap = self.w.bootstrap_issues(repo)[0]['number']
        bootstrap_url = f'https://github.com/{repo}/issues/{bootstrap}'
        self.w.config_path().unlink()
        self.assertEqual(self.onboard(repo).rc, 0)
        # B-1: exactly one bootstrap issue exists across the first run and the resume.
        self.assertEqual(len(self.w.bootstrap_issues(repo)), 1)
        # Onboarding must not re-add an item already present: the bootstrap issue is
        # never explicitly added twice across the first run and the resume.
        adds = [c['argv'] for c in self.w.gh_calls()
                if c['argv'][:2] == ['project', 'item-add'] and bootstrap_url in c['argv']]
        self.assertEqual(len(adds), 1)
        urls = [i['url'] for i in self.project_titled(repo)[0]['items']]
        self.assertEqual(urls.count(f'https://github.com/{repo}/issues/{bootstrap}'), 1)

    def test_import_admits_only_open_trusted_non_renovate_issues(self):
        repo = self.w.add_repo(OWNER, 'demo', default_branch='trunk')
        trusted = [self.w.add_issue(repo, f'{a} work', association=a) for a in ('OWNER', 'MEMBER', 'COLLABORATOR')]
        refused = {
            'NONE': self.w.add_issue(repo, 'none', association='NONE'),
            'CONTRIBUTOR': self.w.add_issue(repo, 'contributor', association='CONTRIBUTOR'),
            'FIRST_TIMER': self.w.add_issue(repo, 'first timer', association='FIRST_TIMER'),
            'missing': self.w.add_issue(repo, 'missing', association=None),
            'pull request': self.w.add_issue(repo, 'pr', association='OWNER', pull_request=True),
            'closed': self.w.add_issue(repo, 'closed', association='OWNER', state='closed'),
            'spoofed renovate login': self.w.add_issue(
                repo, 'spoof', association='NONE',
                user={'login': 'renovate[bot]', 'type': 'User', 'id': 1}),
            'verified renovate bot': self.w.add_issue(
                repo, 'bot', association='OWNER', user={'login': 'renovate[bot]', 'type': 'Bot', 'id': 29139614}),
        }
        result = self.onboard(repo)
        self.assertEqual(result.rc, 0, result.stderr)
        titled = self.project_titled(repo)[0]
        urls = {int(i['url'].rsplit('/', 1)[1]) for i in titled['items']}
        bootstrap = self.w.bootstrap_issues(repo)[0]['number']
        self.assertEqual(urls, set(trusted) | {bootstrap})
        for label, number in refused.items():
            self.assertNotIn(number, urls, label)
        # Imported trusted issues are schedulable only once Status is Todo.
        status = {int(i['url'].rsplit('/', 1)[1]): i['status'] for i in titled['items']}
        for number in trusted:
            self.assertEqual(status[number], 'Todo', number)

    def test_import_leaves_maintainer_set_status_untouched_and_todo_is_idempotent(self):
        repo = self.w.add_repo(OWNER, 'demo', default_branch='trunk')
        kept = self.w.add_issue(repo, 'kept', association='OWNER')
        self.w.add_project(OWNER, f'AutoController · {repo}',
                           items=[(f'https://github.com/{repo}/issues/{kept}', 'In Progress')])
        self.assertEqual(self.onboard(repo).rc, 0)
        titled = self.project_titled(repo)[0]
        status = {int(i['url'].rsplit('/', 1)[1]): i['status'] for i in titled['items']}
        self.assertEqual(status[kept], 'In Progress')
        # Resume: no further item-edit for an already Todo or maintainer-set row.
        snapshot = len(self.w.gh_calls())
        self.w.config_path().unlink()
        self.assertEqual(self.onboard(repo).rc, 0)
        later = self.w.gh_calls()[snapshot:]
        self.assertFalse([c for c in later if c['argv'][:2] == ['project', 'item-edit']])

    def test_existing_project_with_foreign_repo_issue_refused_before_linking(self):
        repo = self.w.add_repo(OWNER, 'demo', default_branch='trunk')
        other = self.w.add_repo(OWNER, 'other', default_branch='trunk')
        foreign = self.w.add_issue(other, 'foreign', association='OWNER')
        self.w.add_project(OWNER, f'AutoController · {repo}',
                           items=[(f'https://github.com/{other}/issues/{foreign}', 'Todo')])
        result = self.onboard(repo)
        self.assertNotEqual(result.rc, 0)
        self.assertIn('another repository', result.stderr)
        self.assertEqual([c for c in self.w.mutating_gh_calls() if c[:2] == ['project', 'link']], [])
        self.assertFalse(self.w.config_path().exists())
        self.assertEqual(self.w.bootstrap_issues(repo), [])

    def test_project_already_assigned_to_other_configured_repo_refused(self):
        repo = self.w.add_repo(OWNER, 'demo', default_branch='trunk')
        self.w.add_project(OWNER, f'AutoController · {repo}', number=5)
        self.w.write_config({'projects': [{'name': 'other', 'enabled': True, 'repo': f'{OWNER}/other',
                                           'directory': str(self.w.home / 'repositories' / 'other'),
                                           'project_owner': OWNER, 'project_number': 5}]})
        result = self.onboard(repo)
        self.assertNotEqual(result.rc, 0)
        self.assertIn('already assigned', result.stderr)
        self.assertEqual([c for c in self.w.mutating_gh_calls() if c[:2] == ['project', 'link']], [])
        self.assertEqual(self.w.bootstrap_issues(repo), [])

    def test_closed_same_title_project_refused(self):
        repo = self.w.add_repo(OWNER, 'demo', default_branch='trunk')
        self.w.add_project(OWNER, f'AutoController · {repo}', closed=True)
        result = self.onboard(repo)
        self.assertNotEqual(result.rc, 0)
        self.assertIn('closed', result.stderr)
        self.assertEqual(self.w.mutating_gh_calls(), [])
        self.assertFalse(self.w.config_path().exists())

    def test_no_persistent_side_effects_outside_disposable_world(self):
        names = ('config.json', 'onboarding.lock', 'state')
        before = {name: (ROOT / name).exists() for name in names}
        repo = self.w.add_repo(OWNER, 'demo', default_branch='trunk')
        self.assertEqual(self.onboard(repo).rc, 0)
        self.assertEqual({name: (ROOT / name).exists() for name in names}, before)
        self.assertTrue(self.w.config_path().exists())
        self.assertTrue(self.w.checkout_dir(repo).is_relative_to(self.w.root))
        self.assertNotEqual(self.w.home, Path.home())


GATE_RULESET = 'AutoController required check'


def ruleset_posts(world):
    """Every ruleset POST attempt, including ones whose response was lost (rc != 0 recorded)."""
    return [c for c in world.gh_calls() if '--method' in c['argv'] and 'POST' in c['argv']
            and any(a.startswith('repos/') and a.endswith('/rulesets') for a in c['argv'])]


class RequiredCheckGateTests(OnboardingCase):
    """Default-branch enforcement of the 'required' check, against the fake GitHub's protection and rulesets."""

    def test_no_protection_admin_consent_adds_exactly_one_ruleset_and_configures(self):
        repo = self.w.add_repo(OWNER, 'demo', default_branch='trunk', admin=True, protection=None)
        result = self.onboard(repo, stdin='y\ny\n')
        self.assertEqual(result.rc, 0, result.stderr)
        self.assertIn("enforces required check 'required'", result.stdout)
        rulesets = self.w.gh()['rulesets'][repo.lower()]
        self.assertEqual(len(rulesets), 1)
        ruleset = rulesets[0]
        self.assertEqual(ruleset['name'], GATE_RULESET)
        self.assertEqual(ruleset['enforcement'], 'active')
        self.assertEqual(ruleset['bypass_actors'], [])
        self.assertEqual(ruleset['conditions'], {'ref_name': {'include': ['refs/heads/trunk'], 'exclude': []}})
        self.assertEqual(ruleset['rules'][0]['parameters']['required_status_checks'],
                         [{'context': 'required', 'integration_id': 15368}])
        self.assertEqual(len(ruleset_posts(self.w)), 1)
        self.assert_success_state(repo, OWNER, 'trunk')

    def test_lost_config_rerun_adds_no_second_ruleset(self):
        repo = self.w.add_repo(OWNER, 'demo', default_branch='trunk', admin=True, protection=None)
        self.assertEqual(self.onboard(repo, stdin='y\ny\n').rc, 0)
        posts_before = len(ruleset_posts(self.w))
        self.w.config_path().unlink()
        rerun = self.onboard(repo)
        self.assertEqual(rerun.rc, 0, rerun.stderr)
        self.assertEqual(len(self.w.gh()['rulesets'][repo.lower()]), 1)
        self.assertEqual(len(ruleset_posts(self.w)), posts_before)
        self.assertEqual(posts_before, 1)

    def test_declined_or_eof_consent_refuses_without_ruleset_project_config_or_bootstrap(self):
        for stdin in ('y\nn\n', 'y\n'):
            with self.subTest(stdin=stdin):
                world = self.new_world()
                self.w = world
                repo = world.add_repo(OWNER, 'demo', default_branch='trunk', admin=True, protection=None)
                result = self.onboard(repo, stdin=stdin)
                self.assertNotEqual(result.rc, 0)
                self.assertIn('owner declined', result.stderr)
                self.assertIn('Branch protection and rulesets were not changed', result.stderr)
                self.assertEqual(world.gh().get('rulesets', {}).get(repo.lower(), []), [])
                self.assertEqual(ruleset_posts(world), [])
                self.assertEqual(world.projects(), [])
                self.assertEqual(world.bootstrap_issues(repo), [])
                self.assertFalse(world.config_path().exists())

    def test_non_admin_without_protection_fails_closed_with_owner_action_and_no_mutation(self):
        repo = self.w.add_repo(OWNER, 'demo', default_branch='trunk', admin=False, protection=None)
        result = self.onboard(repo, stdin='y\n')
        self.assertNotEqual(result.rc, 0)
        self.assertIn('ask a repository admin', result.stderr)
        self.assertEqual(self.w.mutating_gh_calls(), [])
        self.assertEqual(self.w.gh().get('rulesets', {}), {})
        self.assertEqual(self.w.projects(), [])
        self.assertFalse(self.w.config_path().exists())

    def test_classic_protection_with_stronger_rules_and_existing_ruleset_is_preserved(self):
        stronger = classic_protection(contexts=('lint',), checks=(), enforcement_level='everyone',
                                      required_pull_request_reviews={'required_approving_review_count': 2},
                                      required_signatures={'enabled': True},
                                      required_linear_history={'enabled': True},
                                      enforce_admins={'enabled': True})
        repo = self.w.add_repo(OWNER, 'demo', default_branch='trunk', admin=True, protection=stronger)
        other = {'enforcement': 'active', 'conditions': {'ref_name': {'include': ['~ALL'], 'exclude': []}},
                 'rules': [{'type': 'pull_request', 'parameters': {'required_approving_review_count': 1}}]}
        self.w.add_ruleset(repo, {'name': 'team rules', **other})
        before_protection = json.dumps(self.w.gh()['protection'], sort_keys=True)
        before_rulesets = json.dumps(self.w.gh()['rulesets'], sort_keys=True)
        result = self.onboard(repo, stdin='y\ny\n')
        self.assertEqual(result.rc, 0, result.stderr)
        after = self.w.gh()
        self.assertEqual(json.dumps(after['protection'], sort_keys=True), before_protection)
        before_rows = json.loads(before_rulesets)[repo.lower()]
        self.assertEqual(after['rulesets'][repo.lower()][:len(before_rows)], before_rows)
        self.assertEqual(len(after['rulesets'][repo.lower()]), len(before_rows) + 1)
        for call in self.w.gh_calls():
            argv = call['argv']
            if any(m in argv for m in ('PUT', 'PATCH', 'DELETE')):
                self.assertFalse(any('/protection' in a or '/rulesets' in a for a in argv), argv)
        self.assertEqual(len(ruleset_posts(self.w)), 1)

    def test_owner_disabled_same_named_ruleset_refuses_without_modification(self):
        repo = self.w.add_repo(OWNER, 'demo', default_branch='trunk', admin=True, protection=None)
        self.w.add_ruleset(repo, {'name': GATE_RULESET, 'enforcement': 'disabled',
                                  'conditions': {'ref_name': {'include': ['refs/heads/trunk'], 'exclude': []}},
                                  'rules': [{'type': 'required_status_checks', 'parameters': {
                                      'required_status_checks': [{'context': 'required'}]}}]})
        before = json.dumps(self.w.gh()['rulesets'], sort_keys=True)
        result = self.onboard(repo, stdin='y\ny\n')
        self.assertNotEqual(result.rc, 0)
        self.assertIn('will not modify it', result.stderr)
        self.assertEqual(ruleset_posts(self.w), [])
        self.assertEqual(json.dumps(self.w.gh()['rulesets'], sort_keys=True), before)
        self.assertFalse(self.w.config_path().exists())

    def test_ambiguous_branch_summary_fails_closed(self):
        repo = self.w.add_repo(OWNER, 'demo', default_branch='trunk', admin=True, protection=None)
        self.w.set_raw_response(f'repos/{repo}/branches/trunk', '{"protected": true}')
        result = self.onboard(repo, stdin='y\ny\n')
        self.assertNotEqual(result.rc, 0)
        self.assertIn('could not be verified', result.stderr)
        self.assertEqual(ruleset_posts(self.w), [])
        self.assertEqual(self.w.mutating_gh_calls(), [])
        self.assertFalse(self.w.config_path().exists())

    def test_truncated_or_malformed_rules_responses_fail_closed(self):
        over_limit = json.dumps([[{'type': 'other'}] * 1001])
        cases = {
            'over limit': over_limit,
            'page not a list': json.dumps({'message': 'Not Found'}),
            'rule without type': json.dumps([[{'parameters': {}}]]),
            'required rule without ruleset_id': json.dumps([[{'type': 'required_status_checks', 'parameters': {
                'required_status_checks': [{'context': 'required'}]}}]]),
        }
        for label, text in cases.items():
            with self.subTest(case=label):
                world = self.new_world()
                self.w = world
                repo = world.add_repo(OWNER, 'demo', default_branch='trunk', admin=True, protection=None)
                world.set_raw_response(f'repos/{repo}/rules/branches/trunk', text)
                result = self.onboard(repo, stdin='y\ny\n')
                self.assertNotEqual(result.rc, 0)
                self.assertEqual(ruleset_posts(world), [])
                self.assertFalse(world.config_path().exists())

    def test_rules_endpoint_api_error_fails_closed_without_post(self):
        repo = self.w.add_repo(OWNER, 'demo', default_branch='trunk', admin=True, protection=None)
        self.w.set_gh_faults([fault([f'repos/{repo}/rules/'])])
        result = self.onboard(repo, stdin='y\ny\n')
        self.assertNotEqual(result.rc, 0)
        self.assertEqual(ruleset_posts(self.w), [])
        self.assertFalse(self.w.config_path().exists())

    def test_bypassable_ruleset_does_not_count_and_admin_gets_a_new_ruleset(self):
        for bypass in ('always', 'pull_requests_only', 'exempt'):
            with self.subTest(bypass=bypass):
                world = self.new_world()
                self.w = world
                repo = world.add_repo(OWNER, 'demo', default_branch='trunk', admin=True, protection=None)
                world.add_ruleset(repo, {'name': 'bypassable', 'enforcement': 'active', 'current_user_can_bypass': bypass,
                                         'conditions': {'ref_name': {'include': ['refs/heads/trunk'], 'exclude': []}},
                                         'rules': [{'type': 'required_status_checks', 'parameters': {
                                             'required_status_checks': [{'context': 'required'}]}}]})
                result = self.onboard(repo, stdin='y\ny\n')
                self.assertEqual(result.rc, 0, result.stderr)
                self.assertEqual(len(ruleset_posts(world)), 1)

    def test_classic_non_admins_level_refused_for_admin_account_then_added(self):
        summary = classic_protection(enforcement_level='non_admins')
        repo = self.w.add_repo(OWNER, 'demo', default_branch='trunk', admin=True, protection=summary)
        result = self.onboard(repo, stdin='y\ny\n')
        self.assertEqual(result.rc, 0, result.stderr)
        self.assertEqual(len(ruleset_posts(self.w)), 1)

    def test_classic_everyone_with_required_passes_with_zero_posts(self):
        repo = self.w.add_repo(OWNER, 'demo', default_branch='trunk', admin=True)
        result = self.onboard(repo, stdin='y\n')
        self.assertEqual(result.rc, 0, result.stderr)
        self.assertEqual(ruleset_posts(self.w), [])
        self.assertEqual(self.w.gh().get('rulesets', {}), {})

    def test_resume_with_enforced_protection_prints_no_warning_and_resumes(self):
        repo = self.w.add_repo(OWNER, 'demo', default_branch='trunk', admin=True)
        self.assertEqual(self.onboard(repo).rc, 0)
        result = self.onboard(repo)
        self.assertEqual(result.rc, 0, result.stderr)
        self.assertNotIn('WARNING', result.stderr)
        self.assertIn('already configured', result.stdout.lower())
        self.assertIn(['--user', 'enable', '--now', 'autocontroller.service'], self.w.systemctl_calls())

    def test_resume_after_protection_removed_warns_with_zero_mutations(self):
        repo = self.w.add_repo(OWNER, 'demo', default_branch='trunk', admin=True)
        self.assertEqual(self.onboard(repo).rc, 0)
        self.w.mutate_gh(lambda s: s['protection'].__setitem__(repo.lower(), {'protected': False}))
        snapshot = len(self.w.mutating_gh_calls())
        before_config = self.w.config_path().read_bytes()
        result = self.onboard(repo)
        self.assertEqual(result.rc, 0, result.stderr)
        self.assertIn('WARNING', result.stderr)
        self.assertIn('no longer requires status check', result.stderr)
        self.assertEqual(len(self.w.mutating_gh_calls()), snapshot)
        self.assertEqual(self.w.gh().get('rulesets', {}), {})
        self.assertEqual(self.w.config_path().read_bytes(), before_config)

    def test_owned_same_name_ruleset_refused_before_any_post_or_second_ruleset(self):
        repo = self.w.add_repo(OWNER, 'demo', default_branch='trunk', admin=True, protection=None)
        self.w.add_ruleset(repo, {'name': GATE_RULESET, 'enforcement': 'disabled', 'conditions': {'ref_name': {'include': [], 'exclude': []}}, 'rules': []})
        result = self.onboard(repo, stdin='y\ny\n')
        self.assertNotEqual(result.rc, 0)
        self.assertIn('will not modify it', result.stderr)
        self.assertEqual(len(self.w.gh()['rulesets'][repo.lower()]), 1)

    def test_fake_gh_rejects_duplicate_ruleset_name_with_422(self):
        repo = self.w.add_repo(OWNER, 'demo', default_branch='trunk', admin=True, protection=None)
        doc = {'name': GATE_RULESET, 'target': 'branch', 'enforcement': 'active', 'rules': []}
        first = subprocess.run(['gh', 'api', '--method', 'POST', f'repos/{repo}/rulesets', '--input', '-'],
                               input=json.dumps(doc), text=True, capture_output=True, timeout=60,
                               env=self.w.env(), check=False)
        second = subprocess.run(['gh', 'api', '--method', 'POST', f'repos/{repo}/rulesets', '--input', '-'],
                                input=json.dumps(doc), text=True, capture_output=True, timeout=60,
                                env=self.w.env(), check=False)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 1)
        self.assertIn('HTTP 422', second.stderr)
        self.assertEqual(len(self.w.gh()['rulesets'][repo.lower()]), 1)

    def test_all_branches_ruleset_with_exclusion_is_matched_by_fake(self):
        repo = self.w.add_repo(OWNER, 'demo', default_branch='trunk', admin=True, protection=None)
        self.w.add_ruleset(repo, {'name': 'all but trunk', 'enforcement': 'active',
                                  'conditions': {'ref_name': {'include': ['~ALL'], 'exclude': ['refs/heads/trunk']}},
                                  'rules': [{'type': 'required_status_checks', 'parameters': {
                                      'required_status_checks': [{'context': 'required'}]}}]})
        result = self.onboard(repo, stdin='y\ny\n')
        self.assertEqual(result.rc, 0, result.stderr)
        self.assertEqual(len(ruleset_posts(self.w)), 1)

    def test_lost_ruleset_post_response_resumes_to_one_ruleset_and_no_second_post(self):
        repo = self.w.add_repo(OWNER, 'demo', default_branch='trunk', admin=True, protection=None)
        self.w.set_gh_faults([fault(['POST', f'repos/{repo}/rulesets'], 'after')])
        first = self.onboard(repo, stdin='y\ny\n')
        self.assertNotEqual(first.rc, 0, first.stderr)
        self.assertEqual(len(self.w.gh()['rulesets'][repo.lower()]), 1)
        self.assertFalse(self.w.config_path().exists())
        second = self.onboard(repo, stdin='y\n')
        self.assertEqual(second.rc, 0, second.stderr)
        self.assertEqual(len(self.w.gh()['rulesets'][repo.lower()]), 1)
        self.assertEqual(len(ruleset_posts(self.w)), 1)
        self.assert_success_state(repo, OWNER, 'trunk')
