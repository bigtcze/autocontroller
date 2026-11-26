"""Offline tests for update, rollback, health-driven auto-rollback, history and refusals.

Everything runs in-process against a disposable install. GitHub is a stand-in that
serves real archives built by release_artifacts.build from a throwaway git repo, the
service is a fake whose start succeeds or fails based on the installed VERSION, and
clock/sleep are virtual. No gh, systemctl, network or real HOME is touched.
"""
from __future__ import annotations

import hashlib
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
import lifecycle as lc  # noqa: E402
import release_artifacts as ra  # noqa: E402

REPO = 'example-owner/autocontroller'
GIT_ENV = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.invalid',
           'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.invalid',
           'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1',
           'PATH': os.environ.get('PATH', '/usr/bin:/bin')}


def git(cwd, *args) -> str:
    return subprocess.run(['git', *args], cwd=str(cwd), env=GIT_ENV, capture_output=True, text=True,
                          check=True, timeout=120).stdout.strip()


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeService:
    """Stands in for systemctl --user. Start fails when the installed VERSION is in fail_start_for."""

    def __init__(self, install: Path):
        self.install = install
        self.active = True
        self.verbs: list[str] = []
        self.fail_start_for: set[str] = set()
        self.pid = 1000  # N1: each start gets a new PID, as systemd does
        self.restarts = 0
        self.show_override = None

    def installed_tag(self):
        try:
            return json.loads((self.install / 'VERSION').read_text())['tag']
        except (OSError, ValueError, KeyError):
            return None

    def is_active(self) -> bool:
        return lc.needs_stop(self.unit_state())

    def unit_state(self) -> dict:
        # N1: the `show` shape. A test can override the reported state (auto-restart, PID change,
        # garbage) through self.show_override; otherwise it follows `active`, with a PID only while running.
        self.show_reads = getattr(self, 'show_reads', 0) + 1
        if self.show_override is not None:
            return self.show_override(self)
        if self.active:
            return {'ActiveState': 'active', 'SubState': 'running', 'MainPID': self.pid, 'NRestarts': self.restarts}
        return {'ActiveState': 'inactive', 'SubState': 'dead', 'MainPID': 0, 'NRestarts': self.restarts}

    def stop(self):
        self.verbs.append('stop')
        self.active = False

    def start(self) -> bool:
        self.verbs.append('start')
        if self.installed_tag() in self.fail_start_for:
            return False
        self.active = True
        self.pid += 1
        return True


class FakeGitHub:
    def __init__(self, repo_dir: Path, dist: Path):
        self.repo_dir = repo_dir
        self.dist = dist
        self.releases: dict[str, dict] = {}
        self.tags: dict[str, str] = {}
        self.calls: list[tuple] = []
        self.tamper: str | None = None

    def publish(self, tag: str, commit: str, *, draft=False, prerelease=False):
        out = self.dist / tag
        out.mkdir(parents=True, exist_ok=True)
        ra.build(self.repo_dir, tag, commit, out)
        assets = []
        for name in (ra.ARCHIVE_NAME.format(tag=tag), 'SHA256SUMS'):
            digest = hashlib.sha256((out / name).read_bytes()).hexdigest()
            assets.append({'name': name, 'digest': f'sha256:{digest}'})
        self.releases[tag] = {'tag_name': tag, 'draft': draft, 'prerelease': prerelease,
                              'published_at': None if draft else '2026-01-01T00:00:00Z', 'assets': assets}
        self.tags[tag] = commit

    def release(self, repo, tag=None):
        self.calls.append(('release', tag))
        if tag is None:
            published = [r for r in self.releases.values() if not r['draft'] and not r['prerelease']]
            return max(published, key=lambda r: ra.releases.version(r['tag_name']))
        release = self.releases.get(tag)
        if release is None:
            raise lc.LifecycleError(f'release {tag} not found')
        if release['draft'] or release['prerelease']:
            raise lc.LifecycleError('update source is not a published stable release')
        return release

    def download(self, repo, tag, name, dest: Path):
        self.calls.append(('download', tag, name))
        data = (self.dist / tag / name).read_bytes()
        if self.tamper == name:
            data += b'x'
        (dest / name).write_bytes(data)

    def tag_target(self, repo, tag):
        self.calls.append(('tag_target', tag))
        return self.tags.get(tag)


class World:
    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='lifecycle-world-')
        base = Path(self._tmp.name)
        self.install = base / 'install'
        self.src_repo = base / 'source'
        self.dist = base / 'dist'
        for path in (self.install, self.src_repo):
            path.mkdir(mode=0o700)
        self.state_dir = self.install / 'state'
        self.state_dir.mkdir(mode=0o700)
        self.clock = FakeClock()
        self.service = FakeService(self.install)
        self.on_sleep = None  # optional test hook, called on every virtual sleep
        git(self.src_repo, 'init', '-q', '-b', 'main')
        self.commits = {}
        for tag, text in (('v1.0.0', 'one'), ('v1.1.0', 'two')):
            self.commits[tag] = self._commit(text)
        self.gh = FakeGitHub(self.src_repo, self.dist)
        for tag, commit in self.commits.items():
            self.gh.publish(tag, commit)
        self.install_version('v1.0.0', self.commits['v1.0.0'])

    def cleanup(self):
        self._tmp.cleanup()

    def _commit(self, text: str) -> str:
        for name in ra.EXECUTABLE_PROGRAM_FILES:
            if name == 'release_artifacts.py':
                # A release must declare real lists, so the staged set can be read and validated.
                (self.src_repo / name).write_text((ROOT / 'release_artifacts.py').read_text(encoding='utf-8'))
                continue
            (self.src_repo / name).write_text(f'# {name} {text}\n')
        for name in ra.DATA_PROGRAM_FILES:
            (self.src_repo / name).write_text(f'{name} {text}\n')
        for name in ra.DATA_PROGRAM_DIRS:
            (self.src_repo / name).mkdir(exist_ok=True)
            (self.src_repo / name / 'marker.txt').write_text(f'{name} {text}\n')
        git(self.src_repo, 'add', '-A')
        git(self.src_repo, 'commit', '-q', '-m', text)
        return git(self.src_repo, 'rev-parse', 'HEAD')

    def install_version(self, tag: str, commit: str):
        """Install a REAL release archive, as install.sh does: build, verify, extract, then install the set.

        Generated files (VERSION, MANIFEST.sha256, PROGRAM.json) come from the archive, so the installed
        tree is what a real install holds and doctor's manifest check applies to it.
        """
        dist = self.dist / tag
        dist.mkdir(parents=True, exist_ok=True)
        ra.build(self.src_repo, tag, commit, dist)
        ra.verify(dist, tag, commit)
        seed = self.install / '.seed'
        shutil.rmtree(seed, ignore_errors=True)
        ra.safe_extract(dist / ra.ARCHIVE_NAME.format(tag=tag), seed)
        lc.install_program_files(seed, self.install)
        shutil.rmtree(seed)
        (self.install / 'config.json').write_text('{"projects": []}\n')

    def text(self, name: str) -> str:
        return (self.install / name).read_text()

    def tag(self) -> str:
        return json.loads(self.text('VERSION'))['tag']

    def answering_sleep(self, seconds):
        """Virtual sleep that plays the controller: once a drain request exists it writes a matching drain.ready."""
        self.clock.sleep(seconds)
        request = self.state_dir / lc.DRAIN_REQUEST
        if request.exists() and not (self.state_dir / lc.DRAIN_READY).exists():
            nonce = json.loads(request.read_text())['nonce']
            (self.state_dir / lc.DRAIN_READY).write_text(json.dumps({'nonce': nonce, 'pid': os.getpid(), 'at': 1}))
        if self.on_sleep:
            self.on_sleep(seconds)

    def update_kw(self, **extra):
        base = {'gh': self.gh, 'environ': {}, 'yes': True, 'ask': lambda prompt: 'y',
                'drain_timeout': 60, 'health_seconds': 2, 'check': lambda: True,
                'sleep': self.answering_sleep, 'clock': self.clock}
        base.update(extra)
        return base

    def rollback_kw(self, **extra):
        base = self.update_kw(**extra)
        base.pop('gh')
        return base


class Base(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.addCleanup(self.world.cleanup)
        self.service = self.world.service
        saved = {n: getattr(lc, n) for n in ('service_active', 'unit_state', 'stop_service', 'start_service')}
        self.addCleanup(lambda: [setattr(lc, n, v) for n, v in saved.items()])
        lc.service_active = self.service.is_active
        lc.unit_state = self.service.unit_state  # N1: no fixture may reach the real systemctl show
        lc.stop_service = self.service.stop
        lc.start_service = self.service.start

    def update(self, **kw):
        return lc.update(self.world.install, REPO, **self.world.update_kw(**kw))

    def rollback(self, **kw):
        return lc.rollback(self.world.install, **self.world.rollback_kw(**kw))

    def history(self):
        return json.loads((self.world.install / 'updates' / 'history.json').read_text())


class UpdateTest(Base):
    def test_update_installs_latest_and_records_history(self):
        result = self.update()
        self.assertIn('v1.0.0 -> v1.1.0', result)
        self.assertEqual(self.world.text('autonomy-controller.py'), '# autonomy-controller.py two\n')
        self.assertEqual(self.world.tag(), 'v1.1.0')
        self.assertEqual([(h['action'], h['from'], h['to'], h['result']) for h in self.history()],
                         [('update', 'v1.0.0', 'v1.1.0', 'ok')])

    def test_update_keeps_previous_release_in_rollback_store(self):
        self.update()
        previous = self.world.install / 'rollback' / 'previous'
        self.assertEqual((previous / 'autonomy-controller.py').read_text(), '# autonomy-controller.py one\n')
        self.assertEqual(json.loads((previous / 'VERSION').read_text())['tag'], 'v1.0.0')
        self.assertFalse((self.world.install / 'rollback' / 'incoming').exists())

    def test_running_service_is_stopped_then_started_after_drain(self):
        self.update()
        self.assertEqual(self.service.verbs, ['stop', 'start'])
        self.assertFalse((self.world.state_dir / lc.DRAIN_REQUEST).exists())
        self.assertFalse((self.world.state_dir / lc.DRAIN_READY).exists())

    def test_drain_request_withdrawn_before_new_service_starts(self):
        seen = {}
        original = self.service.start

        def spy():
            seen['request'] = (self.world.state_dir / lc.DRAIN_REQUEST).exists()
            return original()
        lc.start_service = spy
        self.update()
        self.assertFalse(seen['request'], 'drain.request must be gone before start')

    def test_already_current_is_a_no_op(self):
        self.world.install_version('v1.1.0', self.world.commits['v1.1.0'])
        self.assertIn('nothing to do', self.update())
        self.assertEqual(self.service.verbs, [])
        self.assertFalse((self.world.install / 'updates' / 'history.json').exists())

    def test_inactive_service_is_stopped_idempotently_but_never_started(self):
        # S4: stop is always issued (idempotent), so a unit that reads inactive is still stopped.
        # An inactive service is never started by update.
        self.service.active = False
        self.update()
        self.assertEqual(self.service.verbs, ['stop'])
        self.assertEqual(self.world.tag(), 'v1.1.0')

    def test_explicit_tag_selects_that_release(self):
        self.update(tag='v1.0.0')
        self.assertEqual(self.world.tag(), 'v1.0.0')

    def test_draft_or_prerelease_tag_refuses_without_change(self):
        self.world.gh.publish('v1.2.0', self.world.commits['v1.1.0'], draft=True)
        with self.assertRaisesRegex(lc.LifecycleError, 'not a published stable release'):
            self.update(tag='v1.2.0')
        self.assertEqual(self.world.tag(), 'v1.0.0')
        self.assertEqual(self.service.verbs, [])

    def test_digest_mismatch_refuses_before_any_service_change(self):
        self.world.gh.releases['v1.1.0']['assets'][0]['digest'] = 'sha256:' + '0' * 64
        with self.assertRaisesRegex(lc.LifecycleError, 'does not match its release digest'):
            self.update()
        self.assertEqual(self.service.verbs, [])
        self.assertEqual(self.world.tag(), 'v1.0.0')

    def test_missing_digest_refuses(self):
        self.world.gh.releases['v1.1.0']['assets'][0]['digest'] = None
        with self.assertRaisesRegex(lc.LifecycleError, 'no sha256 digest'):
            self.update()

    def test_bytes_tampered_after_digest_refuse(self):
        self.world.gh.tamper = ra.ARCHIVE_NAME.format(tag='v1.1.0')
        with self.assertRaisesRegex(lc.LifecycleError, 'does not match its release digest'):
            self.update()
        self.assertEqual(self.world.tag(), 'v1.0.0')

    def test_missing_asset_refuses(self):
        self.world.gh.releases['v1.1.0']['assets'] = self.world.gh.releases['v1.1.0']['assets'][:1]
        with self.assertRaisesRegex(lc.LifecycleError, 'has no SHA256SUMS asset'):
            self.update()

    def test_tag_commit_must_match_archive_version(self):
        self.world.gh.tags['v1.1.0'] = self.world.commits['v1.0.0']
        with self.assertRaisesRegex(lc.LifecycleError, 'VERSION commit does not match'):
            self.update()
        self.assertEqual(self.service.verbs, [])

    def test_unresolvable_tag_refuses(self):
        del self.world.gh.tags['v1.1.0']
        with self.assertRaisesRegex(lc.LifecycleError, 'does not exist'):
            self.update()

    def test_declined_confirmation_changes_nothing(self):
        with self.assertRaisesRegex(lc.LifecycleError, 'cancelled'):
            self.update(yes=False, ask=lambda prompt: 'n')
        self.assertEqual(self.world.gh.calls, [])
        self.assertEqual(self.world.tag(), 'v1.0.0')

    def test_eof_on_confirmation_cancels(self):
        def closed(prompt):
            raise EOFError
        with self.assertRaisesRegex(lc.LifecycleError, 'cancelled'):
            self.update(yes=False, ask=closed)

    def test_drain_timeout_leaves_service_running_and_request_withdrawn(self):
        with self.assertRaisesRegex(lc.LifecycleError, 'did not drain'):
            self.update(drain_timeout=3, sleep=self.world.clock.sleep)
        self.assertTrue(self.service.active)
        self.assertNotIn('stop', self.service.verbs)
        self.assertFalse((self.world.state_dir / lc.DRAIN_REQUEST).exists())
        self.assertEqual(self.world.tag(), 'v1.0.0')


class DowngradeTest(Base):
    def setUp(self):
        super().setUp()
        self.world.install_version('v1.1.0', self.world.commits['v1.1.0'])

    def test_older_explicit_tag_refuses_before_any_change(self):
        with self.assertRaisesRegex(lc.LifecycleError, 'downgrade refused.*autocontroller rollback'):
            self.update(tag='v1.0.0')
        self.assertEqual(self.world.tag(), 'v1.1.0')
        self.assertEqual(self.service.verbs, [])
        self.assertFalse((self.world.install / 'updates' / 'history.json').exists())
        self.assertFalse((self.world.install / 'rollback' / 'previous').exists())

    def test_same_version_is_still_a_no_op_not_a_refusal(self):
        self.assertIn('nothing to do', self.update(tag='v1.1.0'))

    def test_newer_release_is_still_allowed(self):
        self.world.install_version('v1.0.0', self.world.commits['v1.0.0'])
        self.assertIn('v1.0.0 -> v1.1.0', self.update())

    def test_source_install_without_version_may_take_any_release(self):
        (self.world.install / 'VERSION').unlink()
        self.assertIn('updated', self.update(tag='v1.0.0'))
        self.assertEqual(self.world.tag(), 'v1.0.0')

    def test_unreadable_installed_version_refuses(self):
        (self.world.install / 'VERSION').write_text('{not json')
        with self.assertRaisesRegex(lc.LifecycleError, 'installed VERSION is unreadable'):
            self.update(tag='v1.0.0')
        self.assertEqual(self.service.verbs, [])

    def test_downgrade_refusal_leaves_staging_removed(self):
        with self.assertRaisesRegex(lc.LifecycleError, 'downgrade refused'):
            self.update(tag='v1.0.0')
        self.assertFalse((self.world.install / 'updates' / 'staging').exists())


class HealthAndRollbackTest(Base):
    def test_start_failure_rolls_back_automatically(self):
        self.service.fail_start_for = {'v1.1.0'}
        with self.assertRaisesRegex(lc.LifecycleError, 'restored and running'):
            self.update()
        self.assertEqual(self.world.tag(), 'v1.0.0')
        self.assertEqual(self.world.text('autonomy-controller.py'), '# autonomy-controller.py one\n')
        self.assertFalse((self.world.install / 'rollback' / 'incoming').exists())
        self.assertTrue(self.service.active)
        self.assertEqual(self.history()[-1]['result'], 'rolled_back')

    def test_failed_health_check_rolls_back_and_stops_new_code_first(self):
        seen = []

        def check():
            seen.append(self.world.tag())
            return len(seen) > 1  # new release fails, restored previous passes
        with self.assertRaisesRegex(lc.LifecycleError, 'restored and running'):
            self.update(check=check)
        self.assertEqual(seen, ['v1.1.0', 'v1.0.0'])
        self.assertEqual(self.service.verbs, ['stop', 'start', 'stop', 'start'])

    def test_restored_but_unhealthy_is_reported(self):
        with self.assertRaisesRegex(lc.LifecycleError, 'NOT healthy'):
            self.update(check=lambda: False)
        self.assertEqual(self.history()[-1]['result'], 'rollback_unhealthy')

    def test_service_dying_in_health_window_triggers_rollback(self):
        def crash_after_start(seconds):
            if 'start' in self.service.verbs:
                self.service.active = False
        self.world.on_sleep = crash_after_start
        with self.assertRaisesRegex(lc.LifecycleError, 'failed its health check'):
            self.update()
        self.assertEqual(self.world.tag(), 'v1.0.0')

    def test_auto_restart_during_the_health_window_rolls_back(self):
        # N1: a crash-looping controller goes activating/auto-restart, then comes back with a new PID
        # and NRestarts+1. The first poll saw the old process, so the window is unhealthy and rolls back.
        # One crash, then the old tree is restarted healthy: the crash is a single scripted event.
        def crash_once(seconds):
            if 'start' in self.service.verbs and not getattr(self, 'crashed', False):
                self.crashed = True
                self.service.show_override = lambda s: {'ActiveState': 'activating',
                                                        'SubState': 'auto-restart', 'MainPID': 0,
                                                        'NRestarts': 1}

        self.world.on_sleep = crash_once
        original_stop = lc.stop_service

        def stop_and_clear():
            original_stop()
            self.service.show_override = None  # the rollback's stop ends the scripted crash
        lc.stop_service = stop_and_clear
        self.addCleanup(setattr, lc, 'stop_service', original_stop)
        with self.assertRaisesRegex(lc.LifecycleError, 'failed its health check'):
            self.update()
        self.assertEqual(self.world.tag(), 'v1.0.0')
        self.assertEqual(self.history()[-1]['result'], 'rolled_back')

    def test_main_pid_change_during_the_window_rolls_back(self):
        def new_pid(seconds):
            if 'start' in self.service.verbs and self.service.pid == 1001 and not getattr(self, 'moved', False):
                self.moved = True
                self.service.show_override = lambda s: {'ActiveState': 'active', 'SubState': 'running',
                                                        'MainPID': 9999, 'NRestarts': 0}
        self.world.on_sleep = new_pid
        with self.assertRaisesRegex(lc.LifecycleError, 'failed its health check'):
            self.update()
        self.assertEqual(self.world.tag(), 'v1.0.0')

    def test_update_with_unit_activating_skips_drain_and_stops(self):
        # N1 problem 2: an auto-restart wait has no workers to drain. It must stop at once, not wait the
        # drain timeout, and no drain request may be written for it.
        self.service.show_override = lambda s: {'ActiveState': 'activating', 'SubState': 'auto-restart',
                                                'MainPID': 0, 'NRestarts': 3}
        drain_requests = []
        real_drain = lc.drain

        def spy(state_dir, **kw):
            drain_requests.append(state_dir)
            return real_drain(state_dir, **kw)
        lc.drain = spy
        self.addCleanup(setattr, lc, 'drain', real_drain)
        self.service.verbs.clear()
        with self.assertRaisesRegex(lc.LifecycleError, 'NOT healthy|restored|failed its health check|stop'):
            self.update(drain_timeout=3600)
        self.assertEqual(drain_requests, [], 'an activating unit must not enter the drain path')
        self.assertIn('stop', self.service.verbs)
        self.assertEqual(self.world.tag(), 'v1.0.0')

    def test_unreadable_show_output_during_update_fails_closed(self):
        # Unreadable state raises, it is never read as healthy or as stopped. Nothing is left half-swapped.
        self.service.show_override = lambda s: (_ for _ in ()).throw(lc.LifecycleError('unit state is unknown'))
        with self.assertRaisesRegex(lc.LifecycleError, 'unit state is unknown'):
            self.update()
        self.assertEqual(self.world.tag(), 'v1.0.0')

    def test_inactive_service_failed_check_reports_not_started(self):
        self.service.active = False
        with self.assertRaisesRegex(lc.LifecycleError, 'its check FAILS'):
            self.update(check=lambda: False)
        self.assertFalse(self.service.active)
        self.assertEqual(self.world.tag(), 'v1.0.0')

    def test_rollback_restores_previous_release(self):
        self.update()
        self.assertIn('v1.1.0 -> v1.0.0', self.rollback())
        self.assertEqual(self.world.tag(), 'v1.0.0')

    def test_rollback_is_reversible(self):
        self.update()
        self.rollback()
        self.assertEqual(json.loads((self.world.install / 'rollback' / 'previous' / 'VERSION').read_text())['tag'],
                         'v1.1.0')
        self.rollback()
        self.assertEqual(self.world.tag(), 'v1.1.0')

    def test_rollback_without_previous_refuses(self):
        with self.assertRaisesRegex(lc.LifecycleError, 'no previous release'):
            self.rollback()

    def test_rollback_health_failure_restores_current(self):
        self.update()
        # Only the release being rolled TO is unhealthy; the restored current release passes.
        with self.assertRaisesRegex(lc.LifecycleError, 'current release was restored'):
            self.rollback(check=lambda: self.world.tag() == 'v1.1.0')
        self.assertEqual(self.world.tag(), 'v1.1.0')
        self.assertEqual(self.history()[-1]['result'], 'failed_restored')

    def test_rollback_drains_and_stops_then_starts(self):
        self.update()
        self.service.verbs.clear()
        self.rollback()
        self.assertEqual(self.service.verbs, ['stop', 'start'])

    def test_rollback_of_an_inactive_service_stops_idempotently_and_never_starts(self):
        # S4: stop is always issued; an inactive service is not started by rollback.
        self.update()
        self.service.verbs.clear()
        self.service.active = False
        self.rollback()
        self.assertEqual(self.service.verbs, ['stop'])
        self.assertEqual(self.world.tag(), 'v1.0.0')

    def test_rollback_leaves_config_and_state_alone(self):
        self.update()
        (self.world.state_dir / 'keep.txt').write_text('state')
        self.rollback()
        self.assertEqual(self.world.text('config.json'), '{"projects": []}\n')
        self.assertEqual((self.world.state_dir / 'keep.txt').read_text(), 'state')


class HistoryTest(Base):
    def test_unreadable_history_refuses_before_any_change(self):
        (self.world.install / 'updates').mkdir(mode=0o700)
        (self.world.install / 'updates' / 'history.json').write_text('{not a list')
        with self.assertRaisesRegex(lc.LifecycleError, 'unreadable'):
            self.update()
        self.assertEqual(self.service.verbs, [])
        self.assertEqual(self.world.tag(), 'v1.0.0')
        self.assertEqual((self.world.install / 'updates' / 'history.json').read_text(), '{not a list')

    def test_history_appends_across_operations(self):
        self.update()
        self.rollback()
        self.assertEqual([h['action'] for h in self.history()], ['update', 'rollback'])


class RefusalTest(Base):
    def test_agent_markers_refuse_update_and_rollback_before_any_work(self):
        for marker in ('AUTONOMY_PROJECT_ROOT', 'AUTONOMY_OPENCODE', 'AUTOCONTROLLER_LEARNING_INSTRUCTIONS'):
            with self.subTest(marker=marker):
                with self.assertRaisesRegex(lc.LifecycleError, 'agent session'):
                    self.update(environ={marker: 'x'})
                with self.assertRaisesRegex(lc.LifecycleError, 'agent session'):
                    self.rollback(environ={marker: 'x'})
        self.assertEqual(self.world.gh.calls, [])
        self.assertEqual(self.service.verbs, [])

    def test_agent_refusal_wins_over_yes_flag(self):
        with self.assertRaisesRegex(lc.LifecycleError, 'agent session'):
            self.update(environ={'AUTONOMY_OPENCODE': '/x'}, yes=True)

    def test_lifecycle_lock_waits_and_onboarding_lock_fails_fast(self):
        # Pin the lock modes: a second update waits on lifecycle.lock (blocking), while onboarding.lock
        # is fail-fast. A real wait would hang an in-process test, so the modes are recorded instead.
        modes = []
        original = lc.locked

        def spy(path, *, blocking):
            modes.append((path.name, blocking))
            return original(path, blocking=blocking)
        lc.locked = spy
        try:
            self.update()
        finally:
            lc.locked = original
        self.assertEqual(modes, [(lc.LIFECYCLE_LOCK, True), (lc.ONBOARDING_LOCK, False)])

    def test_onboarding_lock_blocks_update(self):
        with lc.locked(self.world.install / lc.ONBOARDING_LOCK, blocking=False):
            with self.assertRaisesRegex(lc.LifecycleError, 'held by another operation'):
                self.update()

    def test_leftover_incoming_snapshot_blocks_new_swap(self):
        (self.world.install / 'rollback' / 'incoming').mkdir(parents=True, mode=0o700)
        with self.assertRaisesRegex(lc.LifecycleError, 'rollback/incoming exists'):
            self.update()
        self.assertEqual(self.world.tag(), 'v1.0.0')


class DrainCleanupTest(Base):
    def test_exception_in_body_withdraws_request_and_ready(self):
        with self.assertRaises(RuntimeError):
            with lc.drain(self.world.state_dir, timeout=60, sleep=self.world.answering_sleep,
                          clock=self.world.clock):
                raise RuntimeError('boom')
        self.assertFalse((self.world.state_dir / lc.DRAIN_REQUEST).exists())
        self.assertFalse((self.world.state_dir / lc.DRAIN_READY).exists())


if __name__ == '__main__':
    unittest.main()
