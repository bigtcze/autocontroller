"""S2: a drain request counts only while the updater holds lifecycle.lock.

Covers the controller reader (autonomy-controller.py), the lifecycle probe and doctor. A request
written without a live lock (a crashed or killed updater) is stale: it never parks the workers
and doctor reports it as a failure with the removal command.
"""
import fcntl
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DIR))
spec = importlib.util.spec_from_file_location('controller_stale', DIR / 'autonomy-controller.py')
ctl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ctl)
import lifecycle as lc  # noqa: E402


class _Event:
    def is_set(self):
        return True


class Parked:
    def __init__(self):
        self.parked = _Event()


class StaleRequestTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.state = base / 'state'
        self.state.mkdir(mode=0o700)
        self.lock = base / 'lifecycle.lock'
        self.request = self.state / ctl.DRAIN_REQUEST
        self.ready = self.state / ctl.DRAIN_READY
        ctl.json_atomic(self.request, {'nonce': 'n1', 'requested_at': time.time()})

    def hold(self):
        """Hold lifecycle.lock exclusively, as update/rollback/remove does for the whole drain."""
        handle = self.lock.open('a')
        self.addCleanup(handle.close)
        fcntl.flock(handle, fcntl.LOCK_EX)
        return handle

    def test_request_without_lock_file_is_stale(self):
        self.assertTrue(ctl.read_drain_request(self.state, self.lock)['stale'])
        self.assertFalse(ctl.drain_requested(self.state, self.lock))

    def test_request_with_unheld_lock_file_is_stale(self):
        self.lock.touch()
        self.assertFalse(ctl.drain_requested(self.state, self.lock))

    def test_request_is_live_while_the_updater_holds_the_lock(self):
        self.hold()
        self.assertTrue(ctl.drain_requested(self.state, self.lock))
        self.assertEqual(ctl.read_drain_request(self.state, self.lock)['nonce'], 'n1')

    def test_stale_request_never_parks_or_publishes_ready(self):
        ctl.sync_drain_ready(self.state, [Parked()], self.lock)
        self.assertFalse(self.ready.exists())

    def test_live_request_publishes_ready_for_parked_workers(self):
        self.hold()
        ctl.sync_drain_ready(self.state, [Parked()], self.lock)
        self.assertEqual(json.loads(self.ready.read_text())['nonce'], 'n1')

    def test_probe_releases_the_lock_it_takes_for_a_stale_check(self):
        self.lock.touch()
        ctl.lifecycle_lock_held(self.lock)
        handle = self.lock.open('a')
        self.addCleanup(handle.close)
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)  # would raise if the probe leaked its lock

    def test_probe_never_creates_a_missing_lock_file(self):
        ctl.lifecycle_lock_held(self.lock)
        self.assertFalse(self.lock.exists())


class DoctorStaleRequestTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.target = Path(tmp.name) / 'install'
        (self.target / 'state').mkdir(parents=True, mode=0o700)
        self.request = self.target / 'state' / lc.DRAIN_REQUEST
        self.request.write_text(json.dumps({'nonce': 'n1', 'requested_at': 1}))
        self.unit = Path(tmp.name) / 'units'
        self.bin = Path(tmp.name) / 'bin'
        self.unit.mkdir()
        self.bin.mkdir()

    def diagnose(self):
        return lc.diagnose(self.target, unit_dir=self.unit, bin_dir=self.bin)

    def test_stale_request_is_a_failure_with_removal_instruction(self):
        failures, _ = self.diagnose()
        stale = [f for f in failures if 'stale state/drain.request' in f]
        self.assertEqual(len(stale), 1, failures)
        self.assertIn(f'rm {self.request}', stale[0])

    def test_request_under_a_held_lock_is_only_a_warning(self):
        handle = (self.target / lc.LIFECYCLE_LOCK).open('a')
        self.addCleanup(handle.close)
        fcntl.flock(handle, fcntl.LOCK_EX)
        failures, warnings = self.diagnose()
        self.assertFalse(any('drain.request' in f for f in failures), failures)
        self.assertTrue(any('workers are draining' in w for w in warnings), warnings)

    def test_no_request_means_no_drain_finding(self):
        self.request.unlink()
        failures, warnings = self.diagnose()
        self.assertFalse(any('drain' in x for x in failures + warnings))

    def test_doctor_does_not_create_the_lock_file(self):
        self.diagnose()
        self.assertFalse((self.target / lc.LIFECYCLE_LOCK).exists())


if __name__ == '__main__':
    unittest.main()
