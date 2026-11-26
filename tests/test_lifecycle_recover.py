"""S3: a leftover rollback/swap blocks update and rollback, shows as a doctor failure, and is
resolved by `rollback --recover`.

Built on tests/test_lifecycle_update.Base (real release archives, fake service, virtual clock).
Nothing touches systemctl, gh, the network or real HOME.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS))
import lifecycle as lc  # noqa: E402
from test_lifecycle_update import Base  # noqa: E402


class SwapGuardTest(Base):
    """A leftover swap is refused before anything is stopped, and doctor reports it."""

    def leave_swap(self):
        swap = self.world.install / 'rollback' / 'swap'
        swap.mkdir(parents=True, mode=0o700)
        return swap

    def test_update_refused_while_swap_exists_and_nothing_stops(self):
        self.leave_swap()
        with self.assertRaisesRegex(lc.LifecycleError, 'rollback/swap exists'):
            self.update()
        self.assertEqual(self.service.verbs, [])
        self.assertEqual(self.world.tag(), 'v1.0.0')

    def test_rollback_refused_while_swap_exists_and_nothing_stops(self):
        self.update()  # creates rollback/previous so rollback is otherwise possible
        self.assertEqual(self.world.tag(), 'v1.1.0')
        self.service.verbs.clear()
        self.leave_swap()
        with self.assertRaisesRegex(lc.LifecycleError, 'rollback/swap exists'):
            self.rollback()
        self.assertEqual(self.service.verbs, [])
        self.assertEqual(self.world.tag(), 'v1.1.0')

    def test_doctor_reports_leftover_swap_as_a_failure_naming_recover(self):
        self.leave_swap()
        unit_dir = Path(tempfile.mkdtemp(prefix='doctor-unit-'))
        self.addCleanup(shutil.rmtree, unit_dir, True)
        failures, _ = lc.diagnose(self.world.install, unit_dir=unit_dir, bin_dir=unit_dir)
        matching = [f for f in failures if 'rollback/swap exists' in f]
        self.assertEqual(len(matching), 1, failures)
        self.assertIn('rollback --recover', matching[0])


class RecoverTest(Base):
    """`recover` restores the tree a rollback replaced, verifies it, and clears the marker."""

    def setUp(self):
        super().setUp()
        self.update()  # install v1.1.0; v1.0.0 kept in rollback/previous
        self.assertEqual(self.world.tag(), 'v1.1.0')
        self.service.verbs.clear()

    def interrupt_rollback(self, *, tamper_swap=None):
        """The state an interrupted rollback leaves: swap holds the current release (v1.1.0) and the
        install already carries the previous files (v1.0.0), before the rollback could finish."""
        install = self.world.install
        previous = install / 'rollback' / 'previous'
        prev_program, prev_absent = lc.snapshot_program(previous)
        current = lc.program_set(install, present=False)
        swap = install / 'rollback' / 'swap'
        lc.snapshot(install, swap, current)
        if tamper_swap:
            tamper_swap(swap)
        lc.install_program_files(previous, install, prev_program, prev_absent)
        self.assertEqual(self.world.tag(), 'v1.0.0')  # precondition: the interruption really happened
        return swap

    def recover(self, **extra):
        kw = {'environ': {}, 'yes': True, 'ask': lambda prompt: 'y', 'drain_timeout': 60,
              'sleep': self.world.answering_sleep, 'clock': self.world.clock}
        kw.update(extra)
        return lc.recover(self.world.install, **kw)

    def test_recover_restores_the_release_the_rollback_replaced(self):
        swap = self.interrupt_rollback()
        result = self.recover()
        self.assertIn('recovered', result)
        self.assertEqual(self.world.tag(), 'v1.1.0')
        self.assertEqual(self.world.text('autonomy-controller.py'), '# autonomy-controller.py two\n')
        self.assertFalse(swap.exists() or swap.is_symlink())
        self.assertEqual(self.history()[-1]['result'], 'recovered')

    def test_recover_with_service_running_stops_drains_and_restarts(self):
        self.interrupt_rollback()
        self.service.active = True
        self.recover()
        self.assertEqual(self.service.verbs, ['stop', 'start'])
        self.assertFalse((self.world.state_dir / lc.DRAIN_REQUEST).exists())

    def test_recover_with_service_stopped_stops_idempotently_and_never_starts(self):
        self.interrupt_rollback()
        self.service.active = False
        self.recover()
        self.assertEqual(self.service.verbs, ['stop'])
        self.assertNotIn('start', self.service.verbs)

    def test_recover_refuses_when_there_is_no_swap(self):
        with self.assertRaisesRegex(lc.LifecycleError, 'no rollback/swap to recover'):
            self.recover()
        self.assertEqual(self.service.verbs, [])

    def test_recover_refuses_an_unreadable_swap_before_stopping_anything(self):
        swap = self.interrupt_rollback()
        (swap / lc.PROGRAM_RECORD).unlink()
        with self.assertRaisesRegex(lc.LifecycleError, 'no readable PROGRAM.json'):
            self.recover()
        self.assertEqual(self.service.verbs, [])
        self.assertEqual(self.world.tag(), 'v1.0.0')
        self.assertTrue(swap.exists())

    def test_tampered_swap_fails_manifest_keeps_swap_and_leaves_service_stopped(self):
        def tamper(swap):
            (swap / 'autonomy-controller.py').write_text('# not the release\n')

        swap = self.interrupt_rollback(tamper_swap=tamper)
        self.service.active = True
        with self.assertRaisesRegex(lc.LifecycleError, 'fails MANIFEST verification'):
            self.recover()
        # Stopped on purpose: a tree that fails MANIFEST must not run. The swap stays for a retry.
        self.assertEqual(self.service.verbs, ['stop'])
        self.assertTrue(swap.exists())

    def test_unhealthy_restored_tree_while_running_raises_and_leaves_service_stopped(self):
        # Fails without the fix: recover used to return exit 0 and report the service as running/started.
        swap = self.interrupt_rollback()
        self.service.active = True
        before = len(self.history())
        with self.assertRaises(lc.LifecycleError) as ctx:
            self.recover(check=lambda: False)
        message = str(ctx.exception)
        self.assertIn('files were restored', message)
        self.assertIn('service is NOT running', message)
        self.assertFalse(self.service.active, 'an unhealthy restored tree must not be left running')
        self.assertTrue(swap.exists(), 'swap is kept so a retry restores the same tree')
        self.assertEqual(len(self.history()), before, 'no recovered record is written on failure')

    def test_restored_tree_failing_its_check_while_stopped_raises(self):
        # Fails without the fix: a stopped service whose restored tree fails --check exited 0 before.
        swap = self.interrupt_rollback()
        self.service.active = False
        with self.assertRaisesRegex(lc.LifecycleError, 'files were restored .*service is NOT running'):
            self.recover(check=lambda: False)
        self.assertNotIn('start', self.service.verbs)
        self.assertTrue(swap.exists())

    def test_running_service_that_cannot_restart_raises_not_running(self):
        # "Was running and is now stopped": the restore succeeded but start failed. Must raise, not exit 0.
        self.interrupt_rollback()
        self.service.active = True
        # swap holds v1.1.0, so recovery restores v1.1.0; that is the tree whose start must fail.
        self.service.fail_start_for = {'v1.1.0'}
        with self.assertRaisesRegex(lc.LifecycleError, 'service is NOT running'):
            self.recover()
        self.assertFalse(self.service.active)

    def test_healthy_recovery_still_exits_zero_and_clears_the_swap(self):
        swap = self.interrupt_rollback()
        self.service.active = True
        result = self.recover(check=lambda: True)
        self.assertIn('service left running', result)
        self.assertTrue(self.service.active)
        self.assertFalse(swap.exists())

    def test_recover_refused_inside_an_agent_session(self):
        self.interrupt_rollback()
        with self.assertRaisesRegex(lc.LifecycleError, 'refused inside an agent session'):
            self.recover(environ={'OPENCODE_SESSION_ID': 'ses_example'})
        self.assertEqual(self.service.verbs, [])
        self.assertEqual(self.world.tag(), 'v1.0.0')


if __name__ == '__main__':
    unittest.main()
