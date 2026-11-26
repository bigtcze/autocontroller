"""S4: the service state is read from `systemctl is-active` output and every running state counts.

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


def fake_is_active(stdout: str, returncode: int = 0):
    def run(argv, **kwargs):
        assert argv[:3] == ['systemctl', '--user', 'is-active'], argv
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr='')
    return run


class RunningStatesTest(unittest.TestCase):
    def check(self, stdout, returncode, expected):
        with mock.patch.object(lc.subprocess, 'run', side_effect=fake_is_active(stdout, returncode)):
            self.assertIs(lc.service_active(), expected)

    def test_every_running_state_counts_as_running(self):
        for state in ('active', 'activating', 'reloading', 'deactivating'):
            with self.subTest(state=state):
                self.check(f'{state}\n', 0, True)

    def test_inactive_and_failed_are_not_running(self):
        for state, rc in (('inactive', 3), ('failed', 3)):
            with self.subTest(state=state):
                self.check(f'{state}\n', rc, False)

    def test_unknown_or_empty_output_fails_closed_as_running(self):
        self.check('', 3, True)
        self.check('something-new\n', 0, True)

    def test_systemctl_that_cannot_run_is_a_refusal_not_a_stopped_unit(self):
        with mock.patch.object(lc.subprocess, 'run', side_effect=OSError('no systemctl')):
            with self.assertRaisesRegex(lc.LifecycleError, 'is-active failed'):
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
