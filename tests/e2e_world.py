"""Disposable E2E world: temp state, real git checkouts and bare remotes, the
stand-in GitHub (e2e_github), the scripted agent (e2e_agent) and, optionally,
a real memory.Runtime launched over the stand-in Engram (e2e_engram_standin).

Everything lives under one TemporaryDirectory. Nothing touches the real HOME,
/run/user, ~/scripts/autocontroller or the network. Subprocess calls carry timeouts.
"""
from __future__ import annotations

import importlib.util
import json
import os
from contextlib import contextmanager
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
from unittest.mock import patch

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import e2e_github as eg  # noqa: E402  (loads autonomy-controller as eg.ctl)

ctl = eg.ctl
AGENT = TESTS / 'e2e_agent.py'
ENGRAM_STANDIN = TESTS / 'e2e_engram_standin.py'
GIT_TIMEOUT = 60
# Ambient controller/OpenCode/Engram state. ProjectWorker.env() copies os.environ, so any of
# these inherited from a shell would reach the fake agent. The E2E world removes them.
AMBIENT_PREFIXES = ('AUTOCONTROLLER_', 'AUTONOMY_', 'OPENCODE_', 'ENGRAM_')
AMBIENT_NAMES = ('OPENCODE',)


def _is_ambient(name: str) -> bool:
    return name in AMBIENT_NAMES or name.startswith(AMBIENT_PREFIXES)


@contextmanager
def ambient_env_removed():
    """Remove ambient controller/OpenCode/Engram variables from os.environ, restore on exit.

    Defense in depth: scripts/offline-tests.sh already unsets them for the whole run. This keeps
    the E2E tests hermetic when run directly, or from a shell that exports them.
    """
    saved = {k: v for k, v in os.environ.items() if _is_ambient(k)}
    for name in saved:
        del os.environ[name]
    try:
        yield
    finally:
        for name in [k for k in os.environ if _is_ambient(k)]:
            del os.environ[name]
        os.environ.update(saved)
GIT_ENV = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'GIT_CONFIG_GLOBAL': os.devnull,
           'GIT_CONFIG_NOSYSTEM': '1', 'GIT_TERMINAL_PROMPT': '0', 'LC_ALL': 'C.UTF-8'}
IDENT = ['-c', 'user.name=e2e', '-c', 'user.email=e2e@example.invalid', '-c', 'init.defaultBranch=main']
SHA_A = 'a' * 40


def git(args, cwd=None):
    proc = subprocess.run(['git', *IDENT, *args], cwd=cwd, env=GIT_ENV, capture_output=True,
                          text=True, timeout=GIT_TIMEOUT, check=False)
    if proc.returncode:
        raise RuntimeError(f'git {args} rc={proc.returncode}: {proc.stderr.strip()}')
    return proc.stdout


class _ForbiddenConnect(AssertionError):
    """Raised instead of connecting: the E2E suite must never reach a real service."""


def guard_external_access():
    """Hard stop for the two ways a test could reach a production service by accident.

    - AF_UNIX connect to any /run/user/ path (the production memory socket lives there).
    - Engram download (memory.Runtime.download), which is a network fetch.
    Both are patched for the whole test; the caller registers `stop` as a cleanup.
    """
    import socket as _socket
    import memory as learning
    real_connect = _socket.socket.connect

    def guarded_connect(self, address):
        if self.family == _socket.AF_UNIX and str(address).startswith('/run/user/'):
            raise _ForbiddenConnect(f'E2E: refused connect to production socket {address!r}')
        return real_connect(self, address)

    def refused_download(*_args, **_kwargs):
        raise _ForbiddenConnect('E2E: Engram download is never run in the offline suite')

    patchers = [patch.object(_socket.socket, 'connect', new=guarded_connect),
                patch.object(learning.Runtime, 'download', new=staticmethod(refused_download))]
    for patcher in patchers:
        patcher.start()

    class _Stop:
        @staticmethod
        def stop():
            for patcher in reversed(patchers):
                patcher.stop()
    return _Stop


def guard_memory_socket(tmp: Path):
    """Patch memory.socket_path for a whole test to a private temp path, and assert it is safe.

    Returns the started patcher. Its `stop` is registered by the caller as a cleanup.
    Raises AssertionError if the private path would resolve under /run/user.
    """
    import memory as learning
    private = tmp / 'sock' / 'engram.sock'
    private.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if str(private).startswith('/run/user/'):
        raise AssertionError('private memory socket resolved under /run/user')
    patcher = patch.object(learning, 'socket_path', return_value=private)
    patcher.start()
    if str(learning.socket_path()).startswith('/run/user/'):
        patcher.stop()
        raise AssertionError('memory.socket_path still resolves under /run/user')
    return patcher


class World:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.state = tmp / 'state'
        self.e2e = self.state / 'e2e'
        self.e2e.mkdir(parents=True, mode=0o700)
        self.gh = eg.FakeGitHub(self.e2e / 'github.json')
        (self.e2e / 'script.json').write_text('{}', encoding='utf-8')
        self.opencode = tmp / 'opencode-stub'
        self.opencode.write_text('#!/bin/sh\nexit 97\n', encoding='utf-8')
        self.opencode.chmod(0o700)
        self.quota_lock = threading.Lock()
        self.stop = threading.Event()
        self.projects: dict[str, dict] = {}
        self.runtime = None

    # ----- world construction ------------------------------------------------
    def add_project(self, name: str, repo: str, number: int, *, release: dict | None = None,
                    statuses=('Backlog', 'In Progress', 'Review', 'Done')) -> dict:
        owner = repo.split('/')[0]
        self.gh.add_repo(repo)
        self.gh.add_project(repo, owner, number, statuses=statuses)
        remote = self.tmp / 'remotes' / f'{name}.git'
        remote.parent.mkdir(parents=True, exist_ok=True)
        git(['init', '--bare', '-q', '-b', 'main', str(remote)])
        seed = self.tmp / 'seed' / name
        seed.mkdir(parents=True)
        git(['init', '-q', '-b', 'main', str(seed)])
        (seed / 'README.md').write_text(f'# {name}\n', encoding='utf-8')
        git(['add', '-A'], cwd=seed)
        git(['commit', '-q', '-m', 'Seed'], cwd=seed)
        git(['push', '-q', str(remote), 'HEAD:refs/heads/main'], cwd=seed)
        checkout = self.tmp / 'repositories' / name
        git(['clone', '-q', str(remote), str(checkout)])
        cfg = {'name': name, 'repo': repo, 'directory': str(checkout),
               'project_owner': owner, 'project_number': number, 'bootstrap_issue': 0}
        if release is not None:
            cfg['release'] = release
        self.projects[name] = cfg
        return cfg

    def checkout(self, name: str) -> Path:
        return Path(self.projects[name]['directory'])

    def script(self, project: str, steps: list[dict]):
        """Replace the scripted agent steps for one project (consumed in order)."""
        self._script_update(lambda doc: doc.__setitem__(project, list(steps)))

    def _script_update(self, change):
        path = self.e2e / 'script.json'
        with (self.e2e / 'script.lock').open('a') as lock:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX)
            doc = json.loads(path.read_text(encoding='utf-8'))
            change(doc)
            path.write_text(json.dumps(doc, sort_keys=True), encoding='utf-8')

    def sessions(self, phase: str | None = None) -> list[dict]:
        path = self.e2e / 'invocations.jsonl'
        if not path.exists():
            return []
        rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line]
        rows = [r for r in rows if r.get('kind') == 'session']
        return [r for r in rows if phase is None or r['phase'] == phase]

    def global_cfg(self) -> dict:
        cfg = {'state_dir': str(self.state), 'wrapper': str(AGENT), 'opencode': str(self.opencode),
               'poll_seconds': 5, 'post_cycle_seconds': 0, 'ci_poll_seconds': 0,
               'cycle_timeout_seconds': 60, 'selection_timeout_seconds': 60,
               'drain_timeout_seconds': 5, 'agent': 'orchestrator', 'retry_max_seconds': 14400}
        if self.runtime is not None:
            cfg['_memory_runtime'] = self.runtime
        return cfg

    def start_engram(self) -> 'ctl.learning.Runtime':
        """Launch the stand-in Engram through the REAL memory.Runtime.start_process.

        The socket lives in a private 0700 directory inside the temp tree. The
        product socket path is replaced in-process only (no env override in product code).
        """
        import memory as learning
        sock_dir = self.tmp / 'sock'
        sock_dir.mkdir(mode=0o700, exist_ok=True)  # reused on restart, same private 0700 directory
        folder = None
        runtime = None
        patcher = patch.object(learning, 'socket_path', return_value=sock_dir / 'engram.sock')
        patcher.start()
        try:
            runtime = learning.Runtime(self.state)
            folder = runtime.root / 'gen-0123456789abcdef'  # reused on restart: the store persists
            folder.mkdir(mode=0o700, exist_ok=True)
            shim = folder / 'engram'
            shim.write_text(f'#!/bin/sh\nexec {sys.executable} {ENGRAM_STANDIN} "$@"\n', encoding='utf-8')
            shim.chmod(0o700)
            runtime.start_process(folder)
        except BaseException:
            if runtime is not None:
                runtime.close()
            patcher.stop()
            raise
        self.runtime = runtime
        self.engram_patch = patcher
        os.environ['E2E_ENGRAM_SOCKET'] = str(sock_dir / 'engram.sock')
        return runtime

    def restart_engram_via_startup(self):
        """Restart the stand-in through the REAL Runtime.startup() on the same data directory.

        The first install leaves what stage() and a completed startup leave behind: the
        active pointer and the checked-boot marker for this boot. startup() then takes its
        normal path: validate the store, clear the failure marker, start Engram. The upgrade
        lookup (Runtime.latest) is patched to raise, so no network can be reached.
        """
        import memory as learning
        self.stop_engram()
        sock_dir = self.tmp / 'sock'
        # Patch the socket path BEFORE Runtime() is constructed: __init__ resolves it.
        patcher = patch.object(learning, 'socket_path', return_value=sock_dir / 'engram.sock')
        patcher.start()
        runtime = None
        try:
            runtime = learning.Runtime(self.state)
            if runtime.sock != sock_dir / 'engram.sock':  # never the production /run/user socket
                raise AssertionError('Runtime resolved a non-isolated Engram socket path')
            folder = runtime.root / 'gen-0123456789abcdef'
            learning.atomic(runtime.active, {'generation': folder.name, 'version': 'v1.0.0'})
            runtime.boot.write_text(Path('/proc/sys/kernel/random/boot_id').read_text().strip())
            with patch.object(learning.Runtime, 'latest', side_effect=learning.MemoryError('offline: no network')):
                runtime.startup()
        except BaseException:
            if runtime is not None:  # Runtime() itself may have raised before it existed
                runtime.close()
            patcher.stop()
            raise
        self.runtime = runtime
        self.engram_patch = patcher
        os.environ['E2E_ENGRAM_SOCKET'] = str(sock_dir / 'engram.sock')
        return runtime

    def stop_engram(self):
        if self.runtime is not None:
            self.runtime.close()
            self.runtime = None
        if getattr(self, 'engram_patch', None) is not None:
            self.engram_patch.stop()
            self.engram_patch = None
        os.environ.pop('E2E_ENGRAM_SOCKET', None)

    def worker(self, name: str) -> 'ctl.ProjectWorker':
        worker = ctl.ProjectWorker(dict(self.projects[name]), self.global_cfg(), self.stop, self.quota_lock)
        # ProjectWorker.env() reads os.environ during an iteration, so every iteration of a worker
        # made here runs with the ambient controller/OpenCode/Engram variables removed. This covers
        # a test that sets such a leak in its body after setUp, as well as an inherited one.
        raw = worker.one_iteration

        def one_iteration():
            with ambient_env_removed():
                return raw()

        worker.one_iteration = one_iteration
        return worker

    # ----- transport ---------------------------------------------------------
    @contextmanager
    def transport(self):
        """Route the controller's gh/subprocess seams to the fake GitHub."""
        command_fn, run_fn = eg.patch_targets(self.gh)
        with patch.object(ctl, 'command', new=command_fn), patch.object(ctl.subprocess, 'run', new=run_fn):
            yield
