"""Active-session recovery through the REAL safe wrapper and the real controller (issue #21).

The fake OpenCode V2 (tests/e2e_opencode.py) models owned root and descendant sessions with an
evolving active state: every `GET /api/session/active` reports the current active set and then
advances each session's remaining active lifetime by one poll. Polls are the only clock, so the
ordering of wrapper polls, recovery turns and interrupts is deterministic.

Simulated: OpenCode V2 active-session semantics, descendant lifetimes, interrupts, API faults.
Not simulated (live sandbox only): real OpenCode server timing, real subagent durations, real
interrupt delivery. See docs/E2E_TESTING.md.
"""
import json
import os
import subprocess
import sys

import e2e_world as ew
from test_e2e_real_wrapper import RealWrapperBase
from test_e2e_controller import R, SHA_ISSUE

WRAPPER_RC_BLOCKED = 78
WRAPPER_RC_REFUSED_CHECKPOINT = 2
def pr_action(issue: int) -> dict:
    """The coding session's PR, linked to its Issue exactly as the controller expects."""
    return {'gh': ['gh', 'pr', 'create', '--repo', R, '--title', 'Feature', '--body', f'Closes #{issue}',
                   '--head-sha', SHA_ISSUE]}


class ActiveSessionBase(RealWrapperBase):
    """Helpers shared by the active-session tests (all offline, every subprocess has a timeout)."""

    def setUp(self):
        super().setUp()
        self.world.add_project('alpha', R, 1)
        self.checkout = str(self.world.checkout('alpha'))

    # ----- invocation log (in order, across every row kind) ---------------------------------
    def log_rows(self) -> list[dict]:
        path = self.world.e2e / 'invocations.jsonl'
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line]

    def polls_containing(self, sid: str) -> list[int]:
        """Indexes in the ordered log of every active poll whose active set contained `sid`."""
        return [i for i, r in enumerate(self.log_rows()) if r.get('kind') == 'active_poll' and sid in r['ids']]

    def roots(self, title_part: str | None = None) -> list[dict]:
        """Parentless fake sessions (optionally filtered by a title substring)."""
        out = []
        for session in self.world.opencode_sessions().values():
            if session.get('parentID'):
                continue
            if title_part is None or title_part in (session.get('title') or ''):
                out.append(session)
        return out

    def children_of(self, sid: str) -> list[dict]:
        return [s for s in self.world.opencode_sessions().values() if s.get('parentID') == sid]

    def marker_doc(self) -> dict:
        return json.loads(self.pending_marker().read_text(encoding='utf-8'))

    def wrapper_log_text(self) -> str:
        logs = sorted((self.world.state / 'projects' / 'alpha' / 'logs').glob('*.log'))
        return logs[-1].read_text(encoding='utf-8') if logs else ''

    def add_backlog(self, title: str) -> int:
        return self.gh.add_issue(R, title=title, body=title, project='example-owner/1', status='Backlog')

    def open_pr(self) -> dict:
        return next(p for p in self.gh.read()['repos'][R]['prs'].values()
                    if p['state'] == 'open' and p['head']['sha'] == SHA_ISSUE)

    # ----- the REAL wrapper, invoked directly (refusals, --checkpoint, --interrupt-owned) ----
    def direct(self, *argv: str, timeout: int = 60) -> tuple[int, str]:
        env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(self.tmp),
               'AUTONOMY_PROJECT_ROOT': self.checkout, 'AUTONOMY_PROJECT_NAME': 'alpha',
               'AUTONOMY_PENDING_MARKER': str(self.pending_marker()),
               'AUTONOMY_OPENCODE': str(self.world.opencode), 'AUTONOMY_PHASE': 'code'}
        proc = subprocess.run([sys.executable, str(ew.REAL_WRAPPER), *argv], cwd=self.checkout, env=env,
                              capture_output=True, text=True, timeout=timeout, check=False)
        return proc.returncode, proc.stdout + proc.stderr

    def seed_marker(self, session_id: str | None, *, title: str = 'alpha code #1 [guard:1-1]',
                    status: str = 'running') -> None:
        """A pending marker as a previous guard left it (its pid is not alive)."""
        path = self.pending_marker()
        path.parent.mkdir(parents=True, exist_ok=True)
        marker = {'status': status, 'title': title, 'pid': 999999, 'boot_id': None, 'opencode': 'v2',
                  'agent': 'orchestrator'}
        if session_id:
            marker['session_id'] = session_id
        path.write_text(json.dumps(marker, sort_keys=True), encoding='utf-8')

    def seed_root(self, sid: str, *, title: str = 'alpha code #1 [guard:1-1]', active_polls: int = 0,
                  ignore_interrupts: bool = False) -> None:
        self.world.seed_opencode_session(sid, title=title, directory=self.checkout,
                                         active_polls=active_polls, ignore_interrupts=ignore_interrupts)

    def seed_child(self, sid: str, parent: str, *, active_polls: int = 0, title: str = 'subagent') -> None:
        self.world.seed_opencode_session(sid, title=title, directory=self.checkout, parent=parent,
                                         active_polls=active_polls)


class DescendantSettleTests(ActiveSessionBase):
    def test_active_descendants_settle_then_recovery_turns_reconcile_in_the_same_root(self):
        issue = self.add_backlog('Feature A')
        self.world.script('alpha', [
            {'output': 'AUTOCONTROLLER_SELECT_ISSUE #%d' % issue},
            # The root and its nested subagents stay active for several wrapper polls after the CLI returns.
            {'root_active_polls': 2, 'output': 'coding handed off to subagents',
             'descendants': [{'title': 'explore', 'active_polls': 3,
                              'children': [{'title': 'deep', 'active_polls': 1}]}]},
        ])
        # Recovery turn 1 finishes the PR and spawns a late subagent; turn 2 has no further work.
        self.world.script_resume('alpha', 'code', [
            {'actions': [pr_action(issue)], 'descendants': [{'title': 'late', 'active_polls': 2}]},
            {},
        ])
        self.drive('alpha', 1)  # selection
        self.drive('alpha', 1)  # coding through the real wrapper; settle + two recovery turns

        code = self.sessions('code')
        self.assertEqual(len(code), 1)
        root = code[0]['session_id']
        self.assertEqual(len(self.roots('alpha issue #')), 1, 'no second root session for the task')

        resumes = [i for i, r in enumerate(self.log_rows()) if r.get('kind') == 'resume' and r['session_id'] == root]
        self.assertEqual(len(resumes), 2, 'one recovery turn, plus exactly one more for the descendant that spawned in it')

        # The resume happens only after every descendant went inactive (ordering in the log).
        explore = next(s['id'] for s in self.world.opencode_sessions().values() if s.get('title') == 'explore')
        deep = next(s['id'] for s in self.world.opencode_sessions().values() if s.get('title') == 'deep')
        late = next(s['id'] for s in self.world.opencode_sessions().values() if s.get('title') == 'late')
        self.assertLess(max(self.polls_containing(explore)), resumes[0], 'explore settled before turn 1')
        self.assertLess(max(self.polls_containing(deep)), resumes[0], 'nested subagent settled before turn 1')
        self.assertLess(max(self.polls_containing(late)), resumes[1], 'late subagent settled before turn 2')
        self.assertGreater(min(self.polls_containing(late)), resumes[0], 'late subagent only exists after turn 1')

        # The PR flow then continues in the controller: CI pending, then exact-SHA merge.
        pr = self.open_pr()
        self.assertEqual(self.checkpoint()['phase'], 'ci')
        self.gh.set_checks(R, pr['number'], SHA_ISSUE, [{'name': 'required', 'bucket': 'pending'}])
        self.drive('alpha', 1)
        self.gh.set_checks(R, pr['number'], SHA_ISSUE, [{'name': 'required', 'bucket': 'pass'}])
        self.drive('alpha', 2)
        self.assertEqual(self.merges()[0][-1], SHA_ISSUE)
        merged = self.gh.pr(R, pr['number'])
        self.gh.set_run(R, merged['merge_commit_sha'], 'completed', 'success')
        self.drive('alpha', 1)
        self.assertIsNone(self.checkpoint())
        self.assertFalse(self.pending_marker().exists(), 'the marker is removed only after verified completion')


class InterruptionRestartTests(ActiveSessionBase):
    def test_timeout_kills_mid_cycle_then_restart_adopts_the_same_root_and_only_then_selects_next(self):
        first = self.add_backlog('Feature B')
        second = self.add_backlog('Feature C')
        self.world.script('alpha', [
            {'output': 'AUTOCONTROLLER_SELECT_ISSUE #%d' % first},
            # The root and a subagent are still active when the cycle timeout SIGTERMs the wrapper.
            {'root_active_polls': 3, 'block_seconds': 30,
             'descendants': [{'title': 'explore', 'active_polls': 2}]},
            {'output': 'AUTOCONTROLLER_SELECT_ISSUE #%d' % second},
        ])
        self.world.script_resume('alpha', 'code', [{'actions': [pr_action(first)]}])
        self.world.cycle_timeout_seconds = 4

        self.drive('alpha', 1)  # selection -> In Progress
        self.drive('alpha', 1)  # coding: the wrapper is killed mid-cycle (rc 124 path)
        self.world.cycle_timeout_seconds = 60

        root = self.marker_doc()['session_id']
        self.assertEqual(self.marker_doc()['status'], 'running', 'the checkpointed marker survives the kill')
        self.assertEqual(self.checkpoint()['kind'], 'issue')
        self.assertEqual(self.checkpoint()['number'], first)
        retry = json.loads(self.world.worker('alpha').retries_file.read_text(encoding='utf-8'))
        self.assertIn('OpenCode exit 124', retry[f'issue:{first}']['reason'])
        self.assertEqual(self.world.opencode_sessions()[root]['active_polls'], 3, 'the root is still active')

        # Inside the retry backoff: no new selection, no second task, despite the other Backlog Issue.
        self.drive('alpha', 2)
        self.assertEqual(len(self.sessions('selection')), 1)
        self.assertEqual(len(self.sessions('code')), 1)
        self.assertEqual(len(self.roots('alpha issue #')), 1)

        # A restarted worker adopts the SAME active root, waits for the subagent, resumes it once.
        self.expire_retries('alpha')
        self.drive('alpha', 1)
        self.assertIn('Adopted interrupted active cycle: root ' + root, self.wrapper_log_text())
        self.assertEqual(len(self.roots('alpha issue #')), 1, 'no duplicate root')
        self.assertEqual(len(self.sessions('code')), 1, 'no second coding session')
        resumes = [i for i, r in enumerate(self.log_rows()) if r.get('kind') == 'resume' and r['session_id'] == root]
        self.assertEqual(len(resumes), 1)
        explore = next(s['id'] for s in self.world.opencode_sessions().values() if s.get('title') == 'explore')
        self.assertLess(max(self.polls_containing(explore)), resumes[0], 'resume waits for the subagent')
        self.assertEqual(self.checkpoint()['phase'], 'ci')

        # Still in CI: the next Backlog Issue is NOT selected until this task verifiably completes.
        pr = self.open_pr()
        self.gh.set_checks(R, pr['number'], SHA_ISSUE, [{'name': 'required', 'bucket': 'pending'}])
        self.drive('alpha', 1)
        self.assertEqual(len(self.sessions('selection')), 1, 'no new selection while CI is pending')
        self.gh.set_checks(R, pr['number'], SHA_ISSUE, [{'name': 'required', 'bucket': 'pass'}])
        self.drive('alpha', 2)
        self.assertEqual(self.merges()[0][-1], SHA_ISSUE)
        merged = self.gh.pr(R, pr['number'])
        self.gh.set_run(R, merged['merge_commit_sha'], 'completed', 'success')
        self.drive('alpha', 1)
        self.assertIsNone(self.checkpoint())
        self.assertFalse(self.pending_marker().exists())

        self.drive('alpha', 1)  # only now: the next Backlog Issue is selected
        selections = self.sessions('selection')
        self.assertEqual(len(selections), 2)
        merge_row = max(i for i, r in enumerate(self.log_rows()) if r.get('kind') == 'resume' and r['session_id'] == root)
        second_row = [i for i, r in enumerate(self.log_rows()) if r.get('kind') == 'session'
                      and r['session_id'] == selections[1]['session_id']][0]
        self.assertGreater(second_row, merge_row, 'the next task starts only after verified completion')
        self.assertEqual(self.gh.project_status('example-owner/1', second), 'In Progress')


class ApiOutageTests(ActiveSessionBase):
    def test_api_down_during_settle_exits_78_keeps_the_marker_then_the_same_root_resumes(self):
        self.seed_marker('ses_outage_root', status='running')
        self.seed_root('ses_outage_root', active_polls=1)
        # The first API call (version check) passes; the settle poll then fails.
        self.world.set_fault(path='/api/session/active', skip=1, fail=1)
        rc, out = self.direct('run', '--auto', '--title', 'x', 'PROMPT')
        self.assertEqual(rc, WRAPPER_RC_BLOCKED, out)
        marker = self.marker_doc()
        self.assertEqual(marker['session_id'], 'ses_outage_root')
        self.assertEqual(self.world.opencode_sessions()['ses_outage_root']['active_polls'], 0,
                         'the owned root settled through the wrapper before the outage was reached')
        self.assertFalse(self.world.sessions('code'), 'no new work was started')

        # API back: the same root is resumed once and the marker is removed.
        self.world.set_fault()
        self.world.script_resume('alpha', 'code', [{}])
        rc, out = self.direct('run', '--auto', '--title', 'x', 'PROMPT')
        self.assertEqual(rc, 0, out)
        self.assertFalse(self.pending_marker().exists())
        self.assertEqual(len([r for r in self.log_rows() if r.get('kind') == 'resume'
                              and r['session_id'] == 'ses_outage_root']), 1)

    def test_controller_keeps_checkpoint_and_starts_no_work_while_the_api_is_down(self):
        issue = self.add_backlog('Feature E')
        self.world.script('alpha', [
            {'output': 'AUTOCONTROLLER_SELECT_ISSUE #%d' % issue},
            # The cycle is killed (timeout) while the root is still active: marker + checkpoint remain.
            {'root_active_polls': 3, 'block_seconds': 30},
            {'output': 'AUTOCONTROLLER_SELECT_ISSUE #999'},
        ])
        self.world.script_resume('alpha', 'code', [{'actions': [pr_action(issue)]}])
        self.world.cycle_timeout_seconds = 4
        self.drive('alpha', 1)  # selection
        self.drive('alpha', 1)  # coding: killed mid-cycle, the marker records the root
        self.world.cycle_timeout_seconds = 60
        root = self.marker_doc()['session_id']
        self.assertEqual(self.checkpoint()['kind'], 'issue')

        # The API goes down, and the retry backoff expires: the controller runs the wrapper again.
        self.world.set_fault(path=None, fail=-1)
        before = len(self.log_rows())
        try:
            self.expire_retries('alpha')
            self.drive('alpha', 3)
        finally:
            self.world.set_fault()
        new_rows = self.log_rows()[before:]
        self.assertEqual(len(self.sessions('selection')), 1, 'no selection while the API is down')
        self.assertEqual(len(self.sessions('code')), 1, 'no new coding session while the API is down')
        self.assertFalse([r for r in new_rows if r.get('kind') in ('session', 'resume')], 'no session or resume turn')
        self.assertEqual(self.checkpoint()['kind'], 'issue', 'the checkpoint is preserved')
        self.assertEqual(self.marker_doc()['session_id'], root, 'the pending marker still names the same root')

    def test_interrupt_owned_with_api_down_fails_closed_and_keeps_the_pending_interrupt(self):
        self.seed_marker('ses_api_root', status='running')
        self.seed_root('ses_api_root', active_polls=-1)
        self.world.set_fault(path=None, fail=-1)
        try:
            rc, out = self.direct('--interrupt-owned')
        finally:
            self.world.set_fault()
        self.assertEqual(rc, 2, out)
        self.assertIn('INTERRUPT NOT VERIFIED', out)
        self.assertFalse([r for r in self.log_rows() if r.get('kind') == 'interrupt'], 'nothing was interrupted')


class InterruptOwnedTests(ActiveSessionBase):
    def test_interrupts_owned_root_then_descendants_never_the_foreign_root(self):
        self.seed_marker('ses_owned')
        self.seed_root('ses_owned', active_polls=-1)
        self.seed_child('ses_owned_child', 'ses_owned', active_polls=-1)
        self.seed_child('ses_owned_grandchild', 'ses_owned_child', active_polls=-1)
        self.world.seed_opencode_session('ses_foreign', title='someone else', directory=self.checkout,
                                         active_polls=-1)
        rc, out = self.direct('--interrupt-owned')
        self.assertEqual(rc, 0, out)
        interrupts = [r['session_id'] for r in self.log_rows() if r.get('kind') == 'interrupt']
        self.assertEqual(interrupts[0], 'ses_owned', 'the root is interrupted first')
        self.assertEqual(set(interrupts), {'ses_owned', 'ses_owned_child', 'ses_owned_grandchild'})
        self.assertNotIn('ses_foreign', interrupts)

    def test_ignored_interrupt_is_not_verified_and_exits_non_zero(self):
        self.seed_marker('ses_stubborn')
        self.seed_root('ses_stubborn', active_polls=-1, ignore_interrupts=True)
        rc, out = self.direct('--interrupt-owned', timeout=60)
        self.assertEqual(rc, 2, out)
        self.assertIn('did not drain', out)


class PreviousGuardTests(ActiveSessionBase):
    def test_live_previous_guard_refuses_recovery_and_preserves_the_marker(self):
        # A live process whose cmdline contains the wrapper's name: the previous guard is still running.
        guard = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)', 'opencode-project-safe'])
        self.addCleanup(lambda: (guard.kill(), guard.wait(timeout=10)))
        self.seed_marker('ses_prev')
        marker = self.marker_doc()
        marker['pid'] = guard.pid
        self.pending_marker().write_text(json.dumps(marker), encoding='utf-8')
        self.seed_root('ses_prev', active_polls=0)
        rc, out = self.direct('run', '--auto', '--title', 'x', 'PROMPT')
        self.assertEqual(rc, WRAPPER_RC_BLOCKED, out)
        self.assertIn('Previous guard process is still running', out)
        self.assertEqual(self.marker_doc()['session_id'], 'ses_prev')
        self.assertFalse([r for r in self.log_rows() if r.get('kind') == 'resume'], 'no resume turn')


class ForeignAndAmbiguousTests(ActiveSessionBase):
    def test_foreign_active_root_in_project_dir_refuses_a_new_run_and_is_never_interrupted(self):
        foreign = 'ses_foreign_root'
        self.world.seed_opencode_session(foreign, title='someone else', directory=self.checkout, active_polls=-1)
        issue = self.add_backlog('Feature D')
        self.world.script('alpha', [{'output': 'AUTOCONTROLLER_SELECT_ISSUE #%d' % issue}])

        rc, out = self.direct('run', '--auto', '--agent', 'orchestrator', '--title', 'x', 'PROMPT')
        self.assertEqual(rc, WRAPPER_RC_BLOCKED)
        self.assertIn('Active project sessions', out)
        self.assertEqual(len(self.world.opencode_sessions()), 1, 'only the foreign session exists; none created')
        self.assertFalse(self.pending_marker().exists(), 'no marker is written for a refused run')
        self.assertFalse([r for r in self.log_rows() if r.get('kind') == 'interrupt'], 'foreign is never interrupted')

    def test_foreign_root_appearing_during_settle_blocks_and_preserves_the_marker(self):
        # The owned root is still active; a foreign root appears in the project dir during the settle.
        self.seed_marker('ses_owned_root', status='running')
        self.seed_root('ses_owned_root', active_polls=-1)
        self.world.seed_opencode_session('ses_intruder', title='intruder', directory=self.checkout, active_polls=-1)
        rc, out = self.direct('run', '--auto', '--title', 'x', 'PROMPT')
        self.assertEqual(rc, WRAPPER_RC_BLOCKED)
        self.assertIn('Unexpected concurrent project root session', out)
        self.assertEqual(json.loads(self.pending_marker().read_text())['session_id'], 'ses_owned_root')
        self.assertFalse([r for r in self.log_rows() if r.get('kind') == 'interrupt'])

    def test_pending_marker_whose_root_differs_from_the_active_root_is_refused(self):
        self.seed_marker('ses_recorded_root')
        self.seed_root('ses_recorded_root', active_polls=0)
        self.seed_root('ses_other_root', title='other', active_polls=-1)
        rc, out = self.direct('run', '--auto', '--title', 'x', 'PROMPT')
        self.assertEqual(rc, WRAPPER_RC_BLOCKED)
        self.assertIn('belongs to different root session(s): ses_other_root', out)
        self.assertEqual(json.loads(self.pending_marker().read_text())['session_id'], 'ses_recorded_root')

    def test_duplicate_title_roots_are_ambiguous_and_refused_by_checkpoint(self):
        self.seed_marker(None, title='alpha issue #9 [guard:1-2]')
        self.seed_root('ses_dup_a', title='alpha issue #9 [guard:1-2]')
        self.seed_root('ses_dup_b', title='alpha issue #9 [guard:1-2]')
        rc, out = self.direct('--checkpoint')
        self.assertEqual(rc, WRAPPER_RC_REFUSED_CHECKPOINT)
        self.assertIn('ambiguous', out)
        self.assertNotIn('session_id', self.marker_doc(), 'an ambiguous title is never checkpointed')

    def test_legacy_marker_with_two_active_roots_is_refused_by_recovery(self):
        legacy = {'status': 'running', 'pid': 999999, 'boot_id': None, 'opencode': 'v2', 'agent': 'orchestrator'}
        self.pending_marker().parent.mkdir(parents=True, exist_ok=True)
        self.pending_marker().write_text(json.dumps(legacy), encoding='utf-8')
        self.seed_root('ses_legacy_a', title='one', active_polls=-1)
        self.seed_root('ses_legacy_b', title='two', active_polls=-1)
        rc, out = self.direct('run', '--auto', '--title', 'x', 'PROMPT')
        self.assertEqual(rc, WRAPPER_RC_BLOCKED)
        self.assertIn('Legacy pending marker is ambiguous', out)
        self.assertNotIn('session_id', self.marker_doc())

    def test_session_in_another_directory_is_ignored_by_the_wrapper(self):
        other_dir = str(self.tmp / 'elsewhere')
        self.world.seed_opencode_session('ses_elsewhere', title='elsewhere', directory=other_dir, active_polls=-1)
        self.seed_marker('ses_mine')
        self.seed_root('ses_mine', active_polls=0)
        rc, out = self.direct('--checkpoint')
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.marker_doc()['session_id'], 'ses_mine')
        self.assertFalse([r for r in self.log_rows() if r.get('kind') == 'interrupt'])

    def test_checkpoint_with_single_active_root_checkpoints_it(self):
        self.seed_marker(None, title='alpha issue #7 [guard:1-3]')
        self.seed_root('ses_single', title='alpha issue #7 [guard:1-3]', active_polls=-1)
        rc, out = self.direct('--checkpoint')
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.marker_doc()['session_id'], 'ses_single')
