"""Pinned, official cloud image manifest: loading, validation and digest checks."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

MANIFEST = Path(__file__).resolve().parent / 'images.json'
SUPPORTED = ('debian-12', 'debian-13', 'ubuntu-24.04', 'ubuntu-26.04')
ALLOWED_HOSTS = frozenset({'cloud.debian.org', 'cloud-images.ubuntu.com'})
DIGEST_LENGTHS = {'sha256': 64, 'sha512': 128}
FORMATS = frozenset({'qcow2', 'raw'})


class ManifestError(ValueError):
    pass


def validate(data: dict) -> dict:
    """Fail closed on any deviation from the pinned, official-host contract."""
    if not isinstance(data, dict) or data.get('schema') != 1 or data.get('arch') != 'amd64':
        raise ManifestError('manifest must declare schema 1 and arch amd64')
    images = data.get('images')
    if not isinstance(images, dict):
        raise ManifestError('manifest images must be an object')
    if set(images) != set(SUPPORTED):
        raise ManifestError(f'manifest must list exactly {", ".join(SUPPORTED)}')
    for name in SUPPORTED:
        entry = images[name]
        if not isinstance(entry, dict):
            raise ManifestError(f'{name}: entry must be an object')
        url = entry.get('url')
        if not isinstance(url, str):
            raise ManifestError(f'{name}: url missing')
        parts = urlsplit(url)
        if parts.scheme != 'https':
            raise ManifestError(f'{name}: url must use https')
        if parts.hostname not in ALLOWED_HOSTS or parts.username or parts.password or parts.port:
            raise ManifestError(f'{name}: host not in allowlist')
        algorithm = entry.get('algorithm')
        if algorithm not in DIGEST_LENGTHS:
            raise ManifestError(f'{name}: unsupported digest algorithm')
        digest = entry.get('digest')
        if not isinstance(digest, str) or len(digest) != DIGEST_LENGTHS[algorithm] \
                or not re.fullmatch(r'[0-9a-f]+', digest):
            raise ManifestError(f'{name}: digest must be {DIGEST_LENGTHS[algorithm]} lowercase hex chars')
        if entry.get('format') not in FORMATS:
            raise ManifestError(f'{name}: format must be qcow2 or raw')
        osr = entry.get('os_release')
        if not isinstance(osr, dict) or not isinstance(osr.get('ID'), str) or not isinstance(osr.get('VERSION_ID'), str):
            raise ManifestError(f'{name}: os_release ID/VERSION_ID required')
    return data


def load(path: Path = MANIFEST) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f'cannot read manifest {path}: {exc}') from exc
    return validate(data)


def image(name: str, data: dict | None = None) -> dict:
    data = data or load()
    if name not in data['images']:
        raise ManifestError(f'unsupported distro {name!r}; choose one of {", ".join(SUPPORTED)}')
    return data['images'][name]


def verify_digest(path: Path, algorithm: str, expected: str) -> None:
    """Hash the file in chunks and compare; raises ManifestError on any mismatch."""
    if algorithm not in DIGEST_LENGTHS:
        raise ManifestError(f'unsupported digest algorithm {algorithm}')
    h = hashlib.new(algorithm)
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            h.update(chunk)
    if h.hexdigest() != expected.lower():
        raise ManifestError(f'{algorithm} mismatch for {Path(path).name}')
