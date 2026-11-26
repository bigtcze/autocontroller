"""Offline tests for `uninstall`: guard-before-prompt ordering, typed purge, owned links, kept paths.

Runs in-process against the disposable install from test_lifecycle_update.World. systemctl and
daemon-reload are replaced by recorders, so no real service, unit or HOME is touched.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
import lifecycle as lc  # noqa: E402
import release_artifacts as ra  # noqa: E402
from test_lifecycle_update import World, FakeService  # noqa: E402


class UninstallBase(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.addCleanup(self.world.cleanup)
        self.calls: list[tuple] = []
        bases = Path(tempfile.mkdtemp(prefix='uninstall-links-'))
        self.addCleanup(lambda: __import__('shutil').rmtree(bases, ignore_errors=True))
        self.unit_dir = bases / 'systemd'
        self.bin_dir = bases / 'bin'
        self.unit_dir.mkdir()
        self.bin_dir.mkdir()
        saved = {n: getattr(lc, n) for n in ('systemctl', '_daemon_reload', 'service_active', 'unit_state',
                                             'stop_service')}
        self.addCleanup(lambda: [setattr(lc, n, v) for n, v in saved.items()])
        self.service = FakeService(self.world.install)
        lc.service_active = self.service.is_active
        lc.unit_state = self.service.unit_state  # N1: no fixture may reach the real systemctl show
        lc.stop_service = self.service.stop
        lc.systemctl = lambda *verb, timeout=120: (self.calls.append(('systemctl',) + verb) or 0)
        lc._daemon_reload = lambda: self.calls.append(('daemon-reload',))

    def uninstall(self, *, environ=None, ask=lambda prompt: 'y', **kw):
        prompts: list[str] = []

        def recording_ask(prompt):
            prompts.append(prompt)
            return ask(prompt)
        # Virtual clock and answering sleep: a drain that never completes fails fast, never hangs.
        kw.setdefault('drain_timeout', 60)
        kw.setdefault('sleep', self.world.answering_sleep)
        kw.setdefault('clock', self.world.clock)
        result = lc.uninstall(self.world.install, unit_dir=self.unit_dir, bin_dir=self.bin_dir,
                              environ=environ or {}, ask=recording_ask, **kw)
        return result, prompts


class GuardOrderTest(UninstallBase):
    def test_agent_session_refuses_before_any_prompt(self):
        prompts = []

        def spy(prompt):
            prompts.append(prompt)
            return 'y'
        for env in ({'AUTONOMY_PROJECT_ROOT': '/x'}, {'AUTOCONTROLLER_LEARNING_INSTRUCTIONS': 'x'}):
            with self.subTest(env=env):
                with self.assertRaisesRegex(lc.LifecycleError, 'agent session'):
                    lc.uninstall(self.world.install, unit_dir=self.unit_dir, bin_dir=self.bin_dir,
                                 environ=env, ask=spy, yes=False)
                with self.assertRaisesRegex(lc.LifecycleError, 'agent session'):
                    lc.uninstall(self.world.install, unit_dir=self.unit_dir, bin_dir=self.bin_dir,
                                 environ=env, ask=spy, yes=True, purge=True)
        self.assertEqual(prompts, [], 'no prompt may be shown inside an agent session')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.world.tag(), 'v1.0.0')

    def test_purge_typed_word_is_required_even_with_yes(self):
        with self.assertRaisesRegex(lc.LifecycleError, 'purge not confirmed'):
            self.uninstall(yes=True, purge=True, ask=lambda prompt: 'y')
        self.assertEqual(self.calls, [])
        self.assertTrue((self.world.install / 'config.json').exists())

    def test_purge_word_prompt_comes_before_the_general_confirmation(self):
        _, prompts = self.uninstall(yes=False, purge=True, ask=lambda prompt: 'purge' if 'Type' in prompt else 'y')
        self.assertIn('Type purge', prompts[0])
        self.assertEqual(len(prompts), 2)


class UninstallEffectsTest(UninstallBase):
    def test_default_uninstall_keeps_config_and_state(self):
        (self.world.state_dir / 'keep.txt').write_text('state')
        result, _ = self.uninstall(yes=True)
        self.assertEqual(self.world.text('config.json'), '{"projects": []}\n')
        self.assertEqual((self.world.state_dir / 'keep.txt').read_text(), 'state')
        self.assertIn('config.json, state/', result)
        self.assertIn('bash install.sh', result)

    def test_default_uninstall_removes_program_files_and_managed_dirs(self):
        (self.world.install / 'updates').mkdir()
        (self.world.install / 'rollback' / 'previous').mkdir(parents=True)
        (self.world.install / 'lifecycle.lock').write_text('')
        self.uninstall(yes=True)
        for name in ra.EXECUTABLE_PROGRAM_FILES + ra.DATA_PROGRAM_FILES + ra.GENERATED_FILES:
            self.assertFalse((self.world.install / name).exists(), name)
        for name in ra.DATA_PROGRAM_DIRS + ('updates', 'rollback'):
            self.assertFalse((self.world.install / name).exists(), name)
        self.assertFalse((self.world.install / 'lifecycle.lock').exists())

    def test_purge_removes_config_and_state(self):
        self.uninstall(yes=True, purge=True, ask=lambda prompt: 'purge')
        self.assertFalse((self.world.install / 'config.json').exists())
        self.assertFalse((self.world.install / 'state').exists())

    def test_service_stopped_and_disabled_then_daemon_reloaded(self):
        self.uninstall(yes=True)
        self.assertEqual(self.calls, [('systemctl', 'disable'), ('daemon-reload',)])

    def test_inactive_service_is_still_stopped_before_files_are_removed(self):
        # S4: stop is always issued, so a unit that reads inactive (e.g. mid-start) is still stopped.
        self.service.active = False
        self.uninstall(yes=True)
        self.assertEqual(self.service.verbs, ['stop'])

    def test_stop_failure_refuses_and_leaves_program_files_in_place(self):
        # The fake service's stop succeeds; a failing systemctl stop must refuse before any removal.
        saved = lc.stop_service
        lc.stop_service = lambda: (_ for _ in ()).throw(lc.LifecycleError('systemctl stop failed'))
        self.addCleanup(setattr, lc, 'stop_service', saved)
        self.service.active = True
        with self.assertRaisesRegex(lc.LifecycleError, 'systemctl stop failed'):
            self.uninstall(yes=True)
        self.assertTrue((self.world.install / 'autonomy-controller.py').exists())
        self.assertEqual(self.calls, [])

    def test_failed_disable_leaves_program_files_in_place(self):
        lc.systemctl = lambda *verb, timeout=120: (self.calls.append(verb) or 1)
        with self.assertRaisesRegex(lc.LifecycleError, 'disable failed'):
            self.uninstall(yes=True)
        self.assertTrue((self.world.install / 'autonomy-controller.py').exists())


class OwnedLinkTest(UninstallBase):
    def test_links_into_install_root_are_removed(self):
        unit = self.unit_dir / lc.UNIT
        cli = self.bin_dir / 'autocontroller'
        unit.symlink_to(self.world.install / 'autocontroller.service')
        cli.symlink_to(self.world.install / 'onboarding.py')
        self.uninstall(yes=True)
        self.assertFalse(unit.is_symlink())
        self.assertFalse(cli.is_symlink())

    def test_links_pointing_elsewhere_are_left_alone(self):
        elsewhere = Path(self.bin_dir.parent) / 'other-tool.py'
        elsewhere.write_text('x')
        cli = self.bin_dir / 'autocontroller'
        cli.symlink_to(elsewhere)
        self.uninstall(yes=True)
        self.assertTrue(cli.is_symlink())
        self.assertEqual(Path(cli.readlink()), elsewhere)

    def test_regular_file_at_link_path_is_left_alone(self):
        cli = self.bin_dir / 'autocontroller'
        cli.write_text('user file')
        self.uninstall(yes=True)
        self.assertEqual(cli.read_text(), 'user file')


if __name__ == '__main__':
    unittest.main()
