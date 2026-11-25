"""Download and cache pinned cloud images. Digest-verified, fail closed."""
from __future__ import annotations

import os
import tempfile
import urllib.request
from pathlib import Path

from . import manifest

MAX_BYTES = 4 * 1024 ** 3
TIMEOUT_SECONDS = 60
CHUNK = 1 << 20


def cache_path(cache: Path, entry: dict) -> Path:
    # Keyed by digest: a changed pin can never reuse an old file.
    return Path(cache) / f"{entry['algorithm']}-{entry['digest']}.img"


def download(url: str, destination: Path, *, max_bytes: int = MAX_BYTES) -> None:
    """Stream to a temporary file in the destination directory; size-capped."""
    if not url.startswith('https://'):
        raise manifest.ManifestError('refusing non-https download')
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=destination.parent, prefix='.download-', suffix='.part')
    try:
        total = 0
        with os.fdopen(fd, 'wb') as out, urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as resp:
            while True:
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise manifest.ManifestError('image exceeds size cap')
                out.write(chunk)
        os.replace(tmp, destination)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def ensure_base_image(name: str, cache: Path, data: dict | None = None) -> Path:
    """Return a verified cached base image; never trust an unverified file."""
    entry = manifest.image(name, data)
    target = cache_path(cache, entry)
    if target.exists():
        try:
            manifest.verify_digest(target, entry['algorithm'], entry['digest'])
            return target
        except manifest.ManifestError:
            target.unlink()  # corrupt cache entry: re-download, never reuse
    download(entry['url'], target)
    try:
        manifest.verify_digest(target, entry['algorithm'], entry['digest'])
    except manifest.ManifestError:
        target.unlink(missing_ok=True)
        raise
    return target
