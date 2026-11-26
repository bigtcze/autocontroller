#!/usr/bin/env python3
"""STAND-IN for the OpenCode/OMO safe wrapper, used only by the offline E2E suite.

The real ProjectWorker.run_wrapper starts this file exactly as it would start
opencode-project-safe.py: `python3 <wrapper> run --auto [--agent X] --title T PROMPT`
in its own process group with a log file. Behaviour per session is scripted by
the test in `<state_home>/e2e/script.json` (keyed by project name). Each session:

- pops the next scripted step for its project (no step => exit 97, loud failure);
- appends one record to `<state_home>/e2e/invocations.jsonl` (argv, prompt,
  env subset, phase);
- applies the step's GitHub actions to the shared FakeGitHub as actor `agent`;
- optionally leaves a `starting` pending marker (unstarted quota case);
- prints the step's output and exits with the step's rc.

It never talks to a real model, network, gh or OpenCode server.
"""
import fcntl
import json
import os
from pathlib import Path
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))  # autonomy-controller imports release_policy/memory from the repo root

ENV_KEYS = ('AUTONOMY_PHASE', 'AUTONOMY_PROJECT_NAME', 'AUTONOMY_PROJECT_ROOT')
QUOTA_RC = 78


def e2e_dir(pending_marker: str) -> Path:
    # <state_home>/projects/<name>/cycle.pending  ->  <state_home>/e2e
    return Path(pending_marker).resolve().parents[2] / 'e2e'


def pop_step(folder: Path, project: str) -> dict | None:
    script = folder / 'script.json'
    with (folder / 'script.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        doc = json.loads(script.read_text(encoding='utf-8'))
        steps = doc.get(project, [])
        if not steps:
            return None
        step = steps.pop(0)
        doc[project] = steps
        tmp = script.with_name('script.json.tmp')
        tmp.write_text(json.dumps(doc, sort_keys=True), encoding='utf-8')
        tmp.replace(script)
    return step


def log_invocation(folder: Path, record: dict):
    with (folder / 'invocations.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with (folder / 'invocations.jsonl').open('a', encoding='utf-8') as out:
            out.write(json.dumps(record, sort_keys=True) + '\n')


def clear_unstarted_quota(marker: Path) -> int:
    if not marker.exists():
        return 0
    data = json.loads(marker.read_text(encoding='utf-8'))
    if data.get('status') != 'starting' or data.get('session_id'):
        return 2
    marker.unlink()
    return 0


def run_memory_ops(pdir: Path, ops: list[dict]) -> list[dict]:
    """Model the learning CLI for the scripted agent, with the same checks as memory.cli().

    The token comes from the instructions the controller injected into this session's
    environment, not from the test. The grant is checked against the file on disk.
    Engram is the stand-in server at $E2E_ENGRAM_SOCKET (test-only variable; the
    product code never reads it). Each op's outcome is returned, including refusals.
    """
    import fcntl as _fcntl
    import re as _re
    import secrets as _secrets
    import memory as m  # repo root is on sys.path

    instructions = os.environ.get('AUTOCONTROLLER_LEARNING_INSTRUCTIONS', '')
    match = _re.search(r'--token (\S+)', instructions)
    grant_path = pdir / 'memory-grant.json'
    state = pdir.parent.parent
    root = state / 'memory'
    if not match or not grant_path.exists():
        return [{'ok': False, 'error': 'no learning grant in this session'}]
    with (root / 'adapter.lock').open('a') as lock:
        _fcntl.flock(lock, _fcntl.LOCK_EX)
        grant = json.loads(grant_path.read_text(encoding='utf-8'))
        if not _secrets.compare_digest(grant['token'], match.group(1)):
            return [{'ok': False, 'error': 'Invalid session grant'}]
        if grant['kind'] == 'selection':
            return [{'ok': False, 'error': 'No learning during selection'}]
        engram_socket = os.environ.get('E2E_ENGRAM_SOCKET', '')
        if not engram_socket or engram_socket.startswith('/run/user/'):
            # Test-only variable: refuse rather than reach any production or unset socket.
            raise RuntimeError('E2E_ENGRAM_SOCKET must name the private stand-in socket')
        backend = m.Backend(Path(engram_socket))
        results = []
        first_returned = None
        for op in ops:
            arg = op['arg']
            if isinstance(arg, dict) and arg.get('id') == '$first_returned':
                arg = dict(arg, id=first_returned)  # resolved from this session's last search
            try:
                result = m.operation(backend, grant, op['action'], arg)
                if op['action'] == 'search' and result:
                    first_returned = result[0]['id']
                results.append({'ok': True, 'result': result})
            except (m.MemoryError, ValueError, KeyError, TypeError) as exc:
                results.append({'ok': False, 'error': str(exc)})
        m.atomic(grant_path, grant)
    return results


def run_session(argv: list[str]) -> int:
    marker = Path(os.environ['AUTONOMY_PENDING_MARKER'])
    folder = e2e_dir(str(marker))
    if argv[:1] == ['--clear-unstarted-quota']:
        rc = clear_unstarted_quota(marker)
        log_invocation(folder, {'kind': 'clear_unstarted', 'project': os.environ.get('AUTONOMY_PROJECT_NAME'), 'rc': rc})
        return rc
    if argv[:1] == ['--interrupt-owned']:
        log_invocation(folder, {'kind': 'interrupt', 'project': os.environ.get('AUTONOMY_PROJECT_NAME')})
        return 0
    if argv[:2] != ['run', '--auto']:
        print(f'unscripted wrapper form: {argv[:3]}', file=sys.stderr)
        return 4

    rest = argv[2:]
    agent = rest[rest.index('--agent') + 1] if '--agent' in rest else None
    title = rest[rest.index('--title') + 1] if '--title' in rest else ''
    prompt = rest[-1]
    project = os.environ['AUTONOMY_PROJECT_NAME']
    phase = os.environ.get('AUTONOMY_PHASE', 'code')
    log_invocation(folder, {
        'kind': 'session', 'project': project, 'phase': phase, 'agent': agent, 'title': title,
        'prompt': prompt, 'argv': argv,
        'env': {k: os.environ.get(k) for k in ENV_KEYS},
        'learning_instructions': bool(os.environ.get('AUTOCONTROLLER_LEARNING_INSTRUCTIONS')),
        'pid': os.getpid(), 'pgid': os.getpgid(0), 'at': time.time(),
    })
    step = pop_step(folder, project)
    if step is None:
        print(f'UNSCRIPTED AI SESSION for project {project} phase {phase}', file=sys.stderr)
        return 97

    if step.get('actions'):
        from e2e_github import FakeGitHub  # imported lazily: only needed when acting
        store = FakeGitHub(folder / 'github.json')
        for action in step['actions']:
            store.run_gh(action['gh'], actor='agent')
    if step.get('outage_after'):
        from e2e_github import FakeGitHub as _Store  # GitHub goes down right after this session
        _Store(folder / 'github.json').set_outage(True)
    if step.get('memory'):
        results = run_memory_ops(marker.parent, step['memory'])
        log_invocation(folder, {'kind': 'memory', 'project': project, 'results': results})
    if step.get('marker') == 'starting':
        marker.write_text(json.dumps({'status': 'starting', 'title': title}), encoding='utf-8')
    if step.get('output'):
        print(step['output'])
    return int(step.get('rc', 0))


if __name__ == '__main__':
    sys.exit(run_session(sys.argv[1:]))
