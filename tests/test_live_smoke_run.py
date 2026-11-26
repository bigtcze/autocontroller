"""Offline tests for scripts/live-smoke.py `run_smoke` (the live body).

The real `run_smoke` runs. `gh`, `git` and `opencode` are PATH shims that log every
call and answer from a scripted state directory. No real GitHub, OpenCode, network,
ambient credentials or real HOME is used. The shims are the only external boundary.
"""
import importlib.util
import io
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('live_smoke_run', str(ROOT / 'scripts' / 'live-smoke.py'))
live = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(live)

OWNER = 'example-owner'
RUN = 'abc123xyz'
REPO = f'autocontroller-smoke-{RUN}'
FULL = f'{OWNER}/{REPO}'

GH_SHIM = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
S = Path(os.environ['SHIM_STATE'])
argv = sys.argv[1:]
with (S / 'calls.jsonl').open('a') as f:
    f.write(json.dumps({'tool': 'gh', 'argv': argv}) + '\n')
doc = json.loads((S / 'gh.json').read_text())
def fail(msg, rc=1):
    sys.stderr.write(msg + '\n'); sys.exit(rc)
if argv[:2] == ['api', 'user']:
    print(json.dumps({'login': doc['login']})); sys.exit(0)
if argv[:2] == ['repo', 'view']:
    if doc.get('repo_exists'): print(json.dumps({'name': argv[2]})); sys.exit(0)
    fail('GraphQL: Could not resolve to a Repository (HTTP 404)')
if argv[:2] == ['repo', 'create']:
    # create_rc models a failure after the server-side create (repo exists) unless
    # create_makes_repo is False, which models a failure before anything was created.
    if doc.get('create_makes_repo', True):
        doc['repo_exists'] = True
    (S / 'gh.json').write_text(json.dumps(doc)); sys.exit(doc.get('create_rc', 0) or 0)
if argv[:2] == ['repo', 'clone']:
    dest = Path(argv[3]); dest.mkdir(parents=True, exist_ok=True); (dest / '.git').mkdir(); sys.exit(0)
if argv[:2] == ['repo', 'delete']:
    if doc.get('delete_rc'): fail('delete refused', doc['delete_rc'])
    doc['repo_exists'] = False
    (S / 'gh.json').write_text(json.dumps(doc))
    if doc.get('signal_parent_on_delete'):
        # Deliver a real signal to the controller (this shim's parent) while teardown runs.
        import signal
        os.kill(os.getppid(), getattr(signal, doc['signal_parent_on_delete']))
    sys.exit(0)
if argv[:2] == ['issue', 'create']:
    print(f'https://github.com/{doc["repo"]}/issues/{doc["issue"]}'); sys.exit(0)
if argv[:2] in (['issue', 'close'], ['pr', 'close']):
    sys.exit(int(doc.get('close_rc', 0)))
if argv[:1] == ['api'] and len(argv) > 1 and argv[1] == 'repos/' + doc['repo']:
    if not doc.get('repo_exists'): fail('HTTP 404: Not Found', 1)
    if 'repo_json' in doc: print(json.dumps(doc['repo_json'])); sys.exit(0)  # m-b: non-object JSON
    print(json.dumps({'full_name': doc['repo'], 'name': doc['repo'].split('/')[1],
                      'owner': {'login': doc.get('repo_owner', doc['login'])}})); sys.exit(0)
if argv[:1] == ['api'] and any('issues/' in a for a in argv) and 'graphql' not in argv:
    print(json.dumps({'state': 'open', 'number': doc['issue']})); sys.exit(0)
if argv[:2] == ['api', 'graphql']:
    nodes = doc.get('pr_nodes', [])
    print(json.dumps({'data': {'repository': {'issue': {'closedByPullRequestsReferences': {
        'nodes': nodes, 'pageInfo': {'hasNextPage': False}}}}}})); sys.exit(0)
if argv[:1] == ['api'] and 'actions/workflows' in ' '.join(argv):
    print(json.dumps({'workflow_runs': doc.get('runs', [])})); sys.exit(0)
fail(f'unmodelled gh call: {argv}', 97)
'''

GIT_SHIM = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
S = Path(os.environ['SHIM_STATE'])
argv = sys.argv[1:]
with (S / 'calls.jsonl').open('a') as f:
    f.write(json.dumps({'tool': 'git', 'argv': argv}) + '\n')
if 'rev-parse' in argv:
    print('a' * 40); sys.exit(0)
sys.exit(97)
'''

OPENCODE_SHIM = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
S = Path(os.environ['SHIM_STATE'])
argv = sys.argv[1:]
with (S / 'calls.jsonl').open('a') as f:
    f.write(json.dumps({'tool': 'opencode', 'argv': argv, 'cwd': os.getcwd()}) + '\n')
doc = json.loads((S / 'gh.json').read_text())
if doc.get('opencode_open_pr'):
    doc['pr_nodes'] = [{'number': 7, 'state': 'OPEN', 'headRefOid': 'b' * 40, 'baseRefName': 'main',
                        'isCrossRepository': False, 'headRepository': {'nameWithOwner': doc['repo']},
                        'url': 'x', 'mergeCommit': None}]
    (S / 'gh.json').write_text(json.dumps(doc))
sys.exit(int(doc.get('opencode_rc', 0)))
'''


def _calls(state: Path) -> list[dict]:
    path = state / 'calls.jsonl'
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _alive(pid: int) -> bool:
    """Linux: a process counts as alive unless it is gone or a zombie."""
    try:
        state = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError, IndexError):
        return False
    return state not in ('Z', 'X')


def _wait_dead(pid: int, timeout: float = 5.0) -> bool:
    """m2: poll for up to `timeout` seconds; True once the process is gone or a zombie."""
    deadline = time.monotonic() + timeout
    while _alive(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


class SmokeRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='live-run-'))
        self.addCleanup(lambda: __import__('shutil').rmtree(self.tmp, True))
        self.tmp.chmod(0o700)
        self.state = self.tmp / 'state'
        self.state.mkdir(mode=0o700)
        self.shims = self.tmp / 'bin'
        self.shims.mkdir(mode=0o700)
        for name, body in (('gh', GH_SHIM), ('git', GIT_SHIM)):
            (self.shims / name).write_text(body, encoding='utf-8')
            (self.shims / name).chmod(stat.S_IRWXU)
        self.opencode = self.tmp / 'opencode'
        self.opencode.write_text(OPENCODE_SHIM, encoding='utf-8')
        self.opencode.chmod(stat.S_IRWXU)
        self.gh_dir = self.tmp / 'gh-config'
        self.gh_dir.mkdir(mode=0o700)
        (self.gh_dir / live.ALLOWLIST_FILE).write_text(f'{OWNER}\n', encoding='utf-8')  # m3: required by --execute
        self.report = self.tmp / 'reports' / 'smoke.json'
        self.report.parent.mkdir(mode=0o700)
        self.production_root = self.tmp / 'no-production-root'  # absent, injected for hermeticity
        self.write_gh({'login': OWNER, 'repo': FULL, 'issue': 5, 'repo_exists': False,
                       'opencode_open_pr': True, 'opencode_rc': 0, 'pr_nodes': [], 'runs': []})

    def write_gh(self, doc: dict):
        (self.state / 'gh.json').write_text(json.dumps(doc), encoding='utf-8')

    def read_gh(self) -> dict:
        return json.loads((self.state / 'gh.json').read_text())

    def env(self) -> dict:
        return {'PATH': f'{self.shims}:{os.environ.get("PATH", "/usr/bin:/bin")}',
                'HOME': str(self.tmp / 'home'), 'GH_CONFIG_DIR': str(self.gh_dir),
                'SHIM_STATE': str(self.state), live.ENV_OPT_IN: '1'}

    def args(self, *extra):
        return ['--owner', OWNER, '--allow-owner', OWNER, '--repo', REPO, '--run-id', RUN,
                '--model', 'example-provider/example-model', '--max-ai-sessions', '1',
                '--timeout-seconds', '300', live.ACK_FLAG, '--execute',
                '--opencode', str(self.opencode), '--report', str(self.report), *extra]

    def run_smoke_main(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = live.main(self.args(*extra), env=self.env(), production_root=self.production_root)
        return rc, err.getvalue()

    def report_doc(self) -> dict:
        return json.loads(self.report.read_text())

    def tool_calls(self, tool: str) -> list[dict]:
        return [c for c in _calls(self.state) if c['tool'] == tool]

    def test_happy_path_order_one_session_and_report_counts(self):
        rc, err = self.run_smoke_main()
        self.assertEqual(rc, 0, err)
        gh = [c['argv'] for c in self.tool_calls('gh')]
        create = next(i for i, a in enumerate(gh) if a[:2] == ['repo', 'create'])
        view = next(i for i, a in enumerate(gh) if a[:2] == ['repo', 'view'])
        self.assertLess(view, create, 'existence is checked before creation')
        self.assertIn('--private', gh[create])
        self.assertIn('--add-readme', gh[create])
        self.assertEqual(sum(1 for a in gh if a[:2] == ['repo', 'create']), 1)
        opencode = self.tool_calls('opencode')
        self.assertEqual(len(opencode), 1, 'exactly one AI session')
        self.assertEqual(opencode[0]['argv'][:6],
                         ['run', '--model', 'example-provider/example-model', '--agent', 'orchestrator', '--title'])
        self.assertEqual(opencode[0]['argv'][6], f'autocontroller-smoke-{RUN}')
        self.assertTrue(opencode[0]['cwd'].endswith('/checkout'), 'the session runs in the temp checkout')
        doc = self.report_doc()
        self.assertEqual(doc['status'], 'passed', doc)
        self.assertEqual(doc['counts']['created'], 3, 'repo, issue, PR')
        self.assertEqual(doc['counts']['deleted'], 1)
        self.assertEqual(doc['leftovers'], [])
        self.assertEqual(doc['opencode'], 'opencode', 'the report records only the binary basename')
        self.assertEqual(self.read_gh()['repo_exists'], False, 'the sandbox repo was deleted')

    def test_checkout_uses_gh_repo_clone_not_raw_git_clone(self):
        rc, err = self.run_smoke_main()
        self.assertEqual(rc, 0, err)
        gh = [c['argv'] for c in self.tool_calls('gh')]
        self.assertTrue(any(a[:2] == ['repo', 'clone'] and a[2] == FULL for a in gh), gh)
        git = [c['argv'] for c in self.tool_calls('git')]
        self.assertFalse(any('clone' in a for a in git), 'no raw git clone through the sandbox env')

    def test_prompt_above_cap_refuses_before_any_opencode_call(self):
        rc, err = self.run_smoke_main('--max-prompt-bytes', '20')
        self.assertEqual(rc, 2, err)
        self.assertIn('--max-prompt-bytes', err)
        self.assertEqual(self.tool_calls('opencode'), [], 'an over-cap prompt never reaches the agent')

    def test_session_cap_is_structural_one_opencode_call_even_when_pr_missing(self):
        self.write_gh({'login': OWNER, 'repo': FULL, 'issue': 5, 'repo_exists': False,
                       'opencode_open_pr': False, 'opencode_rc': 0, 'pr_nodes': [], 'runs': []})
        rc, err = self.run_smoke_main()
        self.assertEqual(rc, 3, err)
        self.assertEqual(len(self.tool_calls('opencode')), 1, 'the failed check never starts a second session')

    def test_repository_that_already_exists_is_refused_and_never_deleted(self):
        self.write_gh({'login': OWNER, 'repo': FULL, 'issue': 5, 'repo_exists': True,
                       'opencode_open_pr': True, 'opencode_rc': 0, 'pr_nodes': [], 'runs': []})
        rc, err = self.run_smoke_main()
        self.assertEqual(rc, 2, err)
        self.assertIn('already exists', err)
        gh = [c['argv'] for c in self.tool_calls('gh')]
        self.assertFalse(any(a[:2] in (['repo', 'create'], ['repo', 'delete']) for a in gh),
                         'an existing repository is neither created over nor deleted')
        self.assertEqual(self.tool_calls('opencode'), [], 'no AI session for a refused run')

    def test_report_inside_the_checkout_is_refused_before_any_external_call(self):
        inside = Path(live.ROOT) / 'docs' / f'smoke-{RUN}.json'
        self.assertFalse(inside.exists())
        self.report = inside
        rc, err = self.run_smoke_main()
        self.assertEqual(rc, 2, err)
        self.assertIn('outside this checkout', err)
        self.assertFalse(inside.exists())
        self.assertEqual(self.tool_calls('gh'), [], 'refused before any gh call')
        self.assertEqual(self.tool_calls('opencode'), [])

    def test_teardown_after_mid_flow_failure_deletes_only_the_created_repo(self):
        self.write_gh({'login': OWNER, 'repo': FULL, 'issue': 5, 'repo_exists': False,
                       'opencode_open_pr': True, 'opencode_rc': 9, 'pr_nodes': [], 'runs': []})
        rc, err = self.run_smoke_main()
        self.assertEqual(rc, 3, err)
        gh = [c['argv'] for c in self.tool_calls('gh')]
        deletes = [a for a in gh if a[:2] == ['repo', 'delete']]
        self.assertEqual(deletes, [['repo', 'delete', FULL, '--yes']], 'only the repo this run created')
        self.assertEqual(len(self.tool_calls('opencode')), 1)
        doc = self.report_doc()
        self.assertEqual(doc['status'], 'failed')
        self.assertEqual(doc['deleted'], [FULL])
        self.assertEqual(doc['leftovers'], [])

    def test_failed_teardown_exits_three_and_lists_the_leftover(self):
        self.write_gh({'login': OWNER, 'repo': FULL, 'issue': 5, 'repo_exists': False,
                       'opencode_open_pr': True, 'opencode_rc': 0, 'pr_nodes': [], 'runs': [],
                       'delete_rc': 1})
        rc, err = self.run_smoke_main()
        self.assertEqual(rc, 3, err)
        doc = self.report_doc()
        self.assertEqual(doc['status'], 'teardown-failed')
        self.assertEqual(doc['leftovers'], [FULL])
        self.assertEqual(doc['deleted'], [], 'a failed delete is not reported as deleted')

    def test_repo_reverify_mismatch_refuses_to_delete_and_leaves_a_leftover(self):
        self.write_gh({'login': OWNER, 'repo': FULL, 'issue': 5, 'repo_exists': False,
                       'opencode_open_pr': True, 'opencode_rc': 0, 'pr_nodes': [], 'runs': [],
                       'repo_owner': 'someone-else'})
        rc, err = self.run_smoke_main()
        self.assertEqual(rc, 3, err)
        gh = [c['argv'] for c in self.tool_calls('gh')]
        self.assertFalse(any(a[:2] == ['repo', 'delete'] for a in gh), 'owner mismatch: never deleted')
        doc = self.report_doc()
        self.assertEqual(doc['leftovers'], [FULL])
        self.assertEqual(doc['status'], 'teardown-failed')

    def test_create_failing_after_server_side_create_is_deleted_when_verified(self):
        # m1: `gh repo create` fails after the repo exists. Teardown must verify and delete it.
        self.write_gh({'login': OWNER, 'repo': FULL, 'issue': 5, 'repo_exists': False,
                       'opencode_open_pr': True, 'opencode_rc': 0, 'pr_nodes': [], 'runs': [],
                       'create_rc': 1})
        rc, err = self.run_smoke_main()
        self.assertEqual(rc, 3, err)
        gh = [c['argv'] for c in self.tool_calls('gh')]
        self.assertEqual([a for a in gh if a[:2] == ['repo', 'delete']], [['repo', 'delete', FULL, '--yes']])
        doc = self.report_doc()
        self.assertEqual(doc['deleted'], [FULL])
        self.assertEqual(doc['leftovers'], [])

    def test_create_failing_without_a_repo_is_recorded_as_not_created(self):
        # m1: the 404 re-verify proves nothing was created; no delete is attempted.
        self.write_gh({'login': OWNER, 'repo': FULL, 'issue': 5, 'repo_exists': False,
                       'opencode_open_pr': True, 'opencode_rc': 0, 'pr_nodes': [], 'runs': [],
                       'create_rc': 1, 'create_makes_repo': False})
        rc, err = self.run_smoke_main()
        self.assertEqual(rc, 3, err)
        gh = [c['argv'] for c in self.tool_calls('gh')]
        self.assertFalse(any(a[:2] == ['repo', 'delete'] for a in gh), 'nothing to delete')
        doc = self.report_doc()
        self.assertEqual(doc['not_created'], [FULL])
        self.assertEqual(doc['leftovers'], [])
        self.assertNotIn('teardown_errors', doc, 'a 404 is a verification result, not a teardown error')
        self.assertEqual(doc['status'], 'failed', 'not teardown-incomplete: create failed, nothing leaked')

    def test_non_object_json_from_the_ownership_check_is_a_leftover_never_a_delete(self):
        # m-b: `[]` makes verify_repo_owned raise AttributeError. That must become a leftover.
        self.write_gh({'login': OWNER, 'repo': FULL, 'issue': 5, 'repo_exists': False,
                       'opencode_open_pr': True, 'opencode_rc': 0, 'pr_nodes': [], 'runs': [],
                       'repo_json': []})
        rc, err = self.run_smoke_main()
        self.assertEqual(rc, 3, err)
        gh = [c['argv'] for c in self.tool_calls('gh')]
        self.assertFalse(any(a[:2] == ['repo', 'delete'] for a in gh), 'unverified repo is never deleted')
        doc = self.report_doc()
        self.assertEqual(doc['leftovers'], [FULL])
        self.assertEqual(doc['status'], 'teardown-failed')

    def test_failed_close_does_not_stop_the_repo_delete_and_exits_three(self):
        self.write_gh({'login': OWNER, 'repo': FULL, 'issue': 5, 'repo_exists': False,
                       'opencode_open_pr': True, 'opencode_rc': 0, 'pr_nodes': [], 'runs': [],
                       'close_rc': 1})
        rc, err = self.run_smoke_main()
        self.assertEqual(rc, 3, err)
        gh = [c['argv'] for c in self.tool_calls('gh')]
        self.assertTrue(any(a[:2] == ['pr', 'close'] for a in gh), 'PR close attempted')
        self.assertTrue(any(a[:2] == ['issue', 'close'] for a in gh), 'Issue close attempted after PR close failed')
        self.assertTrue(any(a[:2] == ['repo', 'delete'] for a in gh), 'repo delete attempted independently')
        doc = self.report_doc()
        self.assertEqual(doc['status'], 'teardown-incomplete')
        self.assertEqual(len(doc['teardown_errors']), 2)
        self.assertEqual(doc['deleted'], [FULL])

    def test_script_run_as_a_file_from_outside_the_checkout_reaches_the_shimmed_gh(self):
        # Regression for B1: sys.path[0] is scripts/ when run as a file; the controller import
        # must still succeed, and the first external call is the login check.
        home = self.tmp / 'home'
        home.mkdir(mode=0o700)
        env = {'PATH': f'{self.shims}:{os.environ.get("PATH", "/usr/bin:/bin")}', 'HOME': str(home),
               'GH_CONFIG_DIR': str(self.gh_dir), 'SHIM_STATE': str(self.state), live.ENV_OPT_IN: '1'}
        proc = subprocess.run([sys.executable, str(ROOT / 'scripts' / 'live-smoke.py'), *self.args()],
                              cwd=str(self.tmp), env=env, text=True, capture_output=True,
                              timeout=180, check=False)
        self.assertNotIn('ModuleNotFoundError', proc.stderr)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        gh = [c['argv'] for c in self.tool_calls('gh')]
        self.assertEqual(gh[0], ['api', 'user'], 'the import happened before the first gh call')
        self.assertEqual(self.report_doc()['status'], 'passed')

    def test_timed_out_session_is_killed_together_with_its_children(self):
        child_pid = self.tmp / 'child.pid'
        fake = self.tmp / 'slow-opencode'
        fake.write_text(f"#!/bin/sh\nsleep 300 &\necho $! > '{child_pid}'\nsleep 300\n", encoding='utf-8')
        fake.chmod(stat.S_IRWXU)
        report = live.Report(RUN, FULL)
        with self.assertRaises(live.SmokeFailed) as caught:
            live.run_session([str(fake), 'run'], report, self.env(), timeout=2, cwd=self.tmp, note='test')
        self.assertIn('process group was terminated', str(caught.exception))
        self.assertIn('timeout', report.doc['commands'][-1]['note'])
        pid = int(child_pid.read_text())
        self.assertTrue(_wait_dead(pid), 'the background child died with its process group')

    def test_interrupt_during_session_kills_leader_and_child_and_reraises(self):
        # Blocker: an interrupt while the session runs must not leave it alive.
        child_pid = self.tmp / 'child.pid'
        fake = self.tmp / 'slow-opencode'
        fake.write_text(f"#!/bin/sh\nsleep 300 &\necho $! > '{child_pid}'\nsleep 300\n", encoding='utf-8')
        fake.chmod(stat.S_IRWXU)
        report = live.Report(RUN, FULL)
        leader = {}

        def interrupted_communicate(self_proc, *a, **kw):
            # The real session is running. Wait until its child has written its pid, record the
            # leader, then interrupt as Ctrl-C would. run_session must still kill the group.
            leader['proc'] = self_proc
            deadline = time.monotonic() + 10
            while not child_pid.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            raise KeyboardInterrupt

        with unittest.mock.patch.object(subprocess.Popen, 'communicate', interrupted_communicate):
            with self.assertRaises(KeyboardInterrupt):
                live.run_session([str(fake), 'run'], report, self.env(), timeout=300,
                                 cwd=self.tmp, note='test')
        proc = leader['proc']
        pid = int(child_pid.read_text())
        self.assertTrue(_wait_dead(proc.pid), 'the session leader was terminated')
        self.assertTrue(_wait_dead(pid), 'the session child was terminated with its group')
        self.assertIn('interrupted', report.doc['commands'][-1]['note'])

    def test_main_interrupt_during_session_tears_down_exits_three_and_reports_interrupted(self):
        # Main-level: SIGINT-equivalent during the one opencode session. Only the opencode process's
        # communicate() is interrupted; gh and git calls keep their normal path.
        child_pid = self.tmp / 'child.pid'
        slow = self.tmp / 'slow-opencode'
        slow.write_text(f"#!/bin/sh\nsleep 300 &\necho $! > '{child_pid}'\nsleep 300\n", encoding='utf-8')
        slow.chmod(stat.S_IRWXU)
        leader = {}
        real_communicate = subprocess.Popen.communicate

        def communicate(self_proc, *a, **kw):
            if Path(self_proc.args[0]).name != 'slow-opencode':
                return real_communicate(self_proc, *a, **kw)  # gh/git etc.: normal path
            leader['proc'] = self_proc
            deadline = time.monotonic() + 10
            while not child_pid.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            raise KeyboardInterrupt  # what SIGINT/SIGTERM/SIGHUP produce through the handlers

        self.write_gh({'login': OWNER, 'repo': FULL, 'issue': 5, 'repo_exists': False,
                       'opencode_open_pr': True, 'opencode_rc': 0, 'pr_nodes': [], 'runs': []})
        args = self.args()
        args[args.index(str(self.opencode))] = str(slow)
        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch.object(subprocess.Popen, 'communicate', communicate), \
                redirect_stdout(out), redirect_stderr(err):
            rc = live.main(args, env=self.env(), production_root=self.production_root)
        self.assertEqual(rc, 3, err.getvalue())
        self.assertIn('interrupted', err.getvalue())

        gh = [c['argv'] for c in self.tool_calls('gh')]
        self.assertIn(['repo', 'delete', FULL, '--yes'], gh, 'teardown ran and deleted the created repo')
        self.assertTrue(any(a[:2] == ['repo', 'create'] for a in gh), 'the repo was created before the interrupt')

        self.assertTrue(_wait_dead(leader['proc'].pid), 'the session leader was terminated')
        self.assertTrue(_wait_dead(int(child_pid.read_text())), 'the session child was terminated')

        doc = self.report_doc()
        self.assertEqual(doc['status'], 'interrupted')
        self.assertEqual(doc['deleted'], [FULL])
        self.assertEqual(doc['leftovers'], [])

    def test_second_interrupt_during_grace_wait_still_sends_sigkill_to_the_group(self):
        # B-final: a KeyboardInterrupt raised by the first proc.wait inside terminate_group must
        # not skip the SIGKILL. The leader and its child must both be dead, and the exception
        # must still propagate.
        child_pid = self.tmp / 'child.pid'
        fake = self.tmp / 'slow-opencode'
        fake.write_text(f"#!/bin/sh\nsleep 300 &\necho $! > '{child_pid}'\nsleep 300\n", encoding='utf-8')
        fake.chmod(stat.S_IRWXU)
        real_wait = subprocess.Popen.wait
        calls = {'n': 0}

        def interrupting_wait(self_proc, timeout=None):
            calls['n'] += 1
            if calls['n'] == 1:
                raise KeyboardInterrupt  # the second interrupt, during the SIGTERM grace wait
            return real_wait(self_proc, timeout=timeout)

        proc = subprocess.Popen([str(fake), 'run'], start_new_session=True, cwd=self.tmp,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 10
        while not child_pid.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        pid = int(child_pid.read_text())
        with unittest.mock.patch.object(subprocess.Popen, 'wait', interrupting_wait):
            with self.assertRaises(KeyboardInterrupt):
                live.terminate_group(proc)
        self.assertTrue(_wait_dead(proc.pid), 'the leader was killed despite the second interrupt')
        self.assertTrue(_wait_dead(pid), 'the child was killed despite the second interrupt')
        for pipe in (proc.stdout, proc.stderr):  # this test drives the Popen directly: close its pipes
            pipe.close()
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])  # read-only query: adds nothing
        for sig in live.INTERRUPT_SIGNALS:
            self.assertNotIn(sig, mask, f'{sig.name} is unblocked again after terminate_group')

    def test_real_sigterm_during_session_tears_down_and_reports_interrupted(self):
        # m-c: a real SIGTERM delivered to this process while the session runs goes through the
        # handler main installs under --execute. Exit 3, status interrupted, group dead, repo deleted.
        child_pid = self.tmp / 'child.pid'
        slow = self.tmp / 'slow-opencode'
        slow.write_text(f"#!/bin/sh\nsleep 300 &\necho $! > '{child_pid}'\nsleep 300\n", encoding='utf-8')
        slow.chmod(stat.S_IRWXU)
        leader = {}
        real_communicate = subprocess.Popen.communicate

        def communicate(self_proc, *a, **kw):
            if Path(self_proc.args[0]).name != 'slow-opencode':
                return real_communicate(self_proc, *a, **kw)
            leader['proc'] = self_proc
            # A real SIGTERM to this process, from a timer, once the session's child has started.
            def fire():
                deadline = time.monotonic() + 10
                while not child_pid.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                os.kill(os.getpid(), signal.SIGTERM)
            timer = threading.Timer(0.0, fire)
            timer.start()
            try:
                # The handler raises KeyboardInterrupt in the main thread while this call waits.
                return real_communicate(self_proc, *a, **kw)
            finally:
                timer.join(10)

        self.write_gh({'login': OWNER, 'repo': FULL, 'issue': 5, 'repo_exists': False,
                       'opencode_open_pr': True, 'opencode_rc': 0, 'pr_nodes': [], 'runs': []})
        args = self.args()
        args[args.index(str(self.opencode))] = str(slow)
        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch.object(subprocess.Popen, 'communicate', communicate), \
                redirect_stdout(out), redirect_stderr(err):
            rc = live.main(args, env=self.env(), production_root=self.production_root)
        self.assertEqual(rc, 3, err.getvalue())
        self.assertIn('SIGTERM', err.getvalue())
        self.assertTrue(_wait_dead(leader['proc'].pid), 'the session leader is dead')
        self.assertTrue(_wait_dead(int(child_pid.read_text())), 'the session child is dead')
        gh = [c['argv'] for c in self.tool_calls('gh')]
        self.assertIn(['repo', 'delete', FULL, '--yes'], gh)
        doc = self.report_doc()
        self.assertEqual(doc['status'], 'interrupted')
        self.assertEqual(doc['deleted'], [FULL])

    def _signal_during_teardown(self, signame: str):
        """A real signal arrives while teardown runs (inside `gh repo delete`).

        Expected: the signal is held until the teardown block ends, teardown completes (PR and
        Issue closed, repo deleted, report written), the mask is restored, and the pending signal
        is handled once after the block: exit 3, status `interrupted`.
        """
        self.write_gh({'login': OWNER, 'repo': FULL, 'issue': 5, 'repo_exists': False,
                       'opencode_open_pr': True, 'opencode_rc': 0, 'pr_nodes': [], 'runs': [],
                       'signal_parent_on_delete': signame})
        if signame == 'SIGINT':
            # Python installs default_int_handler at startup, but not when SIGINT started ignored.
            # Install it explicitly so the test does not depend on how the runner started Python.
            previous = signal.signal(signal.SIGINT, signal.default_int_handler)
            self.addCleanup(signal.signal, signal.SIGINT, previous)
        mask_before = signal.pthread_sigmask(signal.SIG_BLOCK, [])
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = live.main(self.args(), env=self.env(), production_root=self.production_root)
        self.assertEqual(rc, 3, err.getvalue())
        self.assertIn('interrupted by', err.getvalue())
        self.assertEqual(signal.pthread_sigmask(signal.SIG_BLOCK, []), mask_before,
                         'the signal mask is restored after teardown')
        self.assertNotIn(getattr(signal, signame), signal.sigpending(), 'the pending signal was handled')

        gh = [c['argv'] for c in self.tool_calls('gh')]
        self.assertIn(['repo', 'delete', FULL, '--yes'], gh, 'teardown ran to the repo delete')
        self.assertTrue(any(a[:2] == ['issue', 'close'] for a in gh), 'teardown closed the Issue')
        doc = self.report_doc()
        self.assertEqual(doc['deferred_signals'], [signame], 'the signal was recorded as deferred')
        self.assertEqual(doc['deleted'], [FULL], 'the report records the completed delete')
        self.assertEqual(doc['leftovers'], [])
        self.assertEqual(doc['status'], 'interrupted', 'a run whose teardown finished is not "passed"')

    def test_sigterm_during_teardown_is_deferred_then_handled_once(self):
        self._signal_during_teardown('SIGTERM')

    def test_sigint_during_teardown_is_deferred_then_handled_once(self):
        self._signal_during_teardown('SIGINT')

    def test_m1_mask_is_restored_when_recording_deferred_signals_raises(self):
        # m1: _record_deferred_signals raising must not leave INTERRUPT_SIGNALS blocked.
        mask_before = signal.pthread_sigmask(signal.SIG_BLOCK, [])
        err = io.StringIO()
        self.write_gh({'login': OWNER, 'repo': FULL, 'issue': 5, 'repo_exists': False,
                       'opencode_open_pr': True, 'opencode_rc': 0, 'pr_nodes': [], 'runs': []})
        with unittest.mock.patch.object(live, '_record_deferred_signals',
                                        side_effect=RuntimeError('boom')), \
                redirect_stdout(io.StringIO()), redirect_stderr(err):
            rc = live.main(self.args(), env=self.env(), production_root=self.production_root)
        self.assertEqual(rc, 3, err.getvalue())
        self.assertIn('RuntimeError', err.getvalue())
        mask_after = signal.pthread_sigmask(signal.SIG_BLOCK, [])
        self.assertEqual(mask_after, mask_before, 'the mask is restored although recording raised')
        for sig in live.INTERRUPT_SIGNALS:
            self.assertNotIn(sig, mask_after, f'{sig.name} is unblocked after the failure')

    def test_m5_inherited_sighup_ignore_is_kept_during_the_run_and_restored(self):
        # m5: a SIGHUP inherited as SIG_IGN (nohup) stays ignored during the run, not replaced.
        previous = signal.signal(signal.SIGHUP, signal.SIG_IGN)
        self.addCleanup(signal.signal, signal.SIGHUP, previous)
        seen = {}

        def fake_run_smoke(args, env, *, report_path, workdir):
            seen['hup'] = signal.getsignal(signal.SIGHUP)
            seen['term'] = signal.getsignal(signal.SIGTERM)
            return 0

        with unittest.mock.patch.object(live, 'run_smoke', fake_run_smoke):
            rc, err = self.run_smoke_main()
        self.assertEqual(rc, 0, err)
        self.assertEqual(seen['hup'], signal.SIG_IGN, 'an inherited SIG_IGN is not replaced')
        self.assertIsNot(seen['term'], signal.SIG_IGN)
        self.assertEqual(signal.getsignal(signal.SIGHUP), signal.SIG_IGN, 'SIG_IGN is still in place afterwards')

    def test_sigterm_and_sighup_handlers_installed_during_execute_and_restored(self):
        # Blocker: main installs SIGTERM/SIGHUP handlers under --execute and restores them afterwards.
        seen = {}

        def fake_run_smoke(args, env, *, report_path, workdir):
            seen['term'] = signal.getsignal(signal.SIGTERM)
            seen['hup'] = signal.getsignal(signal.SIGHUP)
            return 0

        previous_hup = signal.signal(signal.SIGHUP, signal.SIG_DFL)  # known start state; nohup would make this vacuous
        self.addCleanup(signal.signal, signal.SIGHUP, previous_hup)
        before_term = signal.getsignal(signal.SIGTERM)
        before_hup = signal.getsignal(signal.SIGHUP)
        with unittest.mock.patch.object(live, 'run_smoke', fake_run_smoke):
            self.run_smoke_main()
        self.assertIsNot(seen['term'], before_term)
        self.assertIsNot(seen['hup'], before_hup)
        self.assertEqual(signal.getsignal(signal.SIGTERM), before_term, 'SIGTERM handler restored')
        self.assertEqual(signal.getsignal(signal.SIGHUP), before_hup, 'SIGHUP handler restored')
