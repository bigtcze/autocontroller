"""NIT: `--drain-timeout` is accepted by update, rollback, remove and uninstall and reaches lifecycle.

onboarding.main is run in-process with sys.argv patched and every lifecycle entry point replaced by a
recorder, so nothing runs, no config is read and no systemctl/gh is touched.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import lifecycle as lc  # noqa: E402
import onboarding  # noqa: E402


class DrainTimeoutFlagTest(unittest.TestCase):
    def run_main(self, *argv):
        """Run onboarding.main with argv; return the recorded (name, kwargs) lifecycle call."""
        calls = []

        def recorder(name, result='ok'):
            def record(*args, **kwargs):
                calls.append((name, kwargs))
                return result
            return record

        fakes = {
            'update': recorder('update'), 'rollback': recorder('rollback'),
            'remove': recorder('remove'), 'uninstall': recorder('uninstall'),
            'recover': recorder('recover'),
        }
        patches = [mock.patch.object(lc, name, fn) for name, fn in fakes.items()]
        patches.append(mock.patch.object(sys, 'argv', ['onboarding.py', *argv]))
        patches.append(mock.patch.object(onboarding, 'unit_and_bin_dirs',
                                         lambda: (Path('/nonexistent/unit'), Path('/nonexistent/bin'))))
        patches.append(mock.patch('builtins.print'))
        # The lifecycle agent guard reads os.environ. Scrub the markers so the test runs the same
        # from an agent shell as from a terminal; the guard itself is tested in test_lifecycle_core.
        clean = {k: v for k, v in os.environ.items()
                 if k != lc.AGENT_SESSION_VAR and not k.startswith(lc.AGENT_ENV_PREFIX)
                 and k not in lc.AGENT_ENV_NAMES}
        patches.append(mock.patch.dict(os.environ, clean, clear=True))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        onboarding.main()
        self.assertEqual(len(calls), 1, calls)
        return calls[0]

    def test_update_forwards_drain_timeout(self):
        name, kwargs = self.run_main('update', '--yes', '--drain-timeout', '42')
        self.assertEqual(name, 'update')
        self.assertEqual(kwargs['drain_timeout'], 42.0)

    def test_rollback_forwards_drain_timeout(self):
        name, kwargs = self.run_main('rollback', '--yes', '--drain-timeout', '17.5')
        self.assertEqual(name, 'rollback')
        self.assertEqual(kwargs['drain_timeout'], 17.5)

    def test_remove_forwards_drain_timeout(self):
        name, kwargs = self.run_main('remove', 'example-owner/alpha', '--yes', '--drain-timeout', '9')
        self.assertEqual(name, 'remove')
        self.assertEqual(kwargs['drain_timeout'], 9.0)

    def test_uninstall_forwards_drain_timeout(self):
        name, kwargs = self.run_main('uninstall', '--yes', '--drain-timeout', '5')
        self.assertEqual(name, 'uninstall')
        self.assertEqual(kwargs['drain_timeout'], 5.0)

    def test_default_is_the_lifecycle_default(self):
        name, kwargs = self.run_main('update', '--yes')
        self.assertEqual(kwargs['drain_timeout'], lc.DEFAULT_DRAIN_TIMEOUT)

    def test_recover_routes_to_lifecycle_recover_with_drain_timeout(self):
        name, kwargs = self.run_main('rollback', '--recover', '--yes', '--drain-timeout', '30')
        self.assertEqual(name, 'recover')
        self.assertEqual(kwargs['drain_timeout'], 30.0)

    def test_recover_is_refused_for_any_other_action(self):
        with self.assertRaisesRegex(ValueError, '--recover applies to rollback only'):
            self.run_main('update', '--recover', '--yes')


if __name__ == '__main__':
    unittest.main()
