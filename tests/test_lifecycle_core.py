"""Offline regression tests for lifecycle.py primitives: agent guard, confirmation, locks, file install."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import lifecycle as lc  # noqa: E402
import release_artifacts as ra  # noqa: E402


class AgentGuardTest(unittest.TestCase):
    def test_no_markers_allows(self):
        lc.refuse_agent({'PATH': '/usr/bin'}, 'update')

    def test_every_autonomy_variable_refuses(self):
        for name in ('AUTONOMY_PROJECT_ROOT', 'AUTONOMY_PENDING_MARKER', 'AUTONOMY_PROJECT_NAME', 'AUTONOMY_OPENCODE'):
            with self.subTest(name=name), self.assertRaisesRegex(lc.LifecycleError, 'agent session'):
                lc.refuse_agent({name: '/x'}, 'update')

    def test_learning_instructions_refuses(self):
        with self.assertRaisesRegex(lc.LifecycleError, 'AUTOCONTROLLER_LEARNING_INSTRUCTIONS'):
            lc.refuse_agent({'AUTOCONTROLLER_LEARNING_INSTRUCTIONS': 'x'}, 'rollback')

    def test_ordinary_opencode_variables_do_not_refuse(self):
        # Broad OPENCODE* matching would block a human shell that runs OpenCode V2.
        lc.refuse_agent({'OPENCODE_CONFIG': '/x', 'OPENCODE_SERVER': 'y'}, 'update')

    def test_markers_are_listed_in_message(self):
        with self.assertRaisesRegex(lc.LifecycleError, 'AUTONOMY_OPENCODE, AUTONOMY_PROJECT_ROOT'):
            lc.refuse_agent({'AUTONOMY_PROJECT_ROOT': 'a', 'AUTONOMY_OPENCODE': 'b'}, 'remove')


class ConfirmTest(unittest.TestCase):
    def test_yes_skips_prompt(self):
        def never(prompt):
            raise AssertionError('prompt must not run with --yes')
        lc.confirm('update', yes=True, ask=never)

    def test_accepts_y_and_yes(self):
        for answer in ('y', 'YES', ' yes '):
            lc.confirm('update', yes=False, ask=lambda prompt, a=answer: a)

    def test_empty_or_other_answer_cancels(self):
        for answer in ('', 'n', 'no', 'purge'):
            with self.subTest(answer=answer), self.assertRaisesRegex(lc.LifecycleError, 'cancelled'):
                lc.confirm('update', yes=False, ask=lambda prompt, a=answer: a)


class LockTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / 'lifecycle.lock'

    def tearDown(self):
        self._tmp.cleanup()

    def test_nonblocking_fails_while_held(self):
        with lc.locked(self.path, blocking=False):
            with self.assertRaisesRegex(lc.LifecycleError, 'held by another operation'):
                with lc.locked(self.path, blocking=False):
                    pass

    def test_released_after_context(self):
        with lc.locked(self.path, blocking=False):
            pass
        with lc.locked(self.path, blocking=False):
            pass


class InstallProgramFilesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.source = base / 'source'
        self.target = base / 'target'
        self.source.mkdir()
        for name in ra.EXECUTABLE_PROGRAM_FILES + ra.DATA_PROGRAM_FILES:
            (self.source / name).write_text(f'# {name} new\n')
        for name in ra.DATA_PROGRAM_DIRS:
            (self.source / name).mkdir()
            (self.source / name / 'file.txt').write_text(f'{name} new\n')
        self.target.mkdir(mode=0o700)

    def tearDown(self):
        self._tmp.cleanup()

    def test_copies_every_listed_file_with_install_modes(self):
        lc.install_program_files(self.source, self.target)
        for name in ra.EXECUTABLE_PROGRAM_FILES:
            with self.subTest(name=name):
                self.assertEqual(os.stat(self.target / name).st_mode & 0o777, 0o700)
                self.assertEqual((self.target / name).read_text(), f'# {name} new\n')
        for name in ra.DATA_PROGRAM_FILES:
            with self.subTest(name=name):
                self.assertEqual(os.stat(self.target / name).st_mode & 0o777, 0o600)
        for name in ra.DATA_PROGRAM_DIRS:
            self.assertEqual((self.target / name / 'file.txt').read_text(), f'{name} new\n')

    def test_lifecycle_install_copies_exactly_install_sh_files(self):
        # install.sh copies the same names; the lifecycle swap must write that exact set and nothing else.
        import re
        text = (ROOT / 'install.sh').read_text(encoding='utf-8')
        executable = re.search(r'for file in ([^;]+); do\n\s+\[\[ "\$SOURCE/\$file"', text)
        self.assertIsNotNone(executable)
        self.assertEqual(tuple(executable.group(1).split()), ra.EXECUTABLE_PROGRAM_FILES)
        lc.install_program_files(self.source, self.target)
        written = sorted(p.name for p in self.target.iterdir() if p.is_file() and p.name not in ra.GENERATED_FILES)
        self.assertEqual(written, sorted(ra.EXECUTABLE_PROGRAM_FILES + ra.DATA_PROGRAM_FILES))

    def test_lifecycle_module_is_a_program_file(self):
        self.assertIn('lifecycle.py', ra.EXECUTABLE_PROGRAM_FILES)
        lc.install_program_files(self.source, self.target)
        self.assertTrue((self.target / 'lifecycle.py').is_file())

    def test_stale_generated_files_removed_when_source_has_none(self):
        (self.target / 'VERSION').write_text('{"tag": "v0.0.1"}\n')
        (self.target / 'MANIFEST.sha256').write_text('old\n')
        lc.install_program_files(self.source, self.target)
        self.assertFalse((self.target / 'VERSION').exists())
        self.assertFalse((self.target / 'MANIFEST.sha256').exists())

    def test_generated_files_copied_when_source_has_them(self):
        (self.source / 'VERSION').write_text('{"tag": "v1.2.3"}\n')
        lc.install_program_files(self.source, self.target)
        self.assertEqual((self.target / 'VERSION').read_text(), '{"tag": "v1.2.3"}\n')

    def test_leaves_unlisted_files_untouched(self):
        (self.target / 'config.json').write_text('{"projects": []}\n')
        (self.target / 'state').mkdir()
        lc.install_program_files(self.source, self.target)
        self.assertEqual((self.target / 'config.json').read_text(), '{"projects": []}\n')
        self.assertTrue((self.target / 'state').is_dir())

    def test_stale_staging_directory_is_cleared(self):
        leftover = self.target / '.tests.new'
        leftover.mkdir()
        (leftover / 'junk').write_text('x')
        lc.install_program_files(self.source, self.target)
        self.assertFalse(leftover.exists())
        self.assertFalse((self.target / 'tests' / 'junk').exists())


if __name__ == '__main__':
    unittest.main()
