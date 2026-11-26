"""Offline tests for the graceful drain handshake: request, park, ready.

Workers run on real threads with a fake `one_iteration`; nothing touches GitHub, OpenCode or systemd.
"""
import fcntl
import importlib.util
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

DIR = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('controller_drain', DIR / 'autonomy-controller.py')
ctl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ctl)


def wait_until(predicate, timeout=10.0):
    # Margin over the worst case: a parked worker re-checks every park_poll (set short in tests).
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class DrainFixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / 'state'
        self.state.mkdir(mode=0o700)
        self.directory = Path(tmp.name) / 'checkout'
        self.directory.mkdir()
        self.stop = threading.Event()
        self.addCleanup(self.stop.set)
        # S2: a drain request is live only while an updater holds this lock; tests hold it per drain.
        self.lock = Path(tmp.name) / 'lifecycle.lock'
        self.lock.touch()
        self.cfg = {'name': 'alpha', 'repo': 'example-owner/alpha', 'project_owner': 'example-owner',
                    'project_number': 1, 'directory': str(self.directory)}
        self.global_cfg = {'state_dir': str(self.state), 'wrapper': str(DIR / 'opencode-project-safe.py'),
                           'opencode': '/nonexistent/opencode', 'poll_seconds': 300}

    def hold_lock(self):
        """Hold lifecycle.lock exclusively, as an update/rollback does for the whole drain."""
        handle = self.lock.open('a')
        self.addCleanup(handle.close)
        fcntl.flock(handle, fcntl.LOCK_EX)

    def worker(self, name='alpha', iteration=None):
        cfg = dict(self.cfg, name=name)
        w = ctl.ProjectWorker(cfg, self.global_cfg, self.stop, threading.Lock())
        w.lock_path = self.lock
        w.park_poll = 0.05  # S5: a parked worker wakes within 50ms, far inside wait_until's margin
        w._validated = True  # skip the GitHub validation step; drain logic is under test
        w.calls = 0
        w.one_iteration = iteration or (lambda: self._count(w))
        return w

    def _count(self, w):
        w.calls += 1
        return 1

    def request(self, nonce='n-1'):
        """A live drain: the request is written while its updater holds lifecycle.lock."""
        if getattr(self, '_held', None) is None:
            self.hold_lock()
            self._held = True
        ctl.json_atomic(self.state / ctl.DRAIN_REQUEST, {'nonce': nonce, 'requested_at': time.time()})

    def start(self, worker):
        thread = threading.Thread(target=worker.run, daemon=True)
        thread.start()
        self.addCleanup(lambda: (self.stop.set(), thread.join(10)))
        return thread


class ReadDrainRequestTests(DrainFixture):
    def test_absent_request_means_no_drain(self):
        self.assertIsNone(ctl.read_drain_request(self.state))

    def test_valid_request_returns_its_nonce(self):
        self.request('abc')
        self.assertEqual(ctl.read_drain_request(self.state, self.lock)['nonce'], 'abc')

    def test_unparseable_request_still_drains_without_nonce(self):
        self.hold_lock()
        (self.state / ctl.DRAIN_REQUEST).write_text('{not json', encoding='utf-8')
        self.assertEqual(ctl.read_drain_request(self.state, self.lock)['nonce'], None)

    def test_request_without_nonce_still_drains_without_nonce(self):
        self.hold_lock()
        (self.state / ctl.DRAIN_REQUEST).write_text('{"requested_at": 1}', encoding='utf-8')
        self.assertEqual(ctl.read_drain_request(self.state, self.lock)['nonce'], None)

    def test_dangling_symlink_request_is_treated_as_present(self):
        self.hold_lock()
        os.symlink(self.state / 'missing-target', self.state / ctl.DRAIN_REQUEST)
        request = ctl.read_drain_request(self.state, self.lock)
        self.assertIsNotNone(request, 'a dangling request is unreadable, not absent')
        self.assertIsNone(request['nonce'])


class SyncDrainReadyTests(DrainFixture):
    def test_no_request_writes_no_ready_file(self):
        w = self.worker()
        w.parked.set()
        ctl.sync_drain_ready(self.state, [w], self.lock)
        self.assertFalse((self.state / ctl.DRAIN_READY).exists())

    def test_ready_is_written_with_nonce_when_every_worker_is_parked(self):
        a, b = self.worker('alpha'), self.worker('beta')
        a.parked.set()
        b.parked.set()
        self.request('nonce-42')
        ctl.sync_drain_ready(self.state, [a, b], self.lock)
        ready = self.state / ctl.DRAIN_READY
        doc = json.loads(ready.read_text(encoding='utf-8'))
        self.assertEqual(doc['nonce'], 'nonce-42')
        self.assertEqual(doc['pid'], os.getpid())
        self.assertEqual(ready.stat().st_mode & 0o777, 0o600)

    def test_ready_is_not_written_while_any_worker_is_still_busy(self):
        a, b = self.worker('alpha'), self.worker('beta')
        a.parked.set()
        self.request()
        ctl.sync_drain_ready(self.state, [a, b], self.lock)
        self.assertFalse((self.state / ctl.DRAIN_READY).exists())

    def test_ready_is_removed_when_request_is_removed(self):
        w = self.worker()
        w.parked.set()
        self.request()
        ctl.sync_drain_ready(self.state, [w], self.lock)
        self.assertTrue((self.state / ctl.DRAIN_READY).exists())
        (self.state / ctl.DRAIN_REQUEST).unlink()
        ctl.sync_drain_ready(self.state, [w], self.lock)
        self.assertFalse((self.state / ctl.DRAIN_READY).exists())

    def test_invalid_request_blocks_work_but_never_writes_ready(self):
        w = self.worker()
        w.parked.set()
        self.hold_lock()
        (self.state / ctl.DRAIN_REQUEST).write_text('garbage', encoding='utf-8')
        ctl.sync_drain_ready(self.state, [w], self.lock)
        self.assertFalse((self.state / ctl.DRAIN_READY).exists())


class WorkerParkingTests(DrainFixture):
    def test_parked_worker_never_starts_an_iteration(self):
        self.request()
        w = self.worker()
        self.start(w)
        self.assertTrue(wait_until(w.parked.is_set), 'worker must park while a request is present')
        time.sleep(0.2)
        self.assertEqual(w.calls, 0)

    def test_in_flight_iteration_finishes_before_the_worker_parks(self):
        release = threading.Event()
        entered = threading.Event()
        state = {'calls': 0}

        def slow_iteration():
            state['calls'] += 1
            entered.set()
            release.wait(10)
            return 1

        w = self.worker(iteration=slow_iteration)
        self.start(w)
        self.assertTrue(entered.wait(5))
        self.request()
        time.sleep(0.1)
        self.assertFalse(w.parked.is_set(), 'a running iteration is never interrupted by a park')
        release.set()
        self.assertTrue(wait_until(w.parked.is_set))
        self.assertEqual(state['calls'], 1, 'the drain must not start a second iteration')

    def test_removing_the_request_resumes_work(self):
        self.request()
        w = self.worker()
        self.start(w)
        self.assertTrue(wait_until(w.parked.is_set))
        (self.state / ctl.DRAIN_REQUEST).unlink()
        self.assertTrue(wait_until(lambda: w.calls >= 1), 'work resumes after the request is cleared')
        self.assertFalse(w.parked.is_set())

    def test_main_loop_ready_publication_follows_worker_parking(self):
        self.request('handshake')
        w = self.worker()
        self.start(w)
        ready = self.state / ctl.DRAIN_READY
        self.assertFalse(ready.exists(), 'ready must not appear before the worker parks')
        self.assertTrue(wait_until(w.parked.is_set))
        ctl.sync_drain_ready(self.state, [w], self.lock)
        self.assertEqual(json.loads(ready.read_text(encoding='utf-8'))['nonce'], 'handshake')


if __name__ == '__main__':
    unittest.main()
