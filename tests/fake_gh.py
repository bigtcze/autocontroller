#!/usr/bin/env python3
"""Stateful, deterministic stand-in for the GitHub CLI (`gh`) used by onboarding tests.

Contract (assumptions to confirm against live gh; see docs/ONBOARDING_TESTING.md):
- State is JSON in the file named by $FAKE_GH_STATE. It is read and written
  under an fcntl lock (sidecar `<state>.lock`) with an atomic replace.
- Every invocation is appended to state["calls"] as {"argv": [...], "rc": N}.
- Only the invocations handled below are supported. Anything else exits 4.
- Fault injection: state["faults"] is a list of
  {"match": [tokens], "mode": "before"|"after", "rc": N, "once": true}.
  A match token must be a prefix of the corresponding argv token and the
  tokens must be contiguous. "before": nothing is applied, exit N.
  "after": the mutation is applied, then exit N and the response is discarded
  (lost response after the server accepted the request).
- Refuses with rc 5 when a GitHub token is present in the environment.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import sys
import tempfile
from pathlib import Path

TOKEN_ENV = ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN')
ISSUE_URL = re.compile(r'^https://github\.com/([^/]+)/([^/]+)/issues/(\d+)$')
DEFAULT_STATUS = ['Todo', 'In Progress', 'Done']
VALUE_OPTIONS = {'--method', '-X', '-f', '-F', '-H', '--owner', '--format', '--limit',
                 '--title', '--repo', '--url', '--scopes', '--hostname', '--git-protocol',
                 '--id', '--project-id', '--field-id', '--single-select-option-id',
                 '--dir', '--pattern'}
BOOL_OPTIONS = {'-i', '--paginate', '--slurp', '--closed', '--web'}


class Fail(Exception):
    """Expected CLI failure with a real-looking message and exit status."""

    def __init__(self, rc: int, message: str):
        super().__init__(message)
        self.rc = rc
        self.message = message


class Unsupported(Fail):
    def __init__(self, what: str):
        super().__init__(4, f'fake gh: unsupported invocation: {what}')


def parse(tokens: list[str]):
    """Return (positional, options, flags, fields) for the supported option set."""
    positional, options, flags, fields = [], {}, set(), []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in VALUE_OPTIONS:
            if i + 1 >= len(tokens):
                raise Unsupported(f'missing value for {tok}')
            value = tokens[i + 1]
            if tok in ('-f', '-F'):
                fields.append(value)
            else:
                options['--method' if tok == '-X' else tok] = value
            i += 2
            continue
        if tok in BOOL_OPTIONS:
            flags.add(tok)
        elif tok.startswith('-'):
            raise Unsupported(f'option {tok}')
        else:
            positional.append(tok)
        i += 1
    return positional, options, flags, fields


def split_scopes(value: str) -> set[str]:
    return {s.strip() for s in value.split(',') if s.strip()}


def find_owner(state: dict, value: str) -> str:
    for owner in state.get('owners', {}):
        if owner.lower() == value.lower():
            return owner
    raise Fail(1, f'HTTP 404: owner {value!r} could not be resolved')


def find_repo(state: dict, owner: str, name: str) -> dict:
    for key, repo in state.get('repos', {}).items():
        if key.lower() == f'{owner}/{name}'.lower():
            return repo
    raise Fail(1, 'HTTP 404: Not Found (https://api.github.com/repos)')


def find_project(state: dict, owner_value: str, number_value: str) -> dict:
    owner = find_owner(state, owner_value)
    try:
        number = int(number_value)
    except ValueError as exc:
        raise Unsupported(f'project number {number_value!r}') from exc
    for project in state.get('projects', []):
        if project['owner'] == owner and project['number'] == number:
            return project
    raise Fail(1, f'could not find project {number} for owner {owner}')


def option_id(name: str) -> str:
    return 'opt-' + re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')


def public_project(project: dict) -> dict:
    return {k: project[k] for k in ('id', 'number', 'title', 'url', 'closed')}


def issue_by_url(state: dict, url: str) -> dict:
    match = ISSUE_URL.match(url)
    if not match:
        raise Fail(1, f'could not resolve to an Issue: {url}')
    owner, name, number = match.group(1), match.group(2), int(match.group(3))
    repo = find_repo(state, owner, name)
    for issue in state.get('issues', {}).get(repo['full_name'].lower(), []):
        if issue['number'] == number:
            return issue
    raise Fail(1, f'could not resolve to an Issue: {url}')


def auth(argv: list[str], state: dict) -> str:
    sub = argv[1]
    _, opts, flags, _ = parse(argv[2:])
    if opts.get('--hostname') != 'github.com':
        raise Unsupported(' '.join(argv))
    if sub == 'status':
        if not state.get('logged_in'):
            raise Fail(1, 'You are not logged into any GitHub hosts. To log in, run: gh auth login')
        return ''
    if sub == 'login':
        if '--web' not in flags or '--scopes' not in opts:
            raise Unsupported(' '.join(argv))
        if state.get('login_fail'):
            raise Fail(1, 'Authentication aborted by user during the device flow')
        state['logged_in'] = True
        state['scopes'] = sorted(split_scopes(opts['--scopes']))
        return ''
    if sub == 'refresh':
        if not state.get('logged_in'):
            raise Fail(1, 'You are not logged in to any GitHub hosts')
        if state.get('refresh_fail'):
            raise Fail(1, 'Authentication refresh was aborted by the user')
        state['scopes'] = sorted(set(state.get('scopes', [])) | split_scopes(opts.get('--scopes', '')))
        return ''
    if sub == 'setup-git':
        if not state.get('logged_in'):
            raise Fail(1, 'You are not logged into any GitHub hosts')
        return ''
    raise Unsupported(' '.join(argv))


def api(args: list[str], state: dict) -> str:
    positional, opts, flags, fields = parse(args)
    if len(positional) != 1:
        raise Unsupported('api ' + ' '.join(args))
    if not state.get('logged_in'):
        raise Fail(1, 'HTTP 401: Requires authentication (https://api.github.com)')
    method = opts.get('--method', 'GET').upper()
    base, _, query = positional[0].partition('?')
    params = dict(part.partition('=')[::2] for part in query.split('&') if part)
    data = dict(field.partition('=')[::2] for field in fields)
    parts = base.split('/')
    if base == 'user' and method == 'GET':
        body = json.dumps(state['user']) + '\n'
        if '-i' not in flags:
            return body
        scopes = ', '.join(sorted(state.get('scopes', [])))
        return ('HTTP/2.0 200 OK\r\nContent-Type: application/json; charset=utf-8\r\n'
                f'X-OAuth-Scopes: {scopes}\r\nX-Accepted-OAuth-Scopes: \r\n\r\n' + body)
    if parts[:1] == ['repos'] and len(parts) in (3, 4):
        repo = find_repo(state, parts[1], parts[2])
        key = repo['full_name'].lower()
        if len(parts) == 3 and method == 'GET':
            return json.dumps(repo) + '\n'
        if len(parts) == 3 and method == 'PATCH' and data.get('has_issues') == 'true':
            if not repo['permissions'].get('admin'):
                raise Fail(1, 'HTTP 403: Must have admin rights to Repository.')
            repo['has_issues'] = True
            return json.dumps(repo) + '\n'
        if len(parts) == 4 and parts[3] == 'issues' and method == 'GET':
            wanted = params.get('state', 'open')
            rows = [i for i in state.get('issues', {}).get(key, []) if wanted == 'all' or i['state'] == wanted]
            return json.dumps([rows] if '--slurp' in flags else rows) + '\n'
        if len(parts) == 4 and parts[3] == 'issues' and method == 'POST':
            if not repo['permissions'].get('push'):
                raise Fail(1, 'HTTP 403: Must have push access to create issues')
            if not repo.get('has_issues'):
                raise Fail(410, 'HTTP 410: Issues are disabled for this repository')
            if not data.get('title'):
                raise Unsupported('issue without title')
            rows = state.setdefault('issues', {}).setdefault(key, [])
            number = max([i['number'] for i in rows] + [0]) + 1
            user = state['user']
            issue = {'number': number, 'title': data['title'], 'body': data.get('body', ''),
                     'state': 'open', 'html_url': f"https://github.com/{repo['full_name']}/issues/{number}",
                     'author_association': repo.get('viewer_association', 'NONE'),
                     'user': {'login': user['login'], 'type': 'User', 'id': user['id']}}
            rows.append(issue)
            return json.dumps(issue) + '\n'
    raise Unsupported('api ' + ' '.join(args))


def project(args: list[str], state: dict) -> str:
    if 'project' not in state.get('scopes', []):
        raise Fail(1, 'missing required scope "project"; run: gh auth refresh --scopes project')
    sub = args[0]
    positional, opts, flags, _ = parse(args[1:])
    if opts.get('--format', 'json') != 'json':
        raise Unsupported('project --format ' + opts['--format'])
    if sub == 'list':
        owner = find_owner(state, opts['--owner'])
        rows = [p for p in state.get('projects', []) if p['owner'] == owner and (not p['closed'] or '--closed' in flags)]
        return json.dumps({'projects': [public_project(p) for p in rows], 'totalCount': len(rows)}) + '\n'
    if sub == 'create':
        owner = find_owner(state, opts['--owner'])
        meta = state['owners'][owner]
        if meta['type'] == 'User' and owner.lower() != state['user']['login'].lower():
            raise Fail(1, 'HTTP 403: cannot create a Project for another user')
        if owner in state.get('project_create_denied', []):
            raise Fail(1, 'HTTP 403: Resource not accessible: Project creation denied for this organization')
        number = max([p['number'] for p in state.get('projects', []) if p['owner'] == owner] + [0]) + 1
        kind = 'orgs' if meta['type'] == 'Organization' else 'users'
        created = {'id': f'PVT_{owner}_{number}', 'number': number, 'title': opts['--title'],
                   'url': f'https://github.com/{kind}/{owner}/projects/{number}', 'closed': False,
                   'owner': owner, 'linked_repos': [], 'items': []}
        state.setdefault('projects', []).append(created)
        return json.dumps(public_project(created)) + '\n'
    if sub == 'link':
        project_row = find_project(state, opts['--owner'], positional[0])
        owner, _, name = opts['--repo'].partition('/')
        repo = find_repo(state, owner, name)
        if repo['full_name'] not in project_row['linked_repos']:
            project_row['linked_repos'].append(repo['full_name'])
        return ''
    if sub == 'view':
        return json.dumps(public_project(find_project(state, opts['--owner'], positional[0]))) + '\n'
    if sub == 'field-list':
        find_project(state, opts['--owner'], positional[0])
        options = [{'id': option_id(n), 'name': n} for n in state.get('status_options', DEFAULT_STATUS)]
        fields = [{'id': 'PVTSSF_status', 'name': 'Status', 'type': 'ProjectV2SingleSelectField', 'options': options}]
        return json.dumps({'fields': fields}) + '\n'
    if sub == 'item-list':
        project_row = find_project(state, opts['--owner'], positional[0])
        rows = []
        for item in project_row['items']:
            issue = issue_by_url(state, item['url'])
            rows.append({'id': item['id'], 'status': item.get('status', ''),
                         'content': {'type': 'Issue', 'url': item['url'], 'title': issue['title'],
                                     'body': issue.get('body', ''), 'number': issue['number'],
                                     'repository': ISSUE_URL.match(item['url']).group(1) + '/' + ISSUE_URL.match(item['url']).group(2)}})
        return json.dumps({'items': rows, 'totalCount': len(rows)}) + '\n'
    if sub == 'item-add':
        project_row = find_project(state, opts['--owner'], positional[0])
        url = opts['--url']
        issue_by_url(state, url)
        if any(item['url'] == url for item in project_row['items']):
            return ''  # idempotent: the same Issue is already a Project item
        project_row['items'].append({'id': f"PVTI_{project_row['number']}_{len(project_row['items']) + 1}",
                                     'url': url, 'status': ''})
        return ''
    if sub == 'item-edit':
        if opts.get('--field-id') != 'PVTSSF_status':
            raise Fail(1, 'unknown field id')
        for project_row in state.get('projects', []):
            if project_row['id'] != opts.get('--project-id'):
                continue
            for item in project_row['items']:
                if item['id'] == opts.get('--id'):
                    names = {option_id(n): n for n in state.get('status_options', DEFAULT_STATUS)}
                    if opts.get('--single-select-option-id') not in names:
                        raise Fail(1, 'unknown status option')
                    item['status'] = names[opts['--single-select-option-id']]
                    return ''
        raise Fail(1, 'could not find project item')
    raise Unsupported('project ' + sub)


def release_api(path: str, state: dict) -> str:
    """Read-only release and tag endpoints. Only published releases are visible unless a test adds drafts."""
    releases = state.get('releases', {})
    if path == 'latest':
        published = [r for r in releases.values() if not r.get('draft') and not r.get('prerelease')]
        if not published:
            raise Fail(1, 'HTTP 404: Not Found (https://api.github.com/repos/releases/latest)')
        return json.dumps(max(published, key=lambda r: r['published_at'])) + '\n'
    if path.startswith('tags/'):
        tag = path[len('tags/'):]
        if tag not in releases:
            raise Fail(1, 'HTTP 404: Not Found (https://api.github.com/repos/releases/tags)')
        return json.dumps(releases[tag]) + '\n'
    raise Unsupported('release api ' + path)


def git_ref_api(path: str, state: dict) -> str:
    """git/ref/tags/<tag>: an annotated or lightweight tag object pointing at a commit."""
    tag = path[len('tags/'):]
    sha = state.get('tags', {}).get(tag)
    if sha is None:
        raise Fail(1, 'HTTP 404: Not Found (https://api.github.com/repos/git/ref)')
    return json.dumps({'ref': f'refs/tags/{tag}', 'object': {'type': 'commit', 'sha': sha}}) + '\n'


def release_cmd(args: list[str], state: dict) -> str:
    """`gh release download <tag> --repo R --dir D --pattern NAME`: copy the named asset file."""
    if not args or args[0] != 'download':
        raise Unsupported('release ' + ' '.join(args))
    positional, opts, _, _ = parse(args[1:])
    if len(positional) != 1 or '--dir' not in opts or '--pattern' not in opts:
        raise Unsupported('release download ' + ' '.join(args))
    tag, name = positional[0], opts['--pattern']
    release = state.get('releases', {}).get(tag)
    if release is None:
        raise Fail(1, f'release not found: {tag}')
    asset = next((a for a in release.get('assets', []) if a['name'] == name), None)
    if asset is None:
        raise Fail(1, f'no assets match the file pattern {name}')
    source = Path(state['asset_dir']) / tag / name
    target = Path(opts['--dir']) / name
    target.write_bytes(source.read_bytes())
    return ''


def handle(argv: list[str], state: dict) -> str:
    if not argv:
        raise Unsupported('(no arguments)')
    if argv[0] == 'auth' and len(argv) > 1:
        return auth(argv, state)
    if argv[0] == 'api':
        if len(argv) == 2 and argv[1].startswith('repos/') and '/releases/' in argv[1]:
            return release_api(argv[1].split('/releases/', 1)[1], state)
        if len(argv) == 2 and argv[1].startswith('repos/') and '/git/ref/' in argv[1]:
            return git_ref_api(argv[1].split('/git/ref/', 1)[1], state)
        if len(argv) == 4 and argv[1:3] == ['--method', 'GET'] and '/releases/' in argv[3]:
            return release_api(argv[3].split('/releases/', 1)[1], state)
        return api(argv[1:], state)
    if argv[0] == 'project' and len(argv) > 1:
        return project(argv[1:], state)
    if argv[0] == 'release' and len(argv) > 1:
        return release_cmd(argv[1:], state)
    raise Unsupported(' '.join(argv))


def matches(tokens: list[str], argv: list[str]) -> bool:
    """True when every token prefixes an argv element, in order (not necessarily adjacent)."""
    if not tokens:
        return False
    position = 0
    for token in tokens:
        while position < len(argv) and not argv[position].startswith(token):
            position += 1
        if position == len(argv):
            return False
        position += 1
    return True


def run_invocation(argv: list[str], state: dict) -> tuple[int, str, str]:
    for fault in state.get('faults', []):
        if not matches(fault.get('match') or [], argv):
            continue
        # `skip` lets a test target the Nth matching call (e.g. the final item-list).
        fault['seen'] = int(fault.get('seen', 0)) + 1
        if fault['seen'] <= int(fault.get('skip', 0)):
            continue
        if fault.get('once', True) and fault.get('fired'):
            continue
        fault['fired'] = int(fault.get('fired', 0)) + 1
        rc = int(fault['rc'])
        if fault.get('mode') == 'before':
            return rc, '', f"fake gh: injected failure before {' '.join(argv)}\n"
        if fault.get('mode') != 'after':
            return 4, '', 'fake gh: invalid fault mode\n'
        try:
            handle(argv, state)  # the server accepted the mutation ...
        except Fail:
            pass
        return rc, '', 'fake gh: injected lost response after the server accepted the request\n'
    try:
        return 0, handle(argv, state), ''
    except Fail as exc:
        return exc.rc, '', exc.message + '\n'
    except Exception as exc:  # fail closed: an internal error is never success
        return 4, '', f'fake gh: internal error: {exc}\n'


def save(path: Path, state: dict):
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix='.fake-gh-')
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(state, stream, sort_keys=True, indent=1)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def main(argv: list[str]) -> int:
    leaked = [name for name in TOKEN_ENV if os.environ.get(name)]
    if leaked:
        # Sidecar record only: lets tests prove the refusal happened without
        # reading or mutating the state file (no state call was made).
        raw_state = os.environ.get('FAKE_GH_STATE')
        if raw_state:
            with open(raw_state + '.refused', 'a', encoding='utf-8') as refused:
                refused.write(json.dumps({'argv': argv, 'env': leaked, 'rc': 5}) + '\n')
        sys.stderr.write('fake gh: refusing to run with ' + ', '.join(leaked) + ' in the environment\n')
        return 5
    raw = os.environ.get('FAKE_GH_STATE')
    if not raw:
        sys.stderr.write('fake gh: FAKE_GH_STATE is not set\n')
        return 4
    path = Path(raw)
    try:
        with path.with_name(path.name + '.lock').open('a', encoding='utf-8') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = json.loads(path.read_text(encoding='utf-8'))
            rc, out, err = run_invocation(argv, state)
            state.setdefault('calls', []).append({'argv': argv, 'rc': rc})
            save(path, state)
    except (OSError, ValueError) as exc:
        sys.stderr.write(f'fake gh: state unavailable: {exc}\n')
        return 4
    sys.stdout.write(out)
    sys.stderr.write(err)
    return rc


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
