"""Disposable onboarding world: install root copy, fake gh/OpenCode/systemctl, bare remotes.

Everything lives under one TemporaryDirectory per World. Nothing is read from or
written to the real HOME, this checkout's config/lock/state, or any network.
Subprocesses get a scrubbed environment built from scratch (no inherited tokens).
"""
from __future__ import annotations

import fcntl
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, NamedTuple

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
sys.path.insert(0, str(ROOT))
import onboarding as onboarding_module  # noqa: E402  (pure helpers only; no side effects)

OWNER = 'example-owner'
ORG = 'example-org'
USER_ID = 1001
BOT = {'login': 'renovate[bot]', 'type': 'Bot', 'id': 29139614}
ROOT_FILES = ('onboarding.py', 'autonomy-controller.py', 'bootstrap_policy.py',
              'release_policy.py', 'release_artifacts.py', 'lifecycle.py', 'memory.py',
              'config.example.json')
TIMEOUT = 120


class Result(NamedTuple):
    rc: int
    stdout: str
    stderr: str


def repo_checkout_name(repo: str) -> str:
    return onboarding_module.repo_key(repo)


class World:
    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='onboarding-world-')
        self.root = Path(self._tmp.name)
        self.install = self.root / 'install'
        self.home = self.root / 'home'
        self.remotes = self.root / 'remotes'
        self.bin = self.root / 'bin'
        self.gh_state = self.root / 'gh-state.json'
        self.sys_state = self.root / 'systemctl-state.json'
        self.oc_log = self.root / 'opencode.log'
        self.gitconfig = self.root / 'gitconfig'
        for path in (self.install, self.home, self.remotes, self.bin):
            path.mkdir(mode=0o700, parents=True)
        self._copy_install()
        self._write_shims()
        self._write_gitconfig()
        self._write_gh_state()
        self.sys_state.write_text(json.dumps({'calls': [], 'faults': []}))

    def cleanup(self):
        self._tmp.cleanup()

    # ----- disposable install root and environment -------------------------
    def _copy_install(self):
        for name in ROOT_FILES:
            shutil.copy2(ROOT / name, self.install / name)
        shutil.copytree(ROOT / 'standards', self.install / 'standards')
        # Same program set install.sh installs; tests/test_lifecycle_cli.py pins that equality.
        import release_artifacts as ra
        for name in ra.EXECUTABLE_PROGRAM_FILES + ra.DATA_PROGRAM_FILES:
            if not (self.install / name).exists():
                shutil.copy2(ROOT / name, self.install / name)
        for name in ra.DATA_PROGRAM_DIRS:
            if not (self.install / name).exists():
                shutil.copytree(ROOT / name, self.install / name)

    def _write_shims(self):
        python = shlex.quote(sys.executable)
        for name, target in (('gh', 'fake_gh.py'), ('opencode', 'fake_opencode.py'),
                             ('systemctl', 'fake_systemctl.py')):
            shim = self.bin / name
            shim.write_text(f'#!/bin/sh\nexec {python} {shlex.quote(str(TESTS / target))} "$@"\n')
            shim.chmod(0o755)

    def _write_gitconfig(self):
        self.gitconfig.write_text(
            f'[url "file://{self.remotes}/"]\n\tinsteadOf = https://github.com/\n'
            '[protocol "file"]\n\tallow = always\n')

    def env(self, **extra) -> dict:
        """Scrubbed environment: built from scratch, shims first on PATH."""
        base = {
            'PATH': f'{self.bin}:{os.environ.get("PATH", "/usr/bin:/bin")}',
            'HOME': str(self.home),
            'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
            'GIT_CONFIG_GLOBAL': str(self.gitconfig), 'GIT_CONFIG_NOSYSTEM': '1',
            'GIT_TERMINAL_PROMPT': '0', 'PYTHONDONTWRITEBYTECODE': '1',
            'FAKE_GH_STATE': str(self.gh_state), 'FAKE_SYSTEMCTL_STATE': str(self.sys_state),
            'FAKE_OPENCODE_LOG': str(self.oc_log),
        }
        base.update(extra)
        return base

    # ----- onboarding invocation ------------------------------------------
    def onboard(self, url: str = f'https://github.com/{OWNER}/demo', *, stdin: str = 'y\n',
                timeout: int = TIMEOUT, extra_env: dict | None = None, action: str = 'add') -> Result:
        proc = subprocess.run([sys.executable, str(self.install / 'onboarding.py'), action, url],
                              input=stdin, capture_output=True, text=True, timeout=timeout,
                              cwd=str(self.install), env=self.env(**(extra_env or {})), check=False)
        return Result(proc.returncode, proc.stdout, proc.stderr)

    # ----- fake GitHub state -------------------------------------------------
    def _write_gh_state(self):
        state = {'logged_in': True, 'scopes': ['project', 'repo', 'workflow'],
                 'user': {'login': OWNER, 'id': USER_ID, 'type': 'User'},
                 'owners': {OWNER: {'type': 'User'}, ORG: {'type': 'Organization'}},
                 'repos': {}, 'issues': {}, 'projects': [], 'faults': [], 'calls': []}
        self.gh_state.write_text(json.dumps(state))

    def gh(self) -> dict:
        return json.loads(self.gh_state.read_text(encoding='utf-8'))

    def mutate_gh(self, change: Callable[[dict], None]):
        with self.gh_state.with_name(self.gh_state.name + '.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = self.gh()
            change(state)
            self.gh_state.write_text(json.dumps(state))

    def gh_calls(self) -> list[dict]:
        return self.gh().get('calls', [])

    def refused_calls(self) -> list[dict]:
        path = self.gh_state.with_name(self.gh_state.name + '.refused')
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line]

    def mutating_gh_calls(self) -> list[list[str]]:
        """Calls that change GitHub state or identity (excluding read-only api/project list)."""
        out = []
        for call in self.gh_calls():
            argv = call['argv']
            if call['rc'] != 0:
                continue
            if argv[:2] == ['project', 'create'] or argv[:2] == ['project', 'link'] \
                    or argv[:2] == ['project', 'item-add'] or argv[:2] == ['project', 'item-edit']:
                out.append(argv)
            elif argv[:1] == ['api'] and ('--method' in argv) and argv[argv.index('--method') + 1] in ('POST', 'PATCH', 'PUT', 'DELETE'):
                out.append(argv)
            elif argv[:2] in (['auth', 'login'], ['auth', 'refresh']):
                out.append(argv)
        return out

    def set_gh_faults(self, faults: list[dict]):
        self.mutate_gh(lambda s: s.__setitem__('faults', faults))

    def set_login(self, logged_in: bool, scopes=('project', 'repo', 'workflow'), **flags):
        def change(state):
            state['logged_in'] = logged_in
            state['scopes'] = sorted(scopes)
            state.update(flags)
        self.mutate_gh(change)

    def add_owner(self, login: str, owner_type: str):
        self.mutate_gh(lambda s: s['owners'].setdefault(login, {'type': owner_type}))

    def add_repo(self, owner: str, name: str, *, default_branch: str = 'main', seed: bool = True,
                 seed_files: dict | None = None, has_issues: bool = True, push: bool = True,
                 admin: bool = False, merge=('squash',), viewer_association: str = 'OWNER') -> str:
        full = f'{owner}/{name}'
        bare = self.remote_path(owner, name)
        bare.parent.mkdir(parents=True, exist_ok=True)
        self._git(['init', '--bare', '-b', default_branch, str(bare)], cwd=self.root)
        if seed:
            self._seed(bare, default_branch, seed_files or {'README.md': f'# {name}\n'})

        def change(state):
            kind = 'Organization' if owner == ORG else 'User'
            state['owners'].setdefault(owner, {'type': kind})
            state['repos'][full] = {
                'full_name': full, 'owner': {'login': owner}, 'default_branch': default_branch,
                'has_issues': has_issues, 'archived': False, 'disabled': False,
                'permissions': {'push': push, 'admin': admin},
                'allow_squash_merge': 'squash' in merge, 'allow_merge_commit': 'merge' in merge,
                'allow_rebase_merge': 'rebase' in merge, 'viewer_association': viewer_association}
            state['issues'].setdefault(full.lower(), [])
        self.mutate_gh(change)
        return full

    def remote_path(self, owner: str, name: str) -> Path:
        return self.remotes / owner / f'{name}.git'

    def add_issue(self, repo: str, title: str, *, association: str = 'OWNER', body: str = '',
                  state: str = 'open', pull_request: bool = False, user: dict | None = None) -> int:
        holder = {}

        def change(state_doc):
            rows = state_doc['issues'].setdefault(repo.lower(), [])
            number = max([i['number'] for i in rows] + [0]) + 1
            issue = {'number': number, 'title': title, 'body': body, 'state': state,
                     'html_url': f'https://github.com/{repo}/{"pull" if pull_request else "issues"}/{number}',
                     'author_association': association,
                     'user': user or {'login': 'example-author', 'type': 'User', 'id': 2002}}
            if pull_request:
                issue['pull_request'] = {'url': f'https://api.github.com/repos/{repo}/pulls/{number}'}
            rows.append(issue)
            holder['n'] = number
        self.mutate_gh(change)
        return holder['n']

    def add_project(self, owner: str, title: str, *, number: int | None = None, closed: bool = False,
                    linked_repos=(), items=(), status_options=None) -> int:
        holder = {}

        def change(state):
            existing = [p['number'] for p in state['projects'] if p['owner'] == owner]
            num = number or (max(existing + [0]) + 1)
            kind = 'orgs' if state['owners'].get(owner, {}).get('type') == 'Organization' else 'users'
            state['projects'].append({
                'id': f'PVT_{owner}_{num}', 'number': num, 'title': title, 'owner': owner,
                'url': f'https://github.com/{kind}/{owner}/projects/{num}', 'closed': closed,
                'linked_repos': list(linked_repos),
                'items': [{'id': f'PVTI_{num}_{i}', 'url': url, 'status': status}
                          for i, (url, status) in enumerate(items, 1)]})
            if status_options is not None:
                state['status_options'] = list(status_options)
            holder['n'] = num
        self.mutate_gh(change)
        return holder['n']

    def projects(self) -> list[dict]:
        return self.gh()['projects']

    def bootstrap_issues(self, repo: str) -> list[dict]:
        return [i for i in self.gh()['issues'].get(repo.lower(), [])
                if '<!-- autocontroller-bootstrap:v1 -->' in (i.get('body') or '')]

    # ----- config, systemctl, opencode --------------------------------------
    def config_path(self) -> Path:
        return self.install / 'config.json'

    def write_config(self, data: dict):
        self.config_path().write_text(json.dumps(data, indent=2) + '\n')
        self.config_path().chmod(0o600)

    def read_config(self) -> dict:
        return json.loads(self.config_path().read_text())

    def systemctl_records(self) -> list[dict]:
        if not self.sys_state.exists():
            return []
        return json.loads(self.sys_state.read_text())['calls']

    def systemctl_calls(self) -> list[list[str]]:
        return [c['argv'] for c in self.systemctl_records()]

    def set_systemctl_faults(self, faults: list[dict]):
        data = json.loads(self.sys_state.read_text())
        data['faults'] = faults
        self.sys_state.write_text(json.dumps(data))

    def opencode_calls(self) -> list[list[str]]:
        if not self.oc_log.exists():
            return []
        return [json.loads(line) for line in self.oc_log.read_text().splitlines() if line]

    # ----- git -------------------------------------------------------------
    def _git(self, args: list[str], cwd: Path, extra_env: dict | None = None) -> str:
        proc = subprocess.run(['git', *args], cwd=str(cwd), env=self.env(**(extra_env or {})),
                              capture_output=True, text=True, timeout=TIMEOUT, check=False)
        if proc.returncode:
            raise RuntimeError(f'git {args} failed rc={proc.returncode}: {proc.stderr.strip()}')
        return proc.stdout

    def _seed(self, bare: Path, branch: str, files: dict):
        work = Path(tempfile.mkdtemp(prefix='seed-', dir=str(self.root)))
        identity = {'GIT_AUTHOR_NAME': 'seed', 'GIT_AUTHOR_EMAIL': 'seed@example.invalid',
                    'GIT_COMMITTER_NAME': 'seed', 'GIT_COMMITTER_EMAIL': 'seed@example.invalid'}
        self._git(['init', '-q', '-b', branch, str(work)], cwd=self.root)
        for rel, text in files.items():
            (work / rel).parent.mkdir(parents=True, exist_ok=True)
            (work / rel).write_text(text)
        self._git(['add', '-A'], cwd=work)
        self._git(['commit', '-q', '-m', 'Seed'], cwd=work, extra_env=identity)
        self._git(['push', '-q', str(bare), f'HEAD:refs/heads/{branch}'], cwd=work)

    def remote_commits(self, owner: str, name: str, branch: str) -> int | None:
        bare = self.remote_path(owner, name)
        proc = subprocess.run(['git', '--git-dir', str(bare), 'rev-list', '--count', f'refs/heads/{branch}'],
                              env=self.env(), capture_output=True, text=True, timeout=TIMEOUT, check=False)
        return int(proc.stdout.strip()) if proc.returncode == 0 else None

    def remote_branch_count(self, owner: str, name: str) -> int:
        bare = self.remote_path(owner, name)
        out = subprocess.run(['git', '--git-dir', str(bare), 'for-each-ref', '--format=%(refname)', 'refs/heads'],
                             env=self.env(), capture_output=True, text=True, timeout=TIMEOUT, check=True).stdout
        return len([line for line in out.splitlines() if line])

    def checkout_dir(self, repo: str) -> Path:
        return self.home / 'repositories' / repo_checkout_name(repo)

    def checkout_git(self, repo: str, *args: str) -> str:
        return self._git(list(args), cwd=self.checkout_dir(repo))

    def gitconfig_bytes(self) -> bytes:
        return self.gitconfig.read_bytes()
