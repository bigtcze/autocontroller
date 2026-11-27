#!/usr/bin/env python3
"""FAKE OpenCode V2 CLI for the offline E2E suite (used only by the real safe wrapper).

`World(real_wrapper=True)` sets AUTONOMY_OPENCODE to a shim that execs this file with
sys.executable. The real `opencode-project-safe.py` (started via tests/e2e_real_wrapper.py) then
talks to it exactly as it would talk to OpenCode V2:

- `api GET /api/info` -> {"data": {"version": "2.0.0"}}
- `api GET /api/session/active` -> {"data": {<id>: {...}}}: the sessions whose active lifetime is
  not over. Each call reports the current set and then advances every lifetime by one poll under
  the same lock (`active_polls`: N > 0 = active for N more polls, -1 = active until interrupted,
  0 = inactive). Every call is logged as an `active_poll` row with the ids it returned.
- `api GET /api/session` -> list of sessions
- `api GET /api/session/<id>` -> session, or exit 1 with "404" on stderr when unknown
- `api POST /api/session/<id>/interrupt` -> {"data": {"ok": true}} and logs an `interrupt` row; the
  session becomes inactive unless flagged `ignore_interrupts`. Unknown id -> exit 1 with "404".
- any `api` call may fail (exit 1, stderr "API unavailable") while the store's `faults.api` says so:
  `{'path': P or null, 'skip': K, 'fail': N}` (N = -1: until cleared).
- `run --title T [--auto] [--agent A] [--model M] PROMPT` -> creates a root session titled T (the
  wrapper's unique `[guard:...]` title) and performs the project's next scripted step through the
  same `perform_step` engine as the scripted-wrapper mode. A step with `unstarted_quota` prints the
  quota text and exits 1 WITHOUT creating a session. Optional step keys: `root_active_polls`
  (root's active lifetime after the CLI returns), `descendants` (nested `{title, active_polls,
  children}` under the root, same directory), `foreign` (extra ROOT sessions in the project
  directory, or another `directory`), `block_seconds` (the process sleeps, capped at 30 s, so a
  controller cycle timeout can kill it mid-cycle; applied after the step's GitHub actions).
- `run --session ID [--agent A] [--model M] PROMPT` -> recovery turn: logs a `resume` record,
  applies the next scripted resume plan for (project, phase) (`descendants` spawned under the same
  root, `actions` as GitHub mutations), and prints the completion nonce found in the prompt (exactly
  one must be present). With no plan the behaviour is unchanged.

Every invocation is logged in `<state>/e2e/invocations.jsonl` (sessions with kind `session`,
recovery turns with kind `resume`, active polls with kind `active_poll`, interrupts with kind
`interrupt`). Session state lives in `<state>/e2e/opencode.json` under a flock. Any unmodelled
form: stderr, exit 97. It never reaches a real service.

Simulated only: the active-session lifetimes are poll counts, not OpenCode's real timing; interrupts
take effect instantly; descendant spawning is scripted. Live OpenCode behaviour needs the sandbox.
"""
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import sys
import time
from urllib.parse import unquote

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import e2e_agent as agent  # noqa: E402  (shared step engine and invocation log)

STORE = 'opencode.json'
LOCK = 'opencode.lock'
NONCE = re.compile(r'PROJECT_RECOVERY_COMPLETE_[0-9a-f]{24}')
VALUE_FLAGS = ('--title', '--agent', '--model', '--session')
BOOL_FLAGS = ('--auto',)
UNMODELLED_RC = 97
BLOCK_CAP_SECONDS = 30  # a scripted `block_seconds` is capped so a test can never hang


class Unmodelled(Exception):
    pass


def _locked(root: Path, change=None):
    """Read the session store under one exclusive lock; apply `change(doc)` and persist if given."""
    with (root / LOCK).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = root / STORE
        doc = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'sessions': {}}
        result = change(doc) if change else doc
        if change:
            tmp = root / (STORE + '.tmp')
            tmp.write_text(json.dumps(doc, sort_keys=True), encoding='utf-8')
            tmp.replace(path)
    return result


def emit(data) -> int:
    print(json.dumps({'data': data}))
    return 0


def _consume_api_fault(root: Path, path: str) -> bool:
    """`faults.api` = {'path': P or null, 'skip': K, 'fail': N}, persisted in the store.

    Calls whose path equals P (every call when P is null) first pass K times, then fail N times
    (N = -1: fail until the fault is cleared by removing the key). A failing call exits 1 with
    stderr, like an unreachable OpenCode server.
    """
    def change(doc):
        fault = doc.get('faults', {}).get('api')
        if not fault or fault.get('path') not in (None, path):
            return False
        if int(fault.get('skip', 0)) > 0:
            fault['skip'] = int(fault['skip']) - 1
            return False
        fail = int(fault.get('fail', 0))
        if fail == 0:
            return False
        if fail > 0:
            fault['fail'] = fail - 1
        return True
    return _locked(root, change)


def _active_poll(root: Path) -> dict:
    """One GET /api/session/active: report the active set, then advance each lifetime by one poll.

    `active_polls` = N > 0: reports active for N more polls. -1: active until interrupted.
    0 or absent: inactive. Report and decrement happen under one lock, so the state evolves
    deterministically with the number of polls, never with wall-clock time.
    """
    def change(doc):
        active = {}
        for sid, session in doc['sessions'].items():
            lifetime = int(session.get('active_polls', 0))
            if lifetime != 0:
                active[sid] = {'status': 'busy'}
            if lifetime > 0:
                session['active_polls'] = lifetime - 1
        return active
    active = _locked(root, change)
    agent.log_invocation(root, {'kind': 'active_poll', 'ids': sorted(active), 'at': time.time()})
    return active


def api(root: Path, args: list[str]) -> int:
    if len(args) != 3:
        raise Unmodelled(f'api form {args}')
    method, path = args[1], args[2]
    if _consume_api_fault(root, path):
        print(f'fake OpenCode V2: API unavailable: {method} {path}', file=sys.stderr)
        return 1
    if (method, path) == ('GET', '/api/info'):
        return emit({'version': '2.0.0'})
    if (method, path) == ('GET', '/api/session/active'):
        return emit(_active_poll(root))
    if (method, path) == ('GET', '/api/session'):
        return emit(list(_locked(root)['sessions'].values()))
    match = re.fullmatch(r'/api/session/([^/]+)', path)
    if method == 'GET' and match:
        sid = unquote(match.group(1))
        session = _locked(root)['sessions'].get(sid)
        if session is None:
            print(f'session {sid} not found: 404', file=sys.stderr)
            return 1
        return emit(session)
    match = re.fullmatch(r'/api/session/([^/]+)/interrupt', path)
    if method == 'POST' and match:
        sid = unquote(match.group(1))

        def change(doc):
            session = doc['sessions'].get(sid)
            if session is None:
                return False
            if not session.get('ignore_interrupts'):
                session['active_polls'] = 0  # interrupted: inactive from the next poll on
            return True
        if not _locked(root, change):
            print(f'session {sid} not found: 404', file=sys.stderr)
            return 1
        agent.log_invocation(root, {'kind': 'interrupt', 'session_id': sid, 'at': time.time()})
        return emit({'ok': True})
    raise Unmodelled(f'api {method} {path}')


def parse_run(args: list[str]) -> tuple[dict, str]:
    opts: dict = {'title': None, 'agent': None, 'model': None, 'session': None}
    positional: list[str] = []
    index = 0
    while index < len(args):
        value = args[index]
        if value in BOOL_FLAGS:
            index += 1
            continue
        if value in VALUE_FLAGS:
            if index + 1 >= len(args):
                raise Unmodelled(f'missing value for {value}')
            opts[value[2:]] = args[index + 1]
            index += 2
            continue
        if value.startswith('-'):
            raise Unmodelled(f'flag {value}')
        positional.append(value)
        index += 1
    if len(positional) != 1:
        raise Unmodelled('run needs exactly one prompt')
    return opts, positional[0]


def learning_present(prompt: str) -> bool:
    text = os.environ.get('AUTOCONTROLLER_LEARNING_INSTRUCTIONS', '')
    return bool(text) and text in prompt


def _new_session(folder: Path, sid: str, title: str, parent: str | None, directory: str,
                 active_polls: int) -> None:
    session = {'id': sid, 'title': title, 'parentID': parent, 'agent': None,
               'location': {'directory': directory}, 'active_polls': active_polls}
    _locked(folder, lambda doc: doc['sessions'].__setitem__(sid, session))


def _spawn(folder: Path, directory: str, parent: str, specs: list[dict]) -> None:
    """Descendant specs: {'title', 'active_polls', 'children': [...]}; any depth, same directory."""
    for spec in specs:
        sid = 'ses_' + secrets.token_hex(12)
        _new_session(folder, sid, spec.get('title', 'subagent'), parent, directory,
                     int(spec.get('active_polls', 0)))
        _spawn(folder, directory, sid, spec.get('children', []))


def _spawn_foreign(folder: Path, directory: str, specs: list[dict]) -> None:
    """Foreign ROOT sessions: {'title', 'active_polls', 'directory' (default: the project dir)}."""
    for spec in specs:
        sid = 'ses_' + secrets.token_hex(12)
        _new_session(folder, sid, spec.get('title', 'foreign'), None, spec.get('directory', directory),
                     int(spec.get('active_polls', 0)))


def run_new(folder: Path, opts: dict, prompt: str) -> int:
    title = opts['title']
    if not title:
        raise Unmodelled('run without --title')
    project = os.environ['AUTONOMY_PROJECT_NAME']
    phase = os.environ.get('AUTONOMY_PHASE', 'code')
    marker = Path(os.environ['AUTONOMY_PENDING_MARKER'])
    step = agent.pop_step(folder, project)
    if step is None:
        print(f'UNSCRIPTED AI SESSION for project {project} phase {phase}', file=sys.stderr)
        return UNMODELLED_RC
    if step.get('unstarted_quota'):
        # Quota is hit before OpenCode creates a session: no session record is written.
        agent.log_invocation(folder, {'kind': 'attempt', 'via': 'real_wrapper', 'project': project,
                                      'phase': phase, 'title': title})
        print(step.get('output', 'HTTP 429: quota reached'))
        return int(step.get('rc', 1))
    sid = 'ses_' + secrets.token_hex(12)
    directory = str(Path.cwd().resolve())
    session = {'id': sid, 'title': title, 'parentID': None, 'agent': opts['agent'],
               'location': {'directory': directory}, 'active_polls': int(step.get('root_active_polls', 0))}
    _locked(folder, lambda doc: doc['sessions'].__setitem__(sid, session))
    _spawn(folder, directory, sid, step.get('descendants', []))
    _spawn_foreign(folder, directory, step.get('foreign', []))
    agent.log_invocation(folder, {
        'kind': 'session', 'via': 'real_wrapper', 'session_id': sid, 'project': project,
        'phase': phase, 'agent': opts['agent'], 'title': title, 'prompt': prompt,
        'argv': sys.argv[1:], 'learning_instructions': learning_present(prompt),
        'env': {k: os.environ.get(k) for k in agent.ENV_KEYS}, 'pid': os.getpid(), 'at': time.time(),
    })
    block = min(float(step.get('block_seconds', 0)), BLOCK_CAP_SECONDS)
    if block > 0:
        # Simulates a CLI still running after its session exists: the controller cycle timeout
        # SIGTERMs the wrapper's process group during this sleep.
        time.sleep(block)
    return agent.perform_step(folder, marker.parent, project, step, title, None)


def _pop_resume_plan(folder: Path, project: str, phase: str) -> dict:
    """Next scripted recovery-turn plan for a project AND phase (store key `resume`).

    Keyed by phase because every wrapper `run` (selection included) ends with its own recovery
    turn; a plan for the coding session must never be consumed by the selection turn.
    Empty = today's behaviour.
    """
    def change(doc):
        plans = doc.setdefault('resume', {}).get(project, {}).get(phase, [])
        return plans.pop(0) if plans else {}
    return _locked(folder, change)


def run_resume(folder: Path, opts: dict, prompt: str) -> int:
    sid = opts['session']
    session = _locked(folder)['sessions'].get(sid)
    if session is None:
        print(f'session {sid} not found: 404', file=sys.stderr)
        return 1
    project = os.environ.get('AUTONOMY_PROJECT_NAME', '')
    phase = os.environ.get('AUTONOMY_PHASE', 'code')
    agent.log_invocation(folder, {
        'kind': 'resume', 'via': 'real_wrapper', 'session_id': sid, 'agent': opts['agent'],
        'phase': phase, 'learning_instructions': learning_present(prompt), 'at': time.time(),
    })
    plan = _pop_resume_plan(folder, project, phase)
    # Descendants spawned during this recovery turn belong to the SAME root session.
    _spawn(folder, session['location']['directory'], sid, plan.get('descendants', []))
    # The recovery turn finishes the work: its scripted GitHub actions (e.g. the PR) apply here.
    agent.perform_step(folder, folder, project, {'actions': plan.get('actions', [])}, session['title'], None)
    nonces = NONCE.findall(prompt)
    if len(nonces) != 1:
        raise Unmodelled('recovery prompt must carry exactly one completion nonce')
    print(nonces[0])
    return 0


def main(argv: list[str]) -> int:
    try:
        folder = agent.e2e_dir(os.environ['AUTONOMY_PENDING_MARKER'])
        if argv[:1] == ['api']:
            return api(folder, argv)
        if argv[:1] == ['run']:
            opts, prompt = parse_run(argv[1:])
            if opts['session']:
                return run_resume(folder, opts, prompt)
            return run_new(folder, opts, prompt)
        raise Unmodelled(f'top-level form {argv[:3]}')
    except Unmodelled as exc:
        print(f'fake OpenCode V2: unmodelled form: {exc}', file=sys.stderr)
        return UNMODELLED_RC


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
