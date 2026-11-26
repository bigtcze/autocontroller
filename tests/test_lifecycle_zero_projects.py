"""NIT: with zero enabled projects the health check is `py_compile` of each installed executable .py file.

Offline: temporary directories only. Nothing is started, no systemctl, no installed controller is touched.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import lifecycle as lc  # noqa: E402
import release_artifacts as ra  # noqa: E402


class ZeroProjectCheckTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tree = Path(tmp.name) / 'install'
        self.tree.mkdir()
        (self.tree / 'config.json').write_text(json.dumps({'projects': []}))
        for name in ra.EXECUTABLE_PROGRAM_FILES:
            body = 'x = 1\n' if name.endswith('.py') else '#!/bin/sh\n'
            (self.tree / name).write_text(body, encoding='utf-8')
        # The installed set is derived from release_artifacts.py (the hybrid rule), so it must be present.
        (self.tree / 'release_artifacts.py').write_text((ROOT / 'release_artifacts.py').read_text(encoding='utf-8'),
                                                         encoding='utf-8')

    def test_zero_projects_clean_install_passes_the_compile_check(self):
        self.assertEqual(lc.enabled_project_count(self.tree), 0)
        self.assertTrue(lc.run_check(self.tree))

    def test_zero_projects_with_a_syntax_error_in_an_installed_file_fails(self):
        (self.tree / 'memory.py').write_text('def broken(:\n', encoding='utf-8')
        self.assertEqual(lc.enabled_project_count(self.tree), 0)
        self.assertFalse(lc.run_check(self.tree))

    def test_zero_projects_with_a_missing_installed_py_file_fails(self):
        (self.tree / 'lifecycle.py').unlink()
        self.assertFalse(lc.run_check(self.tree))

    def test_zero_projects_with_a_symlinked_installed_py_file_fails(self):
        target = self.tree.parent / 'elsewhere.py'
        target.write_text('x = 1\n', encoding='utf-8')
        (self.tree / 'lifecycle.py').unlink()
        (self.tree / 'lifecycle.py').symlink_to(target)
        self.assertFalse(lc.run_check(self.tree))

    def test_zero_projects_with_an_unreadable_installed_set_fails(self):
        (self.tree / 'release_artifacts.py').write_text('EXECUTABLE_PROGRAM_FILES = tuple([])\n', encoding='utf-8')
        self.assertFalse(lc.run_check(self.tree))



if __name__ == '__main__':
    unittest.main()
