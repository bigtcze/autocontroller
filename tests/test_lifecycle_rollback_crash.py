"""N4: a crash at each step of the rollback store commit leaves a state `recover` finishes or restores.

Built on tests/test_lifecycle_update.Base (real release archives, fake service, virtual clock). Each crash is
injected into the real rollback() by making one call raise, so the leftover is the one the code leaves.
Nothing touches systemctl, gh, the network or real HOME.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
import lifecycle as lc  # noqa: E402
from test_lifecycle_update import Base  # noqa: E402


class Boom(Exception):
    pass


class RollbackCrashTest(Base):
    """Starts at v1.1.0 with v1.0.0 kept in rollback/previous. Each test crashes one rollback step."""

    def setUp(self):
        super().setUp()
        self.update()  # install v1.1.0; v1.0.0 kept in rollback/previous
        self.assertEqual(self.world.tag(), 'v1.1.0')
        rollback = self.world.install / 'rollback'
        self.store = rollback / 'previous'
        self.aside = rollback / 'previous.old'
        self.swap = rollback / 'swap'
        self.service.verbs.clear()

    def recover(self, **extra):
        kw = {'environ': {}, 'yes': True, 'ask': lambda prompt: 'y', 'drain_timeout': 60,
              'sleep': self.world.answering_sleep, 'clock': self.world.clock}
        kw.update(extra)
        return lc.recover(self.world.install, **kw)

    def crash_on_replace_into(self, dst: Path):
        """Raise Boom on the first os.replace whose destination is `dst`; other replaces run normally."""
        real = os.replace

        def replace(src, to, *a, **k):
            if Path(to) == dst:
                raise Boom(f'crash moving {src} -> {to}')
            return real(src, to, *a, **k)
        return mock.patch.object(lc.os, 'replace', side_effect=replace)

    def crash_on_final_remove(self, path: Path):
        """Raise Boom on the SECOND removal of `path`. rollback removes it once before the aside rename
        (harmless) and once after the commit; only the second is the crash point under test."""
        real = lc._remove
        seen = {'count': 0}

        def remove(p):
            if Path(p) == path:
                seen['count'] += 1
                if seen['count'] == 2:
                    raise Boom(f'crash removing {p} after the commit')
            return real(p)
        return mock.patch.object(lc, '_remove', side_effect=remove)

    def rollback(self):
        return lc.rollback(self.world.install, **self.world.rollback_kw())

    def test_crash_during_install_restores_v11_and_keeps_the_store(self):
        # swap holds v1.1.0 and previous is intact. Recover restores v1.1.0 and does not consume previous.
        with mock.patch.object(lc, 'install_program_files', side_effect=Boom('crash during install')):
            with self.assertRaises(Boom):
                self.rollback()
        self.assertTrue(self.swap.exists())
        self.assertTrue(self.store.is_dir())
        self.recover()
        self.assertEqual(self.world.tag(), 'v1.1.0')
        self.assertFalse(self.swap.exists())
        self.assertTrue(self.store.is_dir(), 'a restore never consumes the kept previous store')
        self.assertFalse(self.aside.exists())

    def test_crash_at_failed_health_path_restores_v11_and_keeps_the_store(self):
        # Install already holds v1.0.0 and the health check crashes. Swap holds v1.1.0, previous is intact.
        with mock.patch.object(lc, 'verified', side_effect=Boom('crash in the health check')):
            with self.assertRaises(Boom):
                self.rollback()
        self.assertEqual(self.world.tag(), 'v1.0.0')
        self.assertTrue(self.swap.exists())
        self.assertTrue(self.store.is_dir())
        self.recover()
        self.assertEqual(self.world.tag(), 'v1.1.0')
        self.assertFalse(self.swap.exists())
        self.assertTrue(self.store.is_dir())

    def test_crash_between_previous_aside_and_swap_into_place_finishes_the_commit(self):
        # Health passed. previous was renamed to previous.old, and the swap->previous move crashed.
        # The install is already v1.0.0, so recover finishes the commit: v1.1.0 becomes the kept store.
        with self.crash_on_replace_into(self.store):
            with self.assertRaises(Boom):
                self.rollback()
        self.assertEqual(self.world.tag(), 'v1.0.0')
        self.assertTrue(self.aside.is_dir(), 'the kept store was renamed aside, not deleted')
        self.assertTrue(self.swap.exists())
        self.assertFalse(self.store.exists())
        self.recover()
        self.assertEqual(self.world.tag(), 'v1.0.0', 'finishing the commit must not restore v1.1.0 over v1.0.0')
        self.assertFalse(self.swap.exists())
        self.assertFalse(self.aside.exists())
        self.assertTrue(self.store.is_dir())
        self.assertEqual(self.history()[-1]['result'], 'recovered')
        self.assertEqual(self.history()[-1]['from'], 'v1.1.0')
        self.assertEqual(self.history()[-1]['to'], 'v1.0.0')

    def test_crash_removing_previous_old_leaves_a_tidy_state_that_recover_removes(self):
        # The rollback committed (swap is now previous) and the aside removal crashed.
        with self.crash_on_final_remove(self.aside):
            with self.assertRaises(Boom):
                self.rollback()
        self.assertTrue(self.aside.is_dir())
        self.assertTrue(self.store.is_dir())
        self.assertFalse(self.swap.exists())
        self.assertEqual(self.world.tag(), 'v1.0.0')
        result = self.recover()
        self.assertIn('removed the leftover previous.old', result)
        self.assertFalse(self.aside.exists())
        self.assertEqual(self.world.tag(), 'v1.0.0')
        self.assertEqual(self.history()[-1]['result'], 'recovered')

    def test_missing_store_with_swap_finishes_instead_of_restoring_over_the_install(self):
        # Crash shape: the install is already v1.0.0 (health passed), swap holds v1.1.0, and previous is gone
        # with no previous.old. That is the old code's window. Restoring swap would put v1.1.0 back over
        # the v1.0.0 install; the commit must be finished, so the install stays v1.0.0.
        with self.crash_on_replace_into(self.store):
            with self.assertRaises(Boom):
                self.rollback()
        lc._remove(self.aside)  # the store is gone and nothing is aside
        self.assertFalse(self.store.exists())
        self.assertFalse(self.aside.exists())
        self.assertTrue(self.swap.exists())
        self.assertEqual(self.world.tag(), 'v1.0.0')
        self.recover()
        self.assertEqual(self.world.tag(), 'v1.0.0', 'finishing must not restore v1.1.0 over the v1.0.0 install')
        self.assertTrue(self.store.is_dir())
        self.assertFalse(self.swap.exists())

    def test_inconsistent_store_with_swap_previous_and_aside_all_present_is_refused_before_stopping(self):
        self.swap.mkdir(parents=True, mode=0o700)
        self.aside.mkdir(parents=True, mode=0o700)
        self.assertTrue(self.store.is_dir())
        with self.assertRaisesRegex(lc.LifecycleError, 'inconsistent'):
            self.recover()
        self.assertEqual(self.service.verbs, [])
        self.assertEqual(self.world.tag(), 'v1.1.0')


if __name__ == '__main__':
    unittest.main()
