"""N1: unit state comes from `systemctl --user show` with the full property set, not `is-active`.

`activating` is systemd's auto-restart wait after a crash, so it is not healthy. Drain is only
attempted for a unit that is really running (active/running with a MainPID); otherwise the unit
is stopped without waiting. The health window fails on an auto-restart, a PID change, or
unreadable output.

Offline: subprocess.run is replaced inside lifecycle; no systemctl runs. The model below follows
the same property names that tests/fake_systemctl.py answers for `show`.
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
import lifecycle as lc  # noqa: E402


class UnitModel:
    """A scripted `systemctl --user show` answer. Tests change the fields to model a crash or restart."""

    def __init__(self, **fields):
        self.fields = {'ActiveState': 'active', 'SubState': 'running', 'MainPID': 4242, 'NRestarts': 0}
        self.fields.update(fields)
        self.show_calls = 0
        self.other_calls: list[list[str]] = []
        self.on_poll = None  # optional hook run before each show, e.g. to script an auto-restart

    def run(self, argv, **kwargs):
        if argv[:3] != ['systemctl', '--user', 'show']:
            self.other_calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout='', stderr='')
        self.show_calls += 1
        if self.on_poll:
            self.on_poll(self)
        lines = [f'{key}={value}' for key, value in self.fields.items()]
        return subprocess.CompletedProcess(argv, 0, stdout='\n'.join(lines) + '\n', stderr='')


class UnitStateParseTest(unittest.TestCase):
    def read(self, stdout, returncode=0):
        def run(argv, **kwargs):
            assert argv[:3] == ['systemctl', '--user', 'show'], argv
            return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr='')
        with mock.patch.object(lc.subprocess, 'run', side_effect=run):
            return lc.unit_state()

    def test_parses_all_four_properties(self):
        state = self.read('ActiveState=active\nSubState=running\nMainPID=77\nNRestarts=2\n')
        self.assertEqual(state, {'ActiveState': 'active', 'SubState': 'running', 'MainPID': 77, 'NRestarts': 2})

    def test_nonzero_exit_fails_closed(self):
        with self.assertRaisesRegex(lc.LifecycleError, 'unit state is unknown'):
            self.read('ActiveState=active\nSubState=running\nMainPID=7\nNRestarts=0\n', returncode=1)

    def test_empty_or_partial_output_fails_closed(self):
        for stdout in ('', 'ActiveState=active\n', 'ActiveState=active\nSubState=running\nMainPID=7\n'):
            with self.subTest(stdout=stdout):
                with self.assertRaisesRegex(lc.LifecycleError, 'unit state is unknown|every expected property'):
                    self.read(stdout)

    def test_unknown_active_state_fails_closed(self):
        with self.assertRaisesRegex(lc.LifecycleError, 'unknown unit ActiveState'):
            self.read('ActiveState=bogus\nSubState=running\nMainPID=7\nNRestarts=0\n')

    def test_non_numeric_or_negative_pid_fails_closed(self):
        with self.assertRaisesRegex(lc.LifecycleError, 'non-numeric'):
            self.read('ActiveState=active\nSubState=running\nMainPID=abc\nNRestarts=0\n')
        with self.assertRaisesRegex(lc.LifecycleError, 'negative'):
            self.read('ActiveState=active\nSubState=running\nMainPID=-1\nNRestarts=0\n')

    def test_unexpected_or_duplicate_line_fails_closed(self):
        with self.assertRaisesRegex(lc.LifecycleError, 'unexpected line'):
            self.read('ActiveState=active\nSubState=running\nMainPID=7\nNRestarts=0\nFoo=bar\n')
        with self.assertRaisesRegex(lc.LifecycleError, 'unexpected line'):
            self.read('ActiveState=active\nActiveState=active\nSubState=running\nMainPID=7\nNRestarts=0\n')

    def test_systemctl_that_cannot_run_raises(self):
        with mock.patch.object(lc.subprocess, 'run', side_effect=OSError('no systemctl')):
            with self.assertRaisesRegex(lc.LifecycleError, 'show failed'):
                lc.unit_state()


class PredicateTest(unittest.TestCase):
    def state(self, active, sub, pid=5, restarts=0):
        return {'ActiveState': active, 'SubState': sub, 'MainPID': pid, 'NRestarts': restarts}

    def test_activating_auto_restart_must_be_stopped_but_not_drained(self):
        # N1 problem 2: an auto-restart wait is needs_stop (stop it) but never can_drain (no 3600s wait).
        auto = self.state('activating', 'auto-restart', pid=0)
        self.assertTrue(lc.needs_stop(auto))
        self.assertFalse(lc.can_drain(auto))

    def test_only_active_running_with_a_pid_is_drained(self):
        self.assertTrue(lc.can_drain(self.state('active', 'running', pid=9)))
        self.assertFalse(lc.can_drain(self.state('active', 'running', pid=0)))
        self.assertFalse(lc.can_drain(self.state('active', 'exited', pid=9)))
        self.assertFalse(lc.can_drain(self.state('reloading', 'reload', pid=9)))

    def test_inactive_and_failed_need_no_stop_but_still_stop_is_issued_by_callers(self):
        self.assertFalse(lc.needs_stop(self.state('inactive', 'dead', pid=0)))
        self.assertFalse(lc.needs_stop(self.state('failed', 'failed', pid=0)))
        self.assertTrue(lc.needs_stop(self.state('deactivating', 'stop-sigterm', pid=3)))


class DrainDecisionTest(unittest.TestCase):
    """_stop_after_drain: drain only a really running unit; always issue stop; report needs_stop."""

    def setUp(self):
        self.calls: list[str] = []
        self.drain_entered = 0
        self.saved = {n: getattr(lc, n) for n in ('stop_service', 'drain')}
        self.addCleanup(lambda: [setattr(lc, n, v) for n, v in self.saved.items()])
        lc.stop_service = lambda: self.calls.append('stop')

        class FakeDrain:
            def __init__(outer, *a, **k):
                pass

            def __enter__(outer):
                self.drain_entered += 1
                self.calls.append('drain')

            def __exit__(outer, *exc):
                return False

        lc.drain = FakeDrain

    def run_stop(self, model: UnitModel):
        with mock.patch.object(lc.subprocess, 'run', side_effect=model.run):
            return lc._stop_after_drain(Path('/nonexistent-install'), 3600, sleep=lambda s: None)

    def test_running_unit_is_drained_then_stopped_and_reported_active(self):
        self.assertTrue(self.run_stop(UnitModel(ActiveState='active', SubState='running', MainPID=9)))
        self.assertEqual(self.calls, ['drain', 'stop'])

    def test_activating_unit_is_stopped_without_drain_and_reported_active(self):
        model = UnitModel(ActiveState='activating', SubState='auto-restart', MainPID=0)
        self.assertTrue(self.run_stop(model))
        self.assertEqual(self.calls, ['stop'], 'an auto-restart wait has no workers to drain')
        self.assertEqual(self.drain_entered, 0)

    def test_inactive_unit_is_stopped_without_drain_and_reported_inactive(self):
        self.assertFalse(self.run_stop(UnitModel(ActiveState='inactive', SubState='dead', MainPID=0)))
        self.assertEqual(self.calls, ['stop'])

    def test_unreadable_state_refuses_before_any_stop(self):
        with mock.patch.object(lc.subprocess, 'run', side_effect=OSError('no systemctl')):
            with self.assertRaisesRegex(lc.LifecycleError, 'show failed'):
                lc._stop_after_drain(Path('/nonexistent-install'), 3600, sleep=lambda s: None)
        self.assertEqual(self.calls, [])


class HealthWindowTest(unittest.TestCase):
    """healthy: every poll is active/running with the first poll's MainPID and NRestarts."""

    def healthy(self, model: UnitModel, seconds=3):
        with mock.patch.object(lc.subprocess, 'run', side_effect=model.run):
            return lc.healthy(check=lambda: True, seconds=seconds, sleep=lambda s: None,
                              clock=_FakeClock().now)

    def test_stable_process_stays_healthy_for_the_whole_window(self):
        model = UnitModel()
        self.assertTrue(self.healthy(model, seconds=3))
        self.assertGreaterEqual(model.show_calls, 2)

    def test_auto_restart_during_the_window_is_unhealthy(self):
        # Scripted crash on the second poll: systemd is in its restart wait, then a new PID with NRestarts+1.
        model = UnitModel()

        def crash(m):
            if m.show_calls == 2:
                m.fields.update(ActiveState='activating', SubState='auto-restart', MainPID=0, NRestarts=1)
            elif m.show_calls == 3:
                m.fields.update(ActiveState='active', SubState='running', MainPID=5555, NRestarts=1)
        model.on_poll = crash
        self.assertFalse(self.healthy(model, seconds=3))

    def test_restart_counter_change_alone_is_unhealthy(self):
        model = UnitModel()

        def bump(m):
            if m.show_calls == 2:
                m.fields['NRestarts'] = 1
        model.on_poll = bump
        self.assertFalse(self.healthy(model, seconds=3))

    def test_main_pid_changing_mid_window_is_unhealthy(self):
        model = UnitModel()

        def new_pid(m):
            if m.show_calls == 2:
                m.fields['MainPID'] = 9999
        model.on_poll = new_pid
        self.assertFalse(self.healthy(model, seconds=3))

    def test_activating_at_the_first_poll_is_unhealthy(self):
        self.assertFalse(self.healthy(UnitModel(ActiveState='activating', SubState='auto-restart', MainPID=0)))

    def test_unreadable_output_during_the_window_is_not_healthy(self):
        # R1: an unreadable read inside the window is a failed check (False), not an exception, so the
        # update path can stop the new code and restore the previous release.
        model = UnitModel()

        def garbage(m):
            if m.show_calls == 2:
                m.fields = {'bogus': 'x'}
        model.fields = dict(model.fields)
        model.on_poll = garbage
        with mock.patch.object(lc.subprocess, 'run', side_effect=model.run):
            self.assertFalse(lc.healthy(check=lambda: True, seconds=3, sleep=lambda s: None, clock=_FakeClock().now))


class _FakeClock:
    """Advances one second per call so a window of N seconds polls about N times."""

    def __init__(self):
        self.t = 0.0

    def now(self):
        self.t += 0.5
        return self.t


if __name__ == '__main__':
    unittest.main()
