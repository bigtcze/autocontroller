"""STAND-IN GitHub for the offline E2E acceptance suite (not GitHub).

One JSON file per world holds several repositories, their Projects, Issues,
PRs, checks, workflow runs, comments, tags and releases. Every call is guarded
by an fcntl lock so the in-process controller and the fake agent subprocess
see one consistent store.

Only argv shapes that the real controller emits are modelled. Anything else
raises UnmodelledCall, so an unmodelled GitHub invocation is visible.
Every mutating call is recorded in `mutations` with its actor.
"""
from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import re
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('e2e_ctl', str(ROOT / 'autonomy-controller.py'))
ctl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ctl)

REQUIRED_WORKFLOW = 'required.yml'
RENOVATE_USER = {'login': 'renovate[bot]', 'type': 'Bot', 'id': 29139614}


class UnmodelledCall(AssertionError):
    """A GitHub invocation the fake does not model. Always a test-visible failure."""


def _sha(*parts: str) -> str:
    return hashlib.sha1('|'.join(parts).encode()).hexdigest()


def _not_found(argv: list[str]) -> Exception:
    return ctl.ControllerError(f"Command rc=1: {' '.join(argv)}: gh: Not Found (HTTP 404)")


class FakeGitHub:
    """File-backed, lock-guarded fake. Construct once per world; share the path with agents."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + '.lock')
        # flock serialises processes; this lock serialises threads of one process
        # (flock is per open file description and is not a thread-level guarantee).
        self._thread_lock = threading.RLock()
        if not self.path.exists():
            self._write({'repos': {}, 'projects': {}, 'users': {}, 'mutations': [], 'reads': 0,
                         'next_id': 1000})

    # ----- storage ---------------------------------------------------------
    def _write(self, doc: dict):
        tmp = self.path.with_name(self.path.name + '.tmp')
        tmp.write_text(json.dumps(doc, sort_keys=True), encoding='utf-8')
        tmp.replace(self.path)

    def _txn(self, change):
        """Run `change(doc)` under an exclusive lock; persist the mutated document."""
        with self._thread_lock, self.lock_path.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            doc = json.loads(self.path.read_text(encoding='utf-8'))
            out = change(doc)
            self._write(doc)
            return out

    def read(self) -> dict:
        return self._txn(lambda d: json.loads(json.dumps(d)))

    def _id(self, doc: dict) -> int:
        doc['next_id'] += 1
        return doc['next_id']

    # One deterministic, strictly increasing clock per world. Every GitHub-side
    # event (issue reopen, PR merge) takes the next tick, so event order in the
    # fake always matches the order in which the calls happened.
    _CLOCK_BASE = datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc)

    def _tick(self, doc: dict) -> str:
        doc['clock'] = doc.get('clock', 0) + 1
        return (self._CLOCK_BASE + timedelta(seconds=doc['clock'])).strftime('%Y-%m-%dT%H:%M:%SZ')

    # ----- world building (test-side) ----------------------------------------
    def add_repo(self, repo: str, *, default_branch: str = 'main', viewer: str = 'example-owner'):
        def change(d):
            d['repos'][repo] = {'default_branch': default_branch, 'issues': {}, 'prs': {}, 'tags': {},
                                'tag_objects': {}, 'comments': {}, 'runs': [], 'releases': {},
                                'main_sha': _sha('main', repo, '0'), 'workflows': ['required.yml', 'release.yml'],
                                'release_workflow_ok': True}
        self._txn(change)

    def add_project(self, repo: str, owner: str, number: int, statuses=('Backlog', 'In Progress', 'Review', 'Done')):
        def change(d):
            d['projects'][f'{owner}/{number}'] = {'repo': repo, 'id': f'PVT_{owner}_{number}', 'owner': owner,
                                                  'number': number, 'field_id': f'FLD_{number}',
                                                  'options': {s: f'OPT_{number}_{i}' for i, s in enumerate(statuses)},
                                                  'items': {}}
        self._txn(change)

    def add_issue(self, repo: str, *, title: str, body: str = '', state: str = 'open',
                  project: str | None = None, status: str = 'Backlog',
                  association: str = 'OWNER', author: dict | None = None,
                  labels: tuple = ()) -> int:
        def change(d):
            r = d['repos'][repo]
            n = max([int(k) for k in r['issues']] + [0]) + 1
            r['issues'][str(n)] = {'number': n, 'title': title, 'body': body, 'state': state,
                                   'author_association': association,
                                   'user': author or {'login': 'example-author', 'type': 'User', 'id': 2002},
                                   'labels': [{'name': x} for x in labels], 'milestone': None, 'assignees': [], 'comments': 0,
                                   'html_url': f'https://github.com/{repo}/issues/{n}'}
            if project:
                p = d['projects'][project]
                p['items'][str(n)] = {'item_id': f'PVTI_{repo}_{n}', 'status': status}
            return n
        return self._txn(change)

    def set_outage(self, on: bool):
        """Test-side switch read by every gh call (controller and agent alike), persisted in the store."""
        self._txn(lambda d: d.__setitem__('outage', bool(on)))

    def _outage_active(self) -> bool:
        return bool(self.read().get('outage'))

    def set_issue_state(self, repo: str, n: int, state: str):
        self._txn(lambda d: d['repos'][repo]['issues'][str(n)].__setitem__('state', state))

    def set_project_status(self, project: str, n: int, status: str):
        self._txn(lambda d: d['projects'][project]['items'][str(n)].__setitem__('status', status))

    def add_pr(self, repo: str, *, title: str, body: str, head_sha: str, head_repo: str | None = None,
               head_ref: str = 'feature', user: dict | None = None, draft: bool = False) -> int:
        def change(d):
            r = d['repos'][repo]
            n = max([int(k) for k in r['prs']] + [int(k) for k in r['issues']] + [0]) + 1
            head_repo_name = head_repo or repo
            r['prs'][str(n)] = {'number': n, 'title': title, 'body': body, 'state': 'open',
                                'merged_at': None, 'merge_commit_sha': None, 'draft': draft,
                                'head': {'sha': head_sha, 'ref': head_ref,
                                         'repo': {'full_name': head_repo_name}},
                                'base': {'ref': 'main', 'repo': {'full_name': repo}},
                                'user': user or {'login': 'example-dev', 'type': 'User', 'id': 3003},
                                'labels': [], 'checks': {}, 'html_url': f'https://github.com/{repo}/pull/{n}'}
            return n
        return self._txn(change)

    def push_pr_commit(self, repo: str, n: int, sha: str):
        def change(d):
            pr = d['repos'][repo]['prs'][str(n)]
            pr['head']['sha'] = sha
        self._txn(change)

    def set_draft(self, repo: str, n: int, draft: bool):
        self._txn(lambda d: d['repos'][repo]['prs'][str(n)].__setitem__('draft', draft))

    def set_checks(self, repo: str, n: int, sha: str, rows: list[dict]):
        def change(d):
            d['repos'][repo]['prs'][str(n)]['checks'][sha] = rows
        self._txn(change)

    def set_run(self, repo: str, sha: str, status: str, conclusion: str | None = None):
        def change(d):
            runs = d['repos'][repo]['runs']
            for run in runs:
                if run['head_sha'] == sha and run['event'] == 'push':
                    run.update(status=status, conclusion=conclusion)
                    return
            raise AssertionError(f'no push run for {sha}')
        self._txn(change)

    def settle_ci(self, repo: str):
        """Fake CI provider: green required check on every open PR head, green push run on every merge.

        Scenarios that need red or pending CI set those states explicitly and do not call this.
        Only missing states are filled in; an explicit state is never overwritten.
        """
        def change(d):
            r = d['repos'][repo]
            for pr in r['prs'].values():
                if pr['state'] == 'open' and not pr['draft']:
                    pr['checks'].setdefault(pr['head']['sha'], [{'name': 'required', 'bucket': 'pass'}])
                if pr['merge_commit_sha']:
                    sha = pr['merge_commit_sha']
                    if not any(x['head_sha'] == sha and x['event'] == 'push' for x in r['runs']):
                        r['runs'].append({'id': self._id(d), 'run_number': len(r['runs']) + 1,
                                          'run_attempt': 1, 'head_sha': sha, 'event': 'push',
                                          'status': 'completed', 'conclusion': 'success'})
        self._txn(change)

    def add_comment(self, repo: str, issue: int, body: str, login: str, user_id: int = 4004) -> int:
        def change(d):
            r = d['repos'][repo]
            cid = self._id(d)
            r['comments'].setdefault(str(issue), []).append(
                {'id': cid, 'body': body, 'user': {'login': login, 'type': 'User', 'id': user_id}})
            return cid
        return self._txn(change)

    def add_push_run(self, repo: str, sha: str, status: str, conclusion: str | None = None):
        """Test-side: a required.yml push run for an exact commit (e.g. a release candidate on main)."""
        def change(d):
            runs = d['repos'][repo]['runs']
            runs.append({'id': self._id(d), 'run_number': len(runs) + 1, 'run_attempt': 1,
                         'head_sha': sha, 'event': 'push', 'status': status, 'conclusion': conclusion})
        self._txn(change)

    def set_tag(self, repo: str, tag: str, sha: str):
        """Test-side: a lightweight tag already exists on the remote (for collision cases)."""
        self._txn(lambda d: d['repos'][repo]['tags'].__setitem__(tag, sha))

    def add_merged_pr(self, repo: str, *, days_ago: float, user: dict, title: str = 'chore: deps',
                      labels: tuple = ()) -> int:
        """Test-side: a PR already merged into main `days_ago` days ago (for maintenance checks)."""
        merged = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime('%Y-%m-%dT%H:%M:%SZ')

        def change(d):
            r = d['repos'][repo]
            n = max([int(k) for k in r['prs']] + [int(k) for k in r['issues']] + [0]) + 1
            r['prs'][str(n)] = {'number': n, 'title': title, 'body': '', 'state': 'closed',
                                'merged_at': merged, 'merge_commit_sha': _sha('merged', repo, str(n)),
                                'draft': False,
                                'head': {'sha': _sha('head', repo, str(n)), 'ref': f'pr-{n}',
                                         'repo': {'full_name': repo}},
                                'base': {'ref': 'main', 'repo': {'full_name': repo}},
                                'user': user, 'labels': [{'name': x} for x in labels], 'checks': {},
                                'html_url': f'https://github.com/{repo}/pull/{n}'}
            return n
        return self._txn(change)

    def set_release(self, repo: str, tag: str, *, assets=(), published: bool = True, days_ago: float = 1):
        published_at = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime('%Y-%m-%dT%H:%M:%SZ')

        def change(d):
            d['repos'][repo]['releases'][tag] = {
                'tag_name': tag, 'draft': False, 'prerelease': False,
                'published_at': published_at if published else None,
                'assets': [{'name': a} for a in assets]}
        self._txn(change)

    # ----- introspection (test-side) ------------------------------------------
    def mutations(self) -> list[dict]:
        return self.read()['mutations']

    def mutation_argvs(self, prefix: list[str] | None = None) -> list[list[str]]:
        rows = [m['argv'] for m in self.mutations()]
        return [a for a in rows if not prefix or a[:len(prefix)] == prefix]

    def issue(self, repo: str, n: int) -> dict:
        return self.read()['repos'][repo]['issues'][str(n)]

    def pr(self, repo: str, n: int) -> dict:
        return self.read()['repos'][repo]['prs'][str(n)]

    def project_status(self, project: str, n: int) -> str:
        return self.read()['projects'][project]['items'][str(n)]['status']

    def tag(self, repo: str, tag: str) -> str | None:
        return self.read()['repos'][repo]['tags'].get(tag)

    # ----- gh dispatch ----------------------------------------------------------
    def run_gh(self, argv: list[str], actor: str = 'controller') -> str:
        """Return stdout for a gh invocation, or raise ControllerError like the real helper."""
        if argv[:1] != ['gh']:
            raise UnmodelledCall(f'not a gh call: {argv[:3]}')
        if self._outage_active():
            raise ctl.ControllerError(f"Command rc=1: {' '.join(argv)}: HTTP 503 Service Unavailable")

        def change(d):
            mutating = self._is_mutating(argv)
            if mutating:
                # Recorded before dispatch so refused attempts are counted too.
                d['mutations'].append({'argv': argv, 'actor': actor, 'refused': False})
            try:
                return ('ok', self._dispatch(d, argv, mutating, actor))
            except ctl.ControllerError as exc:
                if mutating:
                    d['mutations'][-1]['refused'] = True
                return ('error', str(exc))

        status, value = self._txn(change)
        if status == 'error':
            raise ctl.ControllerError(value)
        return value

    @staticmethod
    def _is_mutating(argv: list[str]) -> bool:
        if argv[1:3] in (['issue', 'close'], ['issue', 'reopen'], ['pr', 'merge'], ['project', 'item-edit'],
                         ['pr', 'create'], ['pr', 'ready'], ['pr', 'sync'], ['issue', 'comment'],
                         ['release', 'create']):
            return True
        if argv[1:2] == ['api']:
            return '--method' in argv and argv[argv.index('--method') + 1] in ('POST', 'PATCH', 'PUT', 'DELETE')
        return False

    def _dispatch(self, d: dict, argv: list[str], mutating: bool, actor: str) -> str:
        if argv[1:3] == ['project', 'view']:
            return json.dumps({'id': self._project(d, argv)['id']})
        if argv[1:3] == ['project', 'field-list']:
            p = self._project(d, argv)
            return json.dumps({'fields': [{'name': 'Status', 'id': p['field_id'],
                                           'options': [{'name': k, 'id': v} for k, v in p['options'].items()]}]})
        if argv[1:3] == ['project', 'item-list']:
            p = self._project(d, argv)
            repo = d['repos'][p['repo']]
            items = []
            for num, row in p['items'].items():
                issue = repo['issues'][num]
                items.append({'id': row['item_id'], 'status': row['status'], 'content': {
                    'type': 'Issue', 'number': issue['number'], 'title': issue['title'],
                    'url': issue['html_url'], 'body': issue['body']}})
            return json.dumps({'items': items, 'totalCount': len(items)})
        if argv[1:3] == ['project', 'item-edit']:
            return self._item_edit(d, argv)
        if argv[1:3] in (['issue', 'close'], ['issue', 'reopen']):
            repo = argv[argv.index('--repo') + 1]
            issue = d['repos'][repo]['issues'][argv[3]]
            issue['state'] = 'closed' if argv[2] == 'close' else 'open'
            if argv[2] == 'reopen':
                d['repos'][repo].setdefault('reopens', {}).setdefault(argv[3], []).append(self._tick(d))
            return ''
        if argv[1:3] == ['pr', 'merge']:
            return self._merge(d, argv)
        if argv[1:3] == ['pr', 'create']:
            return self._agent_pr_create(d, argv, actor)
        if argv[1:3] == ['pr', 'ready']:
            d['repos'][argv[argv.index('--repo') + 1]]['prs'][argv[3]]['draft'] = False
            return ''
        if argv[1:3] == ['pr', 'sync']:  # stand-in for pushing a commit to the PR branch
            pr = d['repos'][argv[argv.index('--repo') + 1]]['prs'][argv[3]]
            pr['head']['sha'] = argv[argv.index('--head-sha') + 1]
            return ''
        if argv[1:3] == ['issue', 'comment']:
            repo = argv[argv.index('--repo') + 1]
            cid = self._id(d)
            d['repos'][repo]['comments'].setdefault(argv[3], []).append(
                {'id': cid, 'body': argv[argv.index('--body') + 1],
                 'user': {'login': actor, 'type': 'User', 'id': 6006}})
            return ''
        if argv[1:3] == ['release', 'create']:
            # Agents must never publish. The fake records the attempt and refuses it.
            if actor != 'controller':
                raise ctl.ControllerError('forbidden in this repository: agents must not publish releases')
            raise UnmodelledCall('gh release create is not a controller path in the fake')
        if argv[1:3] == ['pr', 'checks']:
            return self._checks(d, argv)
        if argv[1:2] == ['api']:
            return self._api(d, argv, actor)
        raise UnmodelledCall(f'unmodelled gh invocation: {argv}')

    def _project(self, d: dict, argv: list[str]) -> dict:
        owner, number = argv[argv.index('--owner') + 1], argv[3]
        key = f'{owner}/{number}'
        if key not in d['projects']:
            raise UnmodelledCall(f'unknown project {key}')
        return d['projects'][key]

    def _item_edit(self, d: dict, argv: list[str]) -> str:
        item_id = argv[argv.index('--id') + 1]
        opt = argv[argv.index('--single-select-option-id') + 1]
        for p in d['projects'].values():
            for row in p['items'].values():
                if row['item_id'] == item_id:
                    names = [k for k, v in p['options'].items() if v == opt]
                    if not names:
                        raise ctl.ControllerError('Command rc=1: unknown option')
                    row['status'] = names[0]
                    return ''
        raise ctl.ControllerError('Command rc=1: unknown item')

    def _agent_pr_create(self, d: dict, argv: list[str], actor: str) -> str:
        repo = argv[argv.index('--repo') + 1]
        r = d['repos'][repo]
        head_repo = argv[argv.index('--head-repo') + 1] if '--head-repo' in argv else repo
        n = max([int(k) for k in r['prs']] + [int(k) for k in r['issues']] + [0]) + 1
        r['prs'][str(n)] = {'number': n, 'title': argv[argv.index('--title') + 1],
                            'body': argv[argv.index('--body') + 1], 'state': 'open',
                            'merged_at': None, 'merge_commit_sha': None,
                            'draft': '--draft' in argv,
                            'head': {'sha': argv[argv.index('--head-sha') + 1], 'ref': 'agent-branch',
                                     'repo': {'full_name': head_repo}},
                            'base': {'ref': 'main', 'repo': {'full_name': repo}},
                            'user': {'login': actor, 'type': 'User', 'id': 7007},
                            'labels': [], 'checks': {}, 'html_url': f'https://github.com/{repo}/pull/{n}'}
        return f'https://github.com/{repo}/pull/{n}'

    def _merge(self, d: dict, argv: list[str]) -> str:
        repo = argv[argv.index('--repo') + 1]
        n = str(argv[3])
        pr = d['repos'][repo]['prs'][n]
        expected = argv[argv.index('--match-head-commit') + 1]
        if pr['state'] != 'open' or pr['draft']:
            raise ctl.ControllerError(f'Command rc=1: {argv}: PR not mergeable (closed or draft)')
        if expected != pr['head']['sha']:
            raise ctl.ControllerError(f'Command rc=1: {argv}: head commit changed (expected {expected})')
        checks = pr['checks'].get(pr['head']['sha'], [])
        if not any(c.get('name') == 'required' and c.get('bucket') == 'pass' for c in checks):
            raise ctl.ControllerError(f'Command rc=1: {argv}: required status check not passing')
        merge_sha = _sha('merge', repo, n, pr['head']['sha'])
        d['repos'][repo]['main_sha'] = merge_sha
        # A squash/merge commit carries the head tree onto the default branch.
        trees = d['repos'][repo].setdefault('trees', {})
        trees[merge_sha] = dict(trees.get(pr['head']['sha'], {}))
        pr.update(state='closed', merged_at=self._tick(d), merge_commit_sha=merge_sha)
        d['repos'][repo]['runs'].append({'id': self._id(d), 'run_number': len(d['repos'][repo]['runs']) + 1,
                                         'run_attempt': 1, 'head_sha': merge_sha, 'event': 'push',
                                         'status': 'queued', 'conclusion': None})
        # Auto-close linked Issues the way GitHub does for default-branch merges.
        for num in re.findall(r'(?i)closes #(\d+)', pr['body']):
            issue = d['repos'][repo]['issues'].get(num)
            if issue and issue['state'] == 'open':
                issue['state'] = 'closed'
        return ''

    def _checks(self, d: dict, argv: list[str]) -> str:
        repo = argv[argv.index('--repo') + 1]
        pr = d['repos'][repo]['prs'][argv[3]]
        rows = pr['checks'].get(pr['head']['sha'], [])
        if not rows:
            return ''  # stderr carries "no checks reported" (see checks_result)
        return json.dumps(rows)

    def _api(self, d: dict, argv: list[str], actor: str) -> str:
        method = argv[argv.index('--method') + 1] if '--method' in argv else 'GET'
        if argv[2] == 'graphql':
            return json.dumps(self._graphql(d, argv))
        path = next((a for a in argv[2:] if a.startswith(('repos/', 'search/'))), None)
        if path is None:
            raise UnmodelledCall(f'unmodelled api call: {argv}')
        body = {}
        for i, tok in enumerate(argv):
            if tok in ('-f', '-F') and i + 1 < len(argv):
                k, _, v = argv[i + 1].partition('=')
                body[k] = v
        url = urlsplit(path)
        parts = url.path.split('/')
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        if method == 'GET':
            # `gh api --method GET ... -f k=v` sends k=v as URL query parameters (this is how
            # release_policy.merged_since sends its search `q`). Merge them in, URL first.
            query.update(body)
            return json.dumps(self._get(d, parts, query, argv, '--slurp' in argv))
        if method == 'POST':
            return json.dumps(self._post(d, parts, body, argv, actor))
        raise UnmodelledCall(f'unmodelled method {method}: {argv}')

    def _get(self, d, parts, query, argv, slurp):
        if parts[0] == 'search':
            return self._search_issues(d, query, argv)
        repo = f'{parts[1]}/{parts[2]}'
        if repo not in d['repos']:
            raise _not_found(argv)
        r = d['repos'][repo]
        rest = parts[3:]
        if rest == ['pulls']:
            rows = [p for p in r['prs'].values() if p['state'] == 'open']
            rows = [self._public_pr(p) for p in sorted(rows, key=lambda x: x['number'])]
            return [rows] if slurp else rows
        if rest[:1] == ['pulls'] and len(rest) == 2:
            pr = r['prs'].get(rest[1])
            if pr is None:
                raise _not_found(argv)
            return self._public_pr(pr)
        if rest == ['issues'] and parts[-1].startswith('issues'):
            rows = [self._public_issue(i) for i in r['issues'].values()]
            rows += [self._public_pr(p) | {'pull_request': {'url': 'x'}} for p in r['prs'].values()]
            # Label filter as GitHub applies it: only when a `labels` query is present.
            if 'labels' in query:
                wanted = {x for x in query['labels'].split(',') if x}
                rows = [x for x in rows if wanted <= {label['name'] for label in x.get('labels', [])}]
            if query.get('state') == 'open':
                rows = [x for x in rows if x.get('state') == 'open']
            rows = sorted(rows, key=lambda x: -x['number'])
            return [rows] if slurp else rows
        if rest[:1] == ['issues'] and len(rest) == 2:
            issue = r['issues'].get(rest[1])
            if issue is None:
                raise _not_found(argv)
            return self._public_issue(issue)
        if rest[:1] == ['issues'] and len(rest) == 3 and rest[2] == 'comments':
            rows = r['comments'].get(rest[1], [])
            return [rows] if slurp else rows
        if rest[:3] == ['git', 'ref', 'tags'] and len(rest) == 4:
            return self._tag_ref(r, rest[3], argv)
        if rest[:1] == ['compare'] and len(rest) == 2 and '...' in rest[1]:
            base, head = rest[1].split('...', 1)
            # A candidate is "ahead" when it is a different commit than the base tag's target.
            ahead = head != (self._tag_commit(r, base) or base)
            return {'status': 'ahead' if ahead else 'identical', 'ahead_by': 1 if ahead else 0}
        if rest[:2] == ['git', 'tags'] and len(rest) == 3:
            commit = r['tag_objects'].get(rest[2])
            if commit is None:
                raise _not_found(argv)
            return {'sha': rest[2], 'object': {'type': 'commit', 'sha': commit}}
        if rest[:2] == ['releases', 'tags'] and len(rest) == 3:
            rel = r['releases'].get(rest[2])
            if rel is None:
                raise _not_found(argv)
            return rel
        if rest == ['releases']:
            rows = list(r['releases'].values())
            return [rows] if slurp else rows
        if rest[:2] == ['git', 'trees'] and len(rest) == 3:
            files = r.get('trees', {}).get(rest[2])
            if files is None:
                raise _not_found(argv)
            return {'truncated': False, 'tree': [
                {'path': p, 'type': 'blob', 'mode': '100644', 'size': len(c)} for p, c in files.items()]}
        if rest[:1] == ['contents'] and len(rest) >= 2:
            path = '/'.join(rest[1:])
            files = r.get('trees', {}).get(query.get('ref', ''), {})
            if path not in files:
                raise _not_found(argv)
            import base64
            return {'type': 'file', 'encoding': 'base64', 'size': len(files[path]),
                    'content': base64.b64encode(files[path].encode()).decode()}
        if rest[:1] == ['branches'] and len(rest) == 2:
            return {'name': rest[1], 'commit': {'sha': r['main_sha']}}
        if rest[:3] == ['actions', 'workflows', 'required.yml'] and len(rest) == 4 and rest[3] == 'runs':
            sha = query.get('head_sha', '')
            rows = [x for x in r['runs'] if x['head_sha'] == sha and x['event'] == 'push']
            return {'workflow_runs': rows}
        if rest[:2] == ['actions', 'workflows'] and len(rest) == 3:
            if rest[2] not in r['workflows']:
                raise _not_found(argv)
            return {'id': 1, 'path': f'.github/workflows/{rest[2]}'}
        raise UnmodelledCall(f'unmodelled GET {"/".join(parts)} {query}')

    def _search_issues(self, d: dict, query: dict, argv: list[str]):
        """Model only the merged-PR query shape used by release_policy.merged_since."""
        q = query.get('q', '')
        m = re.fullmatch(r'repo:(\S+) is:pr is:merged base:(\S+) merged:>=(\d{4}-\d{2}-\d{2})', q)
        if not m:
            raise UnmodelledCall(f'unmodelled search query: {q!r}')
        repo, base, day = m.group(1), m.group(2), m.group(3)
        r = d['repos'].get(repo)
        if r is None:
            raise _not_found(argv)
        items = [self._public_pr(p) | {'pull_request': {'url': 'x'}}
                 for p in sorted(r['prs'].values(), key=lambda x: x['number'])
                 if p['merged_at'] and p['base']['ref'] == base and p['merged_at'][:10] >= day]
        return {'total_count': len(items), 'incomplete_results': False, 'items': items}

    def _post(self, d, parts, body, argv, actor):
        repo = f'{parts[1]}/{parts[2]}'
        r = d['repos'][repo]
        rest = parts[3:]
        if rest[:1] == ['issues'] and len(rest) == 3 and rest[2] == 'comments':
            cid = self._id(d)
            comment = {'id': cid, 'body': body.get('body', ''), 'user': {'login': 'example-bot', 'type': 'Bot', 'id': 5005}}
            r['comments'].setdefault(rest[1], []).append(comment)
            return comment
        if rest == ['git', 'tags']:
            obj = self._id(d)
            sha = _sha('tagobj', repo, body['tag'], body['object'], str(obj))
            r['tag_objects'][sha] = body['object']
            return {'sha': sha}
        if rest == ['git', 'refs']:
            if actor != 'controller':
                raise ctl.ControllerError('forbidden: agents must not create tags')
            tag = body['ref'].replace('refs/tags/', '')
            if tag in r['tags']:
                raise ctl.ControllerError('Command rc=1: Reference already exists (HTTP 422)')
            r['tags'][tag] = body['sha']
            return {'ref': body['ref']}
        raise UnmodelledCall(f'unmodelled POST {"/".join(parts)}')

    def _tag_ref(self, r: dict, tag: str, argv: list[str]) -> dict:
        if tag not in r['tags']:
            raise _not_found(argv)
        target = r['tags'][tag]
        # Annotated tags point at a tag object; lightweight tags point at a commit.
        kind = 'tag' if target in r['tag_objects'] else 'commit'
        return {'ref': f'refs/tags/{tag}', 'object': {'type': kind, 'sha': target}}

    @staticmethod
    def _tag_commit(r: dict, tag: str) -> str | None:
        value = r['tags'].get(tag)
        if value is None:
            return None
        return r['tag_objects'].get(value, value)

    def _graphql(self, d: dict, argv: list[str]) -> dict:
        owner = argv[argv.index('-F') + 1].split('=', 1)[1]
        repo_name = argv[argv.index('-F', argv.index('-F') + 1) + 1].split('=', 1)[1]
        number = int(argv[-1].split('=', 1)[1])
        full = f'{owner}/{repo_name}'
        r = d['repos'].get(full)
        if r is None or str(number) not in r['issues']:
            return {'data': {'repository': {'issue': None}}}
        nodes = []
        for pr in r['prs'].values():
            if not re.search(rf'(?i)closes #{number}\b', pr['body']):
                continue
            state = {'open': 'OPEN', 'closed': 'MERGED' if pr['merged_at'] else 'CLOSED'}[pr['state']]
            head_repo = pr['head']['repo']['full_name']
            nodes.append({'number': pr['number'], 'url': pr['html_url'], 'state': state,
                          'mergedAt': pr['merged_at'],
                          'headRefOid': pr['head']['sha'], 'baseRefName': 'main',
                          'isCrossRepository': head_repo.lower() != full.lower(),
                          'headRepository': {'nameWithOwner': head_repo},
                          'mergeCommit': {'oid': pr['merge_commit_sha']} if pr['merge_commit_sha'] else None})
        reopens = r.get('reopens', {}).get(str(number), [])
        timeline = {'totalCount': len(reopens), 'nodes': [{'createdAt': reopens[-1]}] if reopens else []}
        return {'data': {'repository': {'issue': {'timelineItems': timeline, 'closedByPullRequestsReferences': {
            'nodes': nodes, 'pageInfo': {'hasNextPage': False}}}}}}

    @staticmethod
    def _public_issue(i: dict) -> dict:
        return {k: v for k, v in i.items() if k != 'comments'}

    @staticmethod
    def _public_pr(p: dict) -> dict:
        out = {k: v for k, v in p.items() if k != 'checks'}
        out['state'] = p['state']
        return out


def checks_exit_code(rows: list[dict]) -> int:
    """Exit codes of `gh pr checks`: 8 while any check is pending, 1 if any failed, 0 if all passed."""
    buckets = {str(r.get('bucket') or '').lower() for r in rows}
    if buckets & {'fail', 'cancel'}:
        return 1
    if buckets & {'pending', 'queued', 'in_progress'} or not buckets <= {'pass', 'skipping'}:
        return 8
    return 0


def checks_result(store: FakeGitHub, argv: list[str]) -> subprocess.CompletedProcess:
    """Shape of `gh pr checks --json`: rows on stdout with the real exit code, or rc=1 + 'no checks'."""
    try:
        out = store.run_gh(argv, actor='controller')
    except ctl.ControllerError as exc:
        return subprocess.CompletedProcess(argv, 1, '', str(exc))
    if not out:
        return subprocess.CompletedProcess(argv, 1, '', 'no checks reported on the pull request')
    return subprocess.CompletedProcess(argv, checks_exit_code(json.loads(out)), out, '')


def patch_targets(store: FakeGitHub):
    """Return (command_fn, run_fn) to patch into autonomy-controller at the argv level.

    - ctl.command(argv,...) : gh/git argv through the store; git is not modelled here.
    - ctl.subprocess.run(argv,...) : used by pr_ci_state; gh argv only.
    Non-gh argv is passed to the real subprocess.run with a timeout.
    """
    def command_fn(argv, **kwargs):
        if argv[:1] == ['gh']:
            return store.run_gh(list(argv), actor='controller')
        return _REAL_COMMAND(argv, **kwargs)

    def run_fn(argv, *args, **kwargs):
        if argv[:1] == ['gh']:
            return checks_result(store, list(argv))
        return _REAL_RUN(argv, *args, **kwargs)

    return command_fn, run_fn


# Captured once at import, before any test patches them, so patching is never nested.
_REAL_COMMAND = ctl.command
_REAL_RUN = subprocess.run
