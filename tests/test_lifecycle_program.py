"""Offline tests for the program set a release declares: update installs it, rollback restores it.

Built on tests/test_lifecycle_update.World (real release archives from release_artifacts.build,
fake service and virtual clock). Nothing touches systemctl, gh, the network or real HOME.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
import lifecycle as lc  # noqa: E402
import release_artifacts as ra  # noqa: E402
from test_lifecycle_update import Base, git  # noqa: E402

RELEASE_LISTS = Path(ROOT / 'release_artifacts.py').read_text(encoding='utf-8')


def declare(text: str, executable=None, data=None, dirs=None) -> str:
    """Return release_artifacts.py text with its program lists replaced (other lines untouched)."""
    import re
    out = text
    for key, value in (('EXECUTABLE_PROGRAM_FILES', executable), ('DATA_PROGRAM_FILES', data),
                       ('DATA_PROGRAM_DIRS', dirs)):
        if value is None:
            continue
        pattern = re.compile(rf'^{key} = \(.*?\)$', re.M | re.S)
        out, n = pattern.subn(f'{key} = {tuple(value)!r}', out, count=1)
        assert n == 1, key
    return out


class ProgramSetTest(Base):
    """A release's own declared set drives install and removal, not this code's set."""

    def publish_v110_with(self, *, executable=None, data=None, dirs=None, extra=None):
        """Replace the v1.1.0 source with a tree whose release_artifacts.py declares a different set."""
        src = self.world.src_repo
        (src / 'release_artifacts.py').write_text(declare(RELEASE_LISTS, executable, data, dirs))
        for name, text in (extra or {}).items():
            (src / name).parent.mkdir(parents=True, exist_ok=True)
            (src / name).write_text(text)
        git(src, 'add', '-A')
        git(src, 'commit', '-q', '-m', 'declares a new set')
        commit = git(src, 'rev-parse', 'HEAD')
        # Re-publish under a new tag so the update path sees only the new set.
        self.world.commits['v1.2.0'] = commit
        self.world.gh.publish('v1.2.0', commit)

    def update_to_latest(self):
        return lc.update(self.world.install, 'example-owner/autocontroller', **self.world.update_kw(tag='v1.2.0'))

    def test_new_module_is_installed_by_update_and_removed_by_rollback(self):
        self.publish_v110_with(executable=ra.EXECUTABLE_PROGRAM_FILES + ('newmod.py',),
                               extra={'newmod.py': '# newmod\n'})
        # The release's extra file must also be in the release's own archive: already built by publish.
        self.update_to_latest()
        self.assertTrue((self.world.install / 'newmod.py').is_file())
        self.assertEqual(self.world.tag(), 'v1.2.0')
        self.rollback()
        self.assertFalse((self.world.install / 'newmod.py').exists(),
                         'a file the newer release added must be removed on rollback')

    def test_dropped_file_is_removed_by_update_and_restored_by_rollback(self):
        dropped = 'opencode-project-safe.py'
        kept = tuple(n for n in ra.EXECUTABLE_PROGRAM_FILES if n != dropped)
        self.publish_v110_with(executable=kept)
        before = (self.world.install / dropped).read_text()
        self.update_to_latest()
        self.assertFalse((self.world.install / dropped).exists(),
                         'a file the new release no longer declares must be removed')
        self.rollback()
        self.assertEqual((self.world.install / dropped).read_text(), before,
                         'the dropped file must come back exactly on rollback')

    def test_snapshot_records_a_missing_program_file_as_absent_and_rollback_restores_absence(self):
        dest = self.world.install / 'rollback' / 'probe'
        (self.world.install / 'standards' / 'marker.txt').unlink()
        (self.world.install / 'README.md').unlink()
        lc.snapshot(self.world.install, dest, lc.current_program())
        program, absent = lc.snapshot_program(dest)
        self.assertIn('README.md', absent)
        self.assertIn('standards', program['dirs'])
        self.assertFalse((dest / 'README.md').exists())

    def test_rollback_when_a_current_program_file_is_missing_still_works(self):
        # S4: a file lost from the current install must not block rollback; the snapshot records it.
        self.update()
        (self.world.install / 'README.md').unlink()
        self.rollback()
        self.assertEqual(self.world.tag(), 'v1.0.0')
        self.assertTrue((self.world.install / 'README.md').is_file())

    def test_unsafe_declared_name_refuses_before_the_service_is_stopped(self):
        self.publish_v110_with(executable=ra.EXECUTABLE_PROGRAM_FILES + ('../escape.py',))
        with self.assertRaisesRegex(lc.LifecycleError, 'unsafe program name'):
            self.update_to_latest()
        self.assertEqual(self.service.verbs, [], 'nothing may be stopped for a refused release')
        self.assertEqual(self.world.tag(), 'v1.0.0')

    def test_protected_name_declared_by_a_release_is_refused(self):
        self.publish_v110_with(data=ra.DATA_PROGRAM_FILES + ('config.json',))
        with self.assertRaisesRegex(lc.LifecycleError, 'protected name'):
            self.update_to_latest()
        self.assertEqual(self.service.verbs, [])

    def test_release_without_release_artifacts_declaration_is_refused(self):
        src = self.world.src_repo
        (src / 'release_artifacts.py').write_text('# no lists\n')
        git(src, 'add', '-A')
        git(src, 'commit', '-q', '-m', 'no lists')
        commit = git(src, 'rev-parse', 'HEAD')
        self.world.commits['v1.2.0'] = commit
        self.world.gh.publish('v1.2.0', commit)
        with self.assertRaisesRegex(lc.LifecycleError, 'does not declare'):
            self.update_to_latest()
        self.assertEqual(self.service.verbs, [])


if __name__ == '__main__':
    unittest.main()
