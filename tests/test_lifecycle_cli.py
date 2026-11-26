"""CLI-level tests for `onboarding.py update|rollback` against a disposable install.

Uses tests/onboarding_harness.World: a copied install root, fake gh, fake systemctl and
a scrubbed environment. Release archives come from release_artifacts.build over a
throwaway git repo, served by fake gh's release/tag/download endpoints. No network,
real systemctl, real gh, or real HOME is touched.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(ROOT))
import lifecycle as lc  # noqa: E402
import onboarding_harness as harness  # noqa: E402
import release_artifacts as ra  # noqa: E402
from onboarding_harness import World  # noqa: E402

GIT_ENV = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.invalid',
           'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.invalid',
           'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1',
           'PATH': os.environ.get('PATH', '/usr/bin:/bin')}
UPDATE_REPO = 'example-owner/autocontroller'


def git(cwd, *args) -> str:
    return subprocess.run(['git', *args], cwd=str(cwd), env=GIT_ENV, capture_output=True, text=True,
                          check=True, timeout=120).stdout.strip()


class CliWorld(World):
    """World plus a published update source served through fake gh."""

    def __init__(self):
        super().__init__()
        self.src = self.root / 'source'
        self.asset_dir = self.root / 'assets'
        self.src.mkdir()
        git(self.src, 'init', '-q', '-b', 'main')
        self.commits = {}
        # Both releases are REAL copies of the checkout's program set, differing only by a
        # marker line. Stub files would leave the installed CLI unable to run its own commands.
        # JSON files get no marker: appending text makes config.example.json invalid JSON.
        for tag, text in (('v1.0.0', 'one'), ('v1.1.0', 'two')):
            for name in ra.EXECUTABLE_PROGRAM_FILES + ra.DATA_PROGRAM_FILES:
                body = (ROOT / name).read_bytes()
                if not name.endswith('.json'):
                    body += f'\n# release marker {text}\n'.encode()
                (self.src / name).write_bytes(body)
            for name in ra.DATA_PROGRAM_DIRS:
                if (self.src / name).exists():
                    shutil.rmtree(self.src / name)
                shutil.copytree(ROOT / name, self.src / name)
                (self.src / name / 'marker.txt').write_text(f'{name} {text}\n')
            git(self.src, 'add', '-A')
            git(self.src, 'commit', '-q', '-m', text)
            commit = git(self.src, 'rev-parse', 'HEAD')
            self.commits[tag] = commit
            out = self.asset_dir / tag
            out.mkdir(parents=True)
            ra.build(self.src, tag, commit, out)
        self.mutate_gh(self._publish_all)

    def _publish_all(self, state: dict):
        state['asset_dir'] = str(self.asset_dir)
        state['releases'] = {}
        state['tags'] = dict(self.commits)
        for tag in self.commits:
            assets = []
            for name in (ra.ARCHIVE_NAME.format(tag=tag), 'SHA256SUMS'):
                digest = hashlib.sha256((self.asset_dir / tag / name).read_bytes()).hexdigest()
                assets.append({'name': name, 'digest': f'sha256:{digest}'})
            state['releases'][tag] = {'tag_name': tag, 'draft': False, 'prerelease': False,
                                      'published_at': f'2026-01-0{len(state["releases"]) + 1}T00:00:00Z',
                                      'assets': assets}

    def seed_install(self):
        """Keep the harness's REAL program files (never overwrite them with stubs); add VERSION and config.

        The installed copy must run the real onboarding.py/lifecycle.py, or the CLI would be
        exercising a stub. Only the generated VERSION marker is new.
        """
        # A real release install: the v1.0.0 archive is extracted and installed through install_program_files,
        # so VERSION, MANIFEST.sha256 and PROGRAM.json are the archive's own files and doctor checks all of them.
        extracted = self.root / 'seed-v1.0.0'
        shutil.rmtree(extracted, ignore_errors=True)
        ra.safe_extract(self.asset_dir / 'v1.0.0' / ra.ARCHIVE_NAME.format(tag='v1.0.0'), extracted)
        lc.install_program_files(extracted, self.install)
        self.write_config({'projects': []})


class LifecycleCliTest(unittest.TestCase):
    def setUp(self):
        self.world = CliWorld()
        self.addCleanup(self.world.cleanup)
        self.world.seed_install()
        self.world.set_systemctl_faults([])

    def run_cli(self, *args, env=None, stdin=''):
        extra = dict(env or {})
        return subprocess.run([sys.executable, str(self.world.install / 'onboarding.py'), *args],
                              input=stdin, capture_output=True, text=True, timeout=300,
                              cwd=str(self.world.install), env=self.world.env(**extra), check=False)

    def installed_tag(self):
        return json.loads((self.world.install / 'VERSION').read_text())['tag']

    def test_update_skips_check_with_zero_enabled_projects_and_succeeds(self):
        # S7: the installed `--check` refuses with no enabled project (exit 2) by design, so the
        # update does not run it. The update succeeds; there is nothing for the check to validate.
        proc = self.run_cli('update', '--yes', '--health-seconds', '0.5')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.installed_tag(), 'v1.1.0')
        history = json.loads((self.world.install / 'updates' / 'history.json').read_text())
        self.assertEqual(history[-1]['result'], 'ok')

    def test_update_with_unreadable_config_is_not_zero_projects_and_changes_nothing(self):
        # An unreadable config is not "zero enabled projects": the skip must not apply. The update
        # refuses before any change (the controller config is read first), so the install stays v1.0.0.
        (self.world.install / 'config.json').write_text('{not json')
        proc = self.run_cli('update', '--yes', '--health-seconds', '0.5')
        self.assertEqual(proc.returncode, 1)
        self.assertIn('config.json', proc.stderr)
        self.assertEqual(self.installed_tag(), 'v1.0.0')

    def test_update_without_yes_declined_exits_nonzero_and_changes_nothing(self):
        proc = self.run_cli('update', '--health-seconds', '0.5', stdin='n\n')
        self.assertEqual(proc.returncode, 1)
        self.assertIn('cancelled', proc.stderr)
        self.assertEqual(self.installed_tag(), 'v1.0.0')

    def test_agent_session_refuses_update_with_exit_one(self):
        proc = self.run_cli('update', '--yes', env={'AUTONOMY_PROJECT_ROOT': '/x'})
        self.assertEqual(proc.returncode, 1)
        self.assertIn('agent session', proc.stderr)
        self.assertEqual(self.installed_tag(), 'v1.0.0')
        self.assertEqual(self.world.gh_calls(), [])

    def test_agent_session_refuses_rollback_with_exit_one(self):
        proc = self.run_cli('rollback', '--yes', env={'AUTOCONTROLLER_LEARNING_INSTRUCTIONS': 'x'})
        self.assertEqual(proc.returncode, 1)
        self.assertIn('agent session', proc.stderr)

    def test_rollback_health_failure_exits_nonzero_and_keeps_current(self):
        # A kept previous release whose installed --check fails with a real config (one enabled
        # project, so S7's zero-project skip does not apply): rollback must restore the current
        # release, report it, and exit nonzero.
        self.world.write_config({'projects': [{'name': 'demo', 'enabled': True, 'repo': 'example-owner/demo',
                                               'directory': str(self.world.install / 'demo')}]})
        (self.world.install / 'rollback').mkdir(mode=0o700)
        previous = self.world.install / 'rollback' / 'previous'
        previous.mkdir(mode=0o700)
        for name in ra.EXECUTABLE_PROGRAM_FILES + ra.DATA_PROGRAM_FILES:
            shutil.copy2(self.world.install / name, previous / name)
        for name in ra.DATA_PROGRAM_DIRS:
            shutil.copytree(self.world.install / name, previous / name)
        (previous / 'VERSION').write_text(json.dumps({'commit': self.world.commits['v1.0.0'], 'tag': 'v1.0.0'}) + '\n')
        # What a real snapshot holds: the release's own PROGRAM.json (copied unchanged) and the SNAPSHOT.json record.
        shutil.copy2(self.world.install / 'PROGRAM.json', previous / 'PROGRAM.json')
        lc.write_program_record(previous, lc.current_program(), [])
        before = self.installed_tag()
        proc = self.run_cli('rollback', '--yes', '--health-seconds', '0.5')
        self.assertEqual(proc.returncode, 1)
        self.assertIn('current release was restored', proc.stderr)
        self.assertEqual(self.installed_tag(), before)

    def test_rollback_without_previous_exits_nonzero(self):
        proc = self.run_cli('rollback', '--yes')
        self.assertEqual(proc.returncode, 1)
        self.assertIn('no previous release', proc.stderr)

    def test_tag_flag_must_be_stable_semver(self):
        proc = self.run_cli('update', '--yes', '--tag', 'v1.0.0-rc1')
        self.assertEqual(proc.returncode, 1)
        self.assertIn('stable vX.Y.Z', proc.stderr)

    def test_tag_on_rollback_refuses(self):
        proc = self.run_cli('rollback', '--yes', '--tag', 'v1.0.0')
        self.assertEqual(proc.returncode, 1)
        self.assertIn('applies to update only', proc.stderr)

    def test_update_uses_no_token_and_reaches_only_fake_gh(self):
        self.run_cli('update', '--yes', '--health-seconds', '0.5', env={'GH_TOKEN': 'secret'})
        self.assertEqual(self.world.refused_calls()[0]['env'], ['GH_TOKEN'])

    def test_uninstall_agent_session_refuses_before_any_prompt(self):
        # stdin is empty: if a prompt were reached it would fail as cancelled, not as agent refusal.
        proc = self.run_cli('uninstall', '--purge', env={'AUTONOMY_OPENCODE': '/x'}, stdin='purge\n')
        self.assertEqual(proc.returncode, 1)
        self.assertIn('agent session', proc.stderr)
        self.assertNotIn('Type purge', proc.stdout + proc.stderr)
        self.assertTrue((self.world.install / 'config.json').exists())
        self.assertTrue((self.world.install / 'onboarding.py').exists())

    def test_uninstall_declined_changes_nothing(self):
        proc = self.run_cli('uninstall', stdin='n\n')
        self.assertEqual(proc.returncode, 1)
        self.assertIn('cancelled', proc.stderr)
        self.assertTrue((self.world.install / 'onboarding.py').exists())

    def test_uninstall_purge_without_typed_word_changes_nothing(self):
        proc = self.run_cli('uninstall', '--purge', '--yes', stdin='\n')
        self.assertEqual(proc.returncode, 1)
        self.assertIn('purge not confirmed', proc.stderr)
        self.assertTrue((self.world.install / 'config.json').exists())
        self.assertTrue((self.world.install / 'onboarding.py').exists())

    def test_status_is_read_only_and_reports_version(self):
        before = sorted(str(p.relative_to(self.world.install)) for p in self.world.install.rglob('*'))
        proc = self.run_cli('status')
        after = sorted(str(p.relative_to(self.world.install)) for p in self.world.install.rglob('*'))
        self.assertEqual(before, after)
        self.assertIn('version: v1.0.0', proc.stdout)
        self.assertIn('drain files: none', proc.stdout)
        self.assertEqual(self.world.gh_calls(), [])

    def test_doctor_reports_modified_program_file_and_exits_nonzero(self):
        # README.md is a data file never imported at startup. Tampering an imported module (memory.py)
        # makes onboarding.py fail at import before doctor runs, so that case is not reported here.
        (self.world.install / 'README.md').write_text('tampered\n')
        proc = self.run_cli('doctor')
        self.assertEqual(proc.returncode, 1)
        self.assertIn('FAILED: modified or missing: README.md', proc.stdout)
        self.assertEqual(self.world.gh_calls(), [])


if __name__ == '__main__':
    unittest.main()
