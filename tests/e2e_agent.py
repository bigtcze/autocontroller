#!/usr/bin/env python3
"""Scripted OMO agent for the offline E2E suite (two entry modes, one step engine).

Mode 1, scripted wrapper (default World): the real `ProjectWorker.run_wrapper` starts this file
in place of opencode-project-safe.py: `python3 <this file> run --auto [--agent X] --title T PROMPT`.

Mode 2, real wrapper (World(real_wrapper=True)): the real `opencode-project-safe.py` runs and
calls the fake OpenCode V2 CLI (tests/e2e_opencode.py), which calls `perform_step` below.

In both modes behaviour per session is scripted by the test in `<state>/e2e/script.json`
(keyed by project name). `perform_step` does the same thing for each step:

- applies the step's GitHub actions to the shared FakeGitHub as actor `agent`;
- runs the step's learning operations through the REAL `memory.cli()` (tests/e2e_memory_cli.py);
- optionally leaves a `starting` pending marker (scripted mode only);
- prints the step's output and returns the step's rc.

Every session appends one record to `<state>/e2e/invocations.jsonl`. It never talks to a real
model, network, gh or OpenCode server.
"""
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))  # autonomy-controller imports release_policy/memory from the repo root

MEMORY_LAUNCHER = HERE / 'e2e_memory_cli.py'
MEMORY_TIMEOUT = 60
ENV_KEYS = ('AUTONOMY_PHASE', 'AUTONOMY_PROJECT_NAME', 'AUTONOMY_PROJECT_ROOT')
QUOTA_RC = 78
UNMODELLED_RC = 97


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


def learning_grant_from_instructions(instructions: str) -> tuple[Path, str] | None:
    """Return (memory.py path, grant path, token) parsed from the controller's instruction text.

    The controller writes `python3 <ROOT>/memory.py --grant <G> --token <T>` (memory.instruction).
    The paths and token are shlex-quoted only when needed; parse each piece with shlex.
    """
    match = re.search(r"python3 ((?:'[^']*'|\"[^\"]*\"|\S)+) --grant ((?:'[^']*'|\"[^\"]*\"|\S)+) --token (\S+)",
                      instructions)
    if not match:
        return None
    script, grant, token = (shlex.split(match.group(1))[0], shlex.split(match.group(2))[0], match.group(3))
    return Path(script), grant, token


def memory_cli(script: Path, grant: str, token: str, action: str, arg, engram_socket: str) -> dict:
    """Run the REAL memory.cli() through the launcher; return the records shape the tests read."""
    argv = [sys.executable, str(MEMORY_LAUNCHER), '--grant', grant, '--token', token, action]
    if action == 'search':
        argv.append(str(arg))
        stdio = {'stdin': subprocess.DEVNULL}
    else:
        stdio = {'input': json.dumps(arg, ensure_ascii=False)}  # record/feedback: JSON on stdin
    # Build the launcher env explicitly: no ambient E2E_* leaks through, only the two values this
    # session's world sets. E2E_ENGRAM_BINARY is an E2E_ name too, so it is stripped here as well.
    env = {k: v for k, v in os.environ.items() if not k.startswith('E2E_')}
    env['E2E_ENGRAM_SOCKET'] = engram_socket
    env['E2E_MEMORY_ROOT'] = os.environ.get('E2E_MEMORY_ROOT', '')
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=MEMORY_TIMEOUT, env=env,
                          check=False, **stdio)
    if proc.returncode:
        return {'ok': False, 'error': (proc.stderr or proc.stdout).strip()}
    return {'ok': True, 'result': json.loads(proc.stdout)}


def run_memory_ops(pdir: Path, ops: list[dict]) -> list[dict]:
    """Execute learning operations for one session through the real memory.cli().

    The token and grant path come from the instruction text the controller injected into this
    session's environment. The memory.py path must be this repository's memory.py. Only
    E2E_ENGRAM_SOCKET (the private stand-in socket) and E2E_MEMORY_ROOT (read by the launcher)
    are test variables. A session with no grant gets a refusal record for each op.
    """
    instructions = os.environ.get('AUTOCONTROLLER_LEARNING_INSTRUCTIONS', '')
    parsed = learning_grant_from_instructions(instructions)
    if parsed is None:
        return [{'ok': False, 'error': 'no learning grant in this session'} for _ in ops]
    script, grant, token = parsed
    if script.resolve() != (HERE.parent / 'memory.py').resolve():
        raise RuntimeError(f'learning instruction names a foreign memory.py: {script}')
    engram_socket = os.environ.get('E2E_ENGRAM_SOCKET', '')
    if not engram_socket or engram_socket.startswith('/run/user/'):
        # Test-only variable: refuse rather than reach any production or unset socket.
        raise RuntimeError('E2E_ENGRAM_SOCKET must name the private stand-in socket')
    results = []
    first_returned = None
    for op in ops:
        arg = op['arg']
        if isinstance(arg, dict) and arg.get('id') == '$first_returned':
            arg = dict(arg, id=first_returned)  # resolved from this session's last search
        outcome = memory_cli(script, grant, token, op['action'], arg, engram_socket)
        if op['action'] == 'search' and outcome['ok'] and outcome['result']:
            first_returned = outcome['result'][0]['id']
        results.append(outcome)
    return results


def oracle_reviews_listing(folder: Path, project: str, kind: str, number: str) -> dict:
    """The `--oracle-reviews` answer for one project's task, from recorded review candidates only."""
    def read(doc):
        return list(doc.get('oracle_reviews', {}).get(project, {}).get(f'{kind}:{number}', []))
    candidates = _locked_store(folder, read)
    log_invocation(folder, {'kind': 'oracle_listing', 'project': project, 'task': f'{kind}:{number}',
                            'candidates': len(candidates)})
    return {'kind': kind, 'number': int(number), 'candidates': candidates}


def _locked_store(folder: Path, change):
    """Read/modify the scripted-GitHub-independent oracle record under the shared script lock."""
    with (folder / 'script.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = folder / 'script.json'
        doc = json.loads(path.read_text(encoding='utf-8'))
        result = change(doc)
        tmp = path.with_name('script.json.tmp')
        tmp.write_text(json.dumps(doc, sort_keys=True), encoding='utf-8')
        tmp.replace(path)
    return result


def record_oracle_review(folder: Path, project: str, kind: str, number: str, *, tree: str, session: str) -> None:
    """Record a completed, succeeded, idle, inactive oracle candidate for the task (scripted only)."""
    def change(doc):
        bucket = doc.setdefault('oracle_reviews', {}).setdefault(project, {}).setdefault(f'{kind}:{number}', [])
        bucket.append({'root': f'ses_root_{session}', 'session': session, 'created': len(bucket) + 1,
                       'idle': len(bucket) + 2, 'outcome': 'succeeded', 'active': False,
                       'trees': [tree], 'snapshot_complete': True})
    _locked_store(folder, change)


def perform_step(folder: Path, pdir: Path, project: str, step: dict, title: str,
                 marker: Path | None) -> int:
    """The single step engine shared by both modes. Returns the session rc."""
    from e2e_github import FakeGitHub, tree_of  # imported lazily: only needed when a step touches GitHub
    if step.get('actions') or step.get('outage_after') or step.get('oracle_review'):
        store = FakeGitHub(folder / 'github.json')
        for action in step.get('actions', []):
            store.run_gh(action['gh'], actor='agent')
        if step.get('oracle_review'):
            # Explicit per step: the oracle reviewed the PR head this step's actions left in place.
            # The task and PR are read from the store; no default-positive evidence is ever recorded.
            target = step['oracle_review']  # {'kind','number','repo','session'[, 'pr']}
            if target.get('pr') is not None:
                head_sha = store.pr(target['repo'], int(target['pr']))['head']['sha']
            else:
                # Without a PR number: the head the step's own actions pushed or opened. It must be
                # named exactly once among the step's `--head-sha` arguments; anything else is a test bug.
                pushed = [a['gh'][a['gh'].index('--head-sha') + 1] for a in step.get('actions', [])
                          if '--head-sha' in a['gh']]
                if len(pushed) != 1:
                    raise AssertionError(f'oracle_review without pr needs exactly one --head-sha, got {len(pushed)}')
                head_sha = pushed[0]
            record_oracle_review(folder, project, target['kind'], str(target['number']),
                                 tree=tree_of(head_sha), session=target['session'])
        if step.get('outage_after'):
            store.set_outage(True)  # GitHub goes down right after this session
    if step.get('memory'):
        results = run_memory_ops(pdir, step['memory'])
        log_invocation(folder, {'kind': 'memory', 'project': project, 'results': results})
    if step.get('marker') == 'starting':
        if marker is None:
            print('scripted marker is not available in real-wrapper mode', file=sys.stderr)
            return UNMODELLED_RC
        marker.write_text(json.dumps({'status': 'starting', 'title': title}), encoding='utf-8')
    if step.get('output'):
        print(step['output'])
    return int(step.get('rc', 0))


def run_session(argv: list[str]) -> int:
    """Scripted-wrapper mode: the controller started this file where the wrapper would run."""
    marker = Path(os.environ['AUTONOMY_PENDING_MARKER'])
    folder = e2e_dir(str(marker))
    if argv[:1] == ['--clear-unstarted-quota']:
        rc = clear_unstarted_quota(marker)
        log_invocation(folder, {'kind': 'clear_unstarted', 'project': os.environ.get('AUTONOMY_PROJECT_NAME'), 'rc': rc})
        return rc
    if argv[:1] == ['--interrupt-owned']:
        log_invocation(folder, {'kind': 'interrupt', 'project': os.environ.get('AUTONOMY_PROJECT_NAME')})
        return 0
    if argv[:1] == ['--oracle-reviews'] and len(argv) == 3:
        listing = oracle_reviews_listing(folder, os.environ['AUTONOMY_PROJECT_NAME'], argv[1], argv[2])
        print(json.dumps(listing, sort_keys=True))
        return 0
    if argv[:2] != ['run', '--auto']:
        print(f'unscripted wrapper form: {argv[:3]}', file=sys.stderr)
        return 4

    rest = argv[2:]
    agent_name = rest[rest.index('--agent') + 1] if '--agent' in rest else None
    title = rest[rest.index('--title') + 1] if '--title' in rest else ''
    prompt = rest[-1]
    project = os.environ['AUTONOMY_PROJECT_NAME']
    phase = os.environ.get('AUTONOMY_PHASE', 'code')
    log_invocation(folder, {
        'kind': 'session', 'project': project, 'phase': phase, 'agent': agent_name, 'title': title,
        'prompt': prompt, 'argv': argv,
        'env': {k: os.environ.get(k) for k in ENV_KEYS},
        'learning_instructions': bool(os.environ.get('AUTOCONTROLLER_LEARNING_INSTRUCTIONS')),
        'pid': os.getpid(), 'pgid': os.getpgid(0), 'at': time.time(),
    })
    step = pop_step(folder, project)
    if step is None:
        print(f'UNSCRIPTED AI SESSION for project {project} phase {phase}', file=sys.stderr)
        return 97
    return perform_step(folder, marker.parent, project, step, title, marker)


if __name__ == '__main__':
    sys.exit(run_session(sys.argv[1:]))
