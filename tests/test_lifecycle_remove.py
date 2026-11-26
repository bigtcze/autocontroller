"""Offline tests for `remove`: guard-before-prompt, in-flight refusal, atomic config, local-data safety.

Checkouts are real git repos under a temporary `repositories` root, with a bare local upstream.
systemctl and the service are stand-ins; no network, real HOME or real service is touched.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
import lifecycle as lc  # noqa: E402
from test_lifecycle_update import World, FakeService  # noqa: E402

GIT_ENV = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.invalid',
           'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.invalid',
           'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1',
           'PATH': os.environ.get('PATH', '/usr/bin:/bin')}
REPO = 'example-owner/demo'


def git(cwd, *args) -> str:
    return subprocess.run(['git', *args], cwd=str(cwd), env=GIT_ENV, capture_output=True, text=True,
                          check=True, timeout=120).stdout.strip()


class RemoveBase(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.addCleanup(self.world.cleanup)
        self.base = Path(tempfile.mkdtemp(prefix='remove-root-'))
        self.addCleanup(lambda: shutil.rmtree(self.base, ignore_errors=True))
        self.repositories = self.base / 'repositories'
        self.repositories.mkdir()
        self.upstream = self.base / 'upstream.git'
        git(self.base, 'init', '-q', '--bare', str(self.upstream))
        self.checkout = self.repositories / 'demo-1234'
        self.make_checkout()
        self.calls: list[tuple] = []
        self.service = FakeService(self.world.install)
        saved = {n: getattr(lc, n) for n in ('systemctl', 'service_active', 'unit_state', 'stop_service',
                                             'start_service')}
        self.addCleanup(lambda: [setattr(lc, n, v) for n, v in saved.items()])
        lc.service_active = self.service.is_active
        lc.unit_state = self.service.unit_state  # N1: no fixture may reach the real systemctl show
        lc.stop_service = self.service.stop
        lc.start_service = self.service.start
        lc.systemctl = self.fake_systemctl
        self.write_config([self.project()])
        self.pdir = self.world.install / 'state' / 'projects' / 'demo-1234'
        self.pdir.mkdir(parents=True)
        (self.world.install / 'state' / 'memory').mkdir(parents=True)
        (self.world.install / 'state' / 'memory' / 'metrics.json').write_text('{"search_total": 1}')

    def fake_systemctl(self, *verb, timeout=120):
        self.calls.append(verb)
        if verb == ('disable', '--now'):
            self.service.active = False
        return 0

    def make_checkout(self, *, push=True):
        git(self.repositories, 'init', '-q', '-b', 'main', str(self.checkout))
        git(self.checkout, 'remote', 'add', 'origin', f'https://github.com/{REPO}.git')
        (self.checkout / 'a.txt').write_text('a\n')
        git(self.checkout, 'add', '-A')
        git(self.checkout, 'commit', '-q', '-m', 'init')
        if push:
            git(self.checkout, 'config', 'url.' + str(self.upstream) + '.insteadOf', f'https://github.com/{REPO}.git')
            git(self.checkout, 'push', '-q', '-u', 'origin', 'main')

    def project(self, **extra):
        return {'name': 'demo-1234', 'enabled': True, 'repo': REPO, 'directory': str(self.checkout), **extra}

    def write_config(self, projects):
        (self.world.install / 'config.json').write_text(json.dumps({'projects': projects}) + '\n')

    def config(self):
        return json.loads((self.world.install / 'config.json').read_text())

    def remove(self, repo=REPO, *, environ=None, ask=lambda prompt: 'y', **kw):
        prompts: list[str] = []

        def recording_ask(prompt):
            prompts.append(prompt)
            return ask(prompt)
        kw.setdefault('yes', True)
        kw.setdefault('drain_timeout', 60)
        kw.setdefault('sleep', self.world.answering_sleep)
        kw.setdefault('clock', self.world.clock)
        kw.setdefault('repositories', self.repositories)
        result = lc.remove(self.world.install, repo, environ=environ or {}, ask=recording_ask, **kw)
        return result, prompts


class GuardAndParseTest(RemoveBase):
    def test_agent_session_refuses_before_any_prompt(self):
        prompts = []
        with self.assertRaisesRegex(lc.LifecycleError, 'agent session'):
            lc.remove(self.world.install, REPO, environ={'AUTONOMY_OPENCODE': '/x'}, yes=False,
                      ask=lambda p: prompts.append(p) or 'y', repositories=self.repositories)
        self.assertEqual(prompts, [])
        self.assertEqual(len(self.config()['projects']), 1)

    def test_repo_match_is_case_insensitive(self):
        result, _ = self.remove('https://GITHUB.com/Example-Owner/DEMO.git', delete_local_data=False)
        self.assertEqual(self.config()['projects'], [])
        self.assertIn('removed example-owner/demo', result.lower())

    def test_unknown_repository_refuses_without_change(self):
        with self.assertRaisesRegex(lc.LifecycleError, 'is not configured'):
            self.remove('example-owner/other')
        self.assertEqual(len(self.config()['projects']), 1)

    def test_bad_repo_argument_refuses(self):
        for bad in ('not a repo', 'https://example.com/a/b', 'owner/..'):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(lc.LifecycleError, 'expected a GitHub repository'):
                    self.remove(bad)

    def test_duplicate_repo_entries_refuse(self):
        self.write_config([self.project(), self.project(name='demo-5678')])
        with self.assertRaisesRegex(lc.LifecycleError, 'more than once'):
            self.remove()

    def test_unsafe_project_name_refuses(self):
        self.write_config([self.project(name='../escape')])
        with self.assertRaisesRegex(lc.LifecycleError, 'project name is unsafe'):
            self.remove()

    def test_declined_confirmation_changes_nothing(self):
        with self.assertRaisesRegex(lc.LifecycleError, 'cancelled'):
            self.remove(yes=False, ask=lambda p: 'n')
        self.assertEqual(len(self.config()['projects']), 1)
        self.assertEqual(self.calls, [])


class InFlightTest(RemoveBase):
    def test_cycle_json_refuses_without_force(self):
        (self.pdir / 'cycle.json').write_text('{"phase": "ci"}')
        with self.assertRaisesRegex(lc.LifecycleError, 'in-flight cycle.*cycle.json'):
            self.remove()
        self.assertEqual(len(self.config()['projects']), 1)
        self.assertEqual(self.calls, [])
        self.assertTrue(self.service.active)

    def test_cycle_pending_refuses_without_force(self):
        (self.pdir / 'cycle.pending').write_text('')
        with self.assertRaisesRegex(lc.LifecycleError, 'in-flight cycle'):
            self.remove()

    def test_force_removes_but_keeps_cycle_files(self):
        (self.pdir / 'cycle.json').write_text('{"phase": "ci"}')
        (self.pdir / 'cycle.pending').write_text('')
        self.remove(force=True)
        self.assertEqual(self.config()['projects'], [])
        self.assertTrue((self.pdir / 'cycle.json').exists())
        self.assertTrue((self.pdir / 'cycle.pending').exists())

    def test_force_cannot_combine_with_local_data_deletion(self):
        (self.pdir / 'cycle.json').write_text('{}')
        with self.assertRaisesRegex(lc.LifecycleError, 'cannot run while a cycle is in flight'):
            self.remove(force=True, delete_local_data=True)
        self.assertTrue(self.checkout.exists())


class ConfigAndServiceTest(RemoveBase):
    def test_last_project_disables_service_now(self):
        self.remove()
        self.assertIn(('disable', '--now'), self.calls)
        self.assertFalse(self.service.active)

    def test_other_projects_remaining_restarts_instead_of_disabling(self):
        self.write_config([self.project(), self.project(name='other-9999', repo='example-owner/other')])
        self.remove()
        self.assertEqual([p['name'] for p in self.config()['projects']], ['other-9999'])
        self.assertNotIn(('disable', '--now'), self.calls)
        self.assertIn('start', self.service.verbs)
        self.assertTrue(self.service.active)

    def test_inactive_service_is_stopped_idempotently_but_never_started_when_projects_remain(self):
        # S4: stop is always issued (idempotent); an inactive service is never started by remove.
        self.write_config([self.project(), self.project(name='other-9999', repo='example-owner/other')])
        self.service.active = False
        self.remove()
        self.assertNotIn('start', self.service.verbs)

    def test_running_service_is_drained_before_config_write(self):
        seen = {}
        original = self.service.stop

        def spy():
            seen['request_existed_at_stop'] = (self.world.state_dir / lc.DRAIN_REQUEST).exists()
            original()
        lc.stop_service = spy
        self.remove()
        self.assertTrue(seen['request_existed_at_stop'])
        self.assertFalse((self.world.state_dir / lc.DRAIN_REQUEST).exists())

    def test_config_write_failure_restores_service_and_keeps_config(self):
        original = lc._write_json

        def failing(path, value):
            # Only the config write fails; the drain request must still be written so the stop happens.
            if Path(path).name == 'config.json':
                raise OSError('disk full')
            return original(path, value)
        lc._write_json = failing
        try:
            with self.assertRaises(OSError):
                self.remove()
        finally:
            lc._write_json = original
        self.assertEqual(len(self.config()['projects']), 1)
        self.assertEqual(self.service.verbs, ['stop', 'start'])
        self.assertTrue(self.service.active)

    def test_config_is_written_atomically_without_temp_leftovers(self):
        self.write_config([self.project(), self.project(name='other-9999', repo='example-owner/other')])
        self.remove()
        self.assertFalse((self.world.install / '.config.json.tmp').exists())
        json.loads((self.world.install / 'config.json').read_text())


class KeepByDefaultTest(RemoveBase):
    def test_default_keeps_checkout_state_and_memory(self):
        (self.pdir / 'memory-grant.json').write_text('{}')
        result, _ = self.remove()
        self.assertTrue(self.checkout.is_dir())
        self.assertTrue((self.checkout / '.git').is_dir())
        self.assertTrue(self.pdir.is_dir())
        self.assertEqual(json.loads((self.world.install / 'state' / 'memory' / 'metrics.json').read_text()),
                         {'search_total': 1})
        self.assertIn('kept checkout', result)
        self.assertIn('memory and GitHub state kept', result)


class DeleteLocalDataTest(RemoveBase):
    def test_clean_pushed_checkout_is_deleted_with_its_state_only(self):
        other = self.world.install / 'state' / 'projects' / 'other-9999'
        other.mkdir(parents=True)
        self.remove(delete_local_data=True)
        self.assertFalse(self.checkout.exists())
        self.assertFalse(self.pdir.exists())
        self.assertTrue(other.is_dir())
        self.assertTrue((self.world.install / 'state' / 'memory' / 'metrics.json').exists())

    def test_uncommitted_change_refuses_and_keeps_checkout(self):
        (self.checkout / 'a.txt').write_text('dirty\n')
        with self.assertRaisesRegex(lc.LifecycleError, 'uncommitted'):
            self.remove(delete_local_data=True)
        self.assertTrue(self.checkout.exists())
        self.assertEqual(len(self.config()['projects']), 1)

    def test_untracked_file_refuses(self):
        (self.checkout / 'new.txt').write_text('x')
        with self.assertRaisesRegex(lc.LifecycleError, 'uncommitted'):
            self.remove(delete_local_data=True)

    def test_unpushed_commit_refuses(self):
        (self.checkout / 'b.txt').write_text('b\n')
        git(self.checkout, 'add', '-A')
        git(self.checkout, 'commit', '-q', '-m', 'local only')
        with self.assertRaisesRegex(lc.LifecycleError, 'not pushed'):
            self.remove(delete_local_data=True)
        self.assertTrue(self.checkout.exists())

    def test_no_upstream_refuses(self):
        git(self.checkout, 'branch', '--unset-upstream')
        with self.assertRaisesRegex(lc.LifecycleError, 'no upstream'):
            self.remove(delete_local_data=True)
        self.assertTrue(self.checkout.exists())

    def test_checkout_outside_repositories_refuses(self):
        outside = self.base / 'elsewhere'
        shutil.move(str(self.checkout), str(outside))
        self.write_config([self.project(directory=str(outside))])
        with self.assertRaisesRegex(lc.LifecycleError, 'not directly under'):
            self.remove(delete_local_data=True)
        self.assertTrue(outside.is_dir())

    def test_symlinked_checkout_refuses(self):
        link = self.repositories / 'linked'
        link.symlink_to(self.checkout)
        self.write_config([self.project(directory=str(link))])
        with self.assertRaisesRegex(lc.LifecycleError, 'missing or a link'):
            self.remove(delete_local_data=True)
        self.assertTrue(self.checkout.is_dir())

    def test_relative_directory_refuses(self):
        self.write_config([self.project(directory='demo-1234')])
        with self.assertRaisesRegex(lc.LifecycleError, 'not absolute'):
            self.remove(delete_local_data=True)

    def test_origin_mismatch_refuses(self):
        git(self.checkout, 'remote', 'set-url', 'origin', 'https://github.com/example-owner/other.git')
        with self.assertRaisesRegex(lc.LifecycleError, 'origin does not match'):
            self.remove(delete_local_data=True)
        self.assertTrue(self.checkout.is_dir())

    def test_stash_refuses(self):
        (self.checkout / 'a.txt').write_text('stash me\n')
        git(self.checkout, 'stash', 'push', '-q')
        with self.assertRaisesRegex(lc.LifecycleError, 'stashed'):
            self.remove(delete_local_data=True)

    def test_delete_refusal_happens_before_any_service_or_config_change(self):
        (self.checkout / 'a.txt').write_text('dirty\n')
        with self.assertRaises(lc.LifecycleError):
            self.remove(delete_local_data=True)
        self.assertEqual(self.calls, [])
        self.assertTrue(self.service.active)
        self.assertEqual(len(self.config()['projects']), 1)

    # B4: a deletable checkout must hold nothing that exists only locally.
    def test_unpushed_local_branch_refuses(self):
        git(self.checkout, 'branch', 'feature')
        git(self.checkout, 'checkout', '-q', 'feature')
        (self.checkout / 'f.txt').write_text('f\n')
        git(self.checkout, 'add', '-A')
        git(self.checkout, 'commit', '-q', '-m', 'feature work')
        git(self.checkout, 'checkout', '-q', 'main')
        with self.assertRaisesRegex(lc.LifecycleError, 'local branch|commits no remote'):
            self.remove(delete_local_data=True)
        self.assertTrue(self.checkout.is_dir())
        self.assertEqual(len(self.config()['projects']), 1)

    def test_tracked_branch_with_unpushed_commit_refuses(self):
        # B4: a branch WITH upstream still refuses when it holds a commit no remote has. The
        # no-upstream check cannot catch this one, so only the branches-not-remotes check can.
        git(self.checkout, 'checkout', '-q', '-b', 'topic')
        (self.checkout / 't.txt').write_text('t1\n')
        git(self.checkout, 'add', '-A')
        git(self.checkout, 'commit', '-q', '-m', 'topic pushed')
        git(self.checkout, 'push', '-q', '-u', 'origin', 'topic')
        (self.checkout / 't.txt').write_text('t2\n')
        git(self.checkout, 'commit', '-qam', 'topic local only')
        git(self.checkout, 'checkout', '-q', 'main')
        with self.assertRaisesRegex(lc.LifecycleError, 'commits no remote has'):
            self.remove(delete_local_data=True)
        self.assertTrue(self.checkout.is_dir())
        self.assertEqual(len(self.config()['projects']), 1)

    def test_local_branch_without_upstream_refuses(self):
        git(self.checkout, 'branch', 'orphan')
        with self.assertRaisesRegex(lc.LifecycleError, 'local branch orphan has no upstream'):
            self.remove(delete_local_data=True)
        self.assertTrue(self.checkout.is_dir())

    def test_linked_worktree_refuses(self):
        other = self.base / 'linked-worktree'
        git(self.checkout, 'worktree', 'add', '-q', '--detach', str(other), 'main')
        with self.assertRaisesRegex(lc.LifecycleError, 'linked worktrees'):
            self.remove(delete_local_data=True)
        self.assertTrue(self.checkout.is_dir())
        self.assertTrue(other.is_dir())

    def test_pushed_branches_only_still_deletes(self):
        git(self.checkout, 'push', '-q', 'origin', 'main:refs/heads/shipped')
        git(self.checkout, 'fetch', '-q', 'origin')
        self.remove(delete_local_data=True)
        self.assertFalse(self.checkout.exists())


class RecheckAfterDrainTest(RemoveBase):
    """B5: the drain runs between confirmation and the config write; a change during it must refuse."""

    def test_cycle_appearing_during_drain_refuses_restarts_and_keeps_config(self):
        def cycle_appears(seconds):
            (self.pdir / 'cycle.json').write_text('{}')
        self.world.on_sleep = cycle_appears
        try:
            with self.assertRaisesRegex(lc.LifecycleError, 'in-flight cycle while draining'):
                self.remove()
        finally:
            self.world.on_sleep = None
        self.assertEqual(self.calls, [], 'no disable or systemctl call may happen on refusal')
        self.assertTrue(self.service.active, 'the service must be restarted when it was active')
        self.assertEqual(len(self.config()['projects']), 1)

    def test_unpushed_commit_appearing_during_drain_refuses_before_config_write(self):
        def commit_appears(seconds):
            if not getattr(self, '_committed', False):
                self._committed = True
                (self.checkout / 'late.txt').write_text('late\n')
                git(self.checkout, 'add', '-A')
                git(self.checkout, 'commit', '-q', '-m', 'late')
        self.world.on_sleep = commit_appears
        try:
            with self.assertRaisesRegex(lc.LifecycleError, 'not pushed'):
                self.remove(delete_local_data=True)
        finally:
            self.world.on_sleep = None
        self.assertEqual(len(self.config()['projects']), 1)
        self.assertTrue(self.checkout.is_dir())
        self.assertTrue(self.service.active)

    def test_no_recheck_change_still_removes(self):
        self.remove()
        self.assertEqual(self.config()['projects'], [])

    def test_force_delete_local_data_refuses_a_cycle_that_starts_during_the_drain(self):
        # NIT: --force keeps cycle files, but --delete-local-data must still refuse a cycle that appears
        # while draining. Before the fix the post-drain re-check was skipped under --force.
        def cycle_appears(seconds):
            (self.pdir / 'cycle.json').write_text('{}')
        self.world.on_sleep = cycle_appears
        try:
            with self.assertRaisesRegex(lc.LifecycleError, 'in-flight cycle while draining'):
                self.remove(force=True, delete_local_data=True)
        finally:
            self.world.on_sleep = None
        self.assertEqual(len(self.config()['projects']), 1, 'config must not be written')
        self.assertTrue(self.checkout.is_dir(), 'the checkout must not be deleted')
        self.assertTrue(self.service.active, 'the service must be restarted when it was active')
        self.assertTrue((self.pdir / 'cycle.json').exists(), 'cycle files are kept')

    def test_force_without_local_data_still_removes_under_a_cycle_that_starts_during_the_drain(self):
        # The post-drain refusal is scoped to --delete-local-data; plain --force keeps its documented behaviour.
        def cycle_appears(seconds):
            (self.pdir / 'cycle.json').write_text('{}')
        self.world.on_sleep = cycle_appears
        try:
            self.remove(force=True)
        finally:
            self.world.on_sleep = None
        self.assertEqual(self.config()['projects'], [])


if __name__ == '__main__':
    unittest.main()
