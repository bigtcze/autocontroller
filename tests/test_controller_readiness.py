"""Startup readiness (issue #20): state/ready.json binds readiness to the current controller process.

Readiness proves only that Engram and every enabled worker initialized in THIS process. It is not
proof of continuous task progress. Offline and deterministic: lifecycle cases reuse the release-archive
World from test_lifecycle_update (virtual clock, fake service); controller cases load
autonomy-controller.py with fake Engram/workers and never touch GitHub, OpenCode, systemctl or the real
state directory. Only the self-pid parse test reads the real /proc.
"""
from __future__ import annotations

import importlib.util
import json
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS))
import lifecycle as lc  # noqa: E402
from test_lifecycle_update import Base, FakeClock, fake_start_ticks, good_marker  # noqa: E402
from test_drain import DrainFixture, wait_until  # noqa: E402

spec = importlib.util.spec_from_file_location('readiness_controller', ROOT / 'autonomy-controller.py')
ctl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ctl)

HEALTHY_STATE = {'ActiveState': 'active', 'SubState': 'running', 'MainPID': 4242, 'NRestarts': 0}


def stat_line(pid: int, comm: str, starttime: int) -> str:
    """A /proc/<pid>/stat line: fields 3..22 follow the last ')'; starttime is field 22 (index 19)."""
    fields = ['S'] + ['0'] * 18 + [str(starttime)]
    return f'{pid} ({comm}) ' + ' '.join(fields) + '\n'


class ProcessStartTicksTest(unittest.TestCase):
    def test_parses_starttime_when_comm_has_spaces_and_parentheses(self):
        line = stat_line(77, 'odd (name) x', 424242)
        with mock.patch.object(lc.Path, 'read_text', return_value=line):
            self.assertEqual(lc.process_start_ticks(77), 424242)
        with mock.patch.object(ctl.Path, 'read_text', return_value=line):
            self.assertEqual(ctl.process_start_ticks(77), 424242)

    def test_real_self_pid_returns_a_positive_integer(self):
        self.assertIsInstance(lc.process_start_ticks(os.getpid()), int)
        self.assertGreater(ctl.process_start_ticks(os.getpid()), 0)

    def test_missing_pid_returns_none(self):
        self.assertIsNone(lc.process_start_ticks(999999999))
        self.assertIsNone(ctl.process_start_ticks(999999999))


class ControllerReadyTest(unittest.TestCase):
    """lifecycle.controller_ready: only the marker of THIS pid and its current start ticks is ready."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='ready-')
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / 'state'
        self.state.mkdir(mode=0o700)
        patcher = mock.patch.object(lc, 'process_start_ticks', side_effect=fake_start_ticks)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, doc):
        (self.state / lc.CONTROLLER_READY).write_text(doc if isinstance(doc, str) else json.dumps(doc))

    def test_current_marker_is_ready(self):
        self.write(good_marker(1234))
        self.assertTrue(lc.controller_ready(self.state, 1234))

    def test_missing_marker_is_not_ready(self):
        self.assertFalse(lc.controller_ready(self.state, 1234))

    def test_previous_pid_marker_is_stale(self):
        self.write(good_marker(1233))
        self.assertFalse(lc.controller_ready(self.state, 1234))

    def test_same_pid_with_other_start_ticks_is_stale_pid_reuse(self):
        self.write({**good_marker(1234), 'started': 1})
        self.assertFalse(lc.controller_ready(self.state, 1234))

    def test_unknown_schema_is_not_ready(self):
        self.write({**good_marker(1234), 'schema': 2})
        self.assertFalse(lc.controller_ready(self.state, 1234))

    def test_garbage_and_non_integer_fields_are_not_ready(self):
        for doc in ('{not json', [], {'schema': 1, 'pid': '1234', 'started': fake_start_ticks(1234)},
                    {'schema': 1, 'pid': True, 'started': fake_start_ticks(1234)}, {**good_marker(1234), 'started': None}):
            with self.subTest(doc=doc):
                self.write(doc)
                self.assertFalse(lc.controller_ready(self.state, 1234))

    def test_process_gone_is_not_ready(self):
        self.write(good_marker(1234))
        with mock.patch.object(lc, 'process_start_ticks', return_value=None):
            self.assertFalse(lc.controller_ready(self.state, 1234))


class SyncControllerReadyTest(unittest.TestCase):
    """autonomy-controller.sync_controller_ready: publish only when memory and every worker are initialized."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='sync-ready-')
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / 'state'
        self.state.mkdir(mode=0o700)
        self.marker = self.state / ctl.CONTROLLER_READY

    @staticmethod
    def worker(initialized: bool):
        w = SimpleNamespace(initialized=threading.Event())
        if initialized:
            w.initialized.set()
        return w

    def test_not_published_when_memory_is_not_ok(self):
        ctl.sync_controller_ready(self.state, [self.worker(True)], memory_ok=False)
        self.assertFalse(self.marker.exists())

    def test_not_published_while_any_worker_is_uninitialized(self):
        ctl.sync_controller_ready(self.state, [self.worker(True), self.worker(False)], memory_ok=True)
        self.assertFalse(self.marker.exists())

    def test_zero_enabled_workers_publish_once_memory_is_ok(self):
        ctl.sync_controller_ready(self.state, [], memory_ok=True)
        self.assertTrue(self.marker.exists())

    def test_marker_binds_this_process_and_its_real_start_ticks(self):
        ctl.sync_controller_ready(self.state, [self.worker(True)], memory_ok=True)
        doc = json.loads(self.marker.read_text(encoding='utf-8'))
        self.assertEqual(doc['schema'], 1)
        self.assertEqual(doc['pid'], os.getpid())
        self.assertEqual(doc['started'], ctl.process_start_ticks(os.getpid()))
        self.assertEqual(self.marker.stat().st_mode & 0o777, 0o600)

    def test_lifecycle_accepts_the_marker_written_by_this_process(self):
        ctl.sync_controller_ready(self.state, [self.worker(True)], memory_ok=True)
        self.assertTrue(lc.controller_ready(self.state, os.getpid()))


class WorkerStartupTest(DrainFixture):
    """ProjectWorker.run: initialization for readiness, including a backoff persisted by a previous process."""

    def make(self, iteration):
        w = ctl.ProjectWorker(dict(self.cfg), self.global_cfg, self.stop, threading.Lock())
        w.lock_path = self.lock
        w.park_poll = 0.05
        w.validate = lambda ignore_conflicts=False: None
        w.one_iteration = iteration
        return w

    def test_persisted_backoff_from_a_previous_process_does_not_block_readiness(self):
        counts = {'validate': 0, 'iterations': 0}

        def iteration():
            counts['iterations'] += 1
            return 300
        w = self.make(iteration)

        def validate(ignore_conflicts=False):
            counts['validate'] += 1
        w.validate = validate
        w.pdir.mkdir(parents=True, exist_ok=True)
        w.worker_backoff.write_text(json.dumps({'failures': 3, 'until': time.time() + 3600, 'reason': 'old'}))
        self.start(w)
        self.assertTrue(wait_until(w.initialized.is_set), 'validation must run before the persisted backoff')
        self.assertEqual(counts['validate'], 1)
        self.assertEqual(counts['iterations'], 0, 'the persisted backoff still holds the task')

    def test_failed_initial_validation_pauses_and_is_not_retried_in_a_tight_loop(self):
        counts = {'validate': 0}
        w = self.make(lambda: 300)

        def validate(ignore_conflicts=False):
            counts['validate'] += 1
            raise ctl.ControllerError('github unreachable')
        w.validate = validate
        self.start(w)
        self.assertTrue(wait_until(w.worker_backoff.exists), 'a failure must go through worker_pause')
        time.sleep(0.3)
        self.assertEqual(counts['validate'], 1)
        self.assertFalse(w.initialized.is_set())

    def test_idle_worker_is_initialized_without_completing_a_task(self):
        # An idle poll (nothing to do) and a CI-waiting poll both return a wait; neither completes a task.
        seen = []
        w = self.make(lambda: seen.append(1) or 90)
        self.start(w)
        self.assertTrue(wait_until(w.initialized.is_set))
        self.assertTrue(wait_until(lambda: seen))
        self.assertFalse(w.worker_backoff.exists())


class MainReadinessTest(unittest.TestCase):
    """autonomy-controller.main: marker lifecycle around Engram startup and the worker loop."""

    class Harness:
        startup_error = None
        worker_initialized = True
        observed: list = []
        marker_at_startup = 'unset'

    class FakeRuntime:
        def __init__(self, state):
            self.failed = Path(state) / 'memory' / 'failure'
            self.failed.parent.mkdir(parents=True, exist_ok=True)

        def metric(self, healthy):
            pass

        def startup(self):
            # A marker from a previous process must already be gone before Engram startup begins.
            MainReadinessTest.Harness.marker_at_startup = (MainReadinessTest.state / ctl.CONTROLLER_READY).exists()
            if MainReadinessTest.Harness.startup_error:
                raise MainReadinessTest.Harness.startup_error

        def check(self, full=False):
            # Runs once per loop, after the loop's readiness publication. Record the marker, then stop.
            path = MainReadinessTest.state / ctl.CONTROLLER_READY
            MainReadinessTest.Harness.observed.append(json.loads(path.read_text()) if path.exists() else None)
            os.kill(os.getpid(), signal.SIGTERM)

        def close(self):
            pass

    class FakeWorker:
        def __init__(self, cfg, global_cfg, stop, quota_lock):
            self.name = str(cfg['name'])
            self.stop = stop
            self.pending = Path(global_cfg['state_dir']) / 'no-such-marker'
            self.initialized = threading.Event()
            if MainReadinessTest.Harness.worker_initialized:
                self.initialized.set()

        def run(self):
            self.stop.wait(60)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='main-ready-')
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        install = base / 'install'
        MainReadinessTest.state = install / 'state'
        MainReadinessTest.state.mkdir(mode=0o700, parents=True)
        config = base / 'config.json'
        config.write_text(json.dumps({'projects': [{
            'name': 'alpha', 'repo': 'example-owner/alpha', 'project_owner': 'example-owner',
            'project_number': 1, 'directory': str(base / 'repositories' / 'alpha')}]}))
        MainReadinessTest.Harness.startup_error = None
        MainReadinessTest.Harness.worker_initialized = True
        MainReadinessTest.Harness.observed = []
        self.marker = MainReadinessTest.state / ctl.CONTROLLER_READY
        patches = [mock.patch.object(ctl, 'DEFAULT_STATE', MainReadinessTest.state),
                   mock.patch.object(ctl, 'INSTALL_ROOT', install),
                   mock.patch.object(ctl.learning, 'Runtime', MainReadinessTest.FakeRuntime),
                   mock.patch.object(ctl, 'ProjectWorker', MainReadinessTest.FakeWorker),
                   mock.patch.object(sys, 'argv', ['autonomy-controller.py', '--config', str(config)])]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        old_umask = os.umask(0o077)
        self.addCleanup(os.umask, old_umask)
        saved = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
        self.addCleanup(lambda: [signal.signal(s, h) for s, h in saved.items()])

    def test_marker_is_published_in_the_loop_and_removed_on_shutdown(self):
        self.assertEqual(ctl.main(), 0)
        observed = MainReadinessTest.Harness.observed
        self.assertTrue(observed and observed[0] is not None, 'marker must exist during the loop')
        self.assertEqual(observed[0]['pid'], os.getpid())
        self.assertEqual(observed[0]['started'], ctl.process_start_ticks(os.getpid()))
        self.assertFalse(self.marker.exists(), 'shutdown must invalidate readiness')

    def test_uninitialized_worker_publishes_nothing(self):
        MainReadinessTest.Harness.worker_initialized = False
        self.assertEqual(ctl.main(), 0)
        self.assertEqual(MainReadinessTest.Harness.observed[0], None)

    def test_failed_engram_startup_publishes_nothing_and_removes_a_stale_marker(self):
        self.marker.write_text(json.dumps({'schema': 1, 'pid': 1, 'started': 1, 'at': 1}))
        MainReadinessTest.Harness.startup_error = RuntimeError('Engram startup/functional probe failed')
        self.assertEqual(ctl.main(), 70)
        self.assertFalse(MainReadinessTest.Harness.marker_at_startup, 'stale marker removed before Engram startup')
        self.assertFalse(self.marker.exists())
        self.assertEqual(MainReadinessTest.Harness.observed, [], 'the worker loop must not start')


class ProbeRequirementTest(unittest.TestCase):
    """lifecycle.healthy readiness: observed AND window elapsed; bounded; reasons are reported."""

    def run_healthy(self, ready, *, state=HEALTHY_STATE, seconds=3, ready_timeout=10, check=lambda: True):
        clock = FakeClock()
        reasons: list[str] = []
        with mock.patch.object(lc, 'unit_state', side_effect=lambda: dict(state)):
            ok = lc.healthy(check=check, seconds=seconds, sleep=clock.sleep, clock=clock, ready=ready,
                            ready_timeout=ready_timeout, reasons=reasons)
        return ok, reasons, clock

    def test_never_ready_fails_at_the_bound_with_a_readiness_reason(self):
        ok, reasons, clock = self.run_healthy(lambda pid: False)
        self.assertFalse(ok)
        self.assertEqual(clock.now, 10, 'the bound is max(seconds, ready_timeout)')
        self.assertIn('did not publish startup readiness', reasons[0])
        self.assertIn('within 10s', reasons[0])

    def test_readiness_observed_late_but_inside_the_bound_passes_after_the_window(self):
        clock = FakeClock()
        reasons: list[str] = []
        with mock.patch.object(lc, 'unit_state', side_effect=lambda: dict(HEALTHY_STATE)):
            ok = lc.healthy(check=lambda: True, seconds=3, sleep=clock.sleep, clock=clock,
                            ready=lambda pid: clock.now >= 5, ready_timeout=10, reasons=reasons)
        self.assertTrue(ok)
        self.assertEqual(clock.now, 5, 'passes once readiness is seen and the window has elapsed')
        self.assertEqual(reasons, [])

    def test_check_failure_reports_check_and_never_asks_for_readiness(self):
        asked = []
        ok, reasons, _ = self.run_healthy(lambda pid: asked.append(pid) or True, check=lambda: False)
        self.assertFalse(ok)
        self.assertEqual(reasons, ['--check failed'])
        self.assertEqual(asked, [])

    def test_inactive_first_poll_reports_not_running(self):
        ok, reasons, _ = self.run_healthy(lambda pid: True, state={**HEALTHY_STATE, 'ActiveState': 'activating',
                                                                   'SubState': 'auto-restart', 'MainPID': 0})
        self.assertFalse(ok)
        self.assertEqual(reasons, ['service not running after start'])

    def test_pid_change_with_readiness_observed_still_fails_with_its_reason(self):
        clock = FakeClock()
        polls = {'n': 0}

        def changing(**_):
            polls['n'] += 1
            return dict(HEALTHY_STATE, MainPID=HEALTHY_STATE['MainPID'] if polls['n'] == 1 else 9999)
        reasons: list[str] = []
        with mock.patch.object(lc, 'unit_state', side_effect=lambda: changing()):
            ok = lc.healthy(check=lambda: True, seconds=3, sleep=clock.sleep, clock=clock,
                            ready=lambda pid: True, reasons=reasons)
        self.assertFalse(ok)
        self.assertTrue(any('changed during the health window (MainPID/NRestarts)' in r for r in reasons), reasons)


class UpdateReadinessTest(Base):
    """update/rollback with the fake service: stale, missing and inactive readiness, plus reasons in history."""

    def update(self, **kw):
        kw.setdefault('ready_timeout', 5)
        return super().update(**kw)

    def expect_stale_rollback(self, make):
        self.service.marker_for['v1.1.0'] = make
        with self.assertRaisesRegex(lc.LifecycleError, 'did not publish startup readiness'):
            self.update()
        self.assertEqual(self.world.tag(), 'v1.0.0')
        self.assertEqual(self.history()[-1]['result'], 'rolled_back')
        self.assertIn('did not publish startup readiness', self.history()[-1]['reason'])

    def test_current_marker_is_accepted_and_bound_to_the_running_pid(self):
        self.assertIn('v1.0.0 -> v1.1.0', self.update())
        self.assertTrue(lc.controller_ready(self.world.state_dir, self.service.pid))
        self.assertEqual(self.history()[-1]['result'], 'ok')

    def test_marker_from_a_previous_pid_is_stale(self):
        self.expect_stale_rollback(lambda pid: good_marker(pid - 1))

    def test_same_pid_with_other_start_ticks_is_stale(self):
        self.expect_stale_rollback(lambda pid: {**good_marker(pid), 'started': 1})

    def test_unknown_schema_marker_is_stale(self):
        self.expect_stale_rollback(lambda pid: {**good_marker(pid), 'schema': 2})

    def test_garbage_marker_is_stale(self):
        self.expect_stale_rollback('{not json')

    def test_missing_readiness_times_out_and_rolls_back_with_a_reason(self):
        self.service.silent_tags.add('v1.1.0')
        with self.assertRaisesRegex(lc.LifecycleError, 'did not publish startup readiness.*within 5s.*restored and running'):
            self.update()
        self.assertEqual(self.world.tag(), 'v1.0.0')
        self.assertTrue(self.service.active)
        self.assertEqual(self.history()[-1]['result'], 'rolled_back')
        self.assertIn('within 5s', self.history()[-1]['reason'])

    def test_rollback_without_readiness_restores_the_current_release(self):
        self.update()
        self.service.silent_tags.add('v1.0.0')
        with self.assertRaisesRegex(lc.LifecycleError, 'did not publish startup readiness.*current release was restored'):
            self.rollback(ready_timeout=5)
        self.assertEqual(self.world.tag(), 'v1.1.0')
        self.assertEqual(self.history()[-1]['result'], 'failed_restored')
        self.assertIn('did not publish startup readiness', self.history()[-1]['reason'])

    def test_readiness_does_not_mask_a_main_pid_change(self):
        original_stop = lc.stop_service

        def moved(seconds):
            if ('start' in self.service.verbs and self.service.show_override is None
                    and self.service.installed_tag() == 'v1.1.0'):
                self.service.show_override = lambda s: {'ActiveState': 'active', 'SubState': 'running',
                                                        'MainPID': 9999, 'NRestarts': 0}

        def stop_and_clear():
            original_stop()
            self.service.show_override = None
        lc.stop_service = stop_and_clear
        self.addCleanup(setattr, lc, 'stop_service', original_stop)
        self.world.on_sleep = moved
        with self.assertRaisesRegex(lc.LifecycleError, 'changed during the health window'):
            self.update()
        self.assertEqual(self.world.tag(), 'v1.0.0')

    def test_inactive_service_needs_no_readiness(self):
        self.service.active = False
        self.service.silent_tags.add('v1.1.0')
        self.assertIn('v1.0.0 -> v1.1.0', self.update())
        self.assertEqual(self.service.verbs, ['stop'], 'an inactive service is never started for readiness')

    def test_stop_after_drain_invalidates_the_marker_before_restart(self):
        self.world.state_dir.mkdir(parents=True, exist_ok=True)
        (self.world.state_dir / lc.CONTROLLER_READY).write_text(json.dumps(good_marker(1000)))
        seen = {}
        original_start = lc.start_service

        def spy():
            seen['marker'] = (self.world.state_dir / lc.CONTROLLER_READY).exists()
            return original_start()
        lc.start_service = spy
        self.addCleanup(setattr, lc, 'start_service', original_start)
        self.update()
        self.assertFalse(seen['marker'], 'a stopped controller must not leave readiness behind')


if __name__ == '__main__':
    unittest.main()
