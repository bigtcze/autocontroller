"""Part C: admission, bootstrap priority and authority rules, asserted at cycle level.

Offline and deterministic. GitHub, OMO and git are patched at the controller's
own seams, mirroring tests/test_quality_gates.py. Unit-level identity and fork
PR predicates live in test_identity_and_pr_selection.py; these tests assert the
same rules through one_iteration()/_cycle() instead of duplicating them.
"""
import ast
import importlib.util
import json
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from onboarded_fixture import onboarded_project_entry  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('admission_controller', str(ROOT / 'autonomy-controller.py'))
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)
releases = m.releases
bootstrap_policy = m.bootstrap
REAL_RENOVATE_PRS = m.renovate_prs

REPO = 'org/repo'
SHA = 'a' * 40
MERGE = 'b' * 40
BOOT = 5
OTHER = 10
BOT = {'login': 'renovate[bot]', 'type': 'Bot', 'id': 29139614}


def onboarded_cfg(directory):
    """Project entry shaped by the shared fixture; test_onboarding_integration asserts
    that onboarding.py writes exactly this shape, so this cannot drift silently."""
    return onboarded_project_entry(name='repo', repo=REPO, directory=directory, base_branch='main',
                                   project_owner='org', project_number=2, bootstrap_issue=BOOT,
                                   merge_method='squash', approver='example-owner')


def executable_strings_containing(tree, needle):
    """Line numbers of string constants that are used as code (not docstrings).

    A docstring is the first statement of a module/class/function body that is a
    bare string expression; it is excluded. Comments never reach the AST.
    """
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], 'value', None), ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings \
                and needle in node.value:
            hits.append(node.lineno)
        # f-strings: the literal fragments are code too (f'item-{x}' still carries 'item-').
        if isinstance(node, ast.JoinedStr):
            literal = ''.join(v.value for v in node.values
                              if isinstance(v, ast.Constant) and isinstance(v.value, str))
            if needle in literal:
                hits.append(node.lineno)
    return hits


def bootstrap_policy_b64(text):
    import base64
    return base64.b64encode(text.encode()).decode()


def issue_url(n, repo=REPO):
    return f'https://github.com/{repo}/issues/{n}'


def board_item(n, status='Backlog'):
    """Shape produced by GitHubProject.items() for an admitted Issue."""
    return {'item_id': f'ITEM{n}', 'number': n, 'title': f'Task {n}', 'url': issue_url(n),
            'status': status, 'priority': 'P3', 'body_sha256': '0' * 64}


def renovate_pr(number=8, *, head_repo=REPO, login=BOT['login'], typ=BOT['type'], bot_id=BOT['id']):
    return {'number': number, 'state': 'open', 'merged_at': None, 'draft': False,
            'user': {'login': login, 'type': typ, 'id': bot_id},
            'head': {'ref': f'renovate/pkg-{number}', 'sha': SHA, 'repo': {'full_name': head_repo}},
            'base': {'ref': 'main', 'repo': {'full_name': REPO}},
            'html_url': f'https://github.com/{REPO}/pull/{number}', 'created_at': '2026-11-25T00:00:00Z'}


def bootstrap_pr(*, merged):
    pr = {'number': 21, 'state': 'open', 'merged_at': None, 'draft': False,
          'head': {'sha': SHA, 'repo': {'full_name': REPO}}, 'base': {'ref': 'main'},
          'merge_commit_sha': None}
    if merged:
        pr.update(state='closed', merged_at='2026-11-26T10:00:00Z', merge_commit_sha=MERGE)
    return pr


class ControllerCase(unittest.TestCase):
    """Default world: bootstrap already verified, so ordinary work is reachable."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / 'checkout').mkdir()
        self.w = m.ProjectWorker(onboarded_cfg(self.root / 'checkout'),
                                 {'state_dir': str(self.root / 'state'), 'poll_seconds': 300,
                                  'wrapper': '/tmp/guard', 'opencode': '/tmp/opencode'},
                                 threading.Event(), threading.Lock())
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.p(self.w, 'ensure_unsafe_group_gone')
        self.p(self.w, 'checkout_neutral', return_value=True)
        self.p(self.w, 'checkout_state', return_value=('main', False))
        self.p(self.w, 'quota_until', return_value=0)
        self.p(self.w, 'check_maintenance', return_value=None)
        self.p(self.w, 'cleanup_checkout')
        self.renovate = self.p(m, 'renovate_prs', return_value=[])
        self.issue_state = self.p(m, 'issue_state', return_value='open')
        self.command = self.p(m, 'command', return_value='')
        self.set_status = self.p(self.w.project, 'set_status')
        self.items = self.p(self.w.project, 'items', return_value=[])
        self.agent = self.p(self.w, 'run_wrapper', return_value=(0, 'AUTOCONTROLLER_NO_ACTION'))
        # Offline guard: any GitHub read not explicitly fixture-backed by a test
        # fails loudly instead of reaching the real gh binary.
        self.gh_json = self.p(m, 'gh_json', side_effect=self.unfixtured_gh)
        self.mark_bootstrap_complete()

    @staticmethod
    def unfixtured_gh(*args, **kwargs):
        raise AssertionError(f'unfixtured GitHub read in test: {args[:3]}')

    @staticmethod
    def activity_only_fixture(*args, **kwargs):
        """Only the selector's recent-activity read is allowed; anything else fails loudly."""
        path = args[1] if len(args) > 1 else ''
        if path.startswith('repos/org/repo/issues?'):
            return []
        raise AssertionError(f'unfixtured GitHub read in test: {args[:3]}')

    @staticmethod
    def bootstrap_tree_fixture():
        """Complete candidate tree plus empty activity: only the failing CI can block."""
        tree = {'truncated': False, 'tree': [
            {'path': p, 'type': 'blob', 'mode': '100644', 'size': 10}
            for p in bootstrap_policy.REQUIRED]}
        mise = {'path': 'mise.toml', 'type': 'blob', 'mode': '100644', 'size': 10}
        renovate = {'path': 'renovate.json', 'type': 'blob', 'mode': '100644', 'size': 10}
        tree['tree'] += [mise, renovate]

        version = {'encoding': 'base64', 'content': bootstrap_policy_b64('1')}

        def fake(*args, **kwargs):
            path = args[1] if len(args) > 1 else ''
            if 'git/trees' in path:
                return tree
            if '.agents/STANDARD_VERSION' in path:
                return version
            if path.startswith('repos/org/repo/issues?'):
                return []
            raise AssertionError(f'unfixtured GitHub read in test: {args[:3]}')
        return fake

    def p(self, obj, name, **kwargs):
        return self.stack.enter_context(patch.object(obj, name, **kwargs))

    def mark_bootstrap_complete(self):
        self.w.bootstrap_done.parent.mkdir(parents=True, exist_ok=True)
        self.w.bootstrap_done.write_text(json.dumps({'issue': BOOT, 'version': '1', 'commit': MERGE}))

    def agent_kinds(self):
        return [c.args[1] for c in self.agent.call_args_list]

    def merge_commands(self):
        return [c.args[0] for c in self.command.call_args_list if list(c.args[0])[:3] == ['gh', 'pr', 'merge']]


class BootstrapBlocksOrdinaryWork(ControllerCase):
    def setUp(self):
        super().setUp()
        self.w.bootstrap_done.unlink(missing_ok=True)
        self.items.return_value = [board_item(BOOT), board_item(OTHER)]

    def stage(self, phase, **extra):
        self.w.save_checkpoint({'kind': 'issue', 'number': BOOT, 'url': issue_url(BOOT), 'phase': phase,
                                'pr_number': 21, 'retry_same_task': True, 'started': time.time(), **extra})

    def assert_only_bootstrap_or_nothing(self, cycle):
        self.renovate.assert_not_called()
        self.agent.assert_not_called()
        self.assertFalse([c for c in cycle.call_args_list if c.args[1] != BOOT])

    def test_failed_bootstrap_pr_ci_blocks_ordinary_work_without_ai(self):
        # Real _cycle: the CI gate itself must block, not a mock.
        self.stage('ci')
        self.p(self.w, 'related_pr', return_value=bootstrap_pr(merged=False))
        self.p(m, 'pr_ci_state', return_value='failed')
        self.gh_json.side_effect = self.bootstrap_tree_fixture()
        self.w._cycle('issue', BOOT, issue_url(BOOT), None)
        self.agent.assert_not_called()
        self.assertEqual(m.json_read(self.w.cycle_state)['phase'], 'code')
        self.assertTrue(self.w.retry_map()[f'issue:{BOOT}']['until'] > time.time())
        self.w.one_iteration()
        self.renovate.assert_not_called()
        self.agent.assert_not_called()
        self.assertNotIn('selection', self.agent_kinds())

    def test_failed_merged_main_ci_blocks_ordinary_work_without_ai(self):
        self.stage('postmerge', merge_commit=MERGE, ready_sha=SHA, bootstrap_main_since=time.time())
        self.p(self.w, 'related_pr', return_value=bootstrap_pr(merged=True))
        self.p(m, 'main_ci_state', return_value='failed')
        self.w._cycle('issue', BOOT, issue_url(BOOT), None)
        self.assertEqual(m.json_read(self.w.cycle_state)['phase'], 'repair')
        self.agent.assert_not_called()
        self.w.one_iteration()
        self.renovate.assert_not_called()
        self.agent.assert_not_called()

    def test_remote_errors_refuse_bootstrap_completion_and_keep_it_blocking(self):
        self.stage('postmerge', merge_commit=MERGE, retry_same_task=False)
        with patch.object(bootstrap_policy, 'remote_errors', return_value=['README.md']):
            with self.assertRaises(m.ControllerError):
                self.w.complete_checkpoint('issue', BOOT, None)
        self.assertFalse(self.w.bootstrap_is_complete())
        self.assertFalse(self.w.bootstrap_done.exists())
        cycle = self.p(self.w, '_cycle')
        self.w.one_iteration()
        self.assertEqual([c.args[:2] for c in cycle.call_args_list], [('issue', BOOT)])
        self.assert_only_bootstrap_or_nothing(cycle)

    def test_closed_bootstrap_without_proof_blocks_ordinary_work(self):
        self.issue_state.return_value = 'closed'
        cycle = self.p(self.w, '_cycle')
        self.w.one_iteration()
        cycle.assert_not_called()
        self.renovate.assert_not_called()
        self.agent.assert_not_called()

    def test_terminal_bootstrap_card_without_proof_blocks_ordinary_work(self):
        self.items.return_value = [board_item(BOOT, status='Done'), board_item(OTHER)]
        cycle = self.p(self.w, '_cycle')
        self.w.one_iteration()
        cycle.assert_not_called()
        self.renovate.assert_not_called()
        self.agent.assert_not_called()


class BootstrapPriority(ControllerCase):
    def test_onboarded_config_is_valid_and_bootstrap_beats_renovate_and_admitted_issues(self):
        cfg = onboarded_cfg(self.root / 'checkout')
        self.assertIsNone(m.validate_config({'projects': [cfg]}))
        self.w.bootstrap_done.unlink()
        self.items.return_value = [board_item(OTHER), board_item(BOOT)]
        self.renovate.return_value = [renovate_pr(8)]
        cycle = self.p(self.w, '_cycle')
        self.w.one_iteration()
        self.assertEqual([c.args[:2] for c in cycle.call_args_list], [('issue', BOOT)])
        self.renovate.assert_not_called()
        self.agent.assert_not_called()

    def test_ordinary_work_resumes_only_after_verified_bootstrap_proof(self):
        self.items.return_value = [board_item(OTHER)]
        self.renovate.return_value = [renovate_pr(8)]
        cycle = self.p(self.w, '_cycle')
        self.w.one_iteration()
        self.assertEqual([c.args[:2] for c in cycle.call_args_list], [('renovate', 8)])


class AdmissionWithoutReady(ControllerCase):
    def raw_board(self):
        return {'totalCount': 9, 'items': [
            {'id': 'i1', 'content': {'type': 'Issue', 'url': issue_url(10), 'title': 'Unset'}},
            {'id': 'i2', 'status': '', 'content': {'type': 'Issue', 'url': issue_url(11), 'title': 'Blank'}},
            {'id': 'i3', 'status': 'Backlog', 'content': {'type': 'Issue', 'url': issue_url(12), 'title': 'B'}},
            {'id': 'i4', 'status': 'Todo', 'content': {'type': 'Issue', 'url': issue_url(13), 'title': 'T'}},
            {'id': 'i5', 'status': 'Custom Lane', 'content': {'type': 'Issue', 'url': issue_url(14), 'title': 'C'}},
            # Never offered: foreign repository, pull request, draft card, terminal status.
            {'id': 'f1', 'status': 'Todo', 'content': {'type': 'Issue',
                                                       'url': 'https://github.com/other/repo/issues/3'}},
            {'id': 'p1', 'status': 'Todo', 'content': {'type': 'PullRequest',
                                                       'url': f'https://github.com/{REPO}/pull/21'}},
            {'id': 'd1', 'status': 'Todo', 'content': {'type': 'DraftIssue', 'title': 'draft-only-card'}},
            {'id': 'x1', 'status': 'Done', 'content': {'type': 'Issue', 'url': issue_url(15)}},
        ]}

    def test_unset_blank_backlog_todo_and_custom_statuses_are_offered_and_others_never(self):
        with patch.object(m, 'gh_json', return_value=self.raw_board()):
            board = m.GitHubProject(REPO, 'org', 2).items()
        self.assertEqual({i['number'] for i in board}, {10, 11, 12, 13, 14, 15})
        self.items.return_value = board
        self.gh_json.side_effect = self.activity_only_fixture
        self.w.one_iteration()
        self.assertEqual(self.agent_kinds(), ['selection'])
        prompt = self.agent.call_args.args[0]
        for n in (10, 11, 12, 13, 14):
            self.assertIn(issue_url(n), prompt)
        self.assertNotIn(issue_url(15), prompt)  # Done is terminal
        self.assertNotIn('other/repo', prompt)
        self.assertNotIn('/pull/21', prompt)
        self.assertNotIn('draft-only-card', prompt)

    def test_empty_board_with_open_unlinked_repository_issues_makes_no_ai_call(self):
        self.items.return_value = []
        self.w.one_iteration()
        self.agent.assert_not_called()
        self.set_status.assert_not_called()
        self.assertFalse([c for c in self.command.call_args_list if 'item-add' in c.args[0]])

    def test_unlisted_selection_is_refused_and_held_back_without_second_ai_call(self):
        self.items.return_value = [board_item(OTHER)]
        self.agent.return_value = (0, 'AUTOCONTROLLER_SELECT_ISSUE #99')
        cycle = self.p(self.w, '_cycle')
        self.w.one_iteration()
        cycle.assert_not_called()
        self.set_status.assert_not_called()
        checkpoint = m.json_read(self.w.cycle_state)
        self.assertIsNotNone(checkpoint)
        self.assertNotIn('chosen_issue', checkpoint)
        self.assertNotIn('decision', checkpoint)
        self.assertTrue(self.w.retry_map()['selection:0']['until'] > time.time())
        self.w.one_iteration()
        self.assertEqual(self.agent_kinds(), ['selection'])


class OutsiderApproval(ControllerCase):
    TAG = 'v2.0.0'

    def comments(self, approval_login):
        request = {'id': 1, 'body': releases.approve_request_body(self.TAG, SHA), 'user': BOT}
        approval = {'id': 2, 'body': f'{releases.APPROVAL_RESPONSE} {self.TAG} {SHA}',
                    'user': {'login': approval_login, 'type': 'User', 'id': 4004}}
        return [request, approval]

    def test_outsider_exact_approval_text_does_not_approve_release(self):
        self.p(m, 'gh_api_pages', return_value=self.comments('example-outsider'))
        posted = self.p(m, 'gh_json', return_value={})
        self.assertFalse(self.w.release_request(11, self.TAG, SHA))
        posted.assert_not_called()

    def test_owner_exact_approval_text_approves_release_positive_control(self):
        self.p(m, 'gh_api_pages', return_value=self.comments('example-owner'))
        self.p(m, 'gh_json', return_value={})
        self.assertTrue(self.w.release_request(11, self.TAG, SHA))


class ForkPullRequests(ControllerCase):
    def test_verified_renovate_is_cycled_but_fork_impersonation_is_not(self):
        # Positive control first: the live path does cycle a genuine same-repository Renovate PR.
        self.renovate.side_effect = REAL_RENOVATE_PRS
        fork = renovate_pr(77, head_repo='example-fork/repo')
        spoof = renovate_pr(78, head_repo='example-fork/repo', typ='User', bot_id=1)
        self.p(m, 'gh_api_pages', return_value=[renovate_pr(8), fork, spoof])
        cycle = self.p(self.w, '_cycle')
        self.w.one_iteration()
        self.assertEqual([c.args[:2] for c in cycle.call_args_list], [('renovate', 8)])
        cycle.reset_mock()
        self.assertEqual([p['number'] for p in REAL_RENOVATE_PRS(REPO)], [8])

    def test_fork_renovate_impersonation_never_runs_agent_or_merges(self):
        self.renovate.side_effect = REAL_RENOVATE_PRS
        self.items.return_value = [board_item(OTHER)]
        fork = renovate_pr(77, head_repo='example-fork/repo')
        spoof = renovate_pr(78, head_repo='example-fork/repo', typ='User', bot_id=1)
        self.p(m, 'gh_api_pages', return_value=[fork, spoof])
        self.gh_json.side_effect = self.activity_only_fixture
        cycle = self.p(self.w, '_cycle')
        self.w.one_iteration()
        self.assertFalse([c for c in cycle.call_args_list if c.args[0] == 'renovate'])
        for c in self.agent.call_args_list:
            self.assertNotIn('pull/77', c.args[0])
            self.assertNotIn('pull/78', c.args[0])
        self.assertEqual(self.merge_commands(), [])

    def test_fork_closing_reference_is_ignored_and_never_merged(self):
        payload = {'data': {'repository': {'issue': {'closedByPullRequestsReferences': {
            'pageInfo': {'hasNextPage': False},
            'nodes': [{'number': 77, 'url': 'https://github.com/example-fork/repo/pull/77',
                       'state': 'OPEN', 'headRefOid': SHA, 'baseRefName': 'main',
                       'isCrossRepository': True, 'headRepository': {'nameWithOwner': 'example-fork/repo'},
                       'mergeCommit': None}]}}}}}
        self.p(m, 'gh_json', side_effect=lambda *a, **k: payload if 'graphql' in a else {})
        self.assertIsNone(m.linked_issue_pr(REPO, OTHER))
        self.agent.return_value = (1, 'failed')
        self.w._cycle('issue', OTHER, issue_url(OTHER), board_item(OTHER))
        self.assertEqual(self.merge_commands(), [])
        for c in self.agent.call_args_list:
            self.assertNotIn('pull/77', c.args[0])
            self.assertNotIn('example-fork', c.args[0])


class OutsiderCommentsDoNotWake(ControllerCase):
    """Cycle-level companion to OutsiderCommentWake (test_agent_backlog_selection)."""

    def test_outsider_comment_after_no_action_does_not_start_second_ai_session(self):
        self.items.return_value = [board_item(OTHER)]
        rows = [{'number': OTHER, 'state': 'open', 'labels': [], 'updated_at': '2026-11-25T08:00:00Z',
                 'comments': 0}]
        self.gh_json.side_effect = lambda *a, **k: rows if str(a[1]).startswith(f'repos/{REPO}/issues?') else \
            self.activity_only_fixture(*a, **k)
        self.w.one_iteration()
        self.assertEqual(self.agent_kinds(), ['selection'])
        # An outsider comments: updated_at and comment count move, no content does.
        rows[0].update(updated_at='2026-11-26T09:00:00Z', comments=9)
        self.w.one_iteration()
        self.assertEqual(self.agent_kinds(), ['selection'])


class NoAutoAdmission(ControllerCase):
    def test_controller_source_never_invokes_project_item_add(self):
        # Parsed AST, not raw text: comments and docstrings are not code and cannot
        # trip or hide the check. Only executable string literals count.
        tree = ast.parse((ROOT / 'autonomy-controller.py').read_text(encoding='utf-8'))
        self.assertEqual(executable_strings_containing(tree, 'item-add'), [])

    def test_item_add_checker_has_positive_and_negative_controls(self):
        # Positive: a real argv literal is caught, including inside a nested list.
        bad = ast.parse("run(['gh','project','item-add','1'])\nx=['item-add']")
        self.assertEqual(len(executable_strings_containing(bad, 'item-add')), 2)
        # Negative: the same words only in a comment or docstring are not code.
        benign = ast.parse('"""Never call item-add here."""\n# item-add is forbidden\ny = "project item-list"')
        self.assertEqual(executable_strings_containing(benign, 'item-add'), [])

    def test_no_cycle_invokes_project_item_add_at_runtime(self):
        gh = self.p(m, 'gh_json', return_value={})
        self.p(self.w, 'related_pr', return_value=None)
        self.items.return_value = [board_item(OTHER)]
        self.agent.return_value = (1, 'failed')
        self.w._cycle('issue', OTHER, issue_url(OTHER), board_item(OTHER))
        self.w.one_iteration()
        argv = [list(c.args[0]) for c in self.command.call_args_list]
        argv += [list(c.args) for c in gh.call_args_list]
        self.assertFalse([a for a in argv if 'item-add' in a])


if __name__ == '__main__':
    unittest.main()
