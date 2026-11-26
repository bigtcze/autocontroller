#!/usr/bin/env python3
"""Deterministic release archives, SHA256 manifests and release qualification.

Archives are built only from git COMMIT objects (never the working tree), so the
same commit yields identical bytes on any distribution. Verification and
extraction share one validator that refuses links, devices, absolute or
traversing names, and unbounded archives. Stdlib only; every subprocess has a
timeout. Pure decision logic lives in release_policy.py.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile

import release_policy as releases

# Source of truth for install.sh. tests/test_release_artifacts.py checks install.sh against these lists.
EXECUTABLE_PROGRAM_FILES = ('autonomy-controller.py', 'opencode-project-safe.py', 'release_policy.py',
                            'release_artifacts.py', 'memory.py', 'bootstrap_policy.py', 'onboarding.py', 'lifecycle.py',
                            'install.sh')
DATA_PROGRAM_FILES = ('README.md', 'config.example.json', 'autocontroller.service')
DATA_PROGRAM_DIRS = ('skills', 'standards', 'tests')
PROGRAM_FILES = {'0700': EXECUTABLE_PROGRAM_FILES, '0600': DATA_PROGRAM_FILES, 'dirs': DATA_PROGRAM_DIRS}
GENERATED_FILES = ('VERSION', 'MANIFEST.sha256')
ARCHIVE_NAME = 'autocontroller-{tag}.tar.gz'
GIT_TIMEOUT = 120
GH_TIMEOUT = 240
MAX_MEMBERS = 5000
MAX_TOTAL_BYTES = 256 * 1024 * 1024
HEX40 = re.compile(r'[0-9a-f]{40}')
HEX64 = re.compile(r'[0-9a-f]{64}')
SAFE_COMPONENT = re.compile(r'[A-Za-z0-9._+@-][A-Za-z0-9._+@ -]*')


class ArtifactError(Exception):
    """Any refusal: one clear line, nonzero exit from the CLI."""


def _run(argv: list[str], *, cwd: Path | None = None, timeout: int = GIT_TIMEOUT) -> bytes:
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ArtifactError(f'command failed: {argv[0]}: {exc}') from exc
    if proc.returncode:
        raise ArtifactError(f'command rc={proc.returncode}: {argv[:3]}: {proc.stderr.decode("utf-8", "replace").strip()[:400]}')
    return proc.stdout


def _git(repo: Path, *args: str, timeout: int = GIT_TIMEOUT) -> bytes:
    # safe.directory: the output of a read-only mounted checkout may belong to another uid in a container.
    return _run(['git', '-c', 'safe.directory=*', '-C', str(repo), *args], timeout=timeout)


def _check_tag_commit(tag: str, commit: str):
    if not releases.version(tag):
        raise ArtifactError(f'tag must be stable SemVer vX.Y.Z: {tag!r}')
    if not HEX40.fullmatch(commit or ''):
        raise ArtifactError('commit must be a full 40-character lowercase hex SHA')


def _safe_name(name: str) -> bool:
    """Relative POSIX path: no absolute, no empty/dot/dotdot components, no backslash or control characters."""
    if not name or name.startswith('/') or '\\' in name or any(ord(c) < 32 for c in name):
        return False
    parts = name.split('/')
    return all(p not in ('', '.', '..') and SAFE_COMPONENT.fullmatch(p) for p in parts)


def tree_entries(repo: Path, commit: str) -> list[tuple[str, str, str]]:
    """(mode, object, path) for every entry of the COMMIT tree. Links and submodules are refused."""
    raw = _git(repo, 'ls-tree', '-r', '-z', '--full-tree', commit)
    entries = []
    for record in raw.split(b'\0'):
        if not record:
            continue
        meta, _, path = record.partition(b'\t')
        mode, kind, obj = meta.decode('ascii').split(' ')
        name = path.decode('utf-8')
        if kind != 'blob' or mode not in ('100644', '100755'):
            raise ArtifactError(f'unsupported tree entry {mode} {kind} {name!r}: links and submodules are refused')
        if not _safe_name(name):
            raise ArtifactError(f'unsafe path in commit: {name!r}')
        entries.append((mode, obj, name))
    return sorted(entries, key=lambda e: e[2])


def _manifest_text(files: dict[str, bytes]) -> bytes:
    lines = [f'{hashlib.sha256(files[name]).hexdigest()}  {name}\n' for name in sorted(files)]
    return ''.join(lines).encode('utf-8')


def release_files(repo: Path, tag: str, commit: str) -> dict[str, tuple[int, bytes]]:
    """Every archive member (relative path -> (mode, bytes)) for one tag at one commit, including generated files."""
    _check_tag_commit(tag, commit)
    if _git(repo, 'cat-file', '-t', commit, timeout=30).strip() != b'commit':
        raise ArtifactError(f'{commit} is not a commit object in {repo}')
    files: dict[str, tuple[int, bytes]] = {}
    for mode, obj, name in tree_entries(repo, commit):
        if name in GENERATED_FILES:
            raise ArtifactError(f'commit must not contain generated file {name}')
        files[name] = (0o755 if mode == '100755' else 0o644, _git(repo, 'cat-file', 'blob', obj))
    version = json.dumps({'commit': commit, 'tag': tag}, sort_keys=True).encode('utf-8') + b'\n'
    files['VERSION'] = (0o644, version)
    manifest_source = {name: data for name, (_, data) in files.items()}
    files['MANIFEST.sha256'] = (0o644, _manifest_text(manifest_source))
    return files


def build(repo_dir: str | Path, tag: str, commit: str, out_dir: str | Path) -> dict[str, Path]:
    """Write autocontroller-<tag>.tar.gz and SHA256SUMS into out_dir. Returns the written paths."""
    repo = Path(repo_dir)
    _check_tag_commit(tag, commit)
    timestamp = int(_git(repo, 'show', '-s', '--format=%ct', commit, timeout=30).strip() or 0)
    files = release_files(repo, tag, commit)
    prefix = f'autocontroller-{tag}'
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    archive = out / ARCHIVE_NAME.format(tag=tag)
    tmp = archive.with_name(f'.{archive.name}.tmp')
    dirs = sorted({d for name in files for d in _parents(name)})
    with tmp.open('wb') as raw:
        # mtime=0 and an empty filename keep the gzip header independent of the build host.
        with gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=0, compresslevel=9) as gz:
            with tarfile.open(fileobj=gz, mode='w', format=tarfile.PAX_FORMAT) as tar:
                for d in dirs:
                    _add(tar, f'{prefix}/{d}', tarfile.DIRTYPE, b'', 0o755, timestamp)
                for name in sorted(files):
                    mode, data = files[name]
                    _add(tar, f'{prefix}/{name}', tarfile.REGTYPE, data, mode, timestamp)
        raw.flush()
        os.fsync(raw.fileno())
    os.chmod(tmp, 0o644)
    os.replace(tmp, archive)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    sums = out / 'SHA256SUMS'
    sums.write_text(f'{digest}  {archive.name}\n', encoding='utf-8')
    os.chmod(sums, 0o644)
    return {'archive': archive, 'sums': sums}


def _parents(name: str) -> list[str]:
    parts = name.split('/')[:-1]
    return ['/'.join(parts[:i]) for i in range(1, len(parts) + 1)]


def _add(tar: tarfile.TarFile, name: str, kind: bytes, data: bytes, mode: int, mtime: int):
    info = tarfile.TarInfo(name)
    info.type = kind
    info.mode = mode
    info.size = len(data) if kind == tarfile.REGTYPE else 0
    info.mtime = mtime
    info.uid = info.gid = 0
    info.uname = info.gname = ''
    tar.addfile(info, io.BytesIO(data) if kind == tarfile.REGTYPE else None)


# ---- verification -----------------------------------------------------------

def read_archive(path: str | Path, tag: str) -> dict[str, bytes]:
    """Return {relative path: bytes} for regular files under the tag prefix.

    Refuses links, devices, unsafe names, names outside the prefix, duplicates, and archives
    that exceed the member or byte bounds. Nothing is written to disk.
    """
    prefix = f'autocontroller-{tag}/'
    files: dict[str, bytes] = {}
    total = 0
    members = 0
    try:
        with tarfile.open(path, 'r:gz') as tar:
            for member in tar:
                members += 1
                if members > MAX_MEMBERS:
                    raise ArtifactError(f'archive has more than {MAX_MEMBERS} members')
                name = member.name
                if name.rstrip('/') == prefix.rstrip('/') and member.isdir():
                    continue  # the archive root directory itself carries no file
                if not name.startswith(prefix) or not _safe_name(name[len(prefix):]):
                    raise ArtifactError(f'archive member outside {prefix} or unsafe: {name!r}')
                rel = name[len(prefix):]
                if member.isdir():
                    continue
                if not member.isreg():
                    raise ArtifactError(f'archive member is not a regular file or directory: {name!r}')
                if member.size < 0 or total + member.size > MAX_TOTAL_BYTES:
                    raise ArtifactError('archive exceeds the byte bound')
                total += member.size
                if rel in files:
                    raise ArtifactError(f'duplicate archive member: {rel}')
                handle = tar.extractfile(member)
                if handle is None:
                    raise ArtifactError(f'unreadable archive member: {rel}')
                files[rel] = handle.read()
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise ArtifactError(f'cannot read archive {Path(path).name}: {exc}') from exc
    return files


def verify(dist_dir: str | Path, tag: str, commit: str | None = None) -> dict[str, bytes]:
    """Verify SHA256SUMS, the archive, VERSION and MANIFEST.sha256. Returns the archive's files."""
    if not releases.version(tag):
        raise ArtifactError(f'tag must be stable SemVer vX.Y.Z: {tag!r}')
    if commit is not None and not HEX40.fullmatch(commit):
        raise ArtifactError('commit must be a full 40-character lowercase hex SHA')
    dist = Path(dist_dir)
    name = ARCHIVE_NAME.format(tag=tag)
    try:
        lines = (dist / 'SHA256SUMS').read_text(encoding='utf-8').splitlines()
    except OSError as exc:
        raise ArtifactError(f'cannot read SHA256SUMS: {exc}') from exc
    if len(lines) != 1:
        raise ArtifactError('SHA256SUMS must list exactly one archive')
    match = re.fullmatch(r'([0-9a-f]{64})  (\S+)', lines[0])
    if not match or match.group(2) != name:
        raise ArtifactError(f'SHA256SUMS must list exactly {name}')
    archive = dist / name
    try:
        actual = hashlib.sha256(archive.read_bytes()).hexdigest()
    except OSError as exc:
        raise ArtifactError(f'cannot read {name}: {exc}') from exc
    if actual != match.group(1):
        raise ArtifactError(f'{name} does not match SHA256SUMS')
    files = read_archive(archive, tag)
    try:
        version = json.loads(files.get('VERSION', b'').decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ArtifactError(f'VERSION is not valid JSON: {exc}') from exc
    if not isinstance(version, dict) or set(version) != {'commit', 'tag'}:
        raise ArtifactError('VERSION must contain exactly tag and commit')
    if version['tag'] != tag:
        raise ArtifactError(f'VERSION tag {version["tag"]!r} does not match {tag}')
    if not HEX40.fullmatch(str(version['commit'])) or (commit is not None and version['commit'] != commit):
        raise ArtifactError('VERSION commit does not match the expected commit')
    verify_manifest(files)
    return files


def verify_manifest(files: dict[str, bytes]):
    """Require MANIFEST.sha256 to cover exactly every other file, with matching SHA256 digests."""
    if 'MANIFEST.sha256' not in files:
        raise ArtifactError('archive has no MANIFEST.sha256')
    covered: dict[str, str] = {}
    for line in files['MANIFEST.sha256'].decode('utf-8').splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  (.+)', line)
        if not match:
            raise ArtifactError(f'malformed MANIFEST.sha256 line: {line[:80]!r}')
        if match.group(2) in covered:
            raise ArtifactError(f'duplicate MANIFEST entry: {match.group(2)}')
        covered[match.group(2)] = match.group(1)
    expected = {name for name in files if name != 'MANIFEST.sha256'}
    if set(covered) != expected:
        raise ArtifactError('MANIFEST.sha256 must cover exactly every other file in the archive')
    for name, digest in covered.items():
        if hashlib.sha256(files[name]).hexdigest() != digest:
            raise ArtifactError(f'MANIFEST.sha256 mismatch for {name}')


def safe_extract(archive: str | Path, dest: str | Path) -> list[str]:
    """Extract the validated members (prefix stripped) into a new or empty dest. Returns written paths."""
    match = re.fullmatch(r'autocontroller-(v\d+\.\d+\.\d+)\.tar\.gz', Path(archive).name)
    if not match or not releases.version(match.group(1)):
        raise ArtifactError(f'archive name must be autocontroller-vX.Y.Z.tar.gz: {Path(archive).name}')
    files = read_archive(archive, match.group(1))
    target = Path(dest)
    if target.exists() and any(target.iterdir()):
        raise ArtifactError(f'extraction destination is not empty: {target}')
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    root = target.resolve()
    written = []
    for name in sorted(files):
        out = (target / name).resolve()
        if root not in out.parents:
            raise ArtifactError(f'extraction escapes destination: {name!r}')
        out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        out.write_bytes(files[name])
        os.chmod(out, 0o600)
        written.append(name)
    return written


# ---- release qualification (network wrapper; tests inject fakes) -------------

def _gh_json(*args: str):
    try:
        proc = subprocess.run(['gh', *args], capture_output=True, text=True, timeout=GH_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ArtifactError(f'gh failed: {exc}') from exc
    if proc.returncode:
        raise ArtifactError(f'gh rc={proc.returncode}: {proc.stderr.strip()[:400]}')
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ArtifactError('gh produced invalid JSON') from exc


def _gh_pages(path: str) -> list:
    data = _gh_json('api', '--method', 'GET', path, '--paginate', '--slurp')
    if not isinstance(data, list) or not all(isinstance(page, list) for page in data):
        raise ArtifactError('gh api pagination output is not an array of arrays')
    rows = [row for page in data for row in page]
    if len(rows) > 1000:
        raise ArtifactError('GitHub collection exceeds the safety limit; refusing truncated decision')
    return rows


def qualify(repo: str, tag: str, sha: str, repo_dir: Path = Path('.')) -> list[str]:
    if not HEX40.fullmatch(sha or ''):
        return ['candidate sha must be a full 40-character lowercase hex SHA']
    git = lambda *a: subprocess.run(['git', '-C', str(repo_dir), *a], capture_output=True, timeout=GIT_TIMEOUT, check=False)

    def is_ancestor(a: str, b: str) -> bool:
        proc = git('merge-base', '--is-ancestor', a, b)
        if proc.returncode not in (0, 1):
            raise ArtifactError(f'merge-base failed for {a}..{b}: {proc.stderr.decode("utf-8", "replace").strip()[:200]}')
        return proc.returncode == 0

    def changed_paths(a: str, b: str) -> list[str]:
        proc = git('diff', '--name-only', a, b)
        if proc.returncode:
            raise ArtifactError(f'git diff failed for {a}..{b}')
        return [line for line in proc.stdout.decode('utf-8', 'replace').splitlines() if line]

    def read_file(commit: str, path: str) -> str | None:
        if git('cat-file', '-e', f'{commit}:{path}').returncode:
            return None
        proc = git('show', f'{commit}:{path}')
        if proc.returncode:
            raise ArtifactError(f'cannot read {path} at {commit}')
        return proc.stdout.decode('utf-8')

    previous = releases.latest_release(repo, _gh_pages, exclude_tag=tag)
    previous_tag = previous['tag_name'] if previous else None
    return releases.qualify_release(
        repo, tag, sha, previous_tag=previous_tag,
        on_main=is_ancestor(sha, 'origin/main'),
        gh_json=lambda *a: _gh_json(*a), pages=_gh_pages,
        is_ancestor=is_ancestor, changed_paths=changed_paths, read_file=read_file)


# ---- CLI -------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    b = sub.add_parser('build')
    b.add_argument('--repo', required=True)
    b.add_argument('--tag', required=True)
    b.add_argument('--commit', required=True)
    b.add_argument('--out', required=True)
    v = sub.add_parser('verify')
    v.add_argument('--dist', required=True)
    v.add_argument('--tag', required=True)
    v.add_argument('--commit')
    q = sub.add_parser('qualify')
    q.add_argument('--repo', required=True)
    q.add_argument('--tag', required=True)
    q.add_argument('--sha', required=True)
    q.add_argument('--checkout', default='.')
    args = parser.parse_args(argv)
    try:
        if args.command == 'build':
            paths = build(args.repo, args.tag, args.commit, args.out)
            print(paths['sums'].read_text(encoding='utf-8'), end='')
            return 0
        if args.command == 'verify':
            verify(args.dist, args.tag, args.commit)
            print(f'verified {ARCHIVE_NAME.format(tag=args.tag)}')
            return 0
        errors = qualify(args.repo, args.tag, args.sha, Path(args.checkout))
        for error in errors:
            print(f'release blocked: {error}', file=sys.stderr)
        if errors:
            return 1
        print(f'release {args.tag} qualified at {args.sha}')
        return 0
    except (ArtifactError, ValueError) as exc:
        print(f'release_artifacts: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
