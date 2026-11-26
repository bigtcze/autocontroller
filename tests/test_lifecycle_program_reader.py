"""N6: the program-set readers. Staged trees need a valid PROGRAM.json; installed trees use PROGRAM.json if
present, else an ast parse of their own release_artifacts.py (literal lists only, never executed); names with
'/' are refused; a PROGRAM.json that is not the release shape is refused.

Offline: works on temporary directories only. Nothing imports or runs the trees it reads.
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


def release_json(**over) -> str:
    doc = {'schema': 1, 'executable': list(ra.EXECUTABLE_PROGRAM_FILES), 'data': list(ra.DATA_PROGRAM_FILES),
           'dirs': list(ra.DATA_PROGRAM_DIRS)}
    doc.update(over)
    return json.dumps(doc, sort_keys=True)


class Tree(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tree = Path(tmp.name) / 'tree'
        self.tree.mkdir()

    def write(self, name, text):
        (self.tree / name).write_text(text, encoding='utf-8')


class StagedReaderTest(Tree):
    def test_missing_program_json_is_refused(self):
        with self.assertRaisesRegex(lc.LifecycleError, 'no PROGRAM.json'):
            lc.staged_program(self.tree)

    def test_invalid_json_is_refused(self):
        self.write('PROGRAM.json', '{not json')
        with self.assertRaisesRegex(lc.LifecycleError, 'unreadable'):
            lc.staged_program(self.tree)

    def test_wrong_schema_or_extra_key_is_refused(self):
        for text in (release_json(schema=2), json.dumps({'schema': 1, 'executable': [], 'data': []}),
                     release_json(extra=[])):
            with self.subTest(text=text[:40]):
                self.write('PROGRAM.json', text)
                with self.assertRaisesRegex(lc.LifecycleError, 'schema 1'):
                    lc.staged_program(self.tree)

    def test_a_program_json_with_a_foreign_shape_is_refused_as_a_staged_release(self):
        # The snapshot record is SNAPSHOT.json and never reaches this reader; any other shape is refused here.
        self.write('PROGRAM.json', json.dumps({'absent': [], 'program': {'executable': [], 'data': [], 'dirs': []}}))
        with self.assertRaisesRegex(lc.LifecycleError, 'schema 1'):
            lc.staged_program(self.tree)

    def test_valid_release_json_is_read_with_json_and_nothing_is_imported(self):
        self.write('PROGRAM.json', release_json())
        self.write('release_artifacts.py', 'raise SystemExit("must never be executed")\n')
        program = lc.staged_program(self.tree)
        self.assertEqual(program['executable'], ra.EXECUTABLE_PROGRAM_FILES)
        self.assertNotIn('program_set_', ' '.join(sys.modules))


class NameSafetyTest(Tree):
    def test_names_with_a_slash_are_refused(self):
        for bad in ('sub/file.py', '/abs.py', 'a\\b.py'):
            with self.subTest(name=bad):
                with self.assertRaisesRegex(lc.LifecycleError, 'unsafe'):
                    lc._safe_program_name(bad)

    def test_traversal_and_protected_names_are_refused(self):
        for bad in ('..', '.', '', 'config.json', 'state'):
            with self.subTest(name=bad):
                with self.assertRaises(lc.LifecycleError):
                    lc._safe_program_name(bad)

    def test_a_program_json_naming_a_nested_path_is_refused(self):
        self.write('PROGRAM.json', release_json(data=['docs/README.md']))
        with self.assertRaisesRegex(lc.LifecycleError, 'unsafe'):
            lc.staged_program(self.tree)


class InstalledReaderTest(Tree):
    def test_program_json_is_used_when_present(self):
        self.write('PROGRAM.json', release_json(data=['README.md']))
        self.write('release_artifacts.py', 'EXECUTABLE_PROGRAM_FILES = ("x.py",)\n')
        self.assertEqual(lc.installed_program(self.tree)['data'], ('README.md',))

    def test_falls_back_to_the_ast_parse_when_there_is_no_program_json(self):
        self.write('release_artifacts.py', (ROOT / 'release_artifacts.py').read_text(encoding='utf-8'))
        program = lc.installed_program(self.tree)
        self.assertEqual(program['executable'], ra.EXECUTABLE_PROGRAM_FILES)
        self.assertEqual(program['dirs'], ra.DATA_PROGRAM_DIRS)

    def test_ast_fallback_never_executes_the_installed_source(self):
        marker = self.tree.parent / 'ran.txt'
        self.write('release_artifacts.py', f"open({str(marker)!r}, 'w').write('x')\n"
                                            "EXECUTABLE_PROGRAM_FILES = ('release_artifacts.py',)\nDATA_PROGRAM_FILES = ()\n"
                                            "DATA_PROGRAM_DIRS = ()\n")
        lc.installed_program(self.tree)
        self.assertFalse(marker.exists())

    def test_missing_both_files_is_refused(self):
        with self.assertRaisesRegex(lc.LifecycleError, 'no PROGRAM.json and no release_artifacts.py'):
            lc.installed_program(self.tree)

    def test_non_literal_installed_lists_are_refused(self):
        self.write('release_artifacts.py', "EXECUTABLE_PROGRAM_FILES = tuple(['a.py'])\n"
                                            "DATA_PROGRAM_FILES = ()\nDATA_PROGRAM_DIRS = ()\n")
        with self.assertRaisesRegex(lc.LifecycleError, 'cannot read program lists'):
            lc.installed_program(self.tree)

    def test_a_symlinked_program_json_is_refused(self):
        target = self.tree.parent / 'elsewhere.json'
        target.write_text(release_json(), encoding='utf-8')
        (self.tree / 'PROGRAM.json').symlink_to(target)
        with self.assertRaisesRegex(lc.LifecycleError, 'not a regular file'):
            lc.installed_program(self.tree)


if __name__ == '__main__':
    unittest.main()
