"""Offline tests for the drain handshake (nonce + live pid), withdrawal and config validation."""
from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import lifecycle as lc  # noqa: E402

SPEC = importlib.util.spec_from_file_location('controller', ROOT / 'autonomy-controller.py')
ctl = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ctl)


class FakeClock:
    """Virtual time: sleeping advances the clock, so timeouts never wait for real seconds."""

    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class DrainTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name) / 'state'
        self.state.mkdir(mode=0o700)
        self.clock = FakeClock()

    def tearDown(self):
        self._tmp.cleanup()

    def controller_answers_after(self, polls: int, pid: int = 4242):
        """Sleep hook that plays the controller: after `polls` sleeps it writes drain.ready with the request's nonce."""
        def sleep(seconds):
            self.clock.sleep(seconds)
            if len(self.clock.sleeps) == polls:
                nonce = json.loads((self.state / lc.DRAIN_REQUEST).read_text())['nonce']
                (self.state / lc.DRAIN_READY).write_text(json.dumps({'nonce': nonce, 'pid': pid, 'at': 1}))
        return sleep

    def kwargs(self, **extra):
        return {'timeout': 60, 'alive': lambda pid: True, 'clock': self.clock, **extra}

    def test_constants_match_controller(self):
        self.assertEqual(lc.DRAIN_REQUEST, ctl.DRAIN_REQUEST)
        self.assertEqual(lc.DRAIN_READY, ctl.DRAIN_READY)

    def test_request_visible_during_body_and_withdrawn_after(self):
        with lc.drain(self.state, **self.kwargs(sleep=self.controller_answers_after(2))) as nonce:
            request = json.loads((self.state / lc.DRAIN_REQUEST).read_text())
            self.assertEqual(request['nonce'], nonce)
            self.assertEqual(len(nonce), 32)
        self.assertFalse((self.state / lc.DRAIN_REQUEST).exists())
        self.assertFalse((self.state / lc.DRAIN_READY).exists())

    def test_polls_once_per_second(self):
        with lc.drain(self.state, **self.kwargs(sleep=self.controller_answers_after(3))):
            pass
        self.assertEqual(self.clock.sleeps, [1.0, 1.0, 1.0])

    def test_stale_ready_from_earlier_drain_never_counts(self):
        (self.state / lc.DRAIN_READY).write_text(json.dumps({'nonce': 'old', 'pid': 4242}))
        with self.assertRaisesRegex(lc.LifecycleError, 'did not drain'):
            with lc.drain(self.state, **self.kwargs(timeout=3, sleep=self.clock.sleep)):
                self.fail('body must not run without a drained controller')
        self.assertFalse((self.state / lc.DRAIN_REQUEST).exists())
        self.assertFalse((self.state / lc.DRAIN_READY).exists())

    def test_timeout_withdraws_request_and_never_enters_body(self):
        entered = []
        with self.assertRaisesRegex(lc.LifecycleError, 'service left running'):
            with lc.drain(self.state, **self.kwargs(timeout=5, sleep=self.clock.sleep)):
                entered.append(True)
        self.assertEqual(entered, [])
        self.assertFalse((self.state / lc.DRAIN_REQUEST).exists())

    def test_default_timeout_is_one_hour(self):
        self.assertEqual(lc.DEFAULT_DRAIN_TIMEOUT, 3600)

    def test_error_in_body_still_withdraws_request(self):
        with self.assertRaises(RuntimeError):
            with lc.drain(self.state, **self.kwargs(sleep=self.controller_answers_after(1))):
                raise RuntimeError('swap failed')
        self.assertFalse((self.state / lc.DRAIN_REQUEST).exists())
        self.assertFalse((self.state / lc.DRAIN_READY).exists())

    def test_dead_controller_pid_is_not_drained(self):
        (self.state / lc.DRAIN_READY).parent.mkdir(parents=True, exist_ok=True)
        nonce = 'n' * 32
        (self.state / lc.DRAIN_READY).write_text(json.dumps({'nonce': nonce, 'pid': 4242}))
        self.assertFalse(lc.drain_ready(self.state, nonce, alive=lambda pid: False))
        self.assertTrue(lc.drain_ready(self.state, nonce, alive=lambda pid: True))

    def test_invalid_pid_values_are_rejected(self):
        nonce = 'n' * 32
        for pid in (0, -1, '4242', True, None, 4.2):
            with self.subTest(pid=pid):
                (self.state / lc.DRAIN_READY).write_text(json.dumps({'nonce': nonce, 'pid': pid}))
                self.assertFalse(lc.drain_ready(self.state, nonce, alive=lambda p: True))

    def test_unreadable_ready_file_is_not_drained(self):
        (self.state / lc.DRAIN_READY).write_text('{garbage')
        self.assertFalse(lc.drain_ready(self.state, 'n' * 32, alive=lambda p: True))

    def test_pid_alive_sees_this_process(self):
        self.assertTrue(lc.pid_alive(os.getpid()))


class ControllerDrainContractTest(unittest.TestCase):
    """The real controller's sync_drain_ready must produce what lifecycle.drain_ready accepts."""

    def test_sync_writes_ready_that_lifecycle_accepts(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            nonce = 'a' * 32
            ctl.json_atomic(state / ctl.DRAIN_REQUEST, {'nonce': nonce, 'requested_at': 1.0})
            # S2: the request is live only while its updater holds lifecycle.lock.
            lock = state / 'lifecycle.lock'
            lock.touch()
            held = lock.open('a')
            self.addCleanup(held.close)
            fcntl.flock(held, fcntl.LOCK_EX)

            class Parked:
                def __init__(self):
                    self.parked = type('E', (), {'is_set': lambda s: True})()

            ctl.sync_drain_ready(state, [Parked()], lock)
            self.assertTrue(lc.drain_ready(state, nonce, alive=lambda pid: pid == os.getpid()))


class UpdateRepositoryConfigTest(unittest.TestCase):
    def config(self, **extra):
        cfg = {'projects': [{'name': 'demo', 'repo': 'example-owner/demo', 'project_owner': 'example-owner',
                             'project_number': 1, 'directory': '/home/example/repositories/demo'}]}
        cfg.update(extra)
        return cfg

    def test_example_config_carries_default_repository(self):
        example = json.loads((ROOT / 'config.example.json').read_text(encoding='utf-8'))
        self.assertEqual(example['update_repository'], 'bigtcze/autocontroller')
        ctl.validate_config(self.config(update_repository=example['update_repository']))

    def test_absent_key_is_accepted(self):
        ctl.validate_config(self.config())

    def test_malformed_values_are_refused(self):
        bad = ('', 'no-slash', '../etc', '-x/y', 'a/b/c', 'a/..', 'owner/repo;rm', 'owner/', '/repo',
               'https://github.com/a/b', 'a/.hidden', 123, None)
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ctl.ControllerError):
                ctl.validate_config(self.config(update_repository=value))


if __name__ == '__main__':
    unittest.main()
