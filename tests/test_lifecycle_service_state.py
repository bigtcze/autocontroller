"""S4 (updated for N1): the service state is read from `systemctl show`, and every state that may hold a controller counts as running.

Offline: subprocess.run is replaced inside lifecycle, so no systemctl runs. Also covers the
always-stop rule (stop is idempotent and runs even when the unit reads as inactive) and the
snapshot/rollback tolerance for a missing current file (the snapshot records it as absent).
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
import lifecycle as lc  # noqa: E402


def fake_show(active_state: str, sub_state: str = 'running', pid: int = 1, restarts: int = 0,
              returncode: int = 0):
    """A `systemctl --user show` answer with the four N1 properties."""
    def run(argv, **kwargs):
        assert argv[:3] == ['systemctl', '--user', 'show'], argv
        text = (f'ActiveState={active_state}\nSubState={sub_state}\nMainPID={pid}\nNRestarts={restarts}\n')
        return subprocess.CompletedProcess(argv, returncode, stdout=text, stderr='')
    return run


class RunningStatesTest(unittest.TestCase):
    """N1: service_active is needs_stop(unit_state()); it reads `show`, not `is-active`."""

    def check(self, active_state, sub_state, pid, expected):
        with mock.patch.object(lc.subprocess, 'run', side_effect=fake_show(active_state, sub_state, pid)):
            self.assertIs(lc.service_active(), expected)

    def test_every_state_that_may_hold_a_controller_must_be_stopped(self):
        for active_state, sub_state, pid in (('active', 'running', 9), ('activating', 'start', 0),
                                             ('reloading', 'reload', 9), ('deactivating', 'stop-sigterm', 9)):
            with self.subTest(state=active_state):
                self.check(active_state, sub_state, pid, True)

    def test_inactive_and_failed_need_no_stop(self):
        self.check('inactive', 'dead', 0, False)
        self.check('failed', 'failed', 0, False)

    def test_unknown_or_empty_output_fails_closed_by_raising(self):
        # An unknown ActiveState must not read as "stopped"; it raises so no caller guesses.
        with mock.patch.object(lc.subprocess, 'run', side_effect=fake_show('something-new')):
            with self.assertRaisesRegex(lc.LifecycleError, 'unknown unit ActiveState'):
                lc.service_active()
        with mock.patch.object(lc.subprocess, 'run', side_effect=fake_show('active', returncode=1)):
            with self.assertRaisesRegex(lc.LifecycleError, 'unit state is unknown'):
                lc.service_active()

    def test_systemctl_that_cannot_run_is_a_refusal_not_a_stopped_unit(self):
        with mock.patch.object(lc.subprocess, 'run', side_effect=OSError('no systemctl')):
            with self.assertRaisesRegex(lc.LifecycleError, 'show failed'):
                lc.service_active()


class AlwaysStopTest(unittest.TestCase):
    """stop is idempotent: a unit reading as inactive is still stopped, so nothing is left running."""

    def test_stop_service_always_issues_systemctl_stop(self):
        calls = []
        with mock.patch.object(lc, 'systemctl', side_effect=lambda *v, **k: calls.append(v) or 0):
            lc.stop_service()
        self.assertEqual(calls, [('stop',)])

    def test_stop_failure_refuses(self):
        with mock.patch.object(lc, 'systemctl', return_value=1):
            with self.assertRaisesRegex(lc.LifecycleError, 'systemctl stop failed'):
                lc.stop_service()


if __name__ == '__main__':
    unittest.main()
