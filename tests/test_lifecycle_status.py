"""Offline tests for read-only status/doctor helpers and managed install-root entries.

Uses the disposable install from test_lifecycle_update.World. Nothing is written except
the fixtures this file creates inside that temporary install.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
import lifecycle as lc  # noqa: E402
import release_artifacts as ra  # noqa: E402
from test_lifecycle_update import World  # noqa: E402


def write_manifest(target: Path):
    """Whole-tree manifest, as a release archive ships: every installed file, including program directories."""
    names = sorted(ra.EXECUTABLE_PROGRAM_FILES + ra.DATA_PROGRAM_FILES)
    for name in ra.DATA_PROGRAM_DIRS:
        names += sorted(p.relative_to(target).as_posix() for p in (target / name).rglob('*') if p.is_file())
    lines = [f'{hashlib.sha256((target / name).read_bytes()).hexdigest()}  {name}' for name in sorted(set(names))]
    (target / 'MANIFEST.sha256').write_text('\n'.join(lines) + '\n')


class IntegrityTest(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.addCleanup(self.world.cleanup)
        self.target = self.world.install
        self.links = Path(tempfile.mkdtemp(prefix='doctor-links-'))
        self.addCleanup(lambda: __import__('shutil').rmtree(self.links, ignore_errors=True))
        self.unit_dir = self.links / 'units'
        self.bin_dir = self.links / 'bin'
        self.unit_dir.mkdir()
        self.bin_dir.mkdir()
        (self.unit_dir / lc.UNIT).symlink_to(self.target / 'autocontroller.service')
        (self.bin_dir / 'autocontroller').symlink_to(self.target / 'onboarding.py')
        write_manifest(self.target)

    def diagnose(self):
        return lc.diagnose(self.target, unit_dir=self.unit_dir, bin_dir=self.bin_dir)

    def test_clean_release_install_has_no_failures(self):
        failures, _ = self.diagnose()
        self.assertEqual(failures, [])

    def test_modified_program_file_is_a_failure(self):
        (self.target / 'memory.py').write_text('tampered\n')
        failures, _ = self.diagnose()
        self.assertIn('modified or missing: memory.py', failures)

    def test_missing_program_file_is_a_failure(self):
        (self.target / 'memory.py').unlink()
        failures, _ = self.diagnose()
        self.assertIn('modified or missing: memory.py', failures)

    def test_source_checkout_without_manifest_is_not_flagged(self):
        (self.target / 'MANIFEST.sha256').unlink()
        self.assertEqual(lc.manifest_problems(self.target), [])

    def test_manifest_that_omits_a_program_file_is_a_failure(self):
        lines = (self.target / 'MANIFEST.sha256').read_text().splitlines()
        (self.target / 'MANIFEST.sha256').write_text('\n'.join(l for l in lines if 'memory.py' not in l) + '\n')
        self.assertIn('program file not covered by manifest: memory.py', lc.manifest_problems(self.target))

    def test_malformed_manifest_line_is_a_failure(self):
        (self.target / 'MANIFEST.sha256').write_text('not a digest line\n')
        self.assertEqual(lc.manifest_problems(self.target), ['MANIFEST.sha256 has a malformed line'])

    def test_dangling_owned_unit_link_is_a_failure(self):
        (self.target / 'autocontroller.service').unlink()
        failures, _ = self.diagnose()
        self.assertTrue(any('unit link is dangling' in f for f in failures), failures)

    def test_link_pointing_elsewhere_is_only_a_warning(self):
        other = self.links / 'elsewhere.py'
        other.write_text('x')
        (self.bin_dir / 'autocontroller').unlink()
        (self.bin_dir / 'autocontroller').symlink_to(other)
        failures, warnings = self.diagnose()
        self.assertFalse(any('CLI link' in f for f in failures))
        self.assertTrue(any('points outside this install' in w for w in warnings))

    def test_missing_cli_link_is_a_warning(self):
        (self.bin_dir / 'autocontroller').unlink()
        _, warnings = self.diagnose()
        self.assertTrue(any('CLI link is missing' in w for w in warnings))

    def test_leftover_drain_request_without_lock_is_a_stale_failure(self):
        # S2: no operation holds lifecycle.lock, so the request is stale (fully covered in test_drain_staleness).
        (self.target / 'state').mkdir(exist_ok=True)
        (self.target / 'state' / lc.DRAIN_REQUEST).write_text('{}')
        failures, _ = self.diagnose()
        self.assertTrue(any('stale state/drain.request' in f for f in failures), failures)

    def test_memory_failure_marker_is_reported(self):
        (self.target / 'state' / 'memory').mkdir(parents=True)
        (self.target / 'state' / 'memory' / 'failure').touch()
        _, warnings = self.diagnose()
        self.assertTrue(any('memory failure marker' in w for w in warnings))

    def test_leftover_incoming_snapshot_is_a_failure(self):
        (self.target / 'rollback' / 'incoming').mkdir(parents=True)
        failures, _ = self.diagnose()
        self.assertTrue(any('rollback/incoming exists' in f for f in failures))

    def test_interrupted_staged_directory_is_warned(self):
        (self.target / '.skills.new').mkdir()
        _, warnings = self.diagnose()
        self.assertTrue(any('.skills.new' in w for w in warnings))

    def test_diagnose_is_read_only(self):
        before = sorted(str(p.relative_to(self.target)) for p in self.target.rglob('*'))
        self.diagnose()
        lc.status_lines(self.target, active=True)
        after = sorted(str(p.relative_to(self.target)) for p in self.target.rglob('*'))
        self.assertEqual(before, after)


class RealArchiveInstallTest(unittest.TestCase):
    """B2: a real archive (whole-tree MANIFEST) installed through install_program_files must pass doctor."""

    GIT_ENV = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.invalid',
               'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.invalid',
               'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1',
               'PATH': os.environ.get('PATH', '/usr/bin:/bin')}

    def git(self, cwd, *args):
        import subprocess
        return subprocess.run(['git', *args], cwd=str(cwd), env=self.GIT_ENV, capture_output=True,
                              text=True, check=True, timeout=120).stdout.strip()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='doctor-real-')
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        src = base / 'src'
        src.mkdir()
        self.git(src, 'init', '-q', '-b', 'main')
        # Every file the repository ships, including ones the install never copies (docs, vmtest).
        repo_files = {'README.md': 'readme\n', 'config.example.json': '{"projects": []}\n',
                      'autocontroller.service': '[Unit]\n', 'install.sh': '#!/usr/bin/env bash\n'}
        for name in ra.EXECUTABLE_PROGRAM_FILES:
            repo_files.setdefault(name, f'# {name}\n')
        for name in ra.DATA_PROGRAM_DIRS:
            repo_files[f'{name}/marker.txt'] = f'{name}\n'
        repo_files['docs/VALIDATION.md'] = 'docs\n'
        repo_files['vmtest/harness.py'] = '# harness\n'
        for rel, text in repo_files.items():
            path = src / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        self.git(src, 'add', '-A')
        self.git(src, 'commit', '-q', '-m', 'release')
        commit = self.git(src, 'rev-parse', 'HEAD')
        dist = base / 'dist'
        ra.build(src, 'v9.9.9', commit, dist)
        self.install = base / 'install'
        self.install.mkdir(mode=0o700)
        extracted = base / 'extracted'
        ra.safe_extract(dist / ra.ARCHIVE_NAME.format(tag='v9.9.9'), extracted)
        lc.install_program_files(extracted, self.install)
        (self.install / 'config.json').write_text('{"projects": []}\n')
        self.links = base / 'links'
        (self.links / 'units').mkdir(parents=True)
        (self.links / 'bin').mkdir()
        (self.links / 'units' / lc.UNIT).symlink_to(self.install / 'autocontroller.service')
        (self.links / 'bin' / 'autocontroller').symlink_to(self.install / 'onboarding.py')

    def test_archive_installed_through_install_program_files_has_zero_doctor_failures(self):
        failures, _ = lc.diagnose(self.install, unit_dir=self.links / 'units', bin_dir=self.links / 'bin')
        self.assertEqual(failures, [])

    def test_installed_tree_has_no_manifest_only_or_extra_program_entries(self):
        self.assertEqual(lc.manifest_problems(self.install), [])
        self.assertFalse((self.install / 'docs').exists(), 'docs are not part of the installed set')

    def test_extra_file_inside_an_installed_program_directory_is_flagged(self):
        (self.install / 'skills' / 'stray.txt').write_text('unexpected\n')
        self.assertIn('unexpected file in installed program directory: skills/stray.txt',
                      lc.manifest_problems(self.install))

    def test_modified_file_inside_an_installed_program_directory_is_a_failure(self):
        (self.install / 'standards' / 'marker.txt').write_text('tampered\n')
        self.assertIn('modified or missing: standards/marker.txt', lc.manifest_problems(self.install))


class StatusTest(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.addCleanup(self.world.cleanup)
        self.target = self.world.install

    def lines(self, active=True):
        return lc.status_lines(self.target, active=active)

    def test_reports_version_service_and_no_history(self):
        lines = self.lines(active=False)
        self.assertIn('version: v1.0.0', lines)
        self.assertIn('service: not active', lines)
        self.assertIn('last update: none recorded', lines)
        self.assertIn('previous kept for rollback: none', lines)

    def test_reports_project_phase_and_pending_marker(self):
        pdir = self.target / 'state' / 'projects' / 'demo-1234'
        pdir.mkdir(parents=True)
        (pdir / 'cycle.json').write_text(json.dumps({'phase': 'ci'}))
        (pdir / 'cycle.pending').write_text('')
        self.assertIn('project demo-1234: phase ci (cycle.pending present)', self.lines())

    def test_reports_drain_files_and_memory_marker(self):
        (self.target / 'state' / lc.DRAIN_REQUEST).write_text('{}')
        (self.target / 'state' / 'memory').mkdir(parents=True)
        (self.target / 'state' / 'memory' / 'failure').touch()
        lines = self.lines()
        self.assertIn(f'drain files: {lc.DRAIN_REQUEST}', lines)
        self.assertIn('memory failure marker: present', lines)

    def test_reports_last_history_entry(self):
        (self.target / 'updates').mkdir()
        (self.target / 'updates' / 'history.json').write_text(json.dumps(
            [{'action': 'update', 'from': 'v1.0.0', 'to': 'v1.1.0', 'result': 'ok', 'at': 1}]))
        self.assertIn('last update: update v1.0.0 -> v1.1.0 (ok)', self.lines())

    def test_reports_previous_release_and_interrupted_incoming(self):
        (self.target / 'rollback' / 'previous').mkdir(parents=True)
        (self.target / 'rollback' / 'previous' / 'VERSION').write_text(json.dumps({'tag': 'v0.9.0', 'commit': 'a' * 40}))
        (self.target / 'rollback' / 'incoming').mkdir()
        lines = self.lines()
        self.assertIn('previous kept for rollback: v0.9.0', lines)
        self.assertIn('rollback/incoming: present (an update did not finish)', lines)


if __name__ == '__main__':
    unittest.main()
