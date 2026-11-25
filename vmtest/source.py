"""In-memory source archive of the checkout: git-tracked + untracked-not-ignored
paths when git works, else a filesystem walk. Never ships .git, state, config.json
or caches.
"""
from __future__ import annotations

import io
import os
import subprocess
import tarfile
from pathlib import Path

EXCLUDED_DIRS = frozenset({'.git', '__pycache__', '.pytest_cache', 'state'})
EXCLUDED_NAMES = frozenset({'config.json', 'controller.lock', 'onboarding.lock'})
EXCLUDED_PARTS = frozenset({'.git', '__pycache__', '.pytest_cache', 'state'})
EXCLUDED_SUFFIXES = ('.pyc', '.log', '.env')
EXCLUDED_TOP = frozenset({'vmtest-artifacts'})


def is_excluded(relative: str) -> bool:
    parts = Path(relative).parts
    if not parts or any(p in EXCLUDED_PARTS for p in parts):
        return True
    if parts[0] in EXCLUDED_TOP or parts[-1] in EXCLUDED_NAMES:
        return True
    return relative.endswith(EXCLUDED_SUFFIXES) or parts[-1].startswith('.env')


def list_files(root: Path) -> list[str]:
    """Return candidate relative paths. Prefer git; fall back to a walk on any git failure."""
    try:
        proc = subprocess.run(['git', '-c', f'safe.directory={root}', '-C', str(root), 'ls-files', '-z',
                               '--cached', '--others', '--exclude-standard'],
                              capture_output=True, timeout=120, check=False)
        if proc.returncode == 0:
            names = [n for n in proc.stdout.decode('utf-8', 'surrogateescape').split('\0') if n]
            if names:
                return names
    except (OSError, subprocess.SubprocessError):
        pass
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]
        for name in filenames:
            found.append(os.path.relpath(os.path.join(dirpath, name), root))
    return found


def build_archive(root: Path) -> bytes:
    """Deterministic gzip tar of the allowed source files; returns bytes."""
    root = Path(root).resolve()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as tar:
        for relative in sorted(set(list_files(root))):
            if is_excluded(relative):
                continue
            path = root / relative
            if path.is_symlink() or not path.is_file():
                continue
            info = tar.gettarinfo(str(path), arcname=relative)
            info.uid = info.gid = 0
            info.uname = info.gname = ''
            info.mtime = 0
            with path.open('rb') as src:
                tar.addfile(info, src)
    return buffer.getvalue()
