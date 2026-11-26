#!/usr/bin/env python3
"""FAKE OpenCode V2 CLI for the offline E2E suite (used only by the real safe wrapper).

`World(real_wrapper=True)` sets AUTONOMY_OPENCODE to a shim that execs this file with
sys.executable. The real `opencode-project-safe.py` (started via tests/e2e_real_wrapper.py) then
talks to it exactly as it would talk to OpenCode V2:

- `api GET /api/info` -> {"data": {"version": "2.0.0"}}
- `api GET /api/session/active` -> {"data": {}}: sessions run synchronously here, so none are active
- `api GET /api/session` -> list of sessions
- `api GET /api/session/<id>` -> session, or exit 1 with "404" on stderr when unknown
- `api POST /api/session/<id>/interrupt` -> {"data": {"ok": true}}
- `run --title T [--auto] [--agent A] [--model M] PROMPT` -> creates a root session titled T (the
  wrapper's unique `[guard:...]` title) and performs the project's next scripted step through the
  same `perform_step` engine as the scripted-wrapper mode. A step with `unstarted_quota` prints the
  quota text and exits 1 WITHOUT creating a session.
- `run --session ID [--agent A] [--model M] PROMPT` -> recovery turn: logs a `resume` record and
  prints the completion nonce found in the prompt (exactly one must be present).

Every invocation is logged in `<state>/e2e/invocations.jsonl` (sessions with kind `session`,
recovery turns with kind `resume`). Session state lives in `<state>/e2e/opencode.json` under an
flock. Any unmodelled form: stderr, exit 97. It never reaches a real service.
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


def api(root: Path, args: list[str]) -> int:
    if len(args) != 3:
        raise Unmodelled(f'api form {args}')
    method, path = args[1], args[2]
    if (method, path) == ('GET', '/api/info'):
        return emit({'version': '2.0.0'})
    if (method, path) == ('GET', '/api/session/active'):
        return emit({})
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
    session = {'id': sid, 'title': title, 'parentID': None, 'agent': opts['agent'],
               'location': {'directory': str(Path.cwd().resolve())}}
    _locked(folder, lambda doc: doc['sessions'].__setitem__(sid, session))
    agent.log_invocation(folder, {
        'kind': 'session', 'via': 'real_wrapper', 'session_id': sid, 'project': project,
        'phase': phase, 'agent': opts['agent'], 'title': title, 'prompt': prompt,
        'argv': sys.argv[1:], 'learning_instructions': learning_present(prompt),
        'env': {k: os.environ.get(k) for k in agent.ENV_KEYS}, 'pid': os.getpid(), 'at': time.time(),
    })
    return agent.perform_step(folder, marker.parent, project, step, title, None)


def run_resume(folder: Path, opts: dict, prompt: str) -> int:
    sid = opts['session']
    if sid not in _locked(folder)['sessions']:
        print(f'session {sid} not found: 404', file=sys.stderr)
        return 1
    phase = os.environ.get('AUTONOMY_PHASE', 'code')
    agent.log_invocation(folder, {
        'kind': 'resume', 'via': 'real_wrapper', 'session_id': sid, 'agent': opts['agent'],
        'phase': phase, 'learning_instructions': learning_present(prompt), 'at': time.time(),
    })
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
