"""E2E acceptance (offline, deterministic): real ProjectWorker over stand-ins.

Service doubles (see docs/E2E_TESTING.md): fake GitHub (e2e_github), scripted
OMO/OpenCode wrapper (e2e_agent), stand-in Engram (e2e_engram_standin). The
controller's real helpers run unmodified; only the gh/subprocess seams and the
wrapper executable are replaced.
"""
import json
import os
import shutil
import tempfile
import threading
import unittest
import unittest.mock
from pathlib import Path

import e2e_world as ew

R = 'example-owner/alpha'
SHA_ISSUE = 'c1' * 20
SHA_RENOVATE = 'd1' * 20
RENOVATE_HEAD = 'e1' * 20
SHA_FIX = 'f2' * 20
REPAIR_SHA = 'a3' * 20


def sha(seed: str) -> str:
    return (seed * 40)[:40]


class E2EBase(unittest.TestCase):
    # Subclasses set True to run the controller through the REAL safe wrapper (see World).
    real_wrapper = False

    def setUp(self):
        # Defense in depth: remove ambient controller/OpenCode/Engram variables for the whole test,
        # restored on cleanup. Installed first, so anything the test sets later (for example a leak
        # in its body) is still removed when a worker runs, by the wrapper below.
        ambient = ew.ambient_env_removed()
        ambient.__enter__()
        self.addCleanup(ambient.__exit__, None, None, None)
        self.tmp = Path(tempfile.mkdtemp(prefix='e2e-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.tmp.chmod(0o700)
        # Guard before anything else: no helper ordering may resolve the production
        # memory socket (/run/user/<uid>/autocontroller-memory). The whole test sees a
        # private socket path; the per-helper patches below only narrow it further.
        self.sock_guard = ew.guard_memory_socket(self.tmp)
        self.addCleanup(self.sock_guard.stop)
        self.external_guard = ew.guard_external_access()
        self.addCleanup(self.external_guard.stop)
        self.world = ew.World(self.tmp, real_wrapper=self.real_wrapper)
        self.gh = self.world.gh
        self.transport = self.world.transport()
        self.transport.__enter__()
        self.addCleanup(self.transport.__exit__, None, None, None)

    def drive(self, name: str, iterations: int) -> list:
        worker = self.world.worker(name)
        return [worker.one_iteration() for _ in range(iterations)]

    def restart(self, name: str = 'alpha'):
        """A new ProjectWorker instance over the same state directory (process restart)."""
        return self.world.worker(name)

    def step_until(self, phase: str, name: str = 'alpha', limit: int = 6):
        """Fresh worker per iteration (a restart each time) until the checkpoint reaches `phase`."""
        for _ in range(limit):
            cp = self.checkpoint(name)
            if cp and cp.get('phase') == phase:
                return
            self.restart(name).one_iteration()
        self.assertEqual((self.checkpoint(name) or {}).get('phase'), phase)

    def sessions(self, phase=None):
        return self.world.sessions(phase)

    def expire_retries(self, name: str):
        """Make every persisted backoff due now, so the next iteration runs immediately."""
        path = self.world.worker(name).retries_file
        if path.exists():
            doc = json.loads(path.read_text(encoding='utf-8'))
            for value in doc.values():
                value['until'] = 0
            path.write_text(json.dumps(doc, sort_keys=True), encoding='utf-8')

    def checkpoint(self, name: str = 'alpha') -> dict | None:
        return ew.ctl.json_read(self.world.worker(name).cycle_state, None)

    def merges(self) -> list[list[str]]:
        return self.gh.mutation_argvs(['gh', 'pr', 'merge'])

    def run_concurrently(self, iterations: dict[str, int]) -> list[BaseException]:
        """One thread and one ProjectWorker per project; each runs its own iteration count."""
        errors: list[BaseException] = []
        workers = {name: self.world.worker(name) for name in iterations}

        def run(name: str):
            try:
                for _ in range(iterations[name]):
                    workers[name].one_iteration()
            except BaseException as exc:  # reported to the test, never swallowed
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(name,), name=f'worker-{name}') for name in iterations]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(300)
        if any(thread.is_alive() for thread in threads):
            errors.append(TimeoutError('a project worker thread did not finish'))
        return errors


class MemorySocketGuardTests(E2EBase):
    """Item A: no E2E test can resolve the production memory socket, whatever the helper order."""

    def test_socket_path_is_private_for_the_whole_test(self):
        import memory as learning
        resolved = str(learning.socket_path())
        self.assertFalse(resolved.startswith('/run/user/'), resolved)
        self.assertTrue(resolved.startswith(str(self.tmp)), resolved)

    def test_runtime_constructed_before_any_helper_uses_only_the_private_socket(self):
        import memory as learning
        runtime = learning.Runtime(self.world.state)
        self.addCleanup(lambda: runtime.sock.unlink(missing_ok=True))
        self.assertTrue(str(runtime.sock).startswith(str(self.tmp)), runtime.sock)
        self.assertFalse(str(runtime.sock).startswith('/run/user/'))

    def test_connect_to_a_run_user_socket_is_refused_before_any_io(self):
        import socket
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(5)
            with self.assertRaises(ew._ForbiddenConnect):
                # Built from the running UID so any /run/user/<uid> path is exercised, not only 1000.
                sock.connect(f'/run/user/{os.getuid()}/autocontroller-memory/engram.sock')

    def test_download_of_engram_is_refused(self):
        import memory as learning
        with self.assertRaises(ew._ForbiddenConnect):
            learning.Runtime.download('https://example.invalid/engram', 1)

    def _learning_session(self):
        """A pdir with a real grant file and the exact instruction text memory.instruction() writes."""
        import memory as learning
        state = self.tmp / 'state'
        pdir = state / 'projects' / 'alpha'
        pdir.mkdir(parents=True, exist_ok=True)
        (state / 'memory').mkdir(parents=True, exist_ok=True)
        token = 'tok-' + 'a' * 16
        (pdir / 'memory-grant.json').write_text(json.dumps(
            {'token': token, 'kind': 'issue', 'repo': R, 'number': 1, 'used': 0, 'queries': [],
             'records': 0, 'feedback': 0, 'returned': []}), encoding='utf-8')
        return pdir, learning.instruction(pdir, token)

    def test_agent_memory_ops_refuse_a_missing_or_production_socket(self):
        """The agent refuses an unset or /run/user socket before any launcher runs.

        The launcher carries the same guard (see MemoryCliLauncherTests), so both layers refuse.
        """
        import os
        import e2e_agent as agent
        pdir, instructions = self._learning_session()
        for value in (None, '', f'/run/user/{os.getuid()}/autocontroller-memory/engram.sock'):
            env = {'AUTOCONTROLLER_LEARNING_INSTRUCTIONS': instructions}
            with unittest.mock.patch.dict(os.environ, env, clear=False):
                if value is None:
                    os.environ.pop('E2E_ENGRAM_SOCKET', None)
                else:
                    os.environ['E2E_ENGRAM_SOCKET'] = value
                with self.assertRaises(RuntimeError, msg=f'socket={value!r}'):
                    agent.run_memory_ops(pdir, [{'action': 'search', 'arg': 'x'}])

    def test_memory_cli_launcher_refuses_a_missing_or_production_socket_and_runs_nothing(self):
        import os
        import subprocess
        import sys
        launcher = ew.TESTS / 'e2e_memory_cli.py'
        pdir, instructions = self._learning_session()
        grant = str(pdir / 'memory-grant.json')
        token = instructions.split('--token ', 1)[1].split()[0]
        cases = [
            {'E2E_MEMORY_ROOT': str(self.tmp)},  # socket unset
            {'E2E_MEMORY_ROOT': str(self.tmp), 'E2E_ENGRAM_SOCKET': ''},
            {'E2E_MEMORY_ROOT': str(self.tmp),
             'E2E_ENGRAM_SOCKET': f'/run/user/{os.getuid()}/autocontroller-memory/engram.sock'},
        ]
        for extra in cases:
            env = {k: v for k, v in os.environ.items() if not k.startswith(('E2E_', 'AUTOCONTROLLER_'))}
            env.update(extra)
            proc = subprocess.run([sys.executable, str(launcher), '--grant', grant, '--token', token,
                                   'search', 'x'], env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(proc.returncode, 3, (extra, proc.stdout, proc.stderr))
            self.assertIn('launcher refused', proc.stderr)
            self.assertEqual(proc.stdout, '', 'a refused launcher never prints a memory result')


class SchedulerHappyPathTests(E2EBase):
    def test_renovate_first_then_backlog_issue_with_exact_sha_merge(self):
        self.world.add_project('alpha', R, 1)
        issue = self.gh.add_issue(R, title='Feature A', body='Implement A', project='example-owner/1',
                                  status='Backlog')
        renovate = self.gh.add_pr(R, title='Bump dep', body='', head_sha=RENOVATE_HEAD,
                                  head_ref='renovate/dep', user=ew.eg.RENOVATE_USER)
        # Fork PR impersonating Renovate: must never reach an agent.
        self.gh.add_pr(R, title='Fork bump', body='', head_sha='f' * 40, head_repo='example-fork/alpha',
                       head_ref='renovate/fork', user=ew.eg.RENOVATE_USER)
        self.world.script('alpha', [
            {'output': 'Orchestrator: renovate verified'},  # renovate code session
            {'output': 'AUTOCONTROLLER_SELECT_ISSUE #%d' % issue},  # OMO selection
            {'actions': [{'gh': ['gh', 'pr', 'create', '--repo', R, '--title', 'Feature A',
                                 '--body', f'Closes #{issue}', '--head-sha', SHA_ISSUE]}],
             'output': 'Orchestrator: fixer completed; oracle reviewed'},
        ])
        drive = self.drive
        # Renovate: code session, then CI pending (no AI, no merge), pass, merge, main green.
        drive('alpha', 1)
        self.assertEqual([s['phase'] for s in self.sessions()], ['code'])
        self.gh.set_checks(R, renovate, RENOVATE_HEAD, [{'name': 'required', 'bucket': 'pending'}])
        drive('alpha', 2)
        self.assertEqual(len(self.sessions()), 1)
        self.assertEqual(self.gh.pr(R, renovate)['state'], 'open')
        self.gh.set_checks(R, renovate, RENOVATE_HEAD, [{'name': 'required', 'bucket': 'pass'}])
        drive('alpha', 2)  # ci -> merge, merge
        self.assertEqual(self.gh.pr(R, renovate)['state'], 'closed')
        merge_sha = self.gh.pr(R, renovate)['merge_commit_sha']
        self.gh.set_run(R, merge_sha, 'queued')
        drive('alpha', 1)  # postmerge pending
        self.assertTrue(self.world.worker('alpha').cycle_state.exists())
        self.gh.set_run(R, merge_sha, 'completed', 'success')
        drive('alpha', 1)  # postmerge green -> complete
        self.assertFalse(self.world.worker('alpha').cycle_state.exists())

        # Backlog Issue: selection (1 AI), coding (1 AI), CI pending, pass, merge, green main.
        drive('alpha', 1)  # selection -> In Progress
        self.assertEqual(self.gh.project_status('example-owner/1', issue), 'In Progress')
        drive('alpha', 1)  # coding session opens the linked PR
        pr = next(p for p in self.gh.read()['repos'][R]['prs'].values()
                  if p['state'] == 'open' and p['head']['sha'] == SHA_ISSUE)
        self.gh.set_checks(R, pr['number'], SHA_ISSUE, [{'name': 'required', 'bucket': 'pending'}])
        drive('alpha', 1)
        self.assertEqual(self.gh.issue(R, issue)['state'], 'open')
        self.gh.set_checks(R, pr['number'], SHA_ISSUE, [{'name': 'required', 'bucket': 'pass'}])
        drive('alpha', 2)
        merged = self.gh.pr(R, pr['number'])
        self.assertEqual(merged['state'], 'closed')
        self.gh.set_run(R, merged['merge_commit_sha'], 'completed', 'success')
        drive('alpha', 1)

        self.assertEqual(self.gh.issue(R, issue)['state'], 'closed')
        self.assertEqual(self.gh.project_status('example-owner/1', issue), 'Done')
        self.assertFalse(self.world.worker('alpha').cycle_state.exists())
        # Exactly three AI sessions: renovate code, one selection, one issue coding.
        self.assertEqual([s['phase'] for s in self.sessions()], ['code', 'selection', 'code'])
        merges = self.gh.mutation_argvs(['gh', 'pr', 'merge'])
        self.assertEqual(len(merges), 2)
        for argv in merges:
            self.assertIn('--match-head-commit', argv)
        self.assertEqual(merges[1][-1], SHA_ISSUE)
        self.assertEqual(merges[0][-1], RENOVATE_HEAD)

    def test_leaked_learning_env_never_reaches_a_selection_session(self):
        """Regression: an ambient AUTOCONTROLLER_LEARNING_INSTRUCTIONS must not grant learning.

        ProjectWorker.env() copies os.environ. A shell that is itself inside an OpenCode session
        can export AUTOCONTROLLER_LEARNING_INSTRUCTIONS. The leak is set here in the test body,
        after setUp, so only the per-iteration removal in World.worker keeps it out of the
        selection session. This test fails without that removal.
        """
        self.world.add_project('alpha', R, 1)
        issue = self.gh.add_issue(R, title='Feature A', body='Implement A', project='example-owner/1',
                                  status='Backlog')
        self.world.script('alpha', [
            {'output': 'AUTOCONTROLLER_SELECT_ISSUE #%d' % issue},
            {'output': 'coding done'},
        ])
        os.environ['AUTOCONTROLLER_LEARNING_INSTRUCTIONS'] = 'leak'
        self.addCleanup(os.environ.pop, 'AUTOCONTROLLER_LEARNING_INSTRUCTIONS', None)
        self.drive('alpha', 2)
        selection, code = self.sessions()
        self.assertEqual(selection['phase'], 'selection')
        self.assertFalse(selection['learning_instructions'],
                         'a leaked env value must not give the selection session a learning grant')

    def test_native_omo_contract_in_every_coding_prompt(self):
        """The coding prompt uses the native OMO roles and invents no custom verdict."""
        self.world.add_project('alpha', R, 1)
        issue = self.gh.add_issue(R, title='Feature A', body='Implement A', project='example-owner/1',
                                  status='Backlog')
        self.world.script('alpha', [
            {'output': 'AUTOCONTROLLER_SELECT_ISSUE #%d' % issue},
            {'output': 'coding done'},
        ])
        self.drive('alpha', 2)
        selection, code = self.sessions()
        for session in (selection, code):
            self.assertEqual(session['agent'], 'orchestrator')
            self.assertEqual(session['title'].split()[0], 'alpha')
        self.assertIn('fixer', code['prompt'])
        self.assertIn('oracle', code['prompt'])
        self.assertNotRegex(code['prompt'], r'(?i)verdict\s*[:=]|REVIEW_?VERDICT|APPROVE_?TOKEN')
        self.assertIn('Do not clone, create git worktrees', code['prompt'],
                      'the agent must be told not to clone or create worktrees')
        self.assertFalse(selection['learning_instructions'], 'selection never gets a learning grant')


class FollowUpAdmissionTests(E2EBase):
    """Follow-up Issues for an authorized task (issue #36): project identity and offered board only.

    SIMULATED: the scripted agent double never runs `gh project item-add`. Here the test
    itself performs the admission the work prompt asks the orchestrator to make. The live
    agent's behaviour is NOT proven by these tests; prompt-contract coverage lives in
    tests/test_omo_roles.py.
    """

    def test_each_project_coding_prompt_names_only_its_own_project(self):
        self.world.add_project('alpha', R, 1)
        beta = 'example-owner/beta'
        self.world.add_project('beta', beta, 2)
        issue_a = self.gh.add_issue(R, title='Alpha task', body='a', project='example-owner/1', status='Backlog')
        issue_b = self.gh.add_issue(beta, title='Beta task', body='b', project='example-owner/2', status='Backlog')
        self.world.script('alpha', [{'output': 'AUTOCONTROLLER_SELECT_ISSUE #%d' % issue_a}, {'output': 'alpha done'}])
        self.world.script('beta', [{'output': 'AUTOCONTROLLER_SELECT_ISSUE #%d' % issue_b}, {'output': 'beta done'}])
        self.drive('alpha', 2)
        self.drive('beta', 2)

        code = {s['project']: s['prompt'] for s in self.sessions('code')}
        self.assertEqual(set(code), {'alpha', 'beta'})
        alpha, beta_prompt = code['alpha'], code['beta']
        self.assertIn(f'Configured GitHub Project for {R}: owner `example-owner`, number 1', alpha)
        self.assertIn('gh project item-add 1 --owner example-owner --url <issue-url>', alpha)
        self.assertNotIn('item-add 2 ', alpha)
        self.assertNotIn(beta, alpha)
        self.assertIn(f'Configured GitHub Project for {beta}: owner `example-owner`, number 2', beta_prompt)
        self.assertIn('gh project item-add 2 --owner example-owner --url <issue-url>', beta_prompt)
        self.assertNotIn('item-add 1 ', beta_prompt)
        self.assertNotIn(R, beta_prompt)

    def test_admitted_follow_up_is_offered_on_next_selection_and_unadmitted_issues_are_not(self):
        """SIMULATED: the admission is seeded by the test, not made by a live agent.

        The admitted follow-up is placed in the Project by the test, standing in for the
        orchestrator's `item-add`. A same-account Issue without Project membership and an
        outsider Issue (association NONE) are also on the world. One selection must offer only
        the admitted follow-up. The live agent's behaviour is NOT proven here; prompt-contract
        coverage lives in tests/test_omo_roles.py.
        """
        self.world.add_project('alpha', R, 1)
        follow_up = self.gh.add_issue(R, title='Follow-up: cleanup', body='Found while fixing A.',
                                      project='example-owner/1', status='Backlog')
        unadmitted = self.gh.add_issue(R, title='Unrelated idea', body='x')
        outsider = self.gh.add_issue(R, title='Outsider request', body='please do X', association='NONE',
                                     author={'login': 'example-outsider', 'type': 'User', 'id': 9009})
        self.world.script('alpha', [{'output': 'AUTOCONTROLLER_NO_ACTION'}])
        self.drive('alpha', 1)

        prompt = self.sessions('selection')[0]['prompt']
        self.assertIn(self.gh.issue(R, follow_up)['html_url'], prompt)
        self.assertNotIn(self.gh.issue(R, unadmitted)['html_url'], prompt)
        self.assertNotIn(self.gh.issue(R, outsider)['html_url'], prompt)
        self.assertFalse([a for a in self.gh.mutation_argvs() if 'item-add' in a])


class ReviewAndCiGateTests(E2EBase):
    """Scenario 2: draft/review, pending/failed CI, exact-SHA merge, red main, repair PR."""

    def test_draft_pending_and_failed_ci_block_merge_until_exact_green_head_then_main_red_repairs(self):
        self.world.add_project('alpha', R, 1)
        renovate = self.gh.add_pr(R, title='Bump dep', body='', head_sha=RENOVATE_HEAD,
                                  head_ref='renovate/dep', user=ew.eg.RENOVATE_USER, draft=True)
        drive = self.drive
        self.world.script('alpha', [
            {'output': 'session 1: left PR as draft'},
            {'actions': [{'gh': ['gh', 'pr', 'ready', str(renovate), '--repo', R]}],
             'output': 'session 2: oracle approved; marked ready'},
            {'actions': [{'gh': ['gh', 'pr', 'sync', str(renovate), '--repo', R, '--head-sha', SHA_FIX]}],
             'output': 'session 3: fixer pushed correction'},
            {'actions': [{'gh': ['gh', 'pr', 'create', '--repo', R, '--title', 'Repair main',
                                 '--body', f'Autocontroller-Repair-For: #{renovate}\nfix main',
                                 '--head-sha', REPAIR_SHA]}],
             'output': 'repair session: repair PR opened'},
        ])

        # 1. Draft PR: the agent runs once, the PR stays draft, nothing merges.
        drive('alpha', 1)
        self.assertEqual(self.gh.pr(R, renovate)['draft'], True)
        self.assertEqual(self.checkpoint()['phase'], 'code')
        self.assertIn('draft', self.checkpoint().get('review_feedback', ''))
        self.assertEqual(self.merges(), [])

        # 2. Ready for review, but CI has not reported a required check: no merge.
        self.expire_retries('alpha')
        drive('alpha', 1)
        self.assertEqual(len(self.sessions('code')), 2)
        self.assertFalse(self.gh.pr(R, renovate)['draft'])
        self.gh.set_checks(R, renovate, RENOVATE_HEAD, [{'name': 'required', 'bucket': 'pending'}])
        drive('alpha', 3)
        self.assertEqual(self.checkpoint()['phase'], 'ci')
        self.assertEqual(len(self.sessions()), 2, 'pending CI must not start an AI session')
        self.assertEqual(self.merges(), [])

        # 3. Required check fails: back to code with feedback, still no merge.
        self.gh.set_checks(R, renovate, RENOVATE_HEAD, [{'name': 'required', 'bucket': 'fail'}])
        drive('alpha', 1)
        self.assertEqual(self.checkpoint()['phase'], 'code')
        self.assertIn('CI failed', self.checkpoint()['review_feedback'])
        self.assertEqual(self.merges(), [])

        # 4. Corrective session pushes a new head; the OLD green/red state must not apply.
        self.expire_retries('alpha')
        self.gh.set_checks(R, renovate, SHA_FIX, [{'name': 'required', 'bucket': 'pass'}])
        drive('alpha', 2)
        self.assertEqual(self.gh.pr(R, renovate)['head']['sha'], SHA_FIX)
        self.assertEqual(self.merges(), [])
        self.assertEqual(len(self.sessions('code')), 3)

        # 5. Exact head green: merge pinned to that SHA with branch-protected squash.
        drive('alpha', 2)
        merged = self.gh.pr(R, renovate)
        self.assertEqual(merged['state'], 'closed')
        self.assertEqual(self.merges()[-1][-1], SHA_FIX)
        self.assertIn('--match-head-commit', self.merges()[-1])

        # 6. Merged main commit is red: a repair task, never Done or merge-as-is.
        self.gh.set_run(R, merged['merge_commit_sha'], 'completed', 'failure')
        drive('alpha', 1)
        self.assertEqual(self.checkpoint()['phase'], 'repair')
        self.expire_retries('alpha')

        # 7. Repair session opens a protected repair PR; it is gated like any other PR.
        drive('alpha', 1)
        repair = next(p for p in self.gh.read()['repos'][R]['prs'].values()
                      if p['head']['sha'] == REPAIR_SHA)
        self.assertEqual(self.checkpoint()['pr_number'], repair['number'])
        self.assertEqual(self.checkpoint()['phase'], 'ci')
        self.assertEqual(len(self.merges()), 1, 'repair PR must not be merged before CI')
        self.gh.set_checks(R, repair['number'], REPAIR_SHA, [{'name': 'required', 'bucket': 'pass'}])
        drive('alpha', 2)
        self.assertEqual(self.gh.pr(R, repair['number'])['state'], 'closed')
        self.assertEqual(self.merges()[-1][-1], REPAIR_SHA)

        # 8. Main CI on the repair merge is green; the original task completes exactly once.
        self.gh.set_run(R, self.gh.pr(R, repair['number'])['merge_commit_sha'], 'completed', 'success')
        drive('alpha', 1)
        self.assertIsNone(self.checkpoint())
        self.assertEqual([s['phase'] for s in self.sessions()],
                         ['code', 'code', 'code', 'repair'])
        self.assertEqual(len(self.merges()), 2)


class KnownFailureRecurrenceTests(E2EBase):
    """Scenario 3: a known failure recurs; a lesson from another task is searched and confirmed."""

    def setUp(self):
        super().setUp()
        self.runtime = self.world.start_engram()
        self.addCleanup(self.world.stop_engram)

    def memory_results(self) -> list[dict]:
        """Every learning-CLI outcome the fake agent produced, in order."""
        path = self.world.e2e / 'invocations.jsonl'
        rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line]
        return [r for r in rows if r.get('kind') == 'memory']

    def test_recurring_failure_reuses_lesson_and_shared_promotion_needs_independent_repo(self):
        self.world.add_project('alpha', R, 1)
        second = 'example-owner/beta'
        self.world.add_project('beta', second, 2)
        issue_a = self.gh.add_issue(R, title='Alpha bug', body='x', project='example-owner/1', status='Backlog')
        issue_b = self.gh.add_issue(second, title='Beta bug', body='y', project='example-owner/2', status='Backlog')
        # Cross-repo reuse only travels through the shared scope, which requires
        # transferable=True and a generalized lesson that does not name its source.
        lesson = {'title': 'Verify cwd before edits', 'trigger': 'working directory mismatch before edit',
                  'lesson': 'Verify the working directory before modifying files.',
                  'proof': 'Reproduced the wrong cwd and verified the corrected path.',
                  'evidence': f'https://github.com/{R}/pull/9', 'transferable': True}
        self.world.script('alpha', [
            {'output': 'AUTOCONTROLLER_SELECT_ISSUE #%d' % issue_a},
            {'memory': [
                {'action': 'search', 'arg': 'working directory mismatch'},
                {'action': 'record', 'arg': lesson},
                # A different query, so the grant's query cache does not hide the candidate.
                {'action': 'search', 'arg': 'cwd mismatch'},
                # Self-confirmation by the origin task must be refused.
                {'action': 'feedback', 'arg': {'id': '$first_returned', 'scope': 'local', 'result': 'confirmed',
                                               'evidence': f'https://github.com/{R}/pull/10',
                                               'reason': 'Self-check of my own lesson.'}},
            ], 'output': 'failed once, recorded lesson'},
        ])
        self.world.script('beta', [
            {'output': 'AUTOCONTROLLER_SELECT_ISSUE #%d' % issue_b},
            {'memory': [
                {'action': 'search', 'arg': 'working directory mismatch'},
                {'action': 'feedback', 'arg': {'id': '$first_returned', 'scope': 'shared', 'result': 'confirmed',
                                               'evidence': f'https://github.com/{second}/pull/3',
                                               'reason': 'Reproduced the same cwd mismatch and verified the fix.'}},
            ], 'output': 'same failure in beta'},
        ])
        self.drive('alpha', 2)
        alpha_ops = self.memory_results()[0]['results']
        self.assertTrue(alpha_ops[1]['ok'])
        self.assertEqual(alpha_ops[1]['result']['status'], 'candidate')
        self.assertTrue(alpha_ops[2]['ok'])
        self.assertEqual(alpha_ops[2]['result'][0]['id'], alpha_ops[1]['result']['id'])
        self.assertFalse(alpha_ops[3]['ok'], 'origin task must not confirm its own lesson')
        self.assertIn('Independent task evidence required', alpha_ops[3]['error'])

        self.drive('beta', 2)
        beta_ops = self.memory_results()[1]['results']
        self.assertTrue(beta_ops[0]['ok'])
        self.assertEqual(beta_ops[0]['result'][0]['lesson'], lesson['lesson'])
        self.assertNotIn('proof', beta_ops[0]['result'][0], 'shared search must not reveal proof text')
        self.assertTrue(beta_ops[1]['ok'], beta_ops[1])
        self.assertEqual(beta_ops[1]['result']['status'], 'shared',
                         'a single independent repo promotes shared scope, not confirmed local')

        # Only one session per repo ran, so no extra AI sessions were started for memory.
        self.assertEqual([s['phase'] for s in self.sessions()], ['selection', 'code', 'selection', 'code'])
        # Learning is granted to coding sessions and never to selection.
        for session in self.sessions():
            self.assertEqual(session['learning_instructions'], session['phase'] != 'selection')

    def test_same_repo_task_cannot_reach_shared_copy_so_no_same_repo_shared_promotion(self):
        """A second task in the same repository cannot confirm the shared copy of a lesson.

        Why this holds, from memory.operation(): `search` visits the local scope first, then the
        shared scope, and skips any entry whose key was already returned (`seen`). So a same-repo
        task that finds the lesson locally never receives the shared copy. Feedback with
        `scope: shared` then requires a `[shared, id]` pair in the session's `returned` list, which
        is never added, so it is refused ('Feedback requires a result returned in this session').
        The shared row therefore keeps its `candidate` status.

        This does NOT exercise the origin rule in memory.py (a shared confirmation promotes to
        `shared` only when some origin is in another repository). That branch is reached only when
        a different repository's task can see the shared copy; it is not covered by this test.
        """
        self.world.add_project('alpha', R, 1)
        issue_a = self.gh.add_issue(R, title='Alpha bug one', body='x', project='example-owner/1', status='Backlog')
        issue_c = self.gh.add_issue(R, title='Alpha bug two', body='z', project='example-owner/1', status='Backlog')
        lesson = {'title': 'Verify cwd before edits', 'trigger': 'working directory mismatch before edit',
                  'lesson': 'Verify the working directory before modifying files.',
                  'proof': 'Reproduced the wrong cwd and verified the corrected path.',
                  'evidence': f'https://github.com/{R}/pull/9', 'transferable': True}
        self.world.script('alpha', [
            {'output': 'AUTOCONTROLLER_SELECT_ISSUE #%d' % issue_a},
            {'memory': [{'action': 'search', 'arg': 'working directory mismatch'},
                        {'action': 'record', 'arg': lesson}], 'output': 'recorded lesson'},
            {'output': 'AUTOCONTROLLER_SELECT_ISSUE #%d' % issue_c},
            {'memory': [{'action': 'search', 'arg': 'working directory mismatch'},
                        {'action': 'feedback', 'arg': {'id': '$first_returned', 'scope': 'shared',
                                                       'result': 'confirmed',
                                                       'evidence': f'https://github.com/{R}/pull/11',
                                                       'reason': 'Reproduced the same cwd mismatch in this repo.'}}],
             'output': 'same repo, second task'},
        ])
        self.drive('alpha', 4)
        rows = [json.loads(line) for line in (self.world.e2e / 'invocations.jsonl').read_text(encoding='utf-8').splitlines()
                if line]
        ops = [r for r in rows if r.get('kind') == 'memory']
        second = ops[1]['results']
        # The search returned the lesson from the LOCAL scope (same repository). A feedback that
        # names scope 'shared' for it is not a returned pair, so it is refused before any promotion.
        self.assertTrue(second[0]['ok'])
        self.assertEqual(second[0]['result'][0]['scope'], 'local')
        self.assertFalse(second[1]['ok'], 'the shared-scope feedback is not a returned pair and is refused')
        self.assertIn('Feedback requires a result returned in this session', second[1]['error'])
        # The same-repo search never reached the shared copy: its stored row is still a candidate.
        import memory as learning
        grant = {'repo': R, 'kind': 'issue', 'number': 99, 'used': 0, 'queries': [], 'records': 0,
                 'feedback': 0, 'returned': []}
        found = learning.operation(self.world.runtime.api, grant, 'search', 'working directory mismatch')
        self.assertTrue(found, 'the lesson is still visible to its own repository')
        self.assertEqual(found[0]['scope'], 'local')
        self.assertNotEqual(found[0]['status'], 'shared', 'same-repo activity never promotes to shared')
        # Read the shared copy straight from the stand-in store, by the same key record() computes.
        import hashlib
        import re
        key = hashlib.sha256(re.sub(r'\W+', ' ', (lesson['trigger'] + ' ' + lesson['lesson']).casefold())
                             .strip().encode()).hexdigest()
        shared_hits = [r for r in self.world.runtime.api.search(learning.SHARED, key)
                       if r.get('topic_key') == key]
        self.assertEqual(len(shared_hits), 1, 'the shared copy exists in the stand-in store')
        _, shared_entry = self.world.runtime.api.get(learning.SHARED, shared_hits[0]['id'])
        self.assertEqual(shared_entry['status'], 'candidate', 'the shared row stays a candidate')


class CiFailureBackoffTests(E2EBase):
    """Scenario 3 (backoff half): a repeating required-CI failure respects the persistent window."""

    def test_repeated_required_ci_failure_backs_off_across_restarts_then_retries_once(self):
        self.world.add_project('alpha', R, 1)
        pr = self.gh.add_pr(R, title='Ready', body='', head_sha=SHA_FIX, head_ref='renovate/ready',
                            user=ew.eg.RENOVATE_USER)
        self.gh.set_checks(R, pr, SHA_FIX, [{'name': 'required', 'bucket': 'fail'}])
        self.world.script('alpha', [{'output': 'renovate verified'}, {'output': 'fixer fixed'}])
        self.restart().one_iteration()  # code session 1
        self.restart().one_iteration()  # ci: required failed -> back to code, retry window opens
        self.assertEqual(self.checkpoint()['phase'], 'code')
        self.assertEqual(len(self.sessions('code')), 1)
        key = f'renovate:{pr}'
        window = json.loads(self.restart().retries_file.read_text())[key]
        self.assertGreater(window['until'], 0)

        # Inside the window, neither a live worker nor a restarted worker starts an AI session.
        for _ in range(3):
            self.restart().one_iteration()
        self.assertEqual(len(self.sessions()), 1, 'the backoff window blocks new AI sessions')
        self.assertEqual(json.loads(self.restart().retries_file.read_text())[key]['until'], window['until'],
                         'the window is persisted, not reset by restarts')

        # Window expires: exactly one new session runs, then the window is set again.
        self.expire_retries('alpha')
        self.restart().one_iteration()
        self.assertEqual(len(self.sessions('code')), 2, 'exactly one new session after expiry')
        self.assertEqual(self.checkpoint()['phase'], 'ci')
        self.assertEqual(self.merges(), [], 'a failing CI never merges')


class MidFlowEngramTests(E2EBase):
    """Scenario 6 (mid-flow): Engram dies while a task waits at ci; restart keeps the checkpoint."""

    def setUp(self):
        super().setUp()
        self.world.start_engram()
        self.addCleanup(self.world.stop_engram)

    def test_engram_death_at_ci_fails_closed_then_startup_restart_completes_without_repeating_ai(self):
        import memory as learning
        self.world.add_project('alpha', R, 1)
        pr = self.gh.add_pr(R, title='Ready', body='', head_sha=SHA_FIX, head_ref='renovate/ready',
                            user=ew.eg.RENOVATE_USER)
        self.gh.set_checks(R, pr, SHA_FIX, [{'name': 'required', 'bucket': 'pass'}])
        self.world.script('alpha', [{'output': 'renovate verified'}])
        self.step_until('ci')  # code session done; the task waits at the merge gate
        sessions_before = len(self.sessions())
        grant = {'repo': R, 'kind': 'renovate', 'number': pr, 'used': 0, 'queries': [], 'records': 0,
                 'feedback': 0, 'returned': []}
        entry = {'title': 'Verify cwd before edits', 'trigger': 'working directory mismatch',
                 'lesson': 'Verify the working directory before modifying files.',
                 'proof': 'Reproduced the wrong cwd and verified the corrected path.',
                 'evidence': f'https://github.com/{R}/pull/{pr}', 'transferable': False}
        first = learning.operation(self.world.runtime.api, grant, 'record', entry)

        # Engram dies. The next iteration fails closed before the merge call.
        self.world.runtime.proc.kill()
        self.world.runtime.proc.wait(timeout=15)
        with self.assertRaises(learning.MemoryError):
            self.restart().one_iteration()
        self.assertTrue(self.world.runtime.failed.exists(), 'the failure marker is recorded')
        self.assertEqual(self.merges(), [], 'no merge while memory is down')
        self.assertEqual(self.checkpoint()['phase'], 'ci', 'the checkpoint is preserved')
        self.assertEqual(len(self.sessions()), sessions_before)

        # Restart through the real startup path: it validates the store, clears the failure
        # marker and starts Engram again on the same data directory.
        self.world.restart_engram_via_startup()
        self.assertFalse(self.world.runtime.failed.exists(), 'startup clears the failure marker')
        found = learning.operation(self.world.runtime.api,
                                   dict(grant, queries=[], returned=[], used=0), 'search', 'working directory')
        self.assertEqual([r['id'] for r in found], [first['id']], 'learned entry persisted across the restart')
        # The CLI path, as a session uses it, must work after the restart too: the launcher needs
        # E2E_MEMORY_ROOT and E2E_ENGRAM_SOCKET republished by the restart, not only the runtime.
        import e2e_agent as agent
        pdir = self.world.state / 'projects' / 'alpha'
        pdir.mkdir(parents=True, exist_ok=True)
        # The same grant creation the controller uses, and the instruction text a session gets.
        token = learning.grant(self.world.state, pdir, R, 'renovate', pr, pending=False)
        script, grant_arg, session_token = agent.learning_grant_from_instructions(learning.instruction(pdir, token))
        cli = agent.memory_cli(script, grant_arg, session_token, 'search', 'working directory',
                               os.environ['E2E_ENGRAM_SOCKET'])
        self.assertTrue(cli['ok'], f"CLI search after restart failed: {cli.get('error')}")
        self.assertEqual([r['id'] for r in cli['result']], [first['id']])

        # The new worker continues the same task: merge, postmerge, complete. No AI work repeats.
        self.step_until('postmerge')
        self.assertEqual(len(self.merges()), 1)
        self.gh.set_run(R, self.gh.pr(R, pr)['merge_commit_sha'], 'completed', 'success')
        self.restart().one_iteration()
        self.assertIsNone(self.checkpoint())
        self.assertEqual(len(self.merges()), 1)
        self.assertEqual(len(self.sessions()), sessions_before, 'no AI work repeated after restart')


class MemoryScopeTests(E2EBase):
    """Scenario 4 (memory half): per-repo scope isolation and search budgets, through the real operation()."""

    def setUp(self):
        super().setUp()
        self.world.start_engram()
        self.addCleanup(self.world.stop_engram)

    @staticmethod
    def _grant(repo: str, number: int) -> dict:
        return {'repo': repo, 'kind': 'issue', 'number': number, 'used': 0, 'queries': [], 'records': 0,
                'feedback': 0, 'returned': [], 'token': 'x'}

    def _record(self, repo: str, number: int, title: str, lesson: str, transferable: bool) -> dict:
        import memory as learning
        entry = {'title': title, 'trigger': 'cwd mismatch ' + title, 'lesson': lesson,
                 'proof': 'Reproduced and verified the fix in this repository.',
                 'evidence': f'https://github.com/{repo}/pull/{number}', 'transferable': transferable}
        return learning.operation(self.world.runtime.api, self._grant(repo, number), 'record', entry)

    def test_local_lesson_never_reaches_another_repo_and_budgets_hold(self):
        import memory as learning
        alpha, beta = R, 'example-owner/beta'
        local = self._record(alpha, 9, 'Alpha local only', 'Alpha-only checkout detail lesson.', False)
        self.assertEqual(local['status'], 'candidate')
        for i in range(6):  # several shared lessons, to exercise the 3-result cap
            self._record(alpha, 20 + i, f'Shared generalized {i}',
                         f'Generic cwd verification lesson number {i}.', True)

        beta_grant = self._grant(beta, 1)
        found = learning.operation(self.world.runtime.api, beta_grant, 'search', 'cwd mismatch')
        self.assertNotIn(local['id'], [r['id'] for r in found], 'a local lesson must never be returned to beta')
        self.assertTrue(all(r['scope'] == 'shared' for r in found), 'beta sees only shared-scope results')
        self.assertLessEqual(len(found), 3)
        self.assertLessEqual(len(json.dumps(found, ensure_ascii=False).encode()), 750)
        self.assertNotIn('proof', found[0] if found else {}, 'shared search must not reveal proof text')

        # Per-grant byte budget: keep searching with distinct queries until the grant is spent.
        for i in range(10):
            learning.operation(self.world.runtime.api, beta_grant, 'search', f'cwd mismatch variant {i}')
        self.assertLessEqual(beta_grant['used'], 2000, 'the per-grant byte budget is 2000')
        self.assertLessEqual(len(beta_grant['queries']), 8, 'query budget is 8 per grant')

    def test_byte_budget_is_what_empties_the_search_once_the_grant_is_spent(self):
        # Minor 1: fresh lessons per query, so only the byte budget (not the query cap) can empty it.
        import memory as learning
        for i in range(12):
            self._record(R, 40 + i, f'Budget lesson {i}', f'Budget filler lesson {i} about cwd mismatch.', True)
        grant = self._grant('example-owner/beta', 2)
        empty_at = None
        for i in range(8):  # the 8-query cap is never reached with distinct queries below the byte cap
            found = learning.operation(self.world.runtime.api, grant, 'search', f'cwd budget query {i}')
            if found == [] and empty_at is None:
                empty_at = i
                used_at_empty = grant['used']
        self.assertIsNotNone(empty_at, 'the grant must run out before the query cap')
        self.assertLess(empty_at, 8, 'query cap (8) was not the limiting factor')
        # Each result is capped by min(750, 2000 - used): once the grant is nearly spent, nothing fits.
        # The first empty result is therefore reached with used within one maximum result of the cap.
        self.assertGreater(used_at_empty, 2000 - 750, 'empty because the byte budget is nearly spent')
        self.assertLessEqual(used_at_empty, 2000)

    def test_each_repo_grant_only_sees_its_own_local_scope(self):
        import memory as learning
        alpha_hit = self._record(R, 30, 'Alpha scoped', 'Alpha scoped lesson about cwd.', False)
        beta_repo = 'example-owner/beta'
        beta_hit = self._record(beta_repo, 31, 'Beta scoped', 'Beta scoped lesson about cwd.', False)
        alpha_found = learning.operation(self.world.runtime.api, self._grant(R, 32), 'search', 'cwd')
        beta_found = learning.operation(self.world.runtime.api, self._grant(beta_repo, 33), 'search', 'cwd')
        self.assertIn(alpha_hit['id'], [r['id'] for r in alpha_found])
        self.assertNotIn(beta_hit['id'], [r['id'] for r in alpha_found])
        self.assertIn(beta_hit['id'], [r['id'] for r in beta_found])
        self.assertNotIn(alpha_hit['id'], [r['id'] for r in beta_found])


class MemoryOutageTests(E2EBase):
    """Scenario 6 (memory half): a dead Engram fails closed; records survive a restart."""

    def setUp(self):
        super().setUp()
        self.world.start_engram()
        self.addCleanup(self.world.stop_engram)

    def test_engram_process_death_blocks_new_work_before_any_ai_session(self):
        import memory as learning
        self.world.add_project('alpha', R, 1)
        self.gh.add_issue(R, title='Feature M', body='m', project='example-owner/1', status='Backlog')
        runtime = self.world.runtime
        runtime.proc.kill()
        runtime.proc.wait(timeout=15)
        with self.assertRaises(learning.MemoryError):
            self.world.worker('alpha').one_iteration()
        self.assertTrue(runtime.failed.exists(), 'the failure is recorded for the next startup')
        self.assertEqual(self.sessions(), [], 'no AI session may start while memory is down')
        self.assertEqual(self.gh.mutations(), [])

    def test_learning_survives_engram_restart_from_the_sqlite_store(self):
        import memory as learning
        self.world.add_project('alpha', R, 1)
        grant = {'repo': R, 'kind': 'issue', 'number': 1, 'used': 0, 'queries': [], 'records': 0,
                 'feedback': 0, 'returned': []}
        entry = {'title': 'Verify cwd before edits', 'trigger': 'working directory mismatch',
                 'lesson': 'Verify the working directory before modifying files.',
                 'proof': 'Reproduced the wrong cwd and verified the corrected path.',
                 'evidence': f'https://github.com/{R}/pull/9', 'transferable': False}
        first = learning.operation(self.world.runtime.api, grant, 'record', entry)
        self.world.stop_engram()
        self.world.start_engram()
        found = learning.operation(self.world.runtime.api, dict(grant, number=2, queries=[], returned=[]),
                                   'search', 'working directory')
        self.assertEqual([r['id'] for r in found], [first['id']])
        self.assertEqual(found[0]['lesson'], entry['lesson'])


class IdleBoardTests(E2EBase):
    """Minor 2: a truly idle project (no work anywhere) costs zero AI sessions and zero mutations."""

    ITERATIONS = 4

    def _assert_fully_idle(self):
        self.expire_retries('alpha')
        self.drive('alpha', self.ITERATIONS)
        self.assertEqual(self.sessions(), [], 'an idle board must never start an AI session')
        self.assertEqual(self.gh.mutations(), [], 'an idle board must never mutate GitHub')
        self.assertIsNone(self.checkpoint(), 'an idle board leaves no checkpoint behind')

    def test_empty_board_makes_no_session_and_no_mutation_over_several_iterations(self):
        self.world.add_project('alpha', R, 1)
        self._assert_fully_idle()

    def test_all_terminal_board_makes_no_session_and_no_mutation(self):
        self.world.add_project('alpha', R, 1)
        self.gh.add_issue(R, title='Shipped feature', body='done', project='example-owner/1', status='Done')
        self.gh.add_issue(R, title='Dropped idea', body='no', project='example-owner/1', status='Done')
        self._assert_fully_idle()


class ConcurrentProjectsTests(E2EBase):
    """Scenario 4: two independent projects run concurrently without crossing state."""

    def test_two_projects_keep_separate_checkpoints_and_never_cross_merge(self):
        self.world.add_project('alpha', R, 1)
        beta = 'example-owner/beta'
        self.world.add_project('beta', beta, 2)
        pa = self.gh.add_pr(R, title='A bump', body='', head_sha='a1' * 20, head_ref='renovate/a',
                            user=ew.eg.RENOVATE_USER)
        pb = self.gh.add_pr(beta, title='B bump', body='', head_sha='b1' * 20, head_ref='renovate/b',
                            user=ew.eg.RENOVATE_USER)
        self.world.script('alpha', [{'output': 'alpha renovate verified'}])
        self.world.script('beta', [{'output': 'beta renovate verified'}])
        # Hold alpha's CI pending while beta's is green: beta must not inherit alpha's state.
        self.gh.set_checks(R, pa, 'a1' * 20, [{'name': 'required', 'bucket': 'pending'}])
        self.gh.set_checks(beta, pb, 'b1' * 20, [{'name': 'required', 'bucket': 'pass'}])

        # Each project runs on its own thread with its own worker, against the shared
        # fake GitHub and quota lock. Iterations within one project stay sequential.
        errors = self.run_concurrently({'alpha': 3, 'beta': 4})
        self.assertEqual(errors, [])

        self.assertEqual(self.gh.pr(R, pa)['state'], 'open', 'alpha stays unmerged while its CI is pending')
        self.assertEqual(self.gh.pr(beta, pb)['state'], 'closed')
        merges = self.gh.mutation_argvs(['gh', 'pr', 'merge'])
        self.assertEqual([m[m.index('--repo') + 1] for m in merges], [beta])
        # Each project keeps only its own checkpoint: alpha still waits on CI, beta waits on main CI.
        self.assertEqual(self.checkpoint('alpha')['phase'], 'ci')
        self.assertEqual(self.checkpoint('alpha')['pr_number'], pa)
        self.assertEqual(self.checkpoint('beta')['phase'], 'postmerge')
        self.assertEqual(self.checkpoint('beta')['pr_number'], pb)
        self.assertEqual({s['project'] for s in self.sessions('code')}, {'alpha', 'beta'})


class IdleAndSelectionTests(E2EBase):
    """Scenario 5: an idle backlog and an OMO NO_ACTION decision cost no further AI sessions."""

    def test_no_action_decision_stops_ai_until_the_board_changes(self):
        self.world.add_project('alpha', R, 1)
        issue = self.gh.add_issue(R, title='Vague idea', body='tbd', project='example-owner/1', status='Backlog')
        self.world.script('alpha', [{'output': 'AUTOCONTROLLER_NO_ACTION'}])
        self.drive('alpha', 1)
        self.assertEqual(len(self.sessions('selection')), 1)
        self.assertEqual(self.gh.mutations(), [])
        self.assertIsNone(self.checkpoint())

        # Unchanged board: idle polling must not start a second selection.
        self.expire_retries('alpha')
        self.drive('alpha', 3)
        self.assertEqual(len(self.sessions()), 1, 'idle board must not start an AI session')

        # A real change to the board wakes triage exactly once; with no script left the fake fails loudly.
        self.gh.set_project_status('example-owner/1', issue, 'Review')
        self.world.script('alpha', [{'output': 'AUTOCONTROLLER_NO_ACTION'}])
        self.drive('alpha', 1)
        self.assertEqual(len(self.sessions('selection')), 2)


class OutageTests(E2EBase):
    """Scenario 6: GitHub outage fails closed; restart keeps the persisted decision."""

    def test_outage_after_selection_keeps_decision_and_restart_does_not_reselect(self):
        self.world.add_project('alpha', R, 1)
        issue = self.gh.add_issue(R, title='Feature Z', body='z', project='example-owner/1', status='Backlog')
        # Selection succeeds; then the outage starts before the controller can claim the Issue.
        self.world.script('alpha', [{'output': 'AUTOCONTROLLER_SELECT_ISSUE #%d' % issue,
                                     'outage_after': True}])
        with self.assertRaises(ew.ctl.ControllerError):
            self.world.worker('alpha').one_iteration()  # claim needs GitHub, which is now down
        self.assertEqual(self.checkpoint()['decision'], 'issue', 'decision persisted before GitHub is needed')
        self.assertEqual(self.gh.project_status('example-owner/1', issue), 'Backlog')
        self.assertEqual(len(self.sessions('selection')), 1)

        # The outage stays up: iterations raise (run() would back off); no AI or mutation occurs.
        for _ in range(2):
            self.expire_retries('alpha')
            with self.assertRaises(ew.ctl.ControllerError):
                self.world.worker('alpha').one_iteration()
        self.assertEqual(len(self.sessions('selection')), 1, 'outage must not trigger another paid selection')
        self.assertEqual(len(self.sessions('code')), 0)

        # Restart: clear the outage; the persisted decision is applied, not recomputed.
        self.gh.set_outage(False)
        self.expire_retries('alpha')
        self.drive('alpha', 1)
        self.assertEqual(len(self.sessions('selection')), 1, 'no duplicate paid selection after restart')
        self.assertEqual(self.gh.project_status('example-owner/1', issue), 'In Progress')

    def test_outage_before_any_ai_session_fails_closed_with_no_mutations(self):
        self.world.add_project('alpha', R, 1)
        self.gh.add_issue(R, title='Feature Q', body='q', project='example-owner/1', status='Backlog')
        self.gh.set_outage(True)
        with self.assertRaises(ew.ctl.ControllerError):
            self.world.worker('alpha').one_iteration()
        self.gh.set_outage(False)
        self.assertEqual(self.sessions(), [])
        self.assertEqual(self.gh.mutations(), [])
        self.assertIsNone(self.checkpoint())


class CheckpointAndQuotaTests(E2EBase):
    """Scenario 7: checkpoint recovery and shared quota cooldown across projects."""

    def test_quota_429_on_one_project_pauses_all_ai_then_recovers(self):
        self.world.add_project('alpha', R, 1)
        beta = 'example-owner/beta'
        self.world.add_project('beta', beta, 2)
        self.gh.add_issue(R, title='Feature A', body='a', project='example-owner/1', status='Backlog')
        self.gh.add_issue(beta, title='Feature B', body='b', project='example-owner/2', status='Backlog')
        self.world.script('alpha', [{'output': 'HTTP 429 quota exceeded', 'rc': 1}])
        self.world.script('beta', [{'output': 'AUTOCONTROLLER_NO_ACTION'}])
        self.drive('alpha', 1)
        cooldown = json.loads((self.world.state / 'ai-cooldown.json').read_text(encoding='utf-8'))
        self.assertGreater(cooldown['until'], 0)
        # While the shared cooldown is active, beta must not start any AI session.
        self.drive('beta', 1)
        self.assertEqual([s['project'] for s in self.sessions()], ['alpha'])

        # After the cooldown expires, recovery resumes the same task without a duplicate.
        cooldown['until'] = 0
        (self.world.state / 'ai-cooldown.json').write_text(json.dumps(cooldown), encoding='utf-8')
        self.expire_retries('alpha')
        self.world.script('alpha', [{'output': 'AUTOCONTROLLER_NO_ACTION'}])
        self.drive('alpha', 1)
        self.assertEqual([s['project'] for s in self.sessions()].count('alpha'), 2)

    def test_quota_during_coding_keeps_checkpoint_and_resumes_same_task_once(self):
        self.world.add_project('alpha', R, 1)
        issue = self.gh.add_issue(R, title='Feature Q', body='q', project='example-owner/1', status='Backlog')
        marker = self.world.state / 'projects' / 'alpha' / 'cycle.pending'
        self.world.script('alpha', [
            {'output': 'AUTOCONTROLLER_SELECT_ISSUE #%d' % issue},
            # Quota hit before OpenCode created a session: the marker stays "starting" with no session id.
            {'output': 'HTTP 429 quota exceeded', 'rc': 1, 'marker': 'starting'},
        ])
        self.drive('alpha', 2)
        # The unstarted marker was verified and cleared; the same task stays checkpointed for retry.
        self.assertFalse(marker.exists(), 'an unstarted quota marker must be cleared, not left pending')
        clears = [json.loads(line) for line in (self.world.e2e / 'invocations.jsonl').read_text().splitlines()
                  if '"kind": "clear_unstarted"' in line]
        self.assertEqual([c['rc'] for c in clears], [0], 'the controller verified the clear with the wrapper')
        checkpoint = self.checkpoint()
        self.assertEqual((checkpoint['kind'], checkpoint['number']), ('issue', issue))
        self.assertTrue(checkpoint.get('retry_same_task'))
        self.assertGreater(json.loads((self.world.state / 'ai-cooldown.json').read_text())['until'], 0)
        first_code = self.sessions('code')[0]

        # Still cooling down: nothing new starts, and the task is not reselected.
        self.drive('alpha', 2)
        self.assertEqual(len(self.sessions()), 2)

        # Cooldown over: the SAME task resumes exactly once, with an identical prompt.
        cooldown = json.loads((self.world.state / 'ai-cooldown.json').read_text())
        cooldown['until'] = 0
        (self.world.state / 'ai-cooldown.json').write_text(json.dumps(cooldown), encoding='utf-8')
        self.expire_retries('alpha')
        self.world.script('alpha', [{'output': 'coding done'}])
        self.drive('alpha', 1)
        code = self.sessions('code')
        self.assertEqual(len(code), 2, 'exactly one resumed coding session')
        self.assertEqual(code[1]['prompt'], first_code['prompt'], 'resume reuses the same task prompt')
        self.assertEqual(len(self.sessions('selection')), 1, 'quota recovery never reselects work')
        self.assertEqual(self.gh.project_status('example-owner/1', issue), 'In Progress')


class CrashRecoveryTests(E2EBase):
    """Scenario 7: a lost merge response, then restarts at ci and postmerge, never duplicate work."""

    def _lose_merge_response(self):
        """Apply `gh pr merge` in the fake GitHub, then raise as if the response was lost."""
        original = self.gh.run_gh

        def lossy(argv, actor='controller'):
            if argv[1:3] == ['pr', 'merge'] and actor == 'controller' and not getattr(self, '_lost', False):
                self._lost = True
                original(argv, actor=actor)  # GitHub accepted and applied the merge
                raise ew.ctl.ControllerError(f'Command rc=1: {argv}: connection reset after merge')
            return original(argv, actor=actor)

        self.gh.run_gh = lossy
        self.addCleanup(setattr, self.gh, 'run_gh', original)

    def _ready_pr(self) -> int:
        self.world.add_project('alpha', R, 1)
        return self.gh.add_pr(R, title='Ready', body='', head_sha=SHA_FIX, head_ref='renovate/ready',
                              user=ew.eg.RENOVATE_USER)

    def test_lost_merge_response_then_restart_completes_without_second_merge(self):
        pr = self._ready_pr()
        self.gh.set_checks(R, pr, SHA_FIX, [{'name': 'required', 'bucket': 'pass'}])
        self.world.script('alpha', [{'output': 'renovate verified'}])
        self.step_until('ci')
        self._lose_merge_response()
        with self.assertRaises(ew.ctl.ControllerError):
            for _ in range(3):
                self.restart().one_iteration()  # the merge call is applied, then its response is lost
        self.assertEqual(self.gh.pr(R, pr)['state'], 'closed', 'GitHub applied the merge')
        self.assertEqual(self.checkpoint()['phase'], 'merge', 'the checkpoint still says merge')

        merge_sha = self.gh.pr(R, pr)['merge_commit_sha']
        self.gh.set_run(R, merge_sha, 'completed', 'success')
        self.restart().one_iteration()  # restart: merged PR is recognised, no second merge
        self.assertEqual(self.checkpoint()['phase'], 'postmerge')
        self.restart().one_iteration()  # restart again: postmerge green -> complete
        self.assertIsNone(self.checkpoint())
        self.assertEqual(len(self.merges()), 1, 'exactly one successful merge in total')
        self.assertEqual(len(self.sessions()), 1, 'no extra AI session after the lost response')

    def test_restart_at_ci_and_postmerge_never_duplicates_ai_or_merge(self):
        pr = self._ready_pr()
        self.world.script('alpha', [{'output': 'renovate verified'}])
        self.step_until('ci')
        self.gh.set_checks(R, pr, SHA_FIX, [{'name': 'required', 'bucket': 'pending'}])
        self.restart().one_iteration()  # restart at ci: still pending, no AI, no merge
        self.assertEqual(self.checkpoint()['phase'], 'ci')
        self.assertEqual(self.merges(), [])

        self.gh.set_checks(R, pr, SHA_FIX, [{'name': 'required', 'bucket': 'pass'}])
        self.step_until('postmerge')  # ci -> merge (one merge) -> postmerge
        self.assertEqual(len(self.merges()), 1)
        merge_sha = self.gh.pr(R, pr)['merge_commit_sha']
        self.gh.set_run(R, merge_sha, 'queued')
        self.restart().one_iteration()  # restart at postmerge: pending main CI, no duplicate merge
        self.assertEqual(self.checkpoint()['phase'], 'postmerge')
        self.assertEqual(len(self.merges()), 1)
        self.gh.set_run(R, merge_sha, 'completed', 'success')
        self.restart().one_iteration()
        self.assertIsNone(self.checkpoint())
        self.assertEqual(len(self.merges()), 1)
        self.assertEqual(len(self.sessions()), 1, 'restarts never start a second AI session')


class ReleaseGateTests(E2EBase):
    """Scenario 8: release gates: owner approval, tag at exact SHA, tag collision fails closed."""

    RELEASE = {'enabled': True, 'approver': 'example-owner', 'required_assets': []}
    SHA = 'b7' * 20

    def _release_candidate(self, tag: str, sha: str) -> int:
        """Project with a feature-release Issue whose candidate is already green on main."""
        self.world.add_project('alpha', R, 1, release=self.RELEASE)
        self.gh.set_release(R, 'v1.0.0', days_ago=20)
        issue = self.gh.add_issue(R, title=f'Release {tag} feature', body='r',
                                  project='example-owner/1', status='In Progress')
        self.gh.add_push_run(R, sha, 'completed', 'success')
        return issue

    def _checkpoint_at(self, phase: str, issue: int, tag: str, sha: str, **extra):
        cp = {'kind': 'issue', 'number': issue, 'url': f'https://github.com/{R}/issues/{issue}',
              'phase': phase, 'release_tag': tag, 'release_feature': True, 'release_sha': sha,
              'previous_release_tag': 'v1.0.0', 'started': 0.0}
        cp.update(extra)
        ew.ctl.json_atomic(self.world.worker('alpha').cycle_state, cp)

    def _phase(self) -> str:
        return self.checkpoint()['phase']

    def test_feature_release_needs_owner_approval_then_tags_exact_sha(self):
        issue = self._release_candidate('v1.1.0', self.SHA)
        self._checkpoint_at('release-approval', issue, 'v1.1.0', self.SHA)
        worker = self.world.worker('alpha')

        # 1. First pass posts the approval request and stops before any tag.
        worker.one_iteration()
        comments = self.gh.read()['repos'][R]['comments'].get(str(issue), [])
        self.assertTrue(any(c['body'].startswith('AUTOCONTROLLER_FEATURE_RELEASE_REQUEST tag=v1.1.0')
                            for c in comments))
        self.assertEqual(self._phase(), 'release-approval')
        self.assertIsNone(self.gh.tag(R, 'v1.1.0'))

        # 2. Text from a non-owner, even the exact approval line, is not approval.
        self.gh.add_comment(R, issue, f'APPROVE FEATURE RELEASE v1.1.0 {self.SHA}', login='example-dev')
        worker.one_iteration()
        self.assertEqual(self._phase(), 'release-approval')
        self.assertIsNone(self.gh.tag(R, 'v1.1.0'))

        # 3. The owner approves the exact candidate: publication moves on, and a tag is created at that SHA.
        self.gh.add_comment(R, issue, f'APPROVE FEATURE RELEASE v1.1.0 {self.SHA}', login='example-owner')
        worker.one_iteration()
        self.assertEqual(self._phase(), 'release-publish')
        worker.one_iteration()
        self.assertEqual(self._phase(), 'release-verify')
        self.assertIsNotNone(self.gh.tag(R, 'v1.1.0'))
        self.assertEqual(ew.eg.ctl.tag_target(R, 'v1.1.0'), self.SHA)

        # 3b. The native release pipeline publishes; verification then completes the Issue.
        self.gh.set_release(R, 'v1.1.0', days_ago=0)
        worker.one_iteration()
        self.assertIsNone(self.checkpoint(), 'verified release closes the checkpoint')
        self.assertEqual(self.gh.issue(R, issue)['state'], 'closed')
        self.assertEqual(self.gh.project_status('example-owner/1', issue), 'Done')

        # 4. Tag writes come only from the controller actor, never an agent.
        tag_writes = [m for m in self.gh.mutations() if 'git/refs' in ' '.join(m['argv'])]
        self.assertEqual(len(tag_writes), 1)
        self.assertEqual(tag_writes[0]['actor'], 'controller')
        self.assertEqual(self.sessions(), [], 'release gates never start an AI session')

    def test_release_verify_waits_for_required_asset_then_completes(self):
        release = dict(self.RELEASE, required_assets=['app.tar.gz'])
        self.world.add_project('alpha', R, 1, release=release)
        self.gh.set_release(R, 'v1.0.0', days_ago=20)
        issue = self.gh.add_issue(R, title='Release v1.1.0 feature', body='r',
                                  project='example-owner/1', status='In Progress')
        self.gh.add_push_run(R, self.SHA, 'completed', 'success')
        self._checkpoint_at('release-verify', issue, 'v1.1.0', self.SHA)
        self.gh.set_tag(R, 'v1.1.0', self.SHA)  # the controller already pushed the tag at the approved SHA
        self.gh.set_release(R, 'v1.1.0', assets=[], days_ago=0)  # published, but the asset is missing
        worker = self.world.worker('alpha')

        worker.one_iteration()
        worker.one_iteration()
        self.assertEqual(self._phase(), 'release-verify', 'no completion while an asset is missing')
        self.assertEqual(self.gh.issue(R, issue)['state'], 'open')
        self.assertEqual(self.gh.project_status('example-owner/1', issue), 'In Progress')

        self.gh.set_release(R, 'v1.1.0', assets=['app.tar.gz'], days_ago=0)  # the asset appears
        worker.one_iteration()
        self.assertIsNone(self.checkpoint(), 'verification completes once the required asset is present')
        self.assertEqual(self.gh.issue(R, issue)['state'], 'closed')
        self.assertEqual(self.gh.project_status('example-owner/1', issue), 'Done')
        self.assertEqual(self.sessions(), [], 'waiting on assets never starts an AI session')

    def test_approval_for_a_different_sha_does_not_release_this_candidate(self):
        issue = self._release_candidate('v1.1.0', self.SHA)
        self._checkpoint_at('release-approval', issue, 'v1.1.0', self.SHA)
        worker = self.world.worker('alpha')
        worker.one_iteration()  # posts the request for this SHA
        self.gh.add_comment(R, issue, 'APPROVE FEATURE RELEASE v1.1.0 ' + 'c0' * 20, login='example-owner')
        worker.one_iteration()
        self.assertEqual(self._phase(), 'release-approval')
        self.assertIsNone(self.gh.tag(R, 'v1.1.0'))

    def test_open_release_blocker_prevents_tag_and_never_starts_ai(self):
        issue = self._release_candidate('v1.1.0', self.SHA)
        blocker = self.gh.add_issue(R, title='Crash on upgrade', labels=('release-blocker',))
        self._checkpoint_at('release-publish', issue, 'v1.1.0', self.SHA)
        worker = self.world.worker('alpha')
        worker.one_iteration()
        worker.one_iteration()
        self.assertEqual(self._phase(), 'release-publish', 'the release stays in publish while blocked')
        self.assertIsNone(self.gh.tag(R, 'v1.1.0'), 'no tag may exist while a blocker is open')
        self.assertEqual(self.gh.mutation_argvs(['gh', 'api', '--method', 'POST']), [],
                         'no tag or release write while blocked')
        self.assertEqual(self.sessions(), [], 'a blocked release never starts an AI session')
        self.assertEqual(self.gh.issue(R, blocker)['state'], 'open')

    def test_missing_blocker_label_prevents_tag_with_notice_and_never_starts_ai(self):
        issue = self._release_candidate('v1.1.0', self.SHA)
        self.gh.set_labels(R, ())  # the label was deleted: GitHub would list no blockers at all
        self._checkpoint_at('release-publish', issue, 'v1.1.0', self.SHA)
        worker = self.world.worker('alpha')
        worker.one_iteration()
        self.assertEqual(self._phase(), 'release-publish')
        self.assertIsNone(self.gh.tag(R, 'v1.1.0'), 'no tag may exist without the blocker label')
        self.assertEqual(self.sessions(), [], 'a missing label never starts an AI session')
        notices = ew.ctl.json_read(worker.notices_file)
        self.assertIn("label 'release-blocker' does not exist", notices['release-blocked:v1.1.0']['message'])

    def test_custom_blocker_label_is_queried_and_blocks_for_other_repositories(self):
        release = dict(self.RELEASE, blocker_label='ship-blocker')
        self.world.add_project('alpha', R, 1, release=release)
        self.gh.set_labels(R, ('release-blocker', 'ship-blocker'))
        self.gh.set_release(R, 'v1.0.0', days_ago=20)
        issue = self.gh.add_issue(R, title='Release v1.1.0 feature', body='r',
                                  project='example-owner/1', status='In Progress')
        self.gh.add_push_run(R, self.SHA, 'completed', 'success')
        ignored = self.gh.add_issue(R, title='Other label', labels=('release-blocker',))  # not the configured label
        blocker = self.gh.add_issue(R, title='Crash on upgrade', labels=('ship-blocker',))
        self._checkpoint_at('release-publish', issue, 'v1.1.0', self.SHA)
        worker = self.world.worker('alpha')
        worker.one_iteration()
        self.assertIsNone(self.gh.tag(R, 'v1.1.0'), 'the configured label gates publication')
        notices = ew.ctl.json_read(worker.notices_file)
        self.assertIn(f'#{blocker}', notices['release-blocked:v1.1.0']['message'])
        self.assertNotIn(f'#{ignored}', notices['release-blocked:v1.1.0']['message'])
        self.gh.set_issue_state(R, blocker, 'closed')
        worker.one_iteration()
        self.assertEqual(self._phase(), 'release-verify')
        self.assertEqual(self.gh.issue(R, ignored)['state'], 'open', 'the other label never blocked')
        self.assertEqual(self.sessions(), [])

    def test_closing_the_blocker_lets_publication_continue(self):
        issue = self._release_candidate('v1.1.0', self.SHA)
        blocker = self.gh.add_issue(R, title='Crash on upgrade', labels=('release-blocker',))
        self._checkpoint_at('release-publish', issue, 'v1.1.0', self.SHA)
        worker = self.world.worker('alpha')
        worker.one_iteration()
        self.assertIsNone(self.gh.tag(R, 'v1.1.0'))
        self.gh.set_issue_state(R, blocker, 'closed')
        worker.one_iteration()
        self.assertEqual(self._phase(), 'release-verify')
        self.assertEqual(ew.eg.ctl.tag_target(R, 'v1.1.0'), self.SHA)
        self.assertEqual(self.sessions(), [])

    def test_required_asset_template_substitutes_the_release_tag(self):
        release = dict(self.RELEASE, required_assets=['autocontroller-{tag}.tar.gz', 'SHA256SUMS'])
        self.world.add_project('alpha', R, 1, release=release)
        self.gh.set_release(R, 'v1.0.0', days_ago=20)
        issue = self.gh.add_issue(R, title='Release v1.1.0 feature', body='r',
                                  project='example-owner/1', status='In Progress')
        self.gh.add_push_run(R, self.SHA, 'completed', 'success')
        self._checkpoint_at('release-verify', issue, 'v1.1.0', self.SHA)
        self.gh.set_tag(R, 'v1.1.0', self.SHA)
        self.gh.set_release(R, 'v1.1.0', assets=['SHA256SUMS'], days_ago=0)  # the archive is still missing
        worker = self.world.worker('alpha')
        worker.one_iteration()
        self.assertEqual(self._phase(), 'release-verify', 'the literal {tag} name is never expected')
        self.gh.set_release(R, 'v1.1.0', assets=['autocontroller-v1.1.0.tar.gz', 'SHA256SUMS'], days_ago=0)
        worker.one_iteration()
        self.assertIsNone(self.checkpoint(), 'the substituted per-tag asset completes verification')

    def test_existing_tag_at_other_sha_fails_closed_and_is_not_moved(self):
        other = 'd0' * 20
        self.world.add_project('alpha', R, 1, release=self.RELEASE)
        self.gh.set_release(R, 'v1.0.0', days_ago=20)
        self.gh.set_tag(R, 'v1.0.1', other)
        self.gh.add_push_run(R, self.SHA, 'completed', 'success')
        self._checkpoint_at('release-publish', 0, 'v1.0.1', self.SHA,
                            kind='maintenance', url='v1.0.1')
        with self.assertRaises(ew.ctl.ControllerError):
            self.world.worker('alpha').one_iteration()
        self.assertEqual(self.gh.tag(R, 'v1.0.1'), other, 'a conflicting tag must never be moved')
        self.assertEqual(self.gh.mutation_argvs(['gh', 'api', '--method', 'POST']), [])


class FakeChecksExitCodeTests(E2EBase):
    """Minor 4: the fake `gh pr checks` uses the real exit codes (8 pending, 1 fail, 0 pass).

    Exercised through the fake store: the same `checks_result` that `run_fn` routes to, then the
    controller's real `pr_ci_state` reading the result (its `subprocess.run` seam is replaced).
    """

    ARGV = ['gh', 'pr', 'checks', '{n}', '--repo', R, '--json', 'name,bucket,workflow']

    def test_controller_reads_pending_failed_and_passed_from_the_fake(self):
        self.world.add_project('alpha', R, 1)
        pr = self.gh.add_pr(R, title='t', body='b', head_sha=SHA_FIX)
        argv = [a.replace('{n}', str(pr)) for a in self.ARGV]
        cases = [('pending', 8, 'pending'), ('fail', 1, 'failed'), ('pass', 0, 'passed')]
        for bucket, code, state in cases:
            self.gh.set_checks(R, pr, SHA_FIX, [{'name': 'required', 'bucket': bucket}])
            done = ew.eg.checks_result(self.gh, list(argv))
            self.assertEqual(done.returncode, code, f'gh pr checks exit for {bucket}')
            with unittest.mock.patch.object(ew.ctl.subprocess, 'run', return_value=done):
                self.assertEqual(ew.ctl.pr_ci_state(R, pr), state, f'controller state for {bucket}')


class WorkerRunLoopOutageTests(E2EBase):
    """M3: the real ProjectWorker.run() loop under a GitHub outage, with a stop event.

    The loop must back off persistently (worker-backoff.json grows and survives), start no AI
    session, and stop cleanly when the stop event is set. Delays are kept to the 1 s floor.
    """

    def test_outage_backs_off_persistently_without_ai_and_stops_cleanly(self):
        self.world.add_project('alpha', R, 1)
        self.gh.add_issue(R, title='Feature Q', body='q', project='example-owner/1', status='Backlog')
        cfg = dict(self.world.projects['alpha'])
        gcfg = self.world.global_cfg()
        gcfg.update(retry_max_seconds=2, poll_seconds=1)
        stop = threading.Event()
        worker = ew.ctl.ProjectWorker(cfg, gcfg, stop, self.world.quota_lock)
        backoff = worker.worker_backoff
        # run() validates the checkout once before its first iteration. The E2E checkouts live in
        # the temp tree (outside DEFAULT_ROOT), so validation is skipped here on purpose and the
        # outage alone must drive every failure. Checkout validation is tested elsewhere.
        worker._validated = True
        self.gh.set_outage(True)

        thread = threading.Thread(target=worker.run, name='worker-alpha', daemon=True)
        thread.start()
        try:
            deadline = __import__('time').monotonic() + 60
            seen = []
            while __import__('time').monotonic() < deadline and len(seen) < 3:
                state = ew.ctl.json_read(backoff, None)
                if state and (not seen or state['failures'] > seen[-1]):
                    seen.append(state['failures'])
                __import__('time').sleep(0.2)
            self.assertGreaterEqual(len(seen), 2, f'backoff must grow across retries, saw {seen}')
            self.assertEqual(seen, sorted(seen), 'the failure count only grows during an outage')
            self.assertTrue(backoff.exists(), 'worker-backoff.json persists while GitHub is down')
            self.assertEqual(self.sessions(), [], 'no AI session starts during the outage')
        finally:
            stop.set()
            thread.join(30)
        self.assertFalse(thread.is_alive(), 'the worker thread stops cleanly when the stop event is set')
        self.assertEqual(self.sessions(), [], 'stopping never starts an AI session')
        self.assertEqual(self.gh.mutations(), [], 'an outage produces no GitHub mutation')


class MaintenanceDueTests(E2EBase):
    """Maintenance releases: the due rule (>=14 days since release, >=3 merged verified Renovate PRs).

    Due: the worker enters the maintenance cycle and the release-preparation session is the ONLY
    AI call. Not due (negatives): no maintenance session and no AI call at all.
    """

    RELEASE = {'enabled': True, 'approver': 'example-owner', 'required_assets': []}
    RENOVATE = {'login': 'renovate[bot]', 'type': 'Bot', 'id': 29139614}
    HUMAN = {'login': 'example-dev', 'type': 'User', 'id': 3003}

    def _project_with_release(self, *, release_days_ago: float, renovate: int, human: int = 0):
        self.world.add_project('alpha', R, 1, release=self.RELEASE)
        self.gh.set_release(R, 'v1.0.0', days_ago=release_days_ago)
        for _ in range(renovate):
            self.gh.add_merged_pr(R, days_ago=2, user=self.RENOVATE, title='chore(deps): bump lib')
        for _ in range(human):
            self.gh.add_merged_pr(R, days_ago=2, user=self.HUMAN, title='feat: something')

    def test_due_release_enters_maintenance_and_the_only_ai_call_is_release_preparation(self):
        self._project_with_release(release_days_ago=20, renovate=3)
        self.world.script('alpha', [{'output': 'release preparation: no PR yet'}])
        self.drive('alpha', 1)

        sessions = self.sessions()
        self.assertEqual(len(sessions), 1, 'exactly one AI session: the release preparation')
        session = sessions[0]
        self.assertEqual(session['phase'], 'code')
        self.assertIn('technical release preparation', session['prompt'])
        self.assertIn('v1.0.1', session['prompt'], 'the next patch tag is the release being prepared')
        # No PR was produced, so the attempt is recorded as an incomplete maintenance task that backs off.
        retries = ew.ctl.json_read(self.world.worker('alpha').retries_file, {}) or {}
        self.assertIn('maintenance:0', retries, 'the failed preparation is recorded, not retried at once')
        self.assertIsNone(self.gh.tag(R, 'v1.0.1'), 'preparation never tags or publishes')
        self.assertEqual(self.gh.mutations(), [], 'no GitHub mutation from an attempt without a PR')

        # Inside the backoff window a second pass starts no further AI session.
        self.drive('alpha', 1)
        self.assertEqual(len(self.sessions()), 1, 'the only AI call is the release-preparation session')

    def test_not_due_with_fewer_than_three_renovate_prs_makes_no_ai_call(self):
        self._project_with_release(release_days_ago=20, renovate=2)
        self.drive('alpha', 2)
        self.assertEqual(self.sessions(), [], 'fewer than three merged Renovate PRs: nothing is due')
        self.assertIsNone(self.checkpoint())

    def test_not_due_with_release_younger_than_fourteen_days_makes_no_ai_call(self):
        self._project_with_release(release_days_ago=5, renovate=3)
        self.drive('alpha', 2)
        self.assertEqual(self.sessions(), [], 'a release younger than 14 days is not due')
        self.assertIsNone(self.checkpoint())

    def test_a_non_technical_merged_pr_blocks_maintenance_even_with_three_renovate_prs(self):
        # A feature merged since the release would be published under a patch version: refused.
        self._project_with_release(release_days_ago=20, renovate=3, human=1)
        self.drive('alpha', 2)
        self.assertEqual(self.sessions(), [], 'a non-technical merged PR suppresses the patch release')
        self.assertIsNone(self.checkpoint())


if __name__ == '__main__':
    unittest.main()
