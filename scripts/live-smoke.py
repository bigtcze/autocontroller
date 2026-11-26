#!/usr/bin/env python3
"""Opt-in live smoke for ONE disposable sandbox repository. NOT RUN BY CI.

This script spends real AI tokens and creates real GitHub objects. It refuses
to do anything unless every guard below holds, and every guard is checked
BEFORE the first external call (`gh`, `git`, `opencode`).

Guards (all required):
  * AUTOCONTROLLER_LIVE_SMOKE=1 in the environment
  * --i-understand-this-spends-tokens on the command line
  * --owner is allowlisted: listed in --allow-owner and/or in the file
    `autocontroller-smoke-allowlist` inside GH_CONFIG_DIR (one login per line)
  * GH_CONFIG_DIR is set, exists, and is not the default ~/.config/gh
  * the production controller root (~/scripts/autocontroller) does not exist
    on this host (existence check only; its contents are never read)
  * GH_TOKEN and GITHUB_TOKEN are unset (no ambient credentials)
  * GITHUB_ACTIONS and CI are unset; GITHUB_EVENT_NAME is never pull_request(_target)
  * --repo fullmatches autocontroller-smoke-[a-z0-9-]+ and contains --run-id
  * --model is an explicit provider/model id and never starts with '-'
  * --max-ai-sessions exactly 1; --timeout-seconds 30..900; --max-prompt-bytes <= 4096
  * after the static guards: the authenticated `gh api user` login equals --owner

Isolation, stated precisely:
  * gh, git and opencode all run with GH_CONFIG_DIR set to the dedicated directory,
    GIT_CONFIG_GLOBAL pointing at a temporary file whose only credential source is
    `gh auth git-credential`, GIT_CONFIG_NOSYSTEM=1, GIT_TERMINAL_PROMPT=0, and with
    SSH_AUTH_SOCK, GIT_ASKPASS, SSH_ASKPASS and GIT_SSH* unset.
  * opencode keeps the operator's real HOME, because it needs the operator's
    provider authentication. The agent therefore runs as the operator's OS account
    and can read files that account can read. Run this script only on a disposable
    VM or account that holds nothing else. HOME is NOT isolated.

It never reads the installed controller (~/scripts/autocontroller), its systemd
unit, its state, the Engram socket or database.

Exit codes: 0 success; 2 refused by a guard (some refusals happen after read-only
gh calls: the existing-repo check and the login check); 3 a smoke step failed,
teardown left a repository behind, or an unexpected exception occurred.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREFIX = 'autocontroller-smoke-'
ENV_OPT_IN = 'AUTOCONTROLLER_LIVE_SMOKE'
ACK_FLAG = '--i-understand-this-spends-tokens'
ALLOWLIST_FILE = 'autocontroller-smoke-allowlist'
FORBIDDEN_CI = ('GITHUB_ACTIONS', 'CI')
FORBIDDEN_EVENTS = ('pull_request', 'pull_request_target')
SCRUBBED_ENV = ('GH_TOKEN', 'GITHUB_TOKEN', 'SSH_AUTH_SOCK', 'GIT_ASKPASS', 'SSH_ASKPASS',
                'GIT_SSH', 'GIT_SSH_COMMAND', 'GIT_CONFIG_PARAMETERS', 'GIT_CONFIG_COUNT')
MAX_SESSIONS = 1
MAX_PROMPT_BYTES = 4096
GH_TIMEOUT = 60
GIT_TIMEOUT = 120
KILL_GRACE_SECONDS = 30
OWNER_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}')
REPO_RE = re.compile(r'autocontroller-smoke-[a-z0-9-]+')
MODEL_RE = re.compile(r'[A-Za-z0-9._-]+/[A-Za-z0-9._:/-]+')
RUN_ID_RE = re.compile(r'[a-z0-9]{6,20}')

_CONTROLLER = None


class Refused(Exception):
    """A guard failed. Raised before any external call."""


class SmokeFailed(Exception):
    """A smoke step failed after the guards passed."""


def refuse(reason: str):
    raise Refused(reason)


def default_production_root() -> Path:
    return Path.home() / 'scripts' / 'autocontroller'


def load_controller():
    """Import autonomy-controller.py read-only from the repo root (cached).

    The repo root is put on sys.path first: when this file runs as a script,
    sys.path[0] is scripts/, and the controller's own imports (release_policy, ...)
    would otherwise fail. Called before any external call.
    """
    global _CONTROLLER
    if _CONTROLLER is None:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        spec = importlib.util.spec_from_file_location('live_ctl', str(ROOT / 'autonomy-controller.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _CONTROLLER = module
    return _CONTROLLER


def read_allowlist(gh_dir: Path) -> set[str]:
    """Sandbox owner logins from GH_CONFIG_DIR/autocontroller-smoke-allowlist. Missing file = none."""
    path = gh_dir / ALLOWLIST_FILE
    if not os.path.lexists(path):  # absent: no extra owners. Present but unreadable: refuse below.
        return set()
    logins = set()
    try:
        text = path.read_text(encoding='utf-8')
    except UnicodeDecodeError:
        refuse(f'{ALLOWLIST_FILE} is not valid UTF-8')
    except OSError:
        refuse(f'{ALLOWLIST_FILE} is not readable')
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if not OWNER_RE.fullmatch(line):
            refuse(f'invalid login in {ALLOWLIST_FILE}')
        logins.add(line.lower())
    return logins


def check_guards(args: argparse.Namespace, env: dict, *, default_gh_dir: Path,
                 production_root: Path | None = None) -> None:
    """Static guard evaluation: no subprocess, no network. Raises Refused."""
    production_root = default_production_root() if production_root is None else production_root
    if env.get(ENV_OPT_IN) != '1':
        refuse(f'set {ENV_OPT_IN}=1 to opt in (this run may spend tokens)')
    if not args.i_understand_this_spends_tokens:
        refuse(f'pass {ACK_FLAG} to confirm the token spend')
    if not OWNER_RE.fullmatch(args.owner):
        refuse('invalid --owner')
    gh_raw = env.get('GH_CONFIG_DIR')
    if not gh_raw:
        refuse('GH_CONFIG_DIR must point at a dedicated gh config directory')
    gh_dir = Path(gh_raw).expanduser()
    if gh_dir.resolve() == default_gh_dir.resolve():
        refuse('GH_CONFIG_DIR must not be the default gh configuration')
    if not gh_dir.is_dir():
        refuse('GH_CONFIG_DIR does not exist')
    allowed = {owner.lower() for owner in args.allow_owner} | read_allowlist(gh_dir)
    if args.owner.lower() not in allowed:
        refuse(f'--owner must be allowlisted: pass --allow-owner or list it in {ALLOWLIST_FILE} '
               'inside GH_CONFIG_DIR')
    if os.path.lexists(production_root):
        refuse('the production controller root exists on this host; run only on a disposable VM/account')
    for name in ('GH_TOKEN', 'GITHUB_TOKEN'):
        if env.get(name):
            refuse(f'{name} must be unset (use the dedicated gh login only)')
    for name in FORBIDDEN_CI:
        if env.get(name):
            refuse(f'refusing to run under CI ({name} is set)')
    if env.get('GITHUB_EVENT_NAME') in FORBIDDEN_EVENTS:
        refuse('refusing to run from a pull_request event')
    if not REPO_RE.fullmatch(args.repo):
        refuse(f'--repo must fullmatch {REPO_RE.pattern}')
    if not args.run_id or not RUN_ID_RE.fullmatch(args.run_id):
        refuse('--run-id must be 6-20 lowercase letters/digits')
    if args.run_id not in args.repo:
        refuse('--repo must contain --run-id (disposable per run)')
    if not args.model or args.model.startswith('-') or not MODEL_RE.fullmatch(args.model):
        refuse('--model must be an explicit provider/model id')
    if args.max_ai_sessions != MAX_SESSIONS:
        refuse(f'--max-ai-sessions must be exactly {MAX_SESSIONS}')
    if not (30 <= args.timeout_seconds <= 900):
        refuse('--timeout-seconds must be between 30 and 900')
    if not (1 <= args.max_prompt_bytes <= MAX_PROMPT_BYTES):
        refuse(f'--max-prompt-bytes must be between 1 and {MAX_PROMPT_BYTES}')


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--owner', required=True, help='sandbox owner (allowlisted)')
    p.add_argument('--allow-owner', action='append', default=[],
                   help=f'allowlisted sandbox owner (repeatable; also read from {ALLOWLIST_FILE})')
    p.add_argument('--repo', required=True, help=f'disposable repo name matching {PREFIX}[a-z0-9-]+')
    p.add_argument('--run-id', required=True, help='unique lowercase id for this run')
    p.add_argument('--model', required=True, help='explicit provider/model id')
    p.add_argument('--max-ai-sessions', type=int, required=True)
    p.add_argument('--timeout-seconds', type=int, default=300)
    p.add_argument('--max-prompt-bytes', type=int, default=MAX_PROMPT_BYTES)
    p.add_argument(ACK_FLAG, dest='i_understand_this_spends_tokens', action='store_true')
    p.add_argument('--opencode', default=None,
                   help='absolute path to the plain opencode binary (required with --execute)')
    p.add_argument('--report', default=None,
                   help='JSON evidence report path, outside this checkout (required with --execute)')
    p.add_argument('--execute', action='store_true',
                   help='actually run the smoke after all guards pass (default: guards only)')
    return p.parse_args(argv)


def _under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def check_execute_inputs(args: argparse.Namespace, repo_root: Path, *,
                         production_root: Path | None = None, gh_dir: Path | None = None) -> None:
    """Guards that apply only to --execute. Raises Refused before any external call.

    m3: `--allow-owner` is self-asserted, so --execute also requires the owner to be listed in the
    allowlist file inside GH_CONFIG_DIR. The real controls are the dedicated GH_CONFIG_DIR, the
    login check (verify_login) and that file.
    """
    production_root = default_production_root() if production_root is None else production_root
    if not args.opencode:
        refuse('--opencode PATH is required with --execute')
    given = Path(args.opencode).expanduser()
    if not given.is_absolute():
        refuse('--opencode must be an absolute path to an executable file')
    binary = given.resolve()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        refuse('--opencode must be an absolute path to an executable file')
    if _under(binary, repo_root.resolve()) or _under(binary, production_root.resolve()):
        refuse('--opencode must not be inside this checkout or the installed controller')
    if binary.name == 'opencode-project-safe.py' or 'autocontroller' in binary.name \
            or 'wrapper' in binary.name:
        refuse('--opencode must be the plain opencode binary, never the controller wrapper')
    if not args.report:
        refuse('--report PATH is required with --execute')
    report = Path(args.report).expanduser().resolve()
    root = repo_root.resolve()
    if report == root or root in report.parents:
        refuse('--report must be outside this checkout')
    if report.exists():
        refuse('--report must not already exist')
    if not report.parent.is_dir():
        refuse('--report parent directory must exist')
    if gh_dir is not None and args.owner.lower() not in read_allowlist(gh_dir):
        refuse(f'--execute requires --owner to be listed in {ALLOWLIST_FILE} inside GH_CONFIG_DIR')


def sandbox_env(base: dict, gh_dir: Path, workdir: Path) -> dict:
    """Environment for every gh, git and opencode call (see the module docstring)."""
    gitconfig = workdir / 'sandbox.gitconfig'
    gitconfig.write_text(
        '[credential "https://github.com"]\n'
        '\thelper = !gh auth git-credential\n'
        '[user]\n'
        '\tname = autocontroller-smoke\n'
        '\temail = smoke@example.invalid\n',
        encoding='utf-8')
    env = {k: v for k, v in base.items() if k not in SCRUBBED_ENV}
    env.update({'GH_CONFIG_DIR': str(gh_dir), 'GIT_CONFIG_GLOBAL': str(gitconfig),
                'GIT_CONFIG_NOSYSTEM': '1', 'GIT_TERMINAL_PROMPT': '0'})
    return env


class Report:
    """JSON evidence written to --report. Records every external command in order."""

    def __init__(self, run_id: str, repo: str):
        self.doc = {'run_id': run_id, 'repo': repo, 'commands': [], 'created': [], 'deleted': [],
                    'checks': [], 'counts': {}, 'status': 'not-started'}

    @staticmethod
    def redact(argv: list[str]) -> list[str]:
        """Argv is recorded for evidence; obvious secret-bearing values are masked."""
        out, mask_next = [], False
        for token in argv:
            if mask_next:
                out.append('<redacted>')
                mask_next = False
                continue
            if token.lower() in ('--token', '--password', '--body-file-token'):
                mask_next = True
            elif re.search(r'(?i)(token|secret|password)=', token):
                out.append(re.sub(r'=.*', '=<redacted>', token))
                continue
            out.append(token)
        return out

    def command(self, argv: list[str], rc: int | None, seconds: float, note: str = '') -> None:
        self.doc['commands'].append({'argv': self.redact(argv), 'rc': rc,
                                     'seconds': round(seconds, 3), 'note': note})

    def check(self, name: str, passed: bool, detail: str = '') -> None:
        self.doc['checks'].append({'name': name, 'passed': bool(passed), 'detail': detail})

    def write(self, path: Path) -> None:
        self.doc['counts'] = {'commands': len(self.doc['commands']), 'created': len(self.doc['created']),
                              'deleted': len(self.doc['deleted']),
                              'checks_passed': sum(c['passed'] for c in self.doc['checks']),
                              'checks_failed': sum(not c['passed'] for c in self.doc['checks'])}
        path.write_text(json.dumps(self.doc, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def run_timed(argv: list[str], report: Report, env: dict, *, timeout: int, cwd: Path | None = None,
              note: str = '', check: bool = True) -> subprocess.CompletedProcess:
    """One bounded external call, always recorded (argv redacted, rc, duration).

    With check=False a non-zero exit is returned to the caller instead of raising.
    """
    started = time.monotonic()
    try:
        proc = subprocess.run(argv, env=env, cwd=cwd, text=True, capture_output=True,
                              timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        report.command(argv, None, time.monotonic() - started, note + ' (timeout)')
        raise SmokeFailed(f'{argv[0]} timed out after {timeout}s') from exc
    except OSError as exc:
        report.command(argv, None, time.monotonic() - started, note + f' (exec error: {exc.__class__.__name__})')
        raise SmokeFailed(f'could not execute {argv[0]}') from exc
    report.command(argv, proc.returncode, time.monotonic() - started, note)
    if check and proc.returncode:
        raise SmokeFailed(f'{" ".join(argv[:2])} failed rc={proc.returncode}: {proc.stderr.strip()[:200]}')
    return proc


INTERRUPT_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


def _killpg(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


def terminate_group(proc: subprocess.Popen) -> None:
    """SIGTERM the session's process group, wait up to KILL_GRACE_SECONDS, then SIGKILL the group.

    The group is killed even if the leader exited, because children may survive it.

    B-final: the signals in INTERRUPT_SIGNALS are blocked for the whole call, so a second
    Ctrl-C or SIGTERM cannot interrupt the grace wait and skip the SIGKILL. The SIGKILL is in a
    `finally`, so it is sent even if a wait raises (for example KeyboardInterrupt from a
    handler that could not be blocked). The previous signal mask is restored afterwards.
    """
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, INTERRUPT_SIGNALS)
    try:
        _killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass
    finally:
        try:
            _killpg(proc.pid, signal.SIGKILL)  # always: the group may outlive its leader
            try:
                proc.wait(timeout=KILL_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                pass
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


def run_session(argv: list[str], report: Report, env: dict, *, timeout: int, cwd: Path,
                note: str) -> None:
    """The single OpenCode session, in its own process group (start_new_session=True).

    Because the session has its own session and group, a Ctrl-C or a terminal hangup does not
    reach it on its own. So every exit path from `communicate` ends with the group terminated:

    - normal exit: the leader is reaped; any child it left in the group is SIGKILLed;
    - timeout: the group gets SIGTERM, a grace period, then SIGKILL, and the run fails;
    - any other BaseException (KeyboardInterrupt from SIGINT/SIGTERM/SIGHUP handlers installed
      by main, or anything else): the group is terminated and the exception is re-raised.

    The wall-clock bound is `timeout`. One top-level session may make several model calls through
    its subagents within that bound; tokens are not metered here.
    """
    display = [Path(argv[0]).name, *argv[1:]]
    started = time.monotonic()
    try:
        proc = subprocess.Popen(argv, env=env, cwd=cwd, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True)
    except OSError as exc:
        report.command(display, None, time.monotonic() - started, note + ' (exec error)')
        raise SmokeFailed('could not execute opencode') from exc
    try:
        _out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        terminate_group(proc)
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                pipe.close()
        report.command(display, None, time.monotonic() - started, note + ' (timeout; group terminated)')
        raise SmokeFailed(f'opencode session exceeded {timeout}s and its process group was terminated')
    except BaseException:
        # Interrupted (or any other unexpected error) while the session may still be running.
        # m4: the record and the pipe close run in `finally`, so they happen even if
        # terminate_group raises (for example KeyboardInterrupt from a wait).
        try:
            terminate_group(proc)
        finally:
            report.command(display, None, time.monotonic() - started,
                           note + ' (interrupted; group terminated)')
            for pipe in (proc.stdout, proc.stderr):  # communicate() normally closes these; it did not run
                if pipe is not None:
                    pipe.close()
        raise
    # Normal exit: the leader has been reaped by communicate(). Remove children it left in the group.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    report.command(display, proc.returncode, time.monotonic() - started, note)
    if proc.returncode:
        raise SmokeFailed(f'opencode failed rc={proc.returncode}: {(err or "").strip()[:200]}')


def gh(args: list[str], env: dict) -> str:
    """One bounded gh call used before the report exists. Only after check_guards passed."""
    proc = subprocess.run(['gh', *args], env=env, text=True, capture_output=True,
                          timeout=GH_TIMEOUT, check=False)
    if proc.returncode:
        raise SmokeFailed(f'gh {args[0]} failed rc={proc.returncode}: {proc.stderr.strip()[:200]}')
    return proc.stdout


def verify_login(args: argparse.Namespace, env: dict) -> None:
    login = json.loads(gh(['api', 'user'], env)).get('login', '')
    if str(login).lower() != args.owner.lower():
        raise Refused(f'authenticated gh login {login!r} does not match --owner')


def build_prompt(args: argparse.Namespace, issue: int) -> str:
    """The single bounded AI prompt. Size-capped before any session starts."""
    prompt = (
        f'In this repository, make one trivial documentation-only change: add a line to README.md '
        f'saying "Smoke run {args.run_id}". Commit it on a new branch named smoke/{args.run_id}, '
        f'push the branch, and open a pull request into main whose body contains the exact line '
        f'"Closes #{issue}". Do not touch any other file. Do not create tags or releases. '
        f'Do not clone repositories or create git worktrees.'
    )
    if len(prompt.encode()) > args.max_prompt_bytes:
        raise Refused(f'prompt is {len(prompt.encode())} bytes, above --max-prompt-bytes {args.max_prompt_bytes}')
    return prompt


def verify_repo_owned(report: Report, env: dict, repo: str, args: argparse.Namespace) -> None:
    """Re-check right before deleting: exact name, allowlisted owner. Otherwise refuse to delete."""
    proc = run_timed(['gh', 'api', f'repos/{repo}'], report, env, timeout=GH_TIMEOUT,
                     note='re-verify before delete')
    data = json.loads(proc.stdout or '{}')
    owner = str((data.get('owner') or {}).get('login') or '')
    if (str(data.get('full_name') or '').lower() != repo.lower() or data.get('name') != args.repo
            or owner.lower() != args.owner.lower()):
        raise SmokeFailed('repository re-verification failed; refusing to delete it')


def _repo_absent(report: Report, env: dict, repo: str) -> bool:
    """True only when `gh api` answers HTTP 404 for the repository (it was never created)."""
    proc = run_timed(['gh', 'api', f'repos/{repo}'], report, env, timeout=GH_TIMEOUT,
                     note='maybe-created check: 404 means not created', check=False)
    return proc.returncode != 0 and bool(re.search(r'HTTP 404|Not Found', proc.stderr or ''))


def _settle_repo(report: Report, env: dict, repo: str, args: argparse.Namespace, *,
                 repo_created: bool) -> str:
    """Teardown decision for the sandbox repository. Returns 'deleted', 'not-created' or raises.

    When `gh repo create` did not return 0, a 404 from the existence check is a verification
    result: nothing was created, so there is nothing to delete and no teardown error. Any other
    answer goes on to the ownership re-verify, and only a verified repository is deleted.
    Raises SmokeFailed when existence or ownership cannot be established; the caller records a leftover.
    """
    if not repo_created and _repo_absent(report, env, repo):
        return 'not-created'  # a verification result: nothing was created, nothing to delete
    verify_repo_owned(report, env, repo, args)
    run_timed(['gh', 'repo', 'delete', repo, '--yes'], report, env,
              timeout=GH_TIMEOUT, note='teardown repo (created by this run)')
    return 'deleted'


def _teardown_step(report: Report, label: str, fn) -> bool:
    """Run one teardown step. Errors are recorded, never stop the remaining steps."""
    try:
        fn()
        return True
    except Exception as exc:  # teardown must attempt every step
        report.doc.setdefault('teardown_errors', []).append(f'{label}: {str(exc)[:200]}')
        return False


def run_smoke(args: argparse.Namespace, env: dict, *, report_path: Path, workdir: Path) -> int:
    """Bounded live smoke: one sandbox repo, at most one OpenCode session, verified, always torn down.

    Order: record run id -> existence probe (refuse if the repo exists) -> create private repo ->
    create one Issue -> `gh repo clone` into a private temp dir -> read-only controller helpers ->
    one OpenCode session -> verify linked same-repo PR -> teardown in `finally`, which deletes only
    a repository this run created, after re-verifying it.
    """
    ctl = load_controller()
    repo = f'{args.owner}/{args.repo}'
    report = Report(args.run_id, args.repo)
    report.doc['opencode'] = Path(args.opencode).name  # basename only, never the full path
    build_prompt(args, 9_999_999_999)  # size check before anything external
    report_path.write_text(json.dumps({'run_id': args.run_id, 'status': 'started'}) + '\n', encoding='utf-8')

    repo_created = False
    maybe_created = False  # set before `gh repo create`; teardown checks it when create raised
    issue_number = None
    pr_number = None
    leftovers: list[str] = []
    outcome = 'failed'
    run_dir = Path(tempfile.mkdtemp(prefix='autocontroller-smoke-run-', dir=workdir))
    run_dir.chmod(0o700)
    saved_environ = dict(os.environ)
    senv: dict = {}  # replaced by the sandbox environment below; teardown only uses it after repo_created
    try:
        senv = sandbox_env(env, Path(env['GH_CONFIG_DIR']).expanduser(), run_dir)
        # The controller helpers call gh/git through os.environ, so the sandbox environment is
        # installed for the run and the caller's environment is restored in `finally`.
        os.environ.clear()
        os.environ.update(senv)

        probe = run_timed(['gh', 'repo', 'view', repo, '--json', 'name'], report, senv, timeout=GH_TIMEOUT,
                          note='existence probe: must not exist', check=False)
        if probe.returncode == 0:
            refuse(f'repository {repo} already exists; refusing to reuse or delete it')
        if not re.search(r'(?i)could not resolve to a repository|HTTP 404|not found', probe.stderr or ''):
            raise SmokeFailed(f'existence probe failed unexpectedly rc={probe.returncode}')

        # A create that raises (timeout, or failure after the server-side create) may still have
        # made the repository. Teardown verifies it and deletes it, or records it as a leftover.
        maybe_created = True
        run_timed(['gh', 'repo', 'create', repo, '--private', '--add-readme'], report, senv,
                  timeout=GH_TIMEOUT, note='sandbox repository')
        repo_created = True  # only after create returned 0; teardown deletes only then
        report.doc['created'].append(repo)
        report.check('repo-created-private', True, repo)

        issue_url = run_timed(['gh', 'issue', 'create', '--repo', repo, '--title', f'smoke {args.run_id}',
                               '--body', 'Disposable live smoke issue.'], report, senv,
                              timeout=GH_TIMEOUT, note='one Issue').stdout.strip()
        issue_number = int(issue_url.rsplit('/', 1)[-1])
        report.doc['created'].append(f'{repo}#{issue_number}')

        clone = run_dir / 'checkout'
        run_timed(['gh', 'repo', 'clone', repo, str(clone), '--', '--quiet'], report, senv,
                  timeout=GIT_TIMEOUT, note='gh clone (sandbox gh config), private temp dir')
        report.check('cloned', (clone / '.git').is_dir(), 'checkout')

        report.check('issue-open-before', ctl.issue_state(repo, issue_number) == 'open')
        report.check('no-linked-pr-before', ctl.linked_issue_pr(repo, issue_number) is None)
        head_default = run_timed(['git', '-C', str(clone), 'rev-parse', 'HEAD'], report, senv,
                                 timeout=GIT_TIMEOUT, note='default-branch HEAD').stdout.strip()
        ci = ctl.main_ci_state(repo, head_default, 'required.yml', missing_is_failed=False)
        report.check('main-ci-recorded', True, ci)  # recorded, not asserted green

        prompt = build_prompt(args, issue_number)
        # Exactly one session: the cap is enforced by the structure, not by a counter.
        run_session([args.opencode, 'run', '--model', args.model, '--agent', 'orchestrator',
                     '--title', f'autocontroller-smoke-{args.run_id}', prompt],
                    report, senv, timeout=args.timeout_seconds, cwd=clone, note='the single AI session')
        report.doc['ai_sessions'] = 1

        linked = ctl.linked_issue_pr(repo, issue_number)
        if linked is None:
            report.check('same-repo-pr-linked', False, 'no PR closes the Issue')
            raise SmokeFailed('no same-repository PR closes the Issue after the session')
        pr_number = int(linked['number'])
        report.doc['created'].append(f'{repo}#{pr_number}')
        same_repo = (not linked.get('isCrossRepository')
                     and (linked.get('headRepository') or {}).get('nameWithOwner', '').lower() == repo.lower())
        report.check('same-repo-pr-linked', same_repo, f'PR #{pr_number}')
        outcome = 'passed' if all(c['passed'] for c in report.doc['checks']) else 'failed'
        if outcome == 'failed':
            raise SmokeFailed('one or more checks failed; see the report')
        return 0
    except Refused:
        outcome = 'refused'
        raise
    except KeyboardInterrupt:
        outcome = 'interrupted'  # teardown still runs in `finally`; the top-level status is this
        raise
    finally:
        # Teardown runs with SIGINT/SIGTERM/SIGHUP blocked, so a second interrupt cannot cut it or
        # the report write short. Every call inside has a timeout (run_timed: GH_TIMEOUT/GIT_TIMEOUT).
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, INTERRUPT_SIGNALS)
        try:
            _teardown(report, report_path, senv, repo, args, outcome=outcome, leftovers=leftovers,
                      pr_number=pr_number, issue_number=issue_number,
                      repo_created=repo_created, maybe_created=maybe_created, run_dir=run_dir,
                      saved_environ=saved_environ)
        finally:
            try:
                _record_deferred_signals(report, report_path)
            finally:
                # m1: the mask is restored even if recording the deferred signals raises.
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


def _record_deferred_signals(report: Report, report_path: Path) -> None:
    """Called with INTERRUPT_SIGNALS still blocked, after teardown. Records any that arrived.

    A blocked signal stays pending. Its handler runs once, after the mask is restored, and raises
    KeyboardInterrupt (exit 3). Here the report is marked `interrupted`, so a run whose teardown
    finished cleanly is not reported as passed. Precedence: teardown-failed (leftover) and
    teardown-incomplete (a close step failed) are kept as they are.
    """
    deferred = sorted(s.name for s in signal.sigpending() & set(INTERRUPT_SIGNALS))
    if not deferred:
        return
    report.doc['deferred_signals'] = deferred
    if report.doc.get('status') not in ('teardown-failed', 'teardown-incomplete'):
        report.doc['status'] = 'interrupted'
    try:
        report.write(report_path)
    except OSError:
        pass  # _teardown already attempted the report write and records failures as leftovers


def _teardown(report: Report, report_path: Path, senv: dict, repo: str, args: argparse.Namespace, *,
              outcome: str, leftovers: list[str], pr_number, issue_number, repo_created: bool,
              maybe_created: bool, run_dir: Path, saved_environ: dict) -> None:
    """Teardown body, called from run_smoke's `finally` with the interrupt signals blocked."""
    # Every step is attempted, independently of the others.
    if pr_number is not None:
        _teardown_step(report, 'close PR', lambda: run_timed(
            ['gh', 'pr', 'close', str(pr_number), '--repo', repo], report, senv,
            timeout=GH_TIMEOUT, note='teardown PR'))
    if issue_number is not None:
        _teardown_step(report, 'close Issue', lambda: run_timed(
            ['gh', 'issue', 'close', str(issue_number), '--repo', repo], report, senv,
            timeout=GH_TIMEOUT, note='teardown Issue'))
    if repo_created or maybe_created:
        try:
            repo_result = _settle_repo(report, senv, repo, args, repo_created=repo_created)
        except Exception as exc:  # m-b: any failure to establish existence or ownership is a leftover
            repo_result = 'leftover'  # never a delete
            report.doc.setdefault('teardown_errors', []).append(f'repo: {str(exc)[:200]}')
        if repo_result == 'deleted':
            report.doc['deleted'].append(repo)
        elif repo_result == 'not-created':
            report.doc.setdefault('not_created', []).append(repo)
        else:
            leftovers.append(repo)
    shutil.rmtree(run_dir, ignore_errors=True)
    os.environ.clear()
    os.environ.update(saved_environ)
    report.doc['leftovers'] = leftovers
    if leftovers:
        report.doc['status'] = 'teardown-failed'
    elif report.doc.get('teardown_errors'):
        report.doc['status'] = 'teardown-incomplete'
    else:
        report.doc['status'] = outcome
    try:
        report.write(report_path)
    except OSError:
        leftovers.append(f'report not written: {report_path}')
    if leftovers:
        raise SmokeFailed(f'teardown failed; leftover repository {repo}; see report')
    if report.doc.get('teardown_errors'):
        raise SmokeFailed('teardown incomplete (a close step failed); see report')


def _interrupt(signum, _frame):
    """SIGTERM/SIGHUP during --execute: unwind like Ctrl-C so teardown and the report still run."""
    raise KeyboardInterrupt(signal.Signals(signum).name)


# m3 single-thread assumption: the deferral below uses signal.pthread_sigmask, which only affects
# the calling thread, and signal handlers run only on the main thread. The mask therefore covers
# the whole controller only if the process has one thread. main() refuses to run --execute when
# that does not hold (checked before the report file is created).
def _require_single_thread() -> None:
    if threading.active_count() != 1:
        raise Refused('--execute requires a single-threaded process (the signal mask assumes it)')


def main(argv: list[str] | None = None, *, env: dict | None = None,
         production_root: Path | None = None) -> int:
    env = dict(os.environ if env is None else env)
    args = parse_args(sys.argv[1:] if argv is None else argv)
    report_path = None  # set only once the exclusive report file exists
    run_started = False  # run_smoke writes the report; before that the empty file is discarded
    previous = {}
    try:
        check_guards(args, env, default_gh_dir=Path.home() / '.config' / 'gh',
                     production_root=production_root)
        if not args.execute:
            print('live smoke guards passed; no external call made (pass --execute to run)')
            return 0
        check_execute_inputs(args, ROOT, production_root=production_root,
                             gh_dir=Path(env['GH_CONFIG_DIR']).expanduser())
        build_prompt(args, 9_999_999_999)  # prompt cap refused before any external call
        _require_single_thread()  # m3: before the report file exists and before any external call
        candidate = Path(args.report).expanduser().resolve()
        # Create the report exclusively so it cannot clobber an existing file. m-a: report_path is
        # assigned only after the exclusive create succeeds, so a pre-existing file is never deleted.
        with candidate.open('x', encoding='utf-8'):
            pass
        report_path = candidate
        load_controller()  # import before any external call (verify_login is the first one)
        verify_login(args, env)
        for sig in (signal.SIGTERM, signal.SIGHUP):
            # m5: if SIGHUP was inherited as SIG_IGN (for example under nohup), keep it ignored.
            # Installing _interrupt there would make a hangup start interrupting the run.
            current = signal.getsignal(sig)
            if sig == signal.SIGHUP and current == signal.SIG_IGN:
                continue
            previous[sig] = signal.signal(sig, _interrupt)
        run_started = True
        return run_smoke(args, env, report_path=report_path, workdir=Path(tempfile.gettempdir()))
    except Refused as exc:
        print(f'live smoke REFUSED: {exc}', file=sys.stderr)
        return 2
    except KeyboardInterrupt as exc:
        reason = str(exc) or 'KeyboardInterrupt'
        status = _report_status(report_path)
        if not run_started:
            detail = 'no run had started, so there was nothing to tear down'
        elif status in ('teardown-failed', 'teardown-incomplete'):
            detail = f'teardown did NOT complete cleanly ({status}); see report'
        else:
            detail = 'teardown ran; see report'
        print(f'live smoke FAILED: interrupted by {reason} ({detail})', file=sys.stderr)
        return 3
    except (SmokeFailed, subprocess.TimeoutExpired) as exc:
        print(f'live smoke FAILED: {exc}', file=sys.stderr)
        return 3
    except Exception as exc:  # any non-smoke exception maps to 3, with a concise message
        print(f'live smoke FAILED: unexpected {exc.__class__.__name__}: {str(exc)[:200]}', file=sys.stderr)
        return 3
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        if not run_started:
            _discard_empty_report(report_path)  # m4: any pre-run refusal or failure leaves no file


def _report_status(report_path: Path | None) -> str:
    """The top-level status the report holds, or '' if it cannot be read (used for the stderr note)."""
    try:
        return str(json.loads(report_path.read_text(encoding='utf-8')).get('status') or '')
    except (OSError, ValueError, AttributeError):
        return ''


def _discard_empty_report(report_path: Path | None) -> None:
    """m4: remove the exclusively-created report when a refusal happened before anything was written."""
    if report_path is not None and report_path.exists() and report_path.stat().st_size == 0:
        report_path.unlink()


if __name__ == '__main__':
    sys.exit(main())
