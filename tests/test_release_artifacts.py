"""Offline tests for release_artifacts: deterministic archives, verification, safe extraction, install list.

Temporary git repositories only; no network, no GitHub, no production paths.
"""
import gzip
import hashlib
import json
import io
import re
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

import release_artifacts as ra

ROOT = Path(__file__).resolve().parents[1]
TAG = 'v1.0.0'
ENV = {'PATH': '/usr/bin:/bin', 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1',
       'GIT_TERMINAL_PROMPT': '0', 'LC_ALL': 'C.UTF-8', 'HOME': '/nonexistent'}


def git(repo, *args):
    proc = subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@example.invalid',
                           '-c', 'commit.gpgsign=false', *args], cwd=repo, env=ENV,
                          capture_output=True, text=True, timeout=60, check=False)
    if proc.returncode:
        raise RuntimeError(f'git {args}: {proc.stderr}')
    return proc.stdout.strip()


def _member_data(members, suffix):
    return next(data for member, data in members if member.name.endswith('/' + suffix))


def _set_member_data(members, suffix, data):
    for index, (member, _) in enumerate(members):
        if member.name.endswith('/' + suffix):
            member.size = len(data)
            members[index] = (member, data)
            return
    raise AssertionError(f'no member {suffix}')


class ArchiveFixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / 'src'
        self.repo.mkdir()
        git(self.repo, 'init', '-q', '-b', 'main')
        (self.repo / 'tool.py').write_text('print(1)\n', encoding='utf-8')
        (self.repo / 'bin').mkdir()
        (self.repo / 'bin' / 'run.sh').write_text('#!/bin/sh\n', encoding='utf-8')
        (self.repo / 'bin' / 'run.sh').chmod(0o755)
        # N6: build derives PROGRAM.json from the committed release_artifacts.py, so the fixture commits the real one.
        (self.repo / 'release_artifacts.py').write_text((ROOT / 'release_artifacts.py').read_text(encoding='utf-8'),
                                                       encoding='utf-8')
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-q', '-m', 'one')
        self.commit = git(self.repo, 'rev-parse', 'HEAD')

    def build(self, out_name='d1'):
        return ra.build(self.repo, TAG, self.commit, self.root / out_name)

    def rewrite_archive(self, out_name, mutate):
        """Rebuild the archive from a list of tar members after `mutate(members)`, with a matching SHA256SUMS."""
        out = self.root / out_name
        out.mkdir(parents=True)
        source = self.build('source')['archive']
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode='w') as tar:
            members = []
            with tarfile.open(source, 'r:gz') as src:
                for member in src.getmembers():
                    data = src.extractfile(member).read() if member.isreg() else None
                    members.append((member, data))
            mutate(members)
            for member, data in members:
                tar.addfile(member, io.BytesIO(data) if data is not None else None)
        archive = out / f'autocontroller-{TAG}.tar.gz'
        archive.write_bytes(gzip.compress(raw.getvalue(), mtime=0))
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        (out / 'SHA256SUMS').write_text(f'{digest}  {archive.name}\n', encoding='utf-8')
        return out


class BuildDeterminismTests(ArchiveFixture):
    def test_two_builds_of_the_same_commit_are_byte_identical(self):
        first = self.build('d1')['archive'].read_bytes()
        second = self.build('d2')['archive'].read_bytes()
        self.assertEqual(first, second)

    def test_archive_contains_commit_files_with_normalized_metadata(self):
        self.build()
        with tarfile.open(self.root / 'd1' / f'autocontroller-{TAG}.tar.gz', 'r:gz') as tar:
            members = {m.name: m for m in tar.getmembers()}
        self.assertIn(f'autocontroller-{TAG}/tool.py', members)
        self.assertEqual(members[f'autocontroller-{TAG}/tool.py'].mode, 0o644)
        self.assertEqual(members[f'autocontroller-{TAG}/bin/run.sh'].mode, 0o755)
        for member in members.values():
            self.assertEqual((member.uid, member.gid, member.uname, member.gname), (0, 0, '', ''))
            self.assertEqual(member.mtime, int(git(self.repo, 'show', '-s', '--format=%ct', self.commit)))

    def test_gzip_header_has_no_name_and_no_timestamp(self):
        self.build()
        raw = (self.root / 'd1' / f'autocontroller-{TAG}.tar.gz').read_bytes()
        self.assertEqual(raw[4:8], b'\0\0\0\0')  # mtime
        self.assertEqual(raw[3] & 0x08, 0)  # FNAME flag clear

    def test_sha256sums_lists_exactly_the_archive(self):
        paths = self.build()
        digest = hashlib.sha256(paths['archive'].read_bytes()).hexdigest()
        self.assertEqual(paths['sums'].read_text(encoding='utf-8'), f'{digest}  autocontroller-{TAG}.tar.gz\n')

    def test_working_tree_changes_are_ignored(self):
        before = self.build('d1')['archive'].read_bytes()
        (self.repo / 'tool.py').write_text('changed but uncommitted\n', encoding='utf-8')
        (self.repo / 'untracked.txt').write_text('junk\n', encoding='utf-8')
        after = self.build('d2')['archive'].read_bytes()
        self.assertEqual(before, after)
        with tarfile.open(self.root / 'd2' / f'autocontroller-{TAG}.tar.gz', 'r:gz') as tar:
            names = tar.getnames()
        self.assertFalse(any(n.endswith('untracked.txt') for n in names))

    def test_generated_files_describe_the_release(self):
        files = ra.verify(self.build()['archive'].parent, TAG, self.commit)
        self.assertEqual(files['VERSION'], f'{{"commit": "{self.commit}", "tag": "{TAG}"}}\n'.encode())
        self.assertIn('  tool.py\n'.encode(), files['MANIFEST.sha256'])
        self.assertNotIn(b'  MANIFEST.sha256', files['MANIFEST.sha256'])


class BuildRefusalTests(ArchiveFixture):
    def test_symlink_in_commit_is_refused(self):
        (self.repo / 'link').symlink_to('tool.py')
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-q', '-m', 'link')
        commit = git(self.repo, 'rev-parse', 'HEAD')
        with self.assertRaises(ra.ArtifactError) as ctx:
            ra.build(self.repo, TAG, commit, self.root / 'x')
        self.assertIn('links and submodules', str(ctx.exception))

    def test_submodule_in_commit_is_refused(self):
        git(self.repo, 'update-index', '--add', '--cacheinfo', '160000,' + 'a' * 40 + ',vendor/sub')
        git(self.repo, 'commit', '-q', '-m', 'sub')
        commit = git(self.repo, 'rev-parse', 'HEAD')
        with self.assertRaises(ra.ArtifactError):
            ra.build(self.repo, TAG, commit, self.root / 'x')

    def test_unsafe_name_in_commit_is_refused(self):
        blob = git(self.repo, 'hash-object', '-w', '--stdin')  # placeholder object, content irrelevant
        git(self.repo, 'update-index', '--add', '--cacheinfo', f'100644,{blob},a b\\c')
        git(self.repo, 'commit', '-q', '-m', 'odd')
        commit = git(self.repo, 'rev-parse', 'HEAD')
        with self.assertRaises(ra.ArtifactError):
            ra.build(self.repo, TAG, commit, self.root / 'x')

    def test_bad_tag_and_commit_are_refused_before_any_git_call(self):
        for tag, commit in (('v1.0.0-rc1', self.commit), ('1.0.0', self.commit), (TAG, 'abc'), (TAG, 'A' * 40)):
            with self.subTest(tag=tag, commit=commit), self.assertRaises(ra.ArtifactError):
                ra.build(self.repo, tag, commit, self.root / 'x')

    def test_unresolvable_commit_fails_closed(self):
        with self.assertRaises(ra.ArtifactError):
            ra.build(self.repo, TAG, 'f' * 40, self.root / 'x')


class VerifyTests(ArchiveFixture):
    def test_verify_accepts_a_fresh_build(self):
        dist = self.build()['archive'].parent
        self.assertIn('tool.py', ra.verify(dist, TAG, self.commit))

    def test_tampered_archive_fails_against_sha256sums(self):
        dist = self.build()['archive'].parent
        archive = dist / f'autocontroller-{TAG}.tar.gz'
        data = bytearray(archive.read_bytes())
        data[-20] ^= 0x01
        archive.write_bytes(bytes(data))
        with self.assertRaisesRegex(ra.ArtifactError, 'does not match SHA256SUMS'):
            ra.verify(dist, TAG, self.commit)

    def test_wrong_commit_is_refused(self):
        dist = self.build()['archive'].parent
        with self.assertRaisesRegex(ra.ArtifactError, 'commit'):
            ra.verify(dist, TAG, 'e' * 40)

    def test_wrong_tag_is_refused(self):
        dist = self.build()['archive'].parent
        with self.assertRaises(ra.ArtifactError):
            ra.verify(dist, 'v2.0.0', self.commit)

    def test_sha256sums_listing_another_file_is_refused(self):
        dist = self.build()['archive'].parent
        (dist / 'SHA256SUMS').write_text(f'{"0" * 64}  other.tar.gz\n', encoding='utf-8')
        with self.assertRaisesRegex(ra.ArtifactError, 'exactly'):
            ra.verify(dist, TAG, self.commit)

    def test_manifest_mismatch_with_matching_sha256sums_is_refused(self):
        def tamper(members):
            _set_member_data(members, 'tool.py', b'evil\n')
        dist = self.rewrite_archive('tampered', tamper)
        with self.assertRaisesRegex(ra.ArtifactError, 'MANIFEST.sha256 mismatch for tool.py'):
            ra.verify(dist, TAG)

    def test_manifest_missing_a_file_is_refused(self):
        def drop_tool_entry(members):
            lines = _member_data(members, 'MANIFEST.sha256').decode().splitlines(keepends=True)
            _set_member_data(members, 'MANIFEST.sha256',
                             ''.join(x for x in lines if not x.endswith('  tool.py\n')).encode())
        dist = self.rewrite_archive('short', drop_tool_entry)
        with self.assertRaisesRegex(ra.ArtifactError, 'exactly every other file'):
            ra.verify(dist, TAG)

    def test_version_file_with_wrong_tag_is_refused(self):
        def wrong_version(members):
            _set_member_data(members, 'VERSION',
                             f'{{"commit": "{self.commit}", "tag": "v9.9.9"}}\n'.encode())
        dist = self.rewrite_archive('badversion', wrong_version)
        with self.assertRaisesRegex(ra.ArtifactError, 'VERSION tag'):
            ra.verify(dist, TAG)


class ArchiveSafetyTests(ArchiveFixture):
    def _hostile(self, member_name, *, kind=tarfile.REGTYPE, linkname=''):
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode='w:gz') as tar:
            info = tarfile.TarInfo(member_name)
            info.type = kind
            info.linkname = linkname
            payload = b'owned\n' if kind == tarfile.REGTYPE else b''
            info.size = len(payload) if kind == tarfile.REGTYPE else 0
            tar.addfile(info, io.BytesIO(payload) if kind == tarfile.REGTYPE else None)
        path = self.root / 'hostile.tar.gz'
        path.write_bytes(raw.getvalue())
        return path

    def test_traversal_member_is_refused_and_nothing_is_written(self):
        archive = self.root / f'autocontroller-{TAG}.tar.gz'
        archive.write_bytes(self._hostile(f'autocontroller-{TAG}/../escape.txt').read_bytes())
        dest = self.root / 'dest'
        with self.assertRaises(ra.ArtifactError):
            ra.safe_extract(archive, dest)
        self.assertFalse((self.root / 'escape.txt').exists())
        self.assertFalse(dest.exists() and any(dest.iterdir()))

    def test_absolute_member_is_refused(self):
        with self.assertRaises(ra.ArtifactError):
            ra.read_archive(self._hostile('/etc/owned'), TAG)

    def test_member_outside_prefix_is_refused(self):
        with self.assertRaises(ra.ArtifactError):
            ra.read_archive(self._hostile('other-root/file'), TAG)

    def test_symlink_member_is_refused(self):
        with self.assertRaises(ra.ArtifactError):
            ra.read_archive(self._hostile(f'autocontroller-{TAG}/link', kind=tarfile.SYMTYPE, linkname='/etc/passwd'), TAG)

    def test_device_member_is_refused(self):
        with self.assertRaises(ra.ArtifactError):
            ra.read_archive(self._hostile(f'autocontroller-{TAG}/dev', kind=tarfile.CHRTYPE), TAG)

    def test_member_count_is_bounded(self):
        original = ra.MAX_MEMBERS
        self.addCleanup(setattr, ra, 'MAX_MEMBERS', original)
        ra.MAX_MEMBERS = 2
        dist = self.build()['archive'].parent
        with self.assertRaisesRegex(ra.ArtifactError, 'more than'):
            ra.read_archive(dist / f'autocontroller-{TAG}.tar.gz', TAG)

    def test_safe_extract_writes_verified_files_under_destination(self):
        archive = self.build()['archive']
        dest = self.root / 'out'
        written = ra.safe_extract(archive, dest)
        self.assertIn('tool.py', written)
        self.assertEqual((dest / 'tool.py').read_text(encoding='utf-8'), 'print(1)\n')
        self.assertEqual((dest / 'tool.py').stat().st_mode & 0o777, 0o600)

    def test_safe_extract_refuses_a_non_empty_destination(self):
        archive = self.build()['archive']
        dest = self.root / 'busy'
        dest.mkdir()
        (dest / 'keep').write_text('x', encoding='utf-8')
        with self.assertRaises(ra.ArtifactError):
            ra.safe_extract(archive, dest)


class InstallListTests(unittest.TestCase):
    def test_install_sh_lists_equal_program_files(self):
        text = (ROOT / 'install.sh').read_text(encoding='utf-8')
        executable = re.search(r'for file in ([^;]+); do\n \[\[ "\$SOURCE/\$file" == "\$TARGET/\$file" \]\] \|\| install -m 700', text)
        data = re.search(r'for file in ([^;]+); do\n \[\[ "\$SOURCE/\$file" == "\$TARGET/\$file" \]\] \|\| install -m 600', text)
        dirs = re.search(r'for dir in ([^;]+); do', text)
        self.assertIsNotNone(executable)
        self.assertIsNotNone(data)
        self.assertIsNotNone(dirs)
        self.assertEqual(tuple(executable.group(1).split()), ra.EXECUTABLE_PROGRAM_FILES)
        self.assertEqual(tuple(data.group(1).split()), ra.DATA_PROGRAM_FILES)
        self.assertEqual(tuple(dirs.group(1).split()), ra.DATA_PROGRAM_DIRS)

    def test_install_sh_copies_release_metadata_and_removes_stale_copies(self):
        text = (ROOT / 'install.sh').read_text(encoding='utf-8')
        # N6: PROGRAM.json travels with VERSION and MANIFEST.sha256 (copied when present, removed when not).
        self.assertIn('for file in VERSION MANIFEST.sha256 PROGRAM.json; do', text)
        self.assertIn('rm -f "$TARGET/$file"', text)
        self.assertIn('release_artifacts.py', ra.EXECUTABLE_PROGRAM_FILES)


class ProgramJsonTests(ArchiveFixture):
    """N6: PROGRAM.json is built from the committed lists, is in MANIFEST, and is parsed without execution."""

    def test_build_writes_program_json_from_the_committed_lists(self):
        files = ra.verify(self.build()['archive'].parent, TAG, self.commit)
        doc = json.loads(files['PROGRAM.json'])
        self.assertEqual(doc, {
            'schema': 1,
            'executable': list(ra.EXECUTABLE_PROGRAM_FILES),
            'data': list(ra.DATA_PROGRAM_FILES),
            'dirs': list(ra.DATA_PROGRAM_DIRS),
        })
        self.assertEqual(files['PROGRAM.json'], ra.program_json(ra.program_lists_from_source(
            (ROOT / 'release_artifacts.py').read_bytes())))

    def test_program_json_is_covered_by_manifest_and_tampering_fails_verify(self):
        dist = self.build()['archive']
        files = ra.verify(dist.parent, TAG, self.commit)
        self.assertIn(b'  PROGRAM.json\n', files['MANIFEST.sha256'])

        def tamper(members):
            _set_member_data(members, 'PROGRAM.json', b'{"schema":1,"executable":[],"data":[],"dirs":[]}\n')
        bad_dir = self.rewrite_archive('bad-program', tamper)
        with self.assertRaisesRegex(ra.ArtifactError, 'MANIFEST.sha256 mismatch for PROGRAM.json'):
            ra.verify(bad_dir, TAG, self.commit)

    def test_build_takes_lists_from_the_building_commit_not_from_the_running_code(self):
        # The commit declares a DIFFERENT set; PROGRAM.json must reflect the commit, not this checkout.
        text = (ROOT / 'release_artifacts.py').read_text(encoding='utf-8')
        text = text.replace("DATA_PROGRAM_FILES = ('README.md', 'config.example.json', 'autocontroller.service')",
                            "DATA_PROGRAM_FILES = ('config.example.json', 'autocontroller.service')")
        self.assertNotEqual(text, (ROOT / 'release_artifacts.py').read_text(encoding='utf-8'))
        (self.repo / 'release_artifacts.py').write_text(text, encoding='utf-8')
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-q', '-m', 'declare a different set')
        commit = git(self.repo, 'rev-parse', 'HEAD')
        dist = ra.build(self.repo, TAG, commit, self.root / 'd-other')['archive']
        files = ra.verify(dist.parent, TAG, commit)
        self.assertNotIn('README.md', json.loads(files['PROGRAM.json'])['data'])

    def test_commit_without_release_artifacts_is_refused_at_build_with_no_module_fallback(self):
        # NIT: a commit that lacks release_artifacts.py is refused. The running module's constants must never
        # stand in for it, even when the repo is this checkout.
        git(self.repo, 'rm', '-q', 'release_artifacts.py')
        git(self.repo, 'commit', '-q', '-m', 'drop the declaring module')
        commit = git(self.repo, 'rev-parse', 'HEAD')
        with self.assertRaisesRegex(ra.ArtifactError, 'has no release_artifacts.py'):
            ra.build(self.repo, TAG, commit, self.root / 'd-nofallback')

    def test_non_literal_list_is_refused_at_build(self):
        for body in ("EXECUTABLE_PROGRAM_FILES = tuple(['a.py'])\n",
                     "EXECUTABLE_PROGRAM_FILES = ('a.py', name)\n",
                     "EXECUTABLE_PROGRAM_FILES = ('a.py',) + ('b.py',)\n"):
            with self.subTest(body=body):
                with self.assertRaisesRegex(ValueError, 'literal|non-string'):
                    ra.program_lists_from_source(body + "DATA_PROGRAM_FILES = ()\nDATA_PROGRAM_DIRS = ()\n")

    LISTS = ("EXECUTABLE_PROGRAM_FILES = ('a.py',)\nDATA_PROGRAM_FILES = ()\nDATA_PROGRAM_DIRS = ()\n")

    def test_augmented_assignment_to_a_list_is_refused(self):
        # R2: `+=` rebinds the list after the literal was read; the parser must not accept it.
        with self.assertRaisesRegex(ValueError, 'other than as one top-level literal'):
            ra.program_lists_from_source(self.LISTS + "EXECUTABLE_PROGRAM_FILES += ('b.py',)\n")

    def test_reassignment_inside_if_is_refused(self):
        # R2: a conditional rebinding is not a top-level assignment and must be refused, not ignored.
        with self.assertRaisesRegex(ValueError, 'other than as one top-level literal'):
            ra.program_lists_from_source(self.LISTS + "if True:\n    EXECUTABLE_PROGRAM_FILES = ('x.py',)\n")

    def test_duplicate_top_level_assignment_is_refused(self):
        # R2: a second top-level assignment of a list name is refused as a duplicate.
        with self.assertRaisesRegex(ValueError, 'more than once'):
            ra.program_lists_from_source(self.LISTS + "EXECUTABLE_PROGRAM_FILES = ('b.py',)\n")

    def test_del_of_a_list_name_is_refused(self):
        # R2: `del` removes the binding the reader relies on; refuse it.
        with self.assertRaisesRegex(ValueError, 'other than as one top-level literal'):
            ra.program_lists_from_source(self.LISTS + "del DATA_PROGRAM_DIRS\n")

    def test_read_only_use_of_a_list_name_is_still_accepted(self):
        # R2 must not over-refuse: reading a list name without binding it is harmless.
        found = ra.program_lists_from_source(self.LISTS + "COPY = (DATA_PROGRAM_DIRS,)\n")
        self.assertEqual(found['DATA_PROGRAM_DIRS'], ())

    def test_duplicate_assignment_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'more than once'):
            ra.program_lists_from_source("DATA_PROGRAM_DIRS = ()\nDATA_PROGRAM_DIRS = ()\n"
                                         "EXECUTABLE_PROGRAM_FILES = ()\nDATA_PROGRAM_FILES = ()\n")

    def test_parser_never_executes_the_source(self):
        # A module-level side effect must not run: only the literal lists are read.
        marker = self.root / 'executed.txt'
        body = (f"open({str(marker)!r}, 'w').write('x')\n"
                "EXECUTABLE_PROGRAM_FILES = ('a.py',)\nDATA_PROGRAM_FILES = ()\nDATA_PROGRAM_DIRS = ()\n")
        ra.program_lists_from_source(body)
        self.assertFalse(marker.exists(), 'the installed source must be parsed, never executed')



if __name__ == '__main__':
    unittest.main()
