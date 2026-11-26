"""S6: release qualification sees a rename into docs/releases as the file it really changed.

Uses a throwaway git repo (no network, no GitHub). The rename case is the one that matters: with
rename detection a file moved from outside docs/releases into it would list only the destination,
which is inside the allowed prefix, and a code change would be waved through.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import release_artifacts as ra  # noqa: E402

TESTS = ROOT / 'tests'
sys.path.insert(0, str(TESTS))
from test_release_qualification import Fakes, SHA  # noqa: E402

GIT_ENV = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.invalid',
           'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.invalid',
           'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV}, check=True)
    return proc.stdout.strip()


class ChangedPathsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp(prefix='changed-paths-')
        self.addCleanup(shutil.rmtree, tmp, True)
        self.repo = Path(tmp)
        git(self.repo, 'init', '-q', '-b', 'main')
        (self.repo / 'docs' / 'releases').mkdir(parents=True)
        (self.repo / 'docs' / 'releases' / 'keep.md').write_text('keep\n')
        (self.repo / 'lifecycle.py').write_text('print("original")\n')
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-q', '-m', 'base')
        self.base = git(self.repo, 'rev-parse', 'HEAD')

    def commit(self, message: str) -> str:
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-q', '-m', message)
        return git(self.repo, 'rev-parse', 'HEAD')

    def test_rename_from_code_into_release_docs_lists_the_source_too(self):
        # The code file moves into docs/releases/. Rename detection would show only the destination.
        git(self.repo, 'mv', 'lifecycle.py', 'docs/releases/lifecycle.md')
        head = self.commit('rename code into release docs')
        paths = ra._changed_paths(self.repo, self.base, head)
        self.assertIn('lifecycle.py', paths)
        self.assertIn('docs/releases/lifecycle.md', paths)

    def test_qualification_refuses_a_rename_that_hides_a_code_change(self):
        # The real qualify_release, fed the REAL changed-path list from git. The green Fakes world
        # is used, so the only failing rule is the rename-hidden code change.
        git(self.repo, 'mv', 'lifecycle.py', 'docs/releases/lifecycle.md')
        head = self.commit('rename code into release docs')
        fakes = Fakes()
        fakes.changed = ra._changed_paths(self.repo, self.base, head)
        self.assertIn('lifecycle.py', fakes.changed)  # precondition: the source is listed
        errors = fakes.qualify(sha=SHA)
        self.assertTrue(any('outside docs/releases/' in e and 'lifecycle.py' in e for e in errors), errors)

    def test_paths_with_spaces_and_newlines_are_listed_exactly(self):
        odd = 'docs/releases/odd name\nfile.md'
        (self.repo / 'docs' / 'releases' / 'odd name\nfile.md').write_text('x\n')
        head = self.commit('odd names')
        self.assertIn(odd, ra._changed_paths(self.repo, self.base, head))

    def test_a_plain_release_doc_change_is_not_flagged(self):
        (self.repo / 'docs' / 'releases' / 'keep.md').write_text('changed\n')
        head = self.commit('release doc only')
        self.assertEqual(ra._changed_paths(self.repo, self.base, head), ['docs/releases/keep.md'])


if __name__ == '__main__':
    unittest.main()
