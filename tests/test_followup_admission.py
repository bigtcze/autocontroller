"""Offline regression tests for controller-authorized follow-up Issue admission (followup.py).

Every GitHub call goes through an in-memory FakeGitHub that models the gh shapes followup.py
and the controller read. Nothing here runs gh or touches the network. Controller-level tests
drive the real ProjectWorker.one_iteration against the same fake.
"""
import importlib.util
import json
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import followup as f  # noqa: E402

REPO = 'example-owner/demo'
OTHER_REPO = 'example-owner/other'
OWNER = 'example-owner'
PROJECT = 1
PARENT = 7
TOKEN = 'a' * 48
VIEWER_LOGIN = 'controller-bot'
VIEWER_ID = 4242
OUTSIDER_ID = 9999
ISSUE_ID_BASE = 90000


def url_for(repo, number):
    return f'https://github.com/{repo}/issues/{number}'


_CONTROLLER = None


def load_controller():
    """The controller module, loaded once from source (no install, no real config)."""
    global _CONTROLLER
    if _CONTROLLER is None:
        spec = importlib.util.spec_from_file_location('followup_test_controller', str(ROOT / 'autonomy-controller.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _CONTROLLER = module
    return _CONTROLLER


class FakeGitHub:
    """Stateful double. Issues are keyed by (repo, number); Projects by owner/number.

    Shapes mirror the live gh/REST output that followup.py reads:
      api user                              -> {login, id}
      api repos/R/issues/N                  -> {id, number, state, html_url, user{login,id}, pull_request?}
      api repos/R/issues?state=all&since=...  -> paginated list (honours since on updated_at)
      api repos/R/issues/N/dependencies/blocked_by GET/POST
      project item-list/view/field-list/item-add/item-edit
      issue create (body on stdin)
    """

    def __init__(self):
        self.repos = {REPO: {}, OTHER_REPO: {}}      # repo -> {number: issue dict}
        self.next_number = 100
        self.projects = {(OWNER, PROJECT): []}        # (owner, number) -> rows {'url','status','item_id'}
        self.deps = {}                                # (repo, number) -> set(blocker numbers)
        self.calls = []
        self.viewer_login = VIEWER_LOGIN
        self.viewer_id = VIEWER_ID
        self.todo_option = 'opt-todo'
        self.todo_option_name = 'Todo'
        self.has_status_field = True
        self.fail = {}                                # argv key -> remaining injected failures
        self.lost_reply = []                          # per issue-create: None | 'error' | 'empty' | 'garbage'
        self.edits = []
        self.created = []                             # (repo, number) created by issue create
        self.add_issue(REPO, PARENT, title='parent', state='open', status='In Progress', user_id=VIEWER_ID)

    # ---- fixture helpers
    def add_issue(self, repo, number, *, title='t', body='', state='open', status=None,
                  user_id=VIEWER_ID, login=None, pull_request=False, in_project=True):
        now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        # Realistic REST shape: `url` is the API URL, `html_url` the web URL.
        issue = {'number': number, 'title': title, 'body': body, 'state': state,
                 'url': f'https://api.github.com/repos/{repo}/issues/{number}',
                 'html_url': url_for(repo, number), 'id': ISSUE_ID_BASE + number,
                 'created_at': now, 'updated_at': now,
                 'user': {'login': login or self.viewer_login, 'id': user_id, 'type': 'User'}}
        if pull_request:
            issue['pull_request'] = {'url': 'x'}
        self.repos.setdefault(repo, {})[number] = issue
        if in_project and status is not None:
            self.add_row(OWNER, PROJECT, url_for(repo, number), status)
        return number

    def add_row(self, owner, project, url, status):
        rows = self.projects.setdefault((owner, project), [])
        rows.append({'url': url, 'status': status, 'item_id': f'PVTI_{len(rows)}_{url.rsplit("/", 1)[1]}'})

    def row(self, url):
        for rows in self.projects.values():
            for r in rows:
                if r['url'] == url:
                    return r
        return None

    def status_of(self, number, repo=REPO):
        r = self.row(url_for(repo, number))
        return r['status'] if r else None

    def _check(self, key):
        if self.fail.get(key, 0) > 0:
            self.fail[key] -= 1
            raise f.FollowupError(f'gh rc=1: injected failure at {key}')

    def _issue_by_id(self, issue_id):
        for issues in self.repos.values():
            for n, i in issues.items():
                if i['id'] == issue_id:
                    return i
        return None

    # ---- the gh surface
    def command(self, argv, *, stdin=None, timeout=120):
        self.calls.append(list(argv))
        if argv[:2] == ['issue', 'create']:
            repo = next(a.split('=', 1)[1] for a in argv if a.startswith('--repo='))
            title = next(a.split('=', 1)[1] for a in argv if a.startswith('--title='))
            number = self.next_number
            self.next_number += 1
            self.add_issue(repo, number, title=title, body=stdin or '', user_id=self.viewer_id)
            self.created.append((repo, number))
            # Server-side success with a lost response: the issue exists, but the reply is
            # an error (rc != 0) or empty/unparsable. Both must be adopted on retry.
            mode = self.lost_reply.pop(0) if self.lost_reply else None
            if mode == 'error':
                raise f.FollowupError('gh rc=1: connection reset after server accepted the issue')
            if mode == 'empty':
                return ''
            if mode == 'garbage':
                return '<html>502 Bad Gateway</html>'
            self._check('issue create')
            return url_for(repo, number) + '\n'
        if argv[:2] == ['project', 'item-add']:
            self._check('item-add')
            owner, project = argv[argv.index('--owner') + 1], int(argv[2])
            url = argv[argv.index('--url') + 1]
            if self.row(url) is None:
                self.add_row(owner, project, url, '')
            return ''
        if argv[:2] == ['project', 'item-edit']:
            self._check('item-edit')
            item_id = argv[argv.index('--id') + 1]
            row = next(r for rows in self.projects.values() for r in rows if r['item_id'] == item_id)
            option = argv[argv.index('--single-select-option-id') + 1]
            self.edits.append((row['url'], option))
            # The row takes the name of the option that was selected, as the real Project does.
            if option == self.todo_option:
                row['status'] = self.todo_option_name
            else:
                row['status'] = option
            return ''
        if argv[:2] == ['project', 'item-list']:
            self._check('item-list')
            owner, project = argv[argv.index('--owner') + 1], int(argv[2])
            rows = self.projects.get((owner, project), [])
            return json.dumps({'items': [{'content': {'url': r['url'], 'type': 'Issue'},
                                          'status': r['status'], 'id': r['item_id']} for r in rows],
                               'totalCount': len(rows)})
        if argv[:2] == ['project', 'view']:
            return json.dumps({'id': 'PVT_project'})
        if argv[:2] == ['project', 'field-list']:
            options = [{'name': self.todo_option_name, 'id': self.todo_option}] if self.todo_option else []
            fields = [{'name': 'Status', 'id': 'PVTSSF_status', 'options': options}] if self.has_status_field else []
            return json.dumps({'fields': fields})
        if argv[:3] == ['api', '--method', 'GET'] and len(argv) > 3 and argv[3].startswith('repos/') \
                and '--paginate' in argv and '/dependencies/' not in argv[3]:
            return self._list(argv[3])
        if argv[:2] == ['api', 'user']:
            return json.dumps({'login': self.viewer_login, 'id': self.viewer_id})
        if argv[0] == 'api':
            return self._api(argv, stdin)
        raise AssertionError(f'unexpected gh call in test: {argv!r}')

    def _list(self, path):
        m = re.fullmatch(r'repos/([^/]+/[^/]+)/issues\?state=all&since=([^&]+)&per_page=100', path)
        if not m:
            raise AssertionError(f'unexpected issue list path: {path}')
        repo, since = m.group(1), m.group(2)
        # Honour the server-side `since` filter on updated_at; no creator filter (identity is by id).
        rows = [i for i in self.repos.get(repo, {}).values() if i['updated_at'] >= since]
        return json.dumps([rows])

    def _api(self, argv, stdin):
        path = next(a for a in argv[1:] if a.startswith('repos/'))
        m = re.fullmatch(r'repos/([^/]+/[^/]+)/issues/(\d+)/dependencies/blocked_by', path)
        if m:
            repo, number = m.group(1), int(m.group(2))
            if '--method' in argv and argv[argv.index('--method') + 1] == 'POST':
                self._check('dependency post')
                blocker = self._issue_by_id(json.loads(stdin or '{}')['issue_id'])
                self.deps.setdefault((repo, number), set()).add(blocker['number'])
                return '{}'
            # Without --paginate gh returns only page 1. The fake serves two pages, with the existing
            # blockers split so that page 2 holds at least one link: a missing --paginate loses it.
            links = [{'number': n} for n in sorted(self.deps.get((repo, number), set()))]
            if '--paginate' not in argv:
                return json.dumps(links[:1])
            half = max(1, len(links) // 2)
            return json.dumps([links[:half], links[half:]] if len(links) > 1 else [links])
        pr = re.fullmatch(r'repos/([^/]+/[^/]+)/pulls/(\d+)', path)
        if pr:
            # Renovate PR read: a verified open PR (fixtures decide head/base elsewhere).
            number, repo = int(pr.group(2)), pr.group(1)
            return json.dumps({'number': number, 'state': 'open', 'merged_at': None, 'draft': False,
                               'user': {'login': 'renovate[bot]', 'type': 'Bot', 'id': 29139614},
                               'head': {'ref': f'renovate/pkg-{number}', 'sha': 'a' * 40,
                                        'repo': {'full_name': repo}},
                               'base': {'ref': 'main', 'repo': {'full_name': repo}},
                               'html_url': url_for(repo, number), 'created_at': '2026-11-25T00:00:00Z'})
        listing = re.fullmatch(r'repos/([^/]+/[^/]+)/issues\?state=all&per_page=100&sort=updated&direction=desc', path)
        if listing:
            # Selector activity read: the repository's issues, comments never included.
            return json.dumps(list(self.repos.get(listing.group(1), {}).values()))
        m = re.fullmatch(r'repos/([^/]+/[^/]+)/issues/(\d+)', path)
        if m:
            repo, number = m.group(1), int(m.group(2))
            issue = self.repos.get(repo, {}).get(number)
            if issue is None:
                raise f.FollowupError(f'gh rc=1: Not Found #{number} in {repo}')
            return json.dumps(issue)
        if re.fullmatch(r'repos/[^/]+/[^/]+/issues/\D.*', path):
            # gh rejects a non-numeric issue path with an HTTP-level error, like the real API.
            raise f.FollowupError(f'gh rc=1: invalid issue path {path}')
        raise AssertionError(f'unexpected api path: {path}')


def make_worker(case):
    m = load_controller()
    cfg = {'name': 'demo', 'repo': REPO, 'directory': str(case.tmp / 'repo'),
           'project_owner': OWNER, 'project_number': PROJECT}
    global_cfg = {'state_dir': str(case.state), 'wrapper': str(case.tmp / 'wrapper.py'),
                  'opencode': str(case.tmp / 'opencode')}
    w = m.ProjectWorker(cfg, global_cfg, threading.Event(), threading.Lock())
    w.pdir.mkdir(parents=True, exist_ok=True)
    return w


class FollowupBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='followup-test-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = self.tmp / 'state'
        self.pdir = self.state / 'projects' / 'demo'
        self.pdir.mkdir(parents=True)
        self.gh = FakeGitHub()
        self.gh.add_issue(REPO, 15, title='outsider', status=None, in_project=False)  # not in Project
        patches = [patch.object(f, 'gh_command', side_effect=self.gh.command),
                   patch.object(f, 'gh_json', side_effect=lambda argv, stdin=None: json.loads(
                       self.gh.command(argv, stdin=stdin) or 'null'))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.grant_path = self.pdir / f.GRANT_FILE
        self.write_grant()
        self.set_checkpoint(PARENT)
        (self.pdir / 'cycle.pending').write_text('{}', encoding='utf-8')

    def write_grant(self, **overrides):
        obj = {'token': TOKEN, 'repo': REPO, 'project_owner': OWNER, 'project_number': PROJECT,
               'parent': PARENT, 'kind': 'issue', 'project_dir': 'demo'}
        obj.update(overrides)
        self.grant_path.write_text(json.dumps(obj), encoding='utf-8')

    def set_checkpoint(self, number, kind='issue'):
        (self.pdir / 'cycle.json').write_text(json.dumps(
            {'kind': kind, 'number': number, 'url': url_for(REPO, number), 'phase': 'code'}), encoding='utf-8')

    def payload(self, **overrides):
        obj = {'key': 'followup-a', 'title': 'Follow-up A', 'body': 'Details.', 'reason': 'Needed for X'}
        obj.update(overrides)
        return json.dumps(obj)

    def create(self, raw=None, token=TOKEN):
        return f.create(self.grant_path, token, raw if raw is not None else self.payload(), self.state)

    def journal(self):
        path = self.pdir / f.JOURNAL_FILE
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding='utf-8'))['entries']

    def reconcile(self, **kw):
        return f.reconcile(self.pdir, REPO, OWNER, PROJECT, **kw)

    def project_status(self, number):
        return self.gh.status_of(number)


class GrantTests(FollowupBase):
    def test_grant_is_token_bound_and_reused_for_the_same_pending_task(self):
        token = f.grant(self.state, self.pdir, REPO, OWNER, PROJECT, 'issue', PARENT, True)
        self.assertEqual(f.grant(self.state, self.pdir, REPO, OWNER, PROJECT, 'issue', PARENT, True), token)

    def test_grant_for_another_task_is_refused_while_pending(self):
        f.grant(self.state, self.pdir, REPO, OWNER, PROJECT, 'issue', PARENT, True)
        with self.assertRaises(f.FollowupError):
            f.grant(self.state, self.pdir, REPO, OWNER, PROJECT, 'issue', PARENT + 1, True)

    def test_new_task_gets_a_fresh_token(self):
        first = f.grant(self.state, self.pdir, REPO, OWNER, PROJECT, 'issue', PARENT, False)
        second = f.grant(self.state, self.pdir, REPO, OWNER, PROJECT, 'issue', PARENT, False)
        self.assertNotEqual(first, second)

    def test_instruction_names_the_cli_and_never_gh_issue_create(self):
        text = f.instruction(self.pdir, TOKEN)
        self.assertIn('followup.py', text)
        self.assertIn(f'--token {TOKEN}', text)
        self.assertIn('Never use gh issue create', text)

    def test_grant_path_must_have_state_shape(self):
        with self.assertRaises(f.FollowupError):
            f.check_grant_path(self.tmp / 'elsewhere' / f.GRANT_FILE, self.state)
        with self.assertRaises(f.FollowupError):
            f.check_grant_path(self.pdir / 'other.json', self.state)

    def test_grant_records_its_project_directory(self):
        f.grant(self.state, self.pdir, REPO, OWNER, PROJECT, 'issue', PARENT, False)
        self.assertEqual(json.loads(self.grant_path.read_text())['project_dir'], 'demo')


class ActiveTaskAuthorityTests(FollowupBase):
    """Item 1: the CLI acts only for the live Issue checkpoint of the granted parent."""

    def test_grant_for_parent_12_while_checkpoint_is_13_is_refused(self):
        self.write_grant(parent=12)
        self.set_checkpoint(13)
        self.gh.add_issue(REPO, 12, status='In Progress')
        with self.assertRaises(f.FollowupError) as ctx:
            self.create()
        self.assertIn('#13', str(ctx.exception))
        self.assertEqual(self.gh.created, [])

    def test_selection_checkpoint_is_refused(self):
        self.set_checkpoint(PARENT, kind='selection')
        with self.assertRaises(f.FollowupError):
            self.create()
        self.assertEqual(self.gh.created, [])

    def test_missing_cycle_json_is_refused(self):
        (self.pdir / 'cycle.json').unlink()
        with self.assertRaises(f.FollowupError):
            self.create()
        self.assertEqual(self.gh.created, [])

    def test_renovate_kind_checkpoint_refused_even_with_matching_number(self):
        self.set_checkpoint(PARENT, kind='renovate')
        with self.assertRaises(f.FollowupError):
            self.create()

    def test_copied_grant_from_another_project_directory_is_refused(self):
        self.write_grant(project_dir='other-project')
        with self.assertRaises(f.FollowupError):
            self.create()
        self.assertEqual(self.gh.created, [])

    def test_wrong_token_refused_without_gh_call(self):
        with self.assertRaises(f.FollowupError):
            self.create(token='b' * 48)
        self.assertEqual(self.gh.calls, [])

    def test_non_issue_kind_refused(self):
        self.write_grant(kind='renovate')
        with self.assertRaises(f.FollowupError):
            self.create()
        self.assertEqual(self.gh.calls, [])

    def test_not_pending_task_refused(self):
        (self.pdir / 'cycle.pending').unlink()
        with self.assertRaises(f.FollowupError):
            self.create()
        self.assertEqual(self.gh.calls, [])


class InputTests(FollowupBase):
    def test_unknown_fields_rejected_so_repo_and_project_cannot_be_chosen(self):
        for field in ('repo', 'project', 'parent', 'owner', 'labels'):
            with self.subTest(field=field):
                with self.assertRaises(f.FollowupError):
                    f.parse_input(self.payload(**{field: 'x'}))

    def test_invalid_key_title_reason_and_size_rejected(self):
        bad = [
            self.payload(key='Bad_Key'), self.payload(key='-leading'), self.payload(key='x' * 49),
            self.payload(title=''), self.payload(title='x' * 201), self.payload(title='a\nb'),
            self.payload(reason=''), self.payload(body='x' * 8001),
        ]
        for raw in bad:
            with self.subTest(raw=raw[:60]):
                with self.assertRaises(f.FollowupError):
                    f.parse_input(raw)

    def test_oversized_stdin_and_non_object_refused(self):
        with self.assertRaises(f.FollowupError):
            f.parse_input('x' * (f.MAX_STDIN_BYTES + 1))
        with self.assertRaises(f.FollowupError):
            f.parse_input('[1,2]')
        with self.assertRaises(f.FollowupError):
            f.parse_input('not json')

    def test_link_lists_bounded_and_integer_only(self):
        with self.assertRaises(f.FollowupError):
            f.parse_input(self.payload(blocks=[1, 2, 3, 4, 5, 6]))
        with self.assertRaises(f.FollowupError):
            f.parse_input(self.payload(blocked_by=['12']))
        with self.assertRaises(f.FollowupError):
            f.parse_input(self.payload(blocked_by=[True]))

    def test_gh_token_environment_is_stripped_for_the_stored_login(self):
        with patch.dict('os.environ', {'GH_TOKEN': 'x', 'GITHUB_TOKEN': 'y', 'KEEP': '1'}):
            env = f.gh_env()
        self.assertNotIn('GH_TOKEN', env)
        self.assertNotIn('GITHUB_TOKEN', env)
        self.assertEqual(env.get('KEEP'), '1')


class LiveValidationTests(FollowupBase):
    """Item 2: parent and every link target are live, open, in-repo, in-Project, nonterminal."""

    def test_closed_parent_refused_before_journaling(self):
        self.gh.repos[REPO][PARENT]['state'] = 'closed'
        with self.assertRaises(f.FollowupError):
            self.create()
        self.assertEqual(self.journal(), [])
        self.assertEqual(self.gh.created, [])

    def test_parent_not_in_project_refused(self):
        self.gh.projects[(OWNER, PROJECT)] = [r for r in self.gh.projects[(OWNER, PROJECT)]
                                              if r['url'] != url_for(REPO, PARENT)]
        with self.assertRaises(f.FollowupError):
            self.create()
        self.assertEqual(self.journal(), [])

    def test_parent_that_is_a_pull_request_refused(self):
        self.gh.repos[REPO][PARENT]['pull_request'] = {'url': 'x'}
        with self.assertRaises(f.FollowupError):
            self.create()

    def test_target_not_in_project_refused(self):
        self.gh.add_issue(REPO, 20, status='Todo')
        self.gh.projects[(OWNER, PROJECT)] = [r for r in self.gh.projects[(OWNER, PROJECT)]
                                              if r['url'] != url_for(REPO, 20)]
        with self.assertRaises(f.FollowupError):
            self.create(self.payload(key='t', blocks=[20]))
        self.assertEqual(self.journal(), [])
        self.assertEqual(self.gh.created, [])

    def test_target_in_another_repo_refused(self):
        # The Project holds a row whose URL is in another repository; that is not this repo's Issue.
        self.gh.add_issue(OTHER_REPO, 30, status='Todo')
        self.gh.add_issue(REPO, 30, status=None, in_project=False)
        self.gh.projects[(OWNER, PROJECT)].append({'url': url_for(OTHER_REPO, 30), 'status': 'Todo',
                                                   'item_id': 'PVTI_other'})
        with self.assertRaises(f.FollowupError):
            self.create(self.payload(key='x', blocked_by=[30]))
        self.assertEqual(self.journal(), [])

    def test_closed_target_refused(self):
        self.gh.add_issue(REPO, 21, status='Todo', state='closed')
        with self.assertRaises(f.FollowupError):
            self.create(self.payload(key='c', blocks=[21]))
        self.assertEqual(self.journal(), [])

    def test_target_with_blank_or_terminal_status_refused(self):
        self.gh.add_issue(REPO, 22, status='')
        self.gh.add_issue(REPO, 23, status='Done')
        for n in (22, 23):
            with self.subTest(number=n):
                with self.assertRaises(f.FollowupError):
                    self.create(self.payload(key=f'k{n}', blocks=[n]))

    def test_target_in_todo_and_in_progress_accepted(self):
        self.gh.add_issue(REPO, 24, status='Todo')
        self.gh.add_issue(REPO, 25, status='In Progress')
        result = self.create(self.payload(key='ok', blocks=[24], blocked_by=[25]))
        self.assertEqual(result['state'], 'done')

    def test_pull_request_as_target_refused(self):
        self.gh.add_issue(REPO, 26, status='Todo', pull_request=True)
        with self.assertRaises(f.FollowupError):
            self.create(self.payload(key='pr', blocks=[26]))


class IdentityTests(FollowupBase):
    """Item 3: authorship is the numeric id recorded at intent time; no login text, no truncation cliff."""

    def test_outsider_with_same_login_text_and_different_id_is_not_adopted(self):
        marker = f.marker(PARENT, 'followup-a')
        self.gh.add_issue(REPO, 70, body=marker, user_id=OUTSIDER_ID, login=VIEWER_LOGIN, status=None,
                          in_project=False)
        self.gh.next_number = 71
        result = self.create()
        self.assertNotEqual(result['number'], 70)
        self.assertEqual(self.gh.repos[REPO][70]['user']['id'], OUTSIDER_ID)  # untouched

    def test_marker_from_another_author_is_not_adopted(self):
        self.gh.add_issue(REPO, 70, body=f.marker(PARENT, 'followup-a'), user_id=OUTSIDER_ID, status=None,
                          in_project=False)
        self.gh.next_number = 71
        self.assertNotEqual(self.create()['number'], 70)

    def test_pull_request_with_marker_is_not_adopted(self):
        self.gh.add_issue(REPO, 72, body=f.marker(PARENT, 'followup-a'), status=None, in_project=False,
                          pull_request=True)
        self.gh.next_number = 73
        self.assertNotEqual(self.create()['number'], 72)

    def test_intent_records_numeric_identity_and_time(self):
        self.create()
        entry = self.journal()[0]
        self.assertEqual(entry['viewer_id'], VIEWER_ID)
        self.assertEqual(entry['viewer_login'], VIEWER_LOGIN)
        self.assertTrue(entry['intent_at'])

    def test_listing_is_since_scoped_and_paginated_not_limited_to_1000(self):
        self.create(self.payload(key='one'))
        listing = [c for c in self.gh.calls if any(a.startswith('repos/') and 'since=' in a for a in c)]
        self.assertTrue(listing, 'the REST listing is used')
        path = next(a for a in listing[0] if a.startswith('repos/'))
        self.assertIn('state=all', path)
        self.assertNotIn('creator=', path, 'identity is checked by numeric id, not by login')
        self.assertIn('per_page=100', path)
        self.assertIn('--paginate', listing[0])
        self.assertFalse([c for c in self.gh.calls if '--limit' in c and c[:2] == ['issue', 'list']])

    def test_created_issue_must_be_authored_by_the_controller_id(self):
        def foreign_create(argv, *, stdin=None, timeout=120):
            out = self.gh.command(argv, stdin=stdin, timeout=timeout)
            if argv[:2] == ['issue', 'create']:
                number = int(out.strip().rsplit('/', 1)[1])
                self.gh.repos[REPO][number]['user']['id'] = OUTSIDER_ID
            return out
        with patch.object(f, 'gh_command', side_effect=foreign_create):
            with self.assertRaises(f.FollowupError):
                self.create()
        self.assertEqual(self.gh.created[-1][0], REPO)
        self.assertFalse([c for c in self.gh.calls if c[:2] == ['project', 'item-add']])

    def test_since_bound_is_intent_time_minus_a_24h_clock_skew_margin(self):
        self.assertEqual(f.since_iso('2026-11-27T10:00:00+00:00'), '2026-11-26T10:00:00Z')

    def test_adoption_finds_own_marked_issue_by_numeric_id(self):
        self.gh.add_issue(REPO, 80, body=f.marker(PARENT, 'followup-a'), status=None, in_project=False)
        entry = {'parent': PARENT, 'key': 'followup-a', 'repo': REPO, 'owner': OWNER, 'project': PROJECT,
                 'viewer_login': VIEWER_LOGIN, 'viewer_id': VIEWER_ID, 'intent_at': '2026-11-27T10:00:00+00:00',
                 'state': 'intent'}
        self.gh.repos[REPO][80]['created_at'] = '2026-11-27T10:00:30Z'
        self.assertEqual(f.find_marked(entry)['number'], 80)


class CreateTests(FollowupBase):
    def test_happy_path_creates_marked_todo_issue_with_provenance_and_journals_done(self):
        result = self.create(self.payload(reason='Needed for X'))
        number = result['number']
        self.assertEqual(result['state'], 'done')
        issue = self.gh.repos[REPO][number]
        self.assertIn(f.marker(PARENT, 'followup-a'), issue['body'])
        self.assertIn(f'Follow-up of #{PARENT}: Needed for X', issue['body'])
        self.assertEqual(self.project_status(number), 'Todo')
        self.assertEqual(self.gh.edits, [(url_for(REPO, number), 'opt-todo')])
        self.assertEqual(self.journal()[0]['state'], 'done')

    def test_same_key_and_same_request_is_idempotent(self):
        first = self.create()
        before = len(self.gh.calls)
        second = self.create()
        self.assertEqual(first['number'], second['number'])
        self.assertFalse([c for c in self.gh.calls[before:] if c[:2] == ['issue', 'create']])
        self.assertEqual(len(self.gh.created), 1)

    def test_same_key_different_request_refused(self):
        self.create()
        with self.assertRaises(f.FollowupError):
            self.create(self.payload(title='Changed'))

    def test_max_five_per_parent_enforced(self):
        for i in range(f.MAX_PER_PARENT):
            self.create(self.payload(key=f'k{i}'))
        with self.assertRaises(f.FollowupError):
            self.create(self.payload(key='k-extra'))
        self.assertEqual(len(self.gh.created), f.MAX_PER_PARENT)

    def test_blocks_creates_native_dependency_and_is_idempotent(self):
        self.gh.add_issue(REPO, 50, status='Todo')
        result = self.create(self.payload(key='dep', blocks=[50]))
        self.assertIn(result['number'], self.gh.deps[(REPO, 50)])
        posts = len([c for c in self.gh.calls if '--method' in c and 'POST' in c])
        self.create(self.payload(key='dep', blocks=[50]))
        self.assertEqual(len([c for c in self.gh.calls if '--method' in c and 'POST' in c]), posts)

    def test_existing_link_on_page_two_is_seen_so_no_duplicate_post(self):
        # The parent of the new follow-up (blocks=[50]) already blocks on 51 and 52. The two-page
        # split puts one of them on page 2; the production GET passes --paginate, so the
        # existing link is seen and no duplicate POST is sent.
        self.gh.add_issue(REPO, 50, status='Todo')
        self.gh.add_issue(REPO, 51, status='Todo')
        self.gh.add_issue(REPO, 52, status='Todo')
        self.gh.deps[(REPO, 50)] = {51, 52}
        f.add_blocker(REPO, 50, 51)  # already present (page 2): must not POST again
        posts = [c for c in self.gh.calls if '--method' in c and 'POST' in c]
        self.assertEqual(posts, [], 'an existing blocker on page 2 is not re-posted')

    def test_dependency_get_is_paginated_and_reads_page_two(self):
        self.gh.add_issue(REPO, 60, status='Todo')
        self.gh.deps[(REPO, 60)] = {61, 62}
        real = self.gh.command
        seen = []

        def spy(argv, *, stdin=None, timeout=120):
            if any('dependencies/blocked_by' in a for a in argv):
                seen.append(list(argv))
            return real(argv, stdin=stdin, timeout=timeout)

        with patch.object(f, 'gh_command', side_effect=spy):
            try:
                blockers = f.blocked_by_numbers(REPO, 60)
            except f.FollowupError:
                blockers = None
        # Asserted first, on the argv the production code sent: a GET without --paginate is the defect.
        self.assertTrue(seen, 'the dependency GET was issued')
        self.assertTrue(all('--paginate' in a for a in seen), f'every dependency GET must paginate: {seen}')
        self.assertEqual(blockers, {61, 62}, 'both pages read')

    def test_blocked_by_parent_is_refused(self):
        with self.assertRaises(f.FollowupError):
            self.create(self.payload(key='self', blocked_by=[PARENT]))
        self.assertEqual(self.journal(), [])

    def test_item_add_is_only_ever_called_from_followup_py(self):
        self.create()
        self.assertTrue(any(c[:2] == ['project', 'item-add'] for c in self.gh.calls))


class LostCreateReplyTests(FollowupBase):
    """B1: issue create succeeds server-side but the reply is an error, empty or unparsable."""

    def assert_retry_adopts(self, mode):
        self.gh.lost_reply = [mode]
        with self.assertRaises(f.FollowupError):
            self.create()
        self.assertEqual(len(self.gh.created), 1)
        self.assertEqual(self.journal()[0]['state'], 'intent')
        # (a) CLI retry with the same key adopts the existing issue, no duplicate create.
        result = self.create()
        self.assertEqual(result['state'], 'done')
        self.assertEqual(len(self.gh.created), 1, 'no duplicate issue')
        self.assertEqual(self.project_status(result['number']), 'Todo')

    def test_cli_retry_adopts_after_error_reply(self):
        self.assert_retry_adopts('error')

    def test_cli_retry_adopts_after_empty_reply(self):
        self.assert_retry_adopts('empty')

    def test_cli_retry_adopts_after_unparsable_reply(self):
        self.assert_retry_adopts('garbage')

    def test_controller_reconcile_adopts_lost_create_and_reaches_done(self):
        self.gh.lost_reply = ['error']
        with self.assertRaises(f.FollowupError):
            self.create()
        # (b) controller reconcile adopts the same issue without a second create.
        results = self.reconcile()
        self.assertEqual([r['ok'] for r in results], [True])
        self.assertEqual(len(self.gh.created), 1)
        self.assertEqual(self.journal()[0]['state'], 'done')

    def test_controller_followup_gate_adopts_lost_create_and_reaches_done(self):
        self.gh.lost_reply = ['garbage']
        with self.assertRaises(f.FollowupError):
            self.create()
        w = make_worker(self)
        with patch.object(w, 'notice'):
            self.assertFalse(w.followup_gate(), 'admission completed, selection not deferred')
        self.assertEqual(len(self.gh.created), 1)
        self.assertEqual(self.journal()[0]['state'], 'done')

    def test_adopted_row_with_mismatched_number_is_refused_not_journaled(self):
        entry = {'parent': PARENT, 'key': 'followup-a', 'repo': REPO, 'owner': OWNER, 'project': PROJECT,
                 'viewer_login': VIEWER_LOGIN, 'viewer_id': VIEWER_ID, 'intent_at': '2026-11-27T10:00:00+00:00',
                 'state': 'intent', 'title': 'Follow-up A', 'body': 'Details.', 'reason': 'Needed for X',
                 'blocks': [], 'blocked_by': []}
        self.gh.add_issue(REPO, 81, body=f.marker(PARENT, 'followup-a'))
        self.gh.repos[REPO][81]['updated_at'] = '2026-11-27T10:00:30Z'
        with patch.object(f, 'find_marked', return_value={'number': 99, 'html_url': url_for(REPO, 81)}):
            with self.assertRaises(f.FollowupError):
                f.step_intent(entry)
        self.assertEqual(entry['state'], 'intent')

    def test_reconcile_converts_malformed_entry_to_per_entry_blocker(self):
        # A 'created' entry missing its `url` raises KeyError inside step_created.
        self.gh.fail['item-add'] = 1
        with self.assertRaises(f.FollowupError):
            self.create()
        entries = self.journal()
        del entries[0]['url']
        (self.pdir / f.JOURNAL_FILE).write_text(json.dumps({'entries': entries}), encoding='utf-8')
        results = self.reconcile()
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]['ok'])
        self.assertIn('malformed follow-up entry', results[0]['error'])
        self.assertIn('KeyError', results[0]['error'])

    def test_one_malformed_entry_does_not_block_a_healthy_sibling(self):
        self.gh.fail['item-add'] = 1
        with self.assertRaises(f.FollowupError):
            self.create()
        entries = self.journal()
        broken = dict(entries[0], key='broken')
        del broken['url']
        entries.append(broken)
        (self.pdir / f.JOURNAL_FILE).write_text(json.dumps({'entries': entries}), encoding='utf-8')
        results = {r['key']: r for r in self.reconcile()}
        self.assertFalse(results['broken']['ok'])
        self.assertTrue(results['followup-a']['ok'], 'the healthy entry still completes')


class TodoAdmissionTests(FollowupBase):
    """Non-blocking 1 and 6: a follow-up is done only when live Todo; one normalizer for option names."""

    def backlog_on_add(self, edit_sticks):
        """Project workflow sets Backlog on add; item-edit either sticks or is silently ignored."""
        original = self.gh.command

        def command(argv, *, stdin=None, timeout=120):
            if argv[:2] == ['project', 'item-edit'] and not edit_sticks:
                self.gh.edits.append(('ignored', argv[argv.index('--single-select-option-id') + 1]))
                return ''
            out = original(argv, stdin=stdin, timeout=timeout)
            if argv[:2] == ['project', 'item-add']:
                self.gh.row(argv[argv.index('--url') + 1])['status'] = 'Backlog'
            return out

        return patch.object(f, 'gh_command', side_effect=command)

    def test_automation_sets_backlog_on_add_and_edit_corrects_it_to_todo(self):
        with self.backlog_on_add(edit_sticks=True):
            result = self.create()
        self.assertEqual(result['state'], 'done')
        self.assertEqual(self.project_status(result['number']), 'Todo')
        self.assertEqual(len(self.gh.edits), 1)

    def test_edit_that_does_not_stick_leaves_a_blocker_and_is_never_done(self):
        with self.backlog_on_add(edit_sticks=False):
            with self.assertRaises(f.FollowupError) as ctx:
                self.create()
        self.assertIn('not Todo after the status edit', str(ctx.exception))
        self.assertEqual(self.journal()[0]['state'], 'added', 'never marked done')

    def test_option_named_to_do_with_hyphen_resolves_in_admission(self):
        self.gh.todo_option_name = 'To-do'
        result = self.create()
        self.assertEqual(result['state'], 'done')
        self.assertEqual(self.project_status(result['number']), 'To-do')

    def test_normalizer_handles_case_whitespace_and_hyphen(self):
        self.assertEqual(f.normalize_status('To-do'), 'to do')
        self.assertEqual(f.normalize_status('  TODO  '), 'todo')
        self.assertEqual(f.normalize_status('To   do'), 'to do')

    def test_project_without_todo_option_is_a_concrete_blocker_and_entry_is_kept(self):
        self.gh.todo_option = None
        with self.assertRaises(f.FollowupError) as ctx:
            self.create()
        self.assertIn('no Todo or To do Status option', str(ctx.exception))
        self.assertEqual(len(self.journal()), 1, 'entry kept')
        self.assertNotEqual(self.journal()[0]['state'], 'done')


class TerminalStatusTests(FollowupBase):
    """N1: a maintainer's terminal status is never overridden; a closed Issue is never admitted."""

    def test_done_status_is_not_overridden_and_becomes_a_blocker(self):
        original = self.gh.command

        def done_on_add(argv, *, stdin=None, timeout=120):
            out = original(argv, stdin=stdin, timeout=timeout)
            if argv[:2] == ['project', 'item-add']:
                self.gh.row(argv[argv.index('--url') + 1])['status'] = 'Done'
            return out

        with patch.object(f, 'gh_command', side_effect=done_on_add):
            with self.assertRaises(f.FollowupError) as ctx:
                self.create()
        self.assertIn("maintainer moved follow-up", str(ctx.exception))
        self.assertIn("'done'", str(ctx.exception))
        self.assertIn('owner action', str(ctx.exception))
        self.assertEqual(self.gh.edits, [], 'no item-edit was sent over a terminal status')
        self.assertEqual(self.journal()[0]['state'], 'added', 'entry kept unfinished, never done')

    def test_closed_issue_is_not_admitted(self):
        original = self.gh.command

        def close_on_add(argv, *, stdin=None, timeout=120):
            out = original(argv, stdin=stdin, timeout=timeout)
            if argv[:2] == ['project', 'item-add']:
                self.gh.repos[REPO][self.gh.created[-1][1]]['state'] = 'closed'
            return out

        with patch.object(f, 'gh_command', side_effect=close_on_add):
            with self.assertRaises(f.FollowupError) as ctx:
                self.create()
        self.assertIn('is closed; not admitted', str(ctx.exception))
        self.assertEqual(self.gh.edits, [], 'a closed Issue is never edited into Todo')
        self.assertNotEqual(self.journal()[0]['state'], 'done')


class CliExitTests(FollowupBase):
    """Non-blocking 2: exit 2 with 'follow-up refused:' only before journaling; exit 3 with pending JSON after."""

    def run_cli(self, payload):
        import io
        out, err = io.StringIO(), io.StringIO()
        argv = ['--grant', str(self.grant_path), '--token', TOKEN, 'create']
        with patch.object(f, 'ROOT', self.tmp), patch('sys.stdin', io.StringIO(payload)), \
                patch('sys.stdout', out), patch('sys.stderr', err):
            rc = f.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_refusal_before_journaling_exits_2_with_refused_message(self):
        self.gh.repos[REPO][PARENT]['state'] = 'closed'
        rc, out, err = self.run_cli(self.payload())
        self.assertEqual(rc, 2)
        self.assertIn('follow-up refused:', err)
        self.assertEqual(self.journal(), [])

    def test_failure_after_journaling_exits_3_with_pending_json(self):
        self.gh.fail['item-add'] = 1
        rc, out, err = self.run_cli(self.payload())
        self.assertEqual(rc, f.EXIT_PENDING)
        result = json.loads(out)
        self.assertTrue(result['pending'])
        self.assertEqual(result['key'], 'followup-a')
        self.assertEqual(result['state'], 'created')
        self.assertIn('error', result)
        self.assertNotIn('follow-up refused', err)
        self.assertEqual(len(self.journal()), 1)

    def test_malformed_reply_after_journaling_exits_3_pending(self):
        original = self.gh.command
        listing = {'calls': 0}

        def backlog_then_malformed(argv, *, stdin=None, timeout=120):
            if argv[:2] == ['project', 'item-add']:
                out = original(argv, stdin=stdin, timeout=timeout)
                # Automation lands the new item in Backlog, so step_added must edit and re-read.
                self.gh.row(argv[argv.index('--url') + 1])['status'] = 'Backlog'
                return out
            if argv[:2] == ['project', 'item-edit']:
                return original(argv, stdin=stdin, timeout=timeout)
            if argv[:2] == ['project', 'item-list'] and self.gh.edits:
                listing['calls'] += 1
                # The re-read reply is malformed: a row with a non-dict content raises TypeError
                # inside item_for's .get, which step_added must convert to a pending blocker.
                return json.dumps({'items': [{'content': 'not-a-dict', 'status': 'Todo', 'id': 'x'}],
                                   'totalCount': 1})
            return original(argv, stdin=stdin, timeout=timeout)

        def malformed_read(argv, stdin=None):
            # project_items reads through gh_json, not gh_command: inject the malformed list there.
            if argv[:2] == ['project', 'item-list'] and self.gh.edits:
                listing['calls'] += 1
                return {'items': [{'content': 'not-a-dict', 'status': 'Todo', 'id': 'x'}], 'totalCount': 1}
            return json.loads(self.gh.command(argv, stdin=stdin) or 'null')

        with patch.object(f, 'gh_command', side_effect=backlog_then_malformed), \
                patch.object(f, 'gh_json', side_effect=malformed_read):
            rc, out, err = self.run_cli(self.payload())
        self.assertGreaterEqual(listing['calls'], 1, 'the malformed re-read happened after journaling')
        self.assertEqual(rc, f.EXIT_PENDING)
        result = json.loads(out)
        self.assertTrue(result['pending'])
        self.assertIn('malformed reply', result['error'])
        self.assertNotIn('follow-up refused', err)
        self.assertEqual(len(self.journal()), 1, 'entry journaled, not refused')

    def test_malformed_input_before_journaling_exits_2_refused(self):
        # A grant whose repo field is not a string fails before journaling: refused, exit 2.
        self.write_grant(repo=None)
        rc, out, err = self.run_cli(self.payload())
        self.assertEqual(rc, f.EXIT_REFUSED)
        self.assertIn('follow-up refused:', err)
        self.assertEqual(self.journal(), [])

    def test_instruction_forbids_recreating_under_a_new_key(self):
        text = f.instruction(self.pdir, TOKEN)
        self.assertIn('exit 3', text)
        self.assertIn('do NOT re-create with a different key', text)
        self.assertIn('retry the SAME key only', text)


class ForgeryAndMarkerTests(FollowupBase):
    """Non-blocking 3 and 4: forged entries refused before item-add; marker must lead the body; reserved word."""

    def tamper_after_failure(self, **changes):
        self.gh.fail['item-add'] = 1
        with self.assertRaises(f.FollowupError):
            self.create()
        entries = self.journal()
        entries[0].update(changes)
        (self.pdir / f.JOURNAL_FILE).write_text(json.dumps({'entries': entries}), encoding='utf-8')

    def test_forged_entry_with_outsider_viewer_id_refused_without_item_add(self):
        self.tamper_after_failure(viewer_id=OUTSIDER_ID)
        self.gh.calls.clear()
        results = self.reconcile()
        self.assertFalse(results[0]['ok'])
        self.assertIn('not written by this controller account', results[0]['error'])
        self.assertFalse([c for c in self.gh.calls if c[:2] == ['project', 'item-add']])

    def test_forged_entry_with_outsider_issue_url_and_id_refused_without_item_add(self):
        # An outsider's issue is planted as the entry's number/url; the body and author do not match.
        self.gh.add_issue(REPO, 90, body='outsider text', user_id=OUTSIDER_ID, status=None, in_project=False)
        self.tamper_after_failure(number=90, url=url_for(REPO, 90), state='created')
        self.gh.calls.clear()
        results = self.reconcile()
        self.assertFalse(results[0]['ok'])
        self.assertIn('does not carry this entry', results[0]['error'])
        self.assertFalse([c for c in self.gh.calls if c[:2] == ['project', 'item-add']])
        self.assertIsNone(self.gh.row(url_for(REPO, 90)))

    def test_body_with_marker_not_at_start_is_not_adopted(self):
        self.gh.add_issue(REPO, 91, body='quoted: ' + f.marker(PARENT, 'followup-a'),
                          status=None, in_project=False)
        self.gh.repos[REPO][91]['updated_at'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        self.assertIsNone(f.find_marked({'parent': PARENT, 'key': 'followup-a', 'repo': REPO,
                                         'viewer_id': VIEWER_ID, 'intent_at': datetime.now(timezone.utc).isoformat(),
                                         'viewer_login': VIEWER_LOGIN}))

    def test_agent_body_containing_reserved_marker_word_is_refused(self):
        for field, raw in (('body', self.payload(body='see autocontroller-followup parent=1')),
                           ('title', self.payload(title='autocontroller-followup')),
                           ('reason', self.payload(reason='AutoController-Followup forged'))):
            with self.subTest(field=field):
                with self.assertRaises(f.FollowupError):
                    f.parse_input(raw)
        self.assertEqual(self.gh.created, [])


class CrashAndResumeTests(FollowupBase):
    def test_failure_after_issue_create_is_adopted_not_duplicated(self):
        self.gh.fail['item-add'] = 1
        with self.assertRaises(f.FollowupError):
            self.create()
        self.assertEqual(self.journal()[0]['state'], 'created')
        created = self.gh.created[0][1]
        results = self.reconcile()
        self.assertEqual([r['ok'] for r in results], [True])
        self.assertEqual(len(self.gh.created), 1)
        self.assertEqual(self.project_status(created), 'Todo')

    def test_failed_todo_edit_leaves_entry_retryable_without_new_issue(self):
        self.gh.fail['item-edit'] = 1
        with self.assertRaises(f.FollowupError):
            self.create()
        self.assertEqual(self.journal()[0]['state'], 'added')
        self.assertTrue(self.reconcile()[0]['ok'])
        self.assertEqual(len(self.gh.created), 1)
        self.assertEqual(self.project_status(self.journal()[0]['number']), 'Todo')

    def test_due_callback_defers_entry_without_gh_calls(self):
        self.gh.fail['item-add'] = 1
        with self.assertRaises(f.FollowupError):
            self.create()
        before = len(self.gh.calls)
        results = self.reconcile(due=lambda parent, key: False)
        self.assertEqual(len(self.gh.calls), before)
        self.assertTrue(results[0]['waiting'])
        self.assertFalse(results[0]['ok'])

    def test_corrupt_journal_is_refused_not_silently_reset(self):
        (self.pdir / f.JOURNAL_FILE).write_text('{"entries": "nope"}', encoding='utf-8')
        with self.assertRaises(f.FollowupError):
            self.reconcile()
        with self.assertRaises(f.FollowupError):
            self.create()

    def test_journal_is_written_0600_and_lock_is_created(self):
        self.create()
        self.assertEqual((self.pdir / f.JOURNAL_FILE).stat().st_mode & 0o777, 0o600)
        self.assertTrue((self.pdir / f.LOCK_FILE).exists())


class ForgedJournalTests(FollowupBase):
    """Item 4: a journal entry that does not match this worker's repo, owner and project is a blocker."""

    def tamper(self, **changes):
        self.gh.fail['item-add'] = 1
        with self.assertRaises(f.FollowupError):
            self.create()
        entries = self.journal()
        entries[0].update(changes)
        (self.pdir / f.JOURNAL_FILE).write_text(json.dumps({'entries': entries}), encoding='utf-8')

    def test_same_repo_different_project_number_is_blocked_with_no_gh_call(self):
        self.tamper(project=PROJECT + 1)
        before = len(self.gh.calls)
        results = self.reconcile()
        self.assertEqual(len(self.gh.calls), before, 'no gh call for a foreign entry')
        self.assertFalse(results[0]['ok'])
        self.assertIn('does not belong', results[0]['error'])
        self.assertEqual(self.journal()[0]['state'], 'created', 'entry kept, not mutated')

    def test_different_owner_is_blocked(self):
        self.tamper(owner='someone-else')
        before = len(self.gh.calls)
        self.assertFalse(self.reconcile()[0]['ok'])
        self.assertEqual(len(self.gh.calls), before)

    def test_repo_and_owner_match_case_insensitively(self):
        self.assertTrue(f.same_target({'repo': 'Example-Owner/Demo', 'owner': 'EXAMPLE-OWNER', 'project': 1},
                                      REPO, OWNER, PROJECT))

    def test_entry_of_another_repo_is_blocked_not_silently_skipped(self):
        self.tamper(repo=OTHER_REPO)
        before = len(self.gh.calls)
        results = self.reconcile()
        self.assertEqual(len(self.gh.calls), before)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]['ok'])


class ControllerGateTests(FollowupBase):
    """Controller reconciliation gate (unit level): no AI, defers selection, backs off per entry."""

    def worker(self):
        return make_worker(self)

    def test_clean_journal_does_not_defer_selection(self):
        self.assertFalse(self.worker().followup_gate())

    def test_failed_entry_defers_selection_sets_per_entry_retry_and_notice(self):
        self.gh.fail['item-add'] = 10
        with self.assertRaises(f.FollowupError):
            self.create()
        w = self.worker()
        with patch.object(w, 'notice') as notice:
            self.assertTrue(w.followup_gate())
        key = f'followup:{PARENT}:followup-a'
        self.assertGreater(w.retry_map()[key]['until'], time.time())
        self.assertEqual(notice.call_args.args[0], 'followup-blocked')

    def test_retry_backoff_suppresses_attempts_until_due(self):
        self.gh.fail['item-add'] = 10
        with self.assertRaises(f.FollowupError):
            self.create()
        w = self.worker()
        with patch.object(w, 'notice'):
            w.followup_gate()
        before = len(self.gh.calls)
        with patch.object(w, 'notice'):
            self.assertTrue(w.followup_gate())
        self.assertEqual(len(self.gh.calls), before)

    def test_recovery_after_backoff_completes_admission_and_clears_the_retry(self):
        self.gh.fail['item-add'] = 10
        with self.assertRaises(f.FollowupError):
            self.create()
        w = self.worker()
        with patch.object(w, 'notice'):
            self.assertTrue(w.followup_gate())
        self.gh.fail['item-add'] = 0
        retries = w.retry_map()
        retries[f'followup:{PARENT}:followup-a']['until'] = 0
        w.retries_file.write_text(json.dumps(retries), encoding='utf-8')
        self.assertFalse(w.followup_gate())
        self.assertNotIn(f'followup:{PARENT}:followup-a', w.retry_map())
        self.assertEqual(self.journal()[0]['state'], 'done')

    def test_unreadable_journal_defers_selection_fail_closed(self):
        (self.pdir / f.JOURNAL_FILE).write_text('{"entries": 3}', encoding='utf-8')
        w = self.worker()
        with patch.object(w, 'notice') as notice:
            self.assertTrue(w.followup_gate())
        self.assertEqual(notice.call_args.args[0], 'followup-blocked')

    def test_forged_entry_defers_selection_and_keeps_entry(self):
        self.gh.fail['item-add'] = 1
        with self.assertRaises(f.FollowupError):
            self.create()
        entries = self.journal()
        entries[0]['project'] = PROJECT + 5
        (self.pdir / f.JOURNAL_FILE).write_text(json.dumps({'entries': entries}), encoding='utf-8')
        w = self.worker()
        before = len(self.gh.calls)
        with patch.object(w, 'notice') as notice:
            self.assertTrue(w.followup_gate())
        self.assertEqual(len(self.gh.calls), before)
        self.assertEqual(notice.call_args.args[0], 'followup-blocked')
        self.assertEqual(len(self.journal()), 1)

    def test_followup_gate_does_not_swallow_unexpected_errors(self):
        w = self.worker()
        with patch.object(load_controller().followup, 'reconcile', side_effect=RuntimeError('bug')):
            with self.assertRaises(RuntimeError):
                w.followup_gate()


class ErrorTypeTests(FollowupBase):
    """Item 5: a grant refusal inside run_wrapper surfaces as ControllerError, not a bare FollowupError."""

    def test_grant_refusal_is_controller_error_at_the_worker_boundary(self):
        m = load_controller()
        w = ControllerGateTests.worker(self)
        with patch.object(f, 'grant', side_effect=f.FollowupError('grant belongs to another task')):
            with self.assertRaises(m.ControllerError):
                w.run_wrapper('prompt', 'issue', PARENT, 'code')


class FakeProcess:
    """Stands in for the wrapper subprocess: exits at once, with an empty log."""
    pid = 424242

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


class RunWrapperGrantTests(FollowupBase):
    """Item 6e: the follow-up grant and prompt reach only issue code/repair sessions."""

    def run_case(self, kind, phase, pending):
        w = make_worker(self)
        if pending:
            (self.pdir / 'cycle.pending').write_text('{}', encoding='utf-8')
        else:
            (self.pdir / 'cycle.pending').unlink(missing_ok=True)
        captured = {}

        def popen(argv, **kwargs):
            captured['argv'] = argv
            captured['env'] = kwargs.get('env', {})
            return FakeProcess()

        with patch('subprocess.Popen', side_effect=popen), \
                patch.object(w, 'drain_group'), patch.object(w, 'prune_logs'), \
                patch.object(w, 'memory_gate'):
            w.run_wrapper('BASE PROMPT', kind, PARENT, phase=phase)
        return w, captured

    def test_issue_code_session_gets_grant_env_and_prompt(self):
        _, cap = self.run_case('issue', 'code', pending=False)
        self.assertIn('followup.py', cap['env'].get('AUTOCONTROLLER_FOLLOWUP_INSTRUCTIONS', ''))
        self.assertIn('followup.py', cap['argv'][-1])
        self.assertTrue((self.pdir / f.GRANT_FILE).exists() is False, 'grant removed after the session')

    def test_issue_repair_session_gets_grant(self):
        _, cap = self.run_case('issue', 'repair', pending=True)
        self.assertIn('followup.py', cap['env'].get('AUTOCONTROLLER_FOLLOWUP_INSTRUCTIONS', ''))
        self.assertTrue((self.pdir / f.GRANT_FILE).exists(), 'grant kept while the task is still pending')

    def test_selection_renovate_and_maintenance_get_no_grant_and_no_file(self):
        for kind, phase in (('selection', 'selection'), ('renovate', 'code'), ('maintenance', 'code')):
            with self.subTest(kind=kind, phase=phase):
                (self.pdir / f.GRANT_FILE).unlink(missing_ok=True)
                _, cap = self.run_case(kind, phase, pending=False)
                self.assertNotIn('AUTOCONTROLLER_FOLLOWUP_INSTRUCTIONS', cap['env'])
                self.assertNotIn('followup.py', cap['argv'][-1])
                self.assertFalse((self.pdir / f.GRANT_FILE).exists())

    def test_pending_grant_for_another_task_is_refused_before_any_session(self):
        # A grant left by task #PARENT+1 (pending) must not be reused for task #PARENT.
        f.write_json(self.grant_path, {'token': 'c' * 48, 'repo': REPO, 'project_owner': OWNER,
                                       'project_number': PROJECT, 'parent': PARENT + 1, 'kind': 'issue',
                                       'project_dir': 'demo'})
        m = load_controller()
        w = make_worker(self)
        (self.pdir / 'cycle.pending').write_text('{}', encoding='utf-8')
        with patch('subprocess.Popen') as popen, patch.object(w, 'memory_gate'):
            with self.assertRaises(m.ControllerError):
                w.run_wrapper('BASE PROMPT', 'issue', PARENT, phase='code')
        popen.assert_not_called()


if __name__ == '__main__':
    unittest.main()
