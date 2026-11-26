#!/usr/bin/env python3
"""Installed-controller lifecycle primitives: agent guard, locks, program-file swap.

Safety invariants (logical guards, not a sandbox):
- Controller-spawned agent sessions carry AUTONOMY_* markers. Refuse lifecycle
  changes while any is present so an agent cannot upgrade or remove its controller.
- Only names in release_artifacts.PROGRAM_FILES are ever written or removed.
- Writes are staged next to the destination and atomically replaced.
"""
from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import release_artifacts as ra
import release_policy as releases

ROOT = Path(__file__).resolve().parent
AGENT_ENV_PREFIX = 'AUTONOMY_'
AGENT_ENV_NAMES = ('AUTOCONTROLLER_LEARNING_INSTRUCTIONS',)
LIFECYCLE_LOCK = 'lifecycle.lock'
ONBOARDING_LOCK = 'onboarding.lock'


class LifecycleError(Exception):
    """One clear refusal line; the CLI exits nonzero."""


# OPENCODE_SESSION_ID is set in every OpenCode session, including ones an agent runs in, so its
# presence refuses even without an AUTONOMY_* marker. Operators run these commands from a plain
# terminal (no OpenCode session), never from inside one.
AGENT_SESSION_VAR = 'OPENCODE_SESSION_ID'


def agent_markers(environ) -> list[str]:
    names = [k for k in environ if k.startswith(AGENT_ENV_PREFIX) or k in AGENT_ENV_NAMES]
    if AGENT_SESSION_VAR in environ:
        names.append(AGENT_SESSION_VAR)
    return sorted(names)


def refuse_agent(environ, action: str):
    markers = agent_markers(environ)
    if markers:
        raise LifecycleError(f'{action} refused inside an agent session ({", ".join(markers)} set); '
                             'run it from a normal terminal, not inside an OpenCode session')


def confirm(action: str, *, yes: bool, ask=input):
    """Interactive confirmation; --yes skips it. Anything but y/yes cancels with no change."""
    if yes:
        return
    try:
        answer = ask(f'{action}? [y/N] ')
    except EOFError:  # no terminal input: never assume consent
        answer = ''
    if answer.strip().lower() not in ('y', 'yes'):
        raise LifecycleError(f'{action} cancelled; nothing changed')


@contextmanager
def locked(path: Path, *, blocking: bool):
    """flock on a sidecar file. Non-blocking callers fail at once if another operation holds it."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open('a') as handle:
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(handle, flags)
        except BlockingIOError as exc:
            raise LifecycleError(f'{path.name} is held by another operation; retry later') from exc
        yield


def _copy(source: Path, dest: Path, mode: int):
    tmp = dest.with_name(f'.{dest.name}.tmp')
    shutil.copyfile(source, tmp)
    os.chmod(tmp, mode)
    os.replace(tmp, dest)


def _remove(path: Path):
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


# ---- program sets ------------------------------------------------------------------
# A tree's program set is the lists in ITS OWN release_artifacts.py, never the running code's.
# An update installs exactly what the verified release declares; a rollback restores exactly
# the set its snapshot held (names a newer release added are removed, dropped ones restored).
PROTECTED_NAMES = frozenset({'config.json', 'state', 'updates', 'rollback', 'VERSION', 'MANIFEST.sha256',
                             'PROGRAM.json', 'SNAPSHOT.json', LIFECYCLE_LOCK, ONBOARDING_LOCK})


def current_program() -> dict:
    return {'executable': tuple(ra.EXECUTABLE_PROGRAM_FILES), 'data': tuple(ra.DATA_PROGRAM_FILES),
            'dirs': tuple(ra.DATA_PROGRAM_DIRS)}


def program_names(program: dict) -> list[str]:
    return [*program['executable'], *program['data'], *program['dirs']]


def _safe_program_name(value) -> str:
    """A plain single path component: no absolute, backslash, empty or traversal part, no '/', not a protected name.

    N6: a program name is one entry directly in the tree, never a nested path, so '/' is refused outright.
    """
    if not isinstance(value, str) or not value or '\\' in value or '/' in value or value.startswith('/'):
        raise LifecycleError(f'release declares an unsafe program name: {value!r}')
    if any(part in ('', '.', '..') for part in value.split('/')):
        raise LifecycleError(f'release declares an unsafe program name: {value!r}')
    if value.split('/')[0] in PROTECTED_NAMES:
        raise LifecycleError(f'release declares a protected name: {value}')
    return value


def _program_from_release_json(doc, origin: str) -> dict:
    """Validate a release's PROGRAM.json document (schema 1, three lists) into a program set."""
    if not isinstance(doc, dict) or doc.get('schema') != 1 or set(doc) != {'schema', 'executable', 'data', 'dirs'}:
        raise LifecycleError(f'{origin} PROGRAM.json is not schema 1 with exactly executable/data/dirs')
    for kind in ('executable', 'data', 'dirs'):
        if not isinstance(doc[kind], list):
            raise LifecycleError(f'{origin} PROGRAM.json {kind} is not a list')
    return {kind: tuple(_safe_program_name(n) for n in doc[kind]) for kind in ('executable', 'data', 'dirs')}


def _program_from_lists(lists: dict) -> dict:
    """Validate the three program lists parsed from a source file (never from an imported module)."""
    return {kind: tuple(_safe_program_name(n) for n in lists[key]) for key, kind in ra.PROGRAM_KINDS.items()}


def _check_program(program: dict, origin: str) -> dict:
    names = program_names(program)
    if len(set(names)) != len(names):
        raise LifecycleError(f'{origin} program lists repeat a name')
    if 'release_artifacts.py' not in program['executable']:
        raise LifecycleError(f'{origin} program lists must include release_artifacts.py')
    return program


def staged_program(tree: Path) -> dict:
    """N6: the program set of a STAGED (incoming) release: its PROGRAM.json is required and must be valid.

    Read with json from the verified tree. No code is imported or executed. Refuses if missing or invalid.
    """
    path = tree / 'PROGRAM.json'
    if path.is_symlink() or not path.is_file():
        raise LifecycleError('release has no PROGRAM.json; refusing it')
    try:
        doc = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise LifecycleError(f'release PROGRAM.json is unreadable: {type(exc).__name__}') from exc
    return _check_program(_program_from_release_json(doc, 'release'), 'release')


def installed_program(tree: Path) -> dict:
    """N6 hybrid rule for the INSTALLED tree (the code already running as this user).

    Use PROGRAM.json if present. Otherwise parse the installed release_artifacts.py with ast (literal lists
    only; nothing is imported or executed). If neither gives a valid set, refuse.
    """
    path = tree / 'PROGRAM.json'
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise LifecycleError('installed PROGRAM.json is not a regular file; refusing')
        try:
            doc = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise LifecycleError(f'installed PROGRAM.json is unreadable: {type(exc).__name__}') from exc
        return _check_program(_program_from_release_json(doc, 'installed'), 'installed')
    source = tree / 'release_artifacts.py'
    if source.is_symlink() or not source.is_file():
        raise LifecycleError('installed tree has no PROGRAM.json and no release_artifacts.py; refusing')
    try:
        lists = ra.program_lists_from_source(source.read_bytes(), source.name)
    except (OSError, ValueError) as exc:
        raise LifecycleError(f'cannot read program lists from the installed {source.name}: {exc}') from exc
    return _check_program(_program_from_lists(lists), 'installed')


def program_set(tree: Path, *, present: bool = True) -> dict:
    """The program set a tree declares, plus the presence checks for an incoming release.

    Staged trees (present=True) must carry a valid PROGRAM.json; the installed rule above applies otherwise.
    Every name must be unique, plain, and present in the tree (files as regular files, directories as
    real directories). Anything else refuses before any change is made.
    """
    program = staged_program(tree) if present else installed_program(tree)
    if present:
        for name in program['executable'] + program['data']:
            if (tree / name).is_symlink() or not (tree / name).is_file():
                raise LifecycleError(f'release is missing program file: {name}')
        for name in program['dirs']:
            if (tree / name).is_symlink() or not (tree / name).is_dir():
                raise LifecycleError(f'release is missing program directory: {name}')
    return program


def install_program_files(source: Path, target: Path, program: dict | None = None, absent=()):
    """Copy the program set (default: this code's own) from source into target with install.sh's modes.

    Names in `absent` (recorded as missing by a snapshot) are removed from target instead of copied.
    Generated VERSION/MANIFEST.sha256 follow the source: copied when present, removed when
    absent, so a source checkout never keeps a stale release version.
    """
    program = program or current_program()
    absent = set(absent)
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in program['executable']:
        if name in absent:
            _remove(target / name)
        else:
            _copy(source / name, target / name, 0o700)
    for name in program['data']:
        if name in absent:
            _remove(target / name)
        else:
            _copy(source / name, target / name, 0o600)
    # Generated files (VERSION, MANIFEST.sha256, PROGRAM.json) follow the source byte-for-byte.
    for name in ra.GENERATED_FILES:
        if (source / name).is_file():
            _copy(source / name, target / name, 0o600)
        else:
            (target / name).unlink(missing_ok=True)
    for name in program['dirs']:
        if name in absent:
            _remove(target / name)
            continue
        staged = target / f'.{name}.new'
        if staged.exists() or staged.is_symlink():
            _remove(staged)
        shutil.copytree(source / name, staged, symlinks=False)
        if (target / name).exists() or (target / name).is_symlink():
            _remove(target / name)
        os.replace(staged, target / name)


# ---- graceful drain and service control ---------------------------------------
# Same file names as autonomy-controller.py (DRAIN_REQUEST/DRAIN_READY); tests pin them.
DRAIN_REQUEST = 'drain.request'
DRAIN_READY = 'drain.ready'
DRAIN_POLL_SECONDS = 1.0
DEFAULT_DRAIN_TIMEOUT = 3600
UNIT = 'autocontroller.service'


def _write_json(path: Path, value):
    tmp = path.with_name(f'.{path.name}.tmp')
    tmp.write_text(json.dumps(value, sort_keys=True) + '\n', encoding='utf-8')
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by someone else: still alive
    return True


def _lifecycle_lock_held(lock: Path) -> bool:
    """Read-only probe: True only while an operation holds lifecycle.lock. Never creates the file.

    Mirrors autonomy-controller.lifecycle_lock_held: a missing file is not held.
    """
    try:
        fd = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def drain_ready(state_dir: Path, nonce: str, *, alive=pid_alive) -> bool:
    """True only when drain.ready echoes OUR nonce and names a live positive integer pid.

    A ready file from an earlier drain carries another nonce; a dead pid is a leftover
    from a crashed controller. Neither proves the current controller has parked.
    """
    doc = _read_json(state_dir / DRAIN_READY)
    if not isinstance(doc, dict) or doc.get('nonce') != nonce:
        return False
    pid = doc.get('pid')
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    return alive(pid)


def wait_drained(state_dir: Path, nonce: str, *, timeout: float, alive=pid_alive,
                 sleep=time.sleep, clock=time.monotonic, poll: float = DRAIN_POLL_SECONDS):
    deadline = clock() + timeout
    while not drain_ready(state_dir, nonce, alive=alive):
        if clock() >= deadline:
            raise LifecycleError(f'controller did not drain within {timeout:g}s; service left running, nothing replaced')
        sleep(poll)


@contextmanager
def drain(state_dir: Path, *, timeout: float = DEFAULT_DRAIN_TIMEOUT, **kwargs):
    """Request a graceful drain and yield once every worker has parked.

    The request is withdrawn on every exit path (success, timeout, error in the body),
    so a failed update can never leave workers parked by a stale request.
    """
    nonce = secrets.token_hex(16)
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    request = state_dir / DRAIN_REQUEST
    try:
        _write_json(request, {'nonce': nonce, 'requested_at': time.time()})
        wait_drained(state_dir, nonce, timeout=timeout, **kwargs)
        yield nonce
    finally:
        request.unlink(missing_ok=True)
        (state_dir / DRAIN_READY).unlink(missing_ok=True)


def systemctl(*verb: str, timeout: int = 120) -> int:
    """Run `systemctl --user <verb> autocontroller.service`; returns the exit code."""
    try:
        return subprocess.run(['systemctl', '--user', *verb, UNIT], capture_output=True,
                              timeout=timeout, check=False).returncode
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LifecycleError(f'systemctl {" ".join(verb)} failed: {exc}') from exc


# N1: the unit is read with `show`, not `is-active`. `activating` is also systemd's auto-restart wait
# after a crash (Restart=on-failure), so a state word alone cannot say the controller is up. Each
# decision uses the full property set, parsed once per call. Unreadable output raises; it never
# reads as healthy or as stopped.
SHOW_PROPERTIES = ('ActiveState', 'SubState', 'MainPID', 'NRestarts')
UNIT_ACTIVE_STATES = frozenset({'active', 'activating', 'reloading', 'deactivating', 'inactive', 'failed',
                                'maintenance', 'refreshing'})


def unit_state() -> dict:
    """Parse `systemctl --user show` for the unit. Raises LifecycleError on any unreadable or unknown value."""
    argv = ['systemctl', '--user', 'show', UNIT, *(f'-p{p}' for p in SHOW_PROPERTIES)]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LifecycleError(f'systemctl show failed: {exc}') from exc
    if proc.returncode != 0:
        raise LifecycleError(f'systemctl show exited {proc.returncode}; the unit state is unknown')
    values: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        key, sep, value = line.partition('=')
        if not sep or key not in SHOW_PROPERTIES or key in values:
            raise LifecycleError('systemctl show returned an unexpected line; the unit state is unknown')
        values[key] = value
    if set(values) != set(SHOW_PROPERTIES):
        raise LifecycleError('systemctl show did not return every expected property; the unit state is unknown')
    if values['ActiveState'] not in UNIT_ACTIVE_STATES or not values['SubState']:
        raise LifecycleError(f'unknown unit ActiveState {values["ActiveState"]!r}; the unit state is unknown')
    try:
        pid, restarts = int(values['MainPID']), int(values['NRestarts'])
    except ValueError as exc:
        raise LifecycleError('systemctl show returned a non-numeric MainPID or NRestarts') from exc
    if pid < 0 or restarts < 0:
        raise LifecycleError('systemctl show returned a negative MainPID or NRestarts')
    return {'ActiveState': values['ActiveState'], 'SubState': values['SubState'], 'MainPID': pid,
            'NRestarts': restarts}


def needs_stop(state: dict) -> bool:
    """Anything but a clean inactive/failed unit must be stopped, including activating (auto-restart wait)."""
    return state['ActiveState'] not in ('inactive', 'failed')


def can_drain(state: dict) -> bool:
    """Drain only a unit that is really running a controller process. Otherwise stop it without waiting."""
    return state['ActiveState'] == 'active' and state['SubState'] == 'running' and state['MainPID'] > 0


def service_active() -> bool:
    """Whether the unit must be stopped. Unreadable state raises, so callers never guess."""
    return needs_stop(unit_state())


def stop_service():
    """Idempotent: `systemctl stop` on an inactive unit succeeds, so it is always run before a swap."""
    if systemctl('stop') != 0:
        raise LifecycleError('systemctl stop failed; the service state is unknown, nothing replaced')


def start_service() -> bool:
    return systemctl('start') == 0


# ---- release download, verification and staging ---------------------------------
# Trust chain: each asset's GitHub API digest -> SHA256SUMS -> archive -> MANIFEST.
# The tag commit resolved from GitHub must equal the archive's VERSION commit.
GH_TIMEOUT = 240
_CONTROLLER = None


def controller():
    """The installed controller module, loaded once: tag_target and gh_json live there."""
    global _CONTROLLER
    if _CONTROLLER is None:
        spec = importlib.util.spec_from_file_location('controller', ROOT / 'autonomy-controller.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _CONTROLLER = module
    return _CONTROLLER


def _artifact(call, *args):
    try:
        return call(*args)
    except ra.ArtifactError as exc:
        raise LifecycleError(str(exc)) from exc


class LiveGitHub:
    """Real gh calls. Tests pass a stand-in with the same three methods, so no network is used."""

    def release(self, repo: str, tag: str | None = None) -> dict:
        """Published stable release: latest unless a tag is named. Drafts and prereleases refuse."""
        path = f'repos/{repo}/releases/tags/{tag}' if tag else f'repos/{repo}/releases/latest'
        data = controller().gh_json('api', '--method', 'GET', path)
        if not isinstance(data, dict) or data.get('draft') or data.get('prerelease') or not data.get('published_at'):
            raise LifecycleError('update source is not a published stable release')
        if not isinstance(data.get('tag_name'), str) or not releases.version(data['tag_name']):
            raise LifecycleError('update source has no stable vX.Y.Z tag')
        return data

    def download(self, repo: str, tag: str, name: str, dest: Path):
        controller().command(['gh', 'release', 'download', tag, '--repo', repo, '--dir', str(dest),
                              '--pattern', name], timeout=GH_TIMEOUT)

    def tag_target(self, repo: str, tag: str) -> str | None:
        return controller().tag_target(repo, tag)


def _asset_sha256(asset: dict) -> str:
    digest = str(asset.get('digest') or '')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
        raise LifecycleError(f'release asset {asset.get("name")!r} has no sha256 digest')
    return digest[len('sha256:'):]


def download_release(gh, repo: str, release: dict, staging: Path) -> Path:
    """Fetch the archive and SHA256SUMS into staging/dist; each must match its API digest."""
    tag = release['tag_name']
    archive = ra.ARCHIVE_NAME.format(tag=tag)
    assets = {a.get('name'): a for a in release.get('assets') or [] if isinstance(a, dict)}
    dist = staging / 'dist'
    dist.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in (archive, 'SHA256SUMS'):
        if name not in assets:
            raise LifecycleError(f'release {tag} has no {name} asset')
        expected = _asset_sha256(assets[name])
        gh.download(repo, tag, name, dist)
        path = dist / name
        if path.is_symlink() or not path.is_file():
            raise LifecycleError(f'{name} was not downloaded as a regular file')
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise LifecycleError(f'{name} does not match its release digest')
    return dist


def verify_extracted(src: Path, tag: str, commit: str):
    """Re-verify the extracted tree on disk: no links, manifest over every file, VERSION, required files."""
    files: dict[str, bytes] = {}
    for path in sorted(src.rglob('*')):
        if path.is_symlink():
            raise LifecycleError(f'extracted tree contains a link: {path.name}')
        if path.is_file():
            files[path.relative_to(src).as_posix()] = path.read_bytes()
    # N3: presence of the program files is checked by program_set against the release's OWN list
    # (update reads it after this). This code's list is not the release's list, so no check here.
    _artifact(ra.verify_manifest, files)
    if _read_json(src / 'VERSION') != {'commit': commit, 'tag': tag}:
        raise LifecycleError('extracted VERSION does not match the tag commit')


def stage_release(gh, repo: str, staging: Path, tag: str | None = None) -> dict:
    """Download, verify and extract the update source into staging/src. Returns tag, commit, src."""
    release = gh.release(repo, tag)
    name = release['tag_name']
    dist = download_release(gh, repo, release, staging)
    commit = gh.tag_target(repo, name)
    if not commit:
        raise LifecycleError(f'tag {name} does not exist in {repo}')
    _artifact(ra.verify, dist, name, commit)
    src = staging / 'src'
    _artifact(ra.safe_extract, dist / ra.ARCHIVE_NAME.format(tag=name), src)
    verify_extracted(src, name, commit)
    return {'tag': name, 'commit': commit, 'src': src}


def fresh_staging(target: Path) -> Path:
    """A new 0700 staging directory under the install's updates/ area, emptied first."""
    staging = target / 'updates' / 'staging'
    _remove(staging)
    staging.mkdir(mode=0o700, parents=True)
    return staging


# ---- snapshot (rollback store) and history ----------------------------------------

SNAPSHOT_RECORD = 'SNAPSHOT.json'


def snapshot(target: Path, dest: Path, program: dict | None = None):
    """Copy the installed program set into dest, replacing any older snapshot.

    `program` is the set the install was built from (default: this code's own). Files the set
    names but the install lacks are recorded as absent, so a rollback can restore that absence
    instead of refusing. SNAPSHOT.json records the set and the absent names; it is the only
    authority a rollback uses to decide what to restore and what to remove. The release's own
    PROGRAM.json (generated, MANIFEST-covered) is copied unchanged, like VERSION and MANIFEST.sha256.
    """
    program = program or current_program()
    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + '.new')
    _remove(tmp)
    tmp.mkdir(mode=0o700)
    absent = []
    for name in program['executable'] + program['data'] + program['dirs']:
        src = target / name
        if not (src.exists() or src.is_symlink()):
            absent.append(name)
            continue
        if name in program['dirs']:
            shutil.copytree(src, tmp / name, symlinks=False)
        else:
            _copy(src, tmp / name, 0o700 if name in program['executable'] else 0o600)
    # The release's own generated files (VERSION, MANIFEST.sha256, PROGRAM.json) are kept unchanged.
    for name in ra.GENERATED_FILES:
        if (target / name).is_file():
            _copy(target / name, tmp / name, 0o600)
    write_program_record(tmp, program, absent)
    _remove(dest)
    os.replace(tmp, dest)


def write_program_record(snap: Path, program: dict, absent) -> None:
    """The one writer of SNAPSHOT.json: the set a snapshot holds and the names it recorded as absent."""
    _write_json(snap / SNAPSHOT_RECORD, {'absent': sorted(absent), 'program': {k: list(v) for k, v in program.items()}})


def snapshot_program(snap: Path) -> tuple[dict, list[str]]:
    """Read the program set and absent names recorded in a snapshot's SNAPSHOT.json.

    The recorded set is the authority; the snapshot's own release_artifacts.py is not re-imported.
    Unreadable, malformed or unsafe records refuse before anything is restored.
    """
    doc = _read_json(snap / SNAPSHOT_RECORD)
    if not isinstance(doc, dict) or not isinstance(doc.get('program'), dict) or not isinstance(doc.get('absent'), list):
        raise LifecycleError(f'{snap.name} has no readable {SNAPSHOT_RECORD}; refusing to restore it')
    raw = doc['program']
    if set(raw) != {'executable', 'data', 'dirs'} or not all(isinstance(raw[k], list) for k in raw):
        raise LifecycleError(f'{snap.name} {SNAPSHOT_RECORD} has a malformed program set')
    program = {k: tuple(_safe_program_name(n) for n in raw[k]) for k in ('executable', 'data', 'dirs')}
    names = program_names(program)
    if len(set(names)) != len(names):
        raise LifecycleError(f'{snap.name} {SNAPSHOT_RECORD} repeats a program name')
    absent = [_safe_program_name(n) for n in doc['absent']]
    if not set(absent) <= set(names):
        raise LifecycleError(f'{snap.name} records an absent name outside its program set')
    return program, absent


def _installed_program(target: Path) -> dict:
    """The installed tree's own set, from its installed release_artifacts.py (presence not required)."""
    return program_set(target, present=False)


def _prune(target: Path, old: dict, keep: dict):
    """Remove names the old set installed but the kept set no longer declares (never a kept name)."""
    keep_names = set(program_names(keep))
    for name in program_names(old):
        if name not in keep_names:
            _remove(target / name)


# Two-phase swap. stage_swap keeps the pre-swap files in rollback/incoming; only
# commit_swap (after health passes) moves them to rollback/previous. abort_swap restores
# incoming, so a failed update leaves the previous release both installed and kept.

def ensure_no_incoming(target: Path):
    """Refuse before anything is stopped: a leftover incoming snapshot is not ours to restore."""
    incoming = target / 'rollback' / 'incoming'
    if incoming.exists() or incoming.is_symlink():
        raise LifecycleError('rollback/incoming exists: an earlier update did not finish; inspect it before retrying')


def ensure_no_swap(target: Path):
    """S3: a leftover rollback/swap is the only copy of the release a rollback replaced. Refuse to overwrite it."""
    swap = target / 'rollback' / 'swap'
    if swap.exists() or swap.is_symlink():
        raise LifecycleError('rollback/swap exists: an earlier rollback did not finish; run `rollback --recover` first')


def stage_swap(target: Path, src: Path, new: dict):
    """Snapshot the current set, then install the verified release's set `new` (read from src)."""
    ensure_no_incoming(target)
    old = _installed_program(target)
    snapshot(target, target / 'rollback' / 'incoming', old)
    install_program_files(src, target, new)
    _prune(target, old, new)


def commit_swap(target: Path):
    rollback = target / 'rollback'
    _remove(rollback / 'previous')
    os.replace(rollback / 'incoming', rollback / 'previous')


def abort_swap(target: Path):
    incoming = target / 'rollback' / 'incoming'
    if not incoming.exists():
        return
    old, absent = snapshot_program(incoming)
    # The set the failed attempt installed is read from the tree as it now stands. If that is
    # unreadable, refuse: restoring without knowing what to remove could leave new-only files.
    attempted = _installed_program(target)
    install_program_files(incoming, target, old, absent)
    _prune(target, attempted, old)
    _remove(incoming)


def installed_version(target: Path) -> str | None:
    return installed_version_at(target)


def installed_version_at(tree: Path) -> str | None:
    """The tag recorded in a tree's VERSION file (installed, snapshot or previous store)."""
    doc = _read_json(tree / 'VERSION')
    return doc.get('tag') if isinstance(doc, dict) else None


def refuse_downgrade(target: Path, tag: str):
    """Refuse a release older than the installed one; rollback is the supported way back.

    No VERSION file means a source checkout, which may take any release. A VERSION that
    exists but cannot be parsed is unknown, not absent, so it refuses too (fail closed).
    """
    if not (target / 'VERSION').exists() and not (target / 'VERSION').is_symlink():
        return
    installed = installed_version(target)
    if installed is None:
        raise LifecycleError('installed VERSION is unreadable; refusing to update until it is inspected')
    current = releases.version(installed)
    wanted = releases.version(tag)
    if current is None:
        raise LifecycleError(f'installed VERSION tag {installed!r} is not vX.Y.Z; refusing to update until it is inspected')
    if wanted is None:
        raise LifecycleError(f'{tag} is not a stable vX.Y.Z release')
    if wanted < current:
        raise LifecycleError(f'downgrade refused: {tag} is older than installed {installed}; '
                             'use `autocontroller rollback` to return to the previous release')


def healthy(*, check, seconds: int, sleep=time.sleep, clock=time.monotonic) -> bool:
    """Run `--check`, then require the SAME controller process to stay up for `seconds`.

    N1: every poll must read active/running with the same nonzero MainPID and the same NRestarts as
    the first poll after start. An auto-restart (a new PID, a higher counter, or a state flip to
    activating) fails the window. Any unreadable state raises, which the caller treats as failure.
    Polling is 1s so a crash is seen quickly.
    """
    if not check():
        return False
    first = unit_state()
    if not can_drain(first):
        return False
    deadline = clock() + seconds
    while clock() < deadline:
        sleep(1.0)
        now = unit_state()
        if not can_drain(now) or now['MainPID'] != first['MainPID'] or now['NRestarts'] != first['NRestarts']:
            return False
    return True


def enabled_project_count(target: Path) -> int | None:
    """Projects the controller would run (enabled defaults to true, as in the controller). None: unreadable."""
    doc = _read_json(target / 'config.json')
    if not isinstance(doc, dict) or not isinstance(doc.get('projects'), list):
        return None
    return sum(1 for p in doc['projects'] if isinstance(p, dict) and p.get('enabled', True))


def _py_compile_installed(target: Path) -> bool:
    """Compile every installed executable .py file (the installed set). Any failure, or an unreadable set, is False."""
    try:
        program = _installed_program(target)
    except LifecycleError:
        return False
    for name in program['executable']:
        if not name.endswith('.py'):
            continue
        path = target / name
        if path.is_symlink() or not path.is_file():
            return False
        try:
            proc = subprocess.run([sys.executable, '-m', 'py_compile', str(path)],
                                  capture_output=True, timeout=120, check=False, cwd=str(target))
        except (OSError, subprocess.TimeoutExpired):
            return False
        if proc.returncode != 0:
            return False
    return True


def run_check(target: Path) -> bool:
    """`autonomy-controller.py --check` of the installed copy, against that install's own config.

    S7: with zero enabled projects the controller's --check refuses by design (there is nothing to
    check). The minimal check then is `py_compile` of each installed executable .py file, run with
    the installed set. An unreadable config still runs the full check, which fails closed.
    """
    if enabled_project_count(target) == 0:
        return _py_compile_installed(target)
    try:
        proc = subprocess.run([sys.executable, str(target / 'autonomy-controller.py'), '--check',
                               '--config', str(target / 'config.json')],
                              capture_output=True, timeout=300, check=False, cwd=str(target))
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def start_and_verify(target: Path, *, seconds: int, check=None, sleep=time.sleep, clock=time.monotonic) -> bool:
    if not start_service():
        return False
    return healthy(check=check or (lambda: run_check(target)), seconds=seconds, sleep=sleep, clock=clock)


def verified(target: Path, *, active: bool, health_seconds: int, check=None, sleep=time.sleep,
             clock=time.monotonic) -> bool:
    """Active services must start and stay up; inactive ones are only checked, never started."""
    if active:
        return start_and_verify(target, seconds=health_seconds, check=check, sleep=sleep, clock=clock)
    return (check or (lambda: run_check(target)))()


# ---- orchestration ------------------------------------------------------------------
# Drain covers only the stop: the request is withdrawn before the new service starts,
# otherwise the new workers would see the request and park at boot.

@contextmanager
def operation(target: Path):
    """lifecycle.lock waits for another update/rollback; onboarding.lock fails fast if onboarding runs."""
    with locked(target / LIFECYCLE_LOCK, blocking=True), locked(target / ONBOARDING_LOCK, blocking=False):
        yield


def _stop_after_drain(target: Path, timeout: float, sleep=time.sleep, clock=time.monotonic):
    """Drain running workers, then stop the unit. Returns whether it was running (so it is restarted).

    S4: `stop` is always issued, even when the unit reads as inactive. A unit that is starting or
    reloading can read as not running yet still hold files open, so the stop is never skipped.
    """
    state = unit_state()
    active = needs_stop(state)
    if can_drain(state):
        # N1: only a really running controller is drained. activating (auto-restart wait) or a unit
        # with no MainPID has no workers to park, so waiting for them would only stall for the timeout.
        with drain(target / 'state', timeout=timeout, sleep=sleep, clock=clock):
            stop_service()
    else:
        stop_service()
    return active


def _stop_unhealthy(target: Path):
    """A failed start may leave the NEW code running. Stop it before old files are restored.

    N1: stop is always issued (idempotent), never gated on a state read, so an auto-restarting or
    activating unit is stopped too. No drain: an unhealthy tree has already failed its checks.
    """
    stop_service()


def update(target: Path, repo: str, *, gh, tag: str | None = None, yes: bool = False, environ,
           ask=input, drain_timeout: float = DEFAULT_DRAIN_TIMEOUT, health_seconds: int = 30,
           check=None, sleep=time.sleep, clock=time.monotonic) -> str:
    refuse_agent(environ, 'update')
    confirm('Update AutoController from the latest release' if tag is None else f'Update AutoController to {tag}',
            yes=yes, ask=ask)
    with operation(target):
        check_history(target)
        ensure_no_incoming(target)
        ensure_no_swap(target)
        staging = fresh_staging(target)
        stage = stage_release(gh, repo, staging, tag)
        before = installed_version(target)
        try:
            refuse_downgrade(target, stage['tag'])
        except LifecycleError:
            _remove(staging)
            raise
        if before == stage['tag']:
            _remove(staging)
            return f'already at {before}; nothing to do'
        # The new release's own list decides what is installed; read and validated before any stop.
        try:
            new_program = program_set(stage['src'])
        except LifecycleError:
            _remove(staging)
            raise
        active = _stop_after_drain(target, drain_timeout, sleep, clock)
        try:
            stage_swap(target, stage['src'], new_program)
        except BaseException:
            # A half-written install must be restored before anything is started again.
            abort_swap(target)
            if active: start_service()
            raise
        ok = verified(target, active=active, health_seconds=health_seconds, check=check, sleep=sleep, clock=clock)
        if ok:
            commit_swap(target)
            append_history(target, {'action': 'update', 'from': before, 'to': stage['tag'], 'result': 'ok'})
            _remove(staging)
            return f'updated {before or "unknown"} -> {stage["tag"]}'
        _stop_unhealthy(target)
        abort_swap(target)
        restored = verified(target, active=active, health_seconds=health_seconds, check=check, sleep=sleep,
                            clock=clock)
        append_history(target, {'action': 'update', 'from': before, 'to': stage['tag'],
                                'result': 'rolled_back' if restored else 'rollback_unhealthy'})
        _remove(staging)
        if active:
            state = 'restored and running' if restored else 'restored but NOT healthy; inspect the service'
        else:
            state = ('restored (service was stopped and is not started; its check passes)' if restored
                     else 'restored but its check FAILS; inspect the install')
        raise LifecycleError(f'update to {stage["tag"]} failed its health check; previous {before or "version"} {state}')


def rollback(target: Path, *, yes: bool = False, environ, ask=input, drain_timeout: float = DEFAULT_DRAIN_TIMEOUT,
             health_seconds: int = 30, check=None, sleep=time.sleep, clock=time.monotonic) -> str:
    refuse_agent(environ, 'rollback')
    confirm('Roll back AutoController to the previous installed release', yes=yes, ask=ask)
    previous = target / 'rollback' / 'previous'
    if not previous.is_dir():
        raise LifecycleError('no previous release is kept; nothing to roll back to')
    with operation(target):
        check_history(target)
        # S3: a leftover swap is refused before anything is stopped; it is resolved by --recover.
        ensure_no_swap(target)
        before = installed_version(target)
        target_version = _read_json(previous / 'VERSION')
        target_tag = target_version.get('tag') if isinstance(target_version, dict) else None
        # Every read that can refuse happens before the service is stopped.
        prev_program, prev_absent = snapshot_program(previous)
        current = _installed_program(target)
        active = _stop_after_drain(target, drain_timeout, sleep, clock)
        swap = target / 'rollback' / 'swap'
        _remove(swap)
        snapshot(target, swap, current)
        try:
            install_program_files(previous, target, prev_program, prev_absent)
            _prune(target, current, prev_program)
        except BaseException:
            # Partly written: put the current release back before anything can start.
            install_program_files(swap, target, *snapshot_program(swap))
            _prune(target, prev_program, current)
            _remove(swap)
            raise
        ok = verified(target, active=active, health_seconds=health_seconds, check=check, sleep=sleep, clock=clock)
        if not ok:
            _stop_unhealthy(target)
            install_program_files(swap, target, *snapshot_program(swap))
            _prune(target, prev_program, current)
            _remove(swap)
            restored = verified(target, active=active, health_seconds=health_seconds, check=check, sleep=sleep,
                                clock=clock)
            append_history(target, {'action': 'rollback', 'from': before, 'to': target_tag,
                                    'result': 'failed_restored' if restored else 'failed_unhealthy'})
            raise LifecycleError(f'rollback to {target_tag} failed its health check; the current release was restored')
        # The previous store now holds the release we just left, so a rollback can itself be undone.
        # N4: the old store is renamed aside, not deleted first, so a crash at any point leaves a
        # recoverable state: previous, previous.old or swap always holds the release that was replaced.
        aside = previous.with_name('previous.old')
        _remove(aside)
        os.replace(previous, aside)
        os.replace(swap, previous)
        _remove(aside)
        append_history(target, {'action': 'rollback', 'from': before, 'to': target_tag, 'result': 'ok'})
        return f'rolled back {before or "unknown"} -> {target_tag or "unknown"}'


def _present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _recovery_state(rollback: Path) -> str:
    """Which leftover a rollback crash left (N4). Each shape maps to exactly one action.

    restore: swap with previous still in place and no previous.old. The crash came before the store
             commit, so the install may already hold the previous release. The replaced tree in swap
             is restored and swap removed; previous (the kept store) is not touched.
    finish:  swap with previous missing, or with previous.old beside it. The rollback passed its health
             check and was inside the commit, so the install already is the previous release. Only the
             store moves: swap becomes previous and previous.old is removed.
    tidy:    previous.old with no swap. The commit completed; only the aside copy is left.

    swap, previous and previous.old together cannot come from this code and are refused.
    """
    swap = _present(rollback / 'swap')
    prev = _present(rollback / 'previous')
    aside = _present(rollback / 'previous.old')
    if swap and prev and aside:
        raise LifecycleError('rollback store is inconsistent (swap, previous and previous.old all present); '
                             'inspect rollback/ before recovering')
    if swap and (aside or not prev):
        return 'finish'
    if swap:
        return 'restore'
    if aside:
        return 'tidy'
    raise LifecycleError('no rollback/swap to recover')


def recover(target: Path, *, yes: bool = False, environ, ask=input, drain_timeout: float = DEFAULT_DRAIN_TIMEOUT,
            health_seconds: int = 30, check=None, sleep=time.sleep, clock=time.monotonic) -> str:
    """S3/N4: resolve a leftover rollback by restoring, finishing or tidying it, never by guessing.

    `swap` holds the release a rollback replaced, copied before any program file moved. The leftover
    shape decides the action (see _recovery_state). A leftover `incoming` is not handled here: it is an
    interrupted update, and it blocks further updates until a human inspects it. abort_swap restores
    `incoming` only inside the update process that created it.
    """
    refuse_agent(environ, 'rollback --recover')
    confirm('Recover the interrupted rollback', yes=yes, ask=ask)
    rollback = target / 'rollback'
    swap = rollback / 'swap'
    store = rollback / 'previous'
    aside = rollback / 'previous.old'
    with operation(target):
        state = _recovery_state(rollback)
        if state == 'tidy':
            # Changes no program file, so the service is not stopped.
            version = installed_version(target)
            _remove(aside)
            append_history(target, {'action': 'rollback', 'from': version, 'to': version, 'result': 'recovered'})
            return 'removed the leftover previous.old; the rollback had already completed'
        # Every read that can refuse happens before the service is stopped.
        swapped_from = installed_version_at(swap)
        if state == 'restore':
            program, absent = snapshot_program(swap)
            attempted = _installed_program(target)
        active = _stop_after_drain(target, drain_timeout, sleep, clock)
        if state == 'restore':
            # A failure here keeps swap, so a retry restores the same tree again.
            install_program_files(swap, target, program, absent)
            _prune(target, attempted, program)
        problems = manifest_problems(target)
        if problems:
            # Left stopped on purpose: a tree that fails MANIFEST must not run. swap and aside are kept.
            raise LifecycleError('tree fails MANIFEST verification (service left stopped); rollback store kept: '
                                 + '; '.join(problems[:3]))
        # Fail closed: the tree must pass the same health rule a rollback applies. Failure stops it again,
        # keeps the leftovers for a retry, and raises so the exit code is nonzero.
        ok = verified(target, active=active, health_seconds=health_seconds, check=check, sleep=sleep, clock=clock)
        if not ok:
            _stop_unhealthy(target)
            what = 'recovered files were restored' if state == 'restore' else 'rollback store was not finished'
            raise LifecycleError(f'{what} (rollback/swap kept) but the service is NOT running: '
                                 'the tree failed its health check; inspect before retrying')
        if state == 'restore':
            _remove(swap)
        else:
            _remove(store)
            os.replace(swap, store)
            _remove(aside)
        # `from` is the version the interrupted operation started from (read from swap, before it moved);
        # `to` is the version now installed.
        append_history(target, {'action': 'rollback', 'from': swapped_from, 'to': installed_version(target),
                                'result': 'recovered'})
    return 'recovered the interrupted rollback; service left ' + ('running' if active else 'stopped')


def check_history(target: Path):
    """Refuse before any change when history is unreadable: a record must never be lost after a swap."""
    path = target / 'updates' / 'history.json'
    if path.exists() and not isinstance(_read_json(path), list):
        raise LifecycleError('updates/history.json is unreadable; refusing to overwrite it')


def append_history(target: Path, entry: dict):
    """Append one record to updates/history.json."""
    path = target / 'updates' / 'history.json'
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    check_history(target)
    history = _read_json(path) if path.exists() else []
    history.append({**entry, 'at': int(time.time())})
    _write_json(path, history)


# ---- uninstall ----------------------------------------------------------------------
# Removes only what install.sh and the lifecycle commands create: program files, the unit
# and CLI links when they point into this install, updates/, rollback/ and lifecycle.lock.
# config.json, state/ and checkouts are kept unless --purge (which never skips its typed word).

PURGE_WORD = 'purge'


def _owned_link(link: Path, target: Path) -> bool:
    """True only for a symlink whose destination lies inside the install root."""
    if not link.is_symlink():
        return False
    try:
        dest = Path(os.readlink(link))
    except OSError:
        return False
    if not dest.is_absolute():
        dest = link.parent / dest
    return dest.resolve().is_relative_to(target.resolve())


def _daemon_reload():
    try:
        code = subprocess.run(['systemctl', '--user', 'daemon-reload'], capture_output=True,
                              timeout=120, check=False).returncode
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LifecycleError(f'systemctl daemon-reload failed: {exc}') from exc
    if code != 0:
        raise LifecycleError('systemctl daemon-reload failed; run it manually')


def uninstall(target: Path, *, unit_dir: Path, bin_dir: Path, environ, yes: bool = False, purge: bool = False,
              ask=input, drain_timeout: float = DEFAULT_DRAIN_TIMEOUT, sleep=time.sleep,
              clock=time.monotonic) -> str:
    refuse_agent(environ, 'uninstall')
    if purge:
        # --yes never bypasses this: the operator must type the word, or nothing changes.
        try:
            typed = ask(f'Type {PURGE_WORD} to also delete config.json and state/: ')
        except EOFError:
            typed = ''
        if typed.strip() != PURGE_WORD:
            raise LifecycleError('purge not confirmed; nothing changed')
    confirm('Uninstall AutoController program files', yes=yes, ask=ask)
    with operation(target):
        _stop_after_drain(target, drain_timeout, sleep, clock)
        if systemctl('disable') != 0:
            raise LifecycleError('systemctl disable failed; program files left in place')
        for link in (unit_dir / UNIT, bin_dir / 'autocontroller'):
            if _owned_link(link, target):
                link.unlink()
        for name in ra.EXECUTABLE_PROGRAM_FILES + ra.DATA_PROGRAM_FILES + ra.GENERATED_FILES:
            (target / name).unlink(missing_ok=True)
        for name in ra.DATA_PROGRAM_DIRS + ('updates', 'rollback'):
            _remove(target / name)
        if purge:
            (target / 'config.json').unlink(missing_ok=True)
            _remove(target / 'state')
        _daemon_reload()
    # Removed last: lifecycle.lock is only meaningful while the operation above holds it.
    (target / LIFECYCLE_LOCK).unlink(missing_ok=True)
    kept = ['checkouts under ~/repositories'] if purge else ['config.json', 'state/', 'checkouts under ~/repositories']
    return ('uninstalled program files; kept: ' + ', '.join(kept) +
            '. To reinstall: bash install.sh from a checkout, then autocontroller add')


# ---- install integrity, status and doctor (read-only) ----------------------------------
# Nothing here creates, locks or writes a file. updates/, rollback/ and lifecycle.lock are
# managed install-root entries: status reports them, doctor flags only interrupted leftovers.

def _digest(path: Path) -> str | None:
    if path.is_symlink() or not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manifest_problems(target: Path) -> list[str]:
    """Release installs: every file in MANIFEST.sha256 must still match. Extra files are ignored.

    A source checkout has no MANIFEST.sha256 and yields no problems here.
    """
    manifest = target / 'MANIFEST.sha256'
    if not manifest.exists() and not manifest.is_symlink():
        return []
    if manifest.is_symlink() or not manifest.is_file():
        return ['MANIFEST.sha256 is not a regular file']
    try:
        text = manifest.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError):
        return ['MANIFEST.sha256 is unreadable']
    listed: dict[str, str] = {}
    for line in text.splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  (.+)', line)
        if not match:
            return ['MANIFEST.sha256 has a malformed line']
        listed[match.group(2)] = match.group(1)
    problems = []
    installed = _installed_names(target, listed)
    for name, digest in sorted(listed.items()):
        if Path(name).is_absolute() or '..' in Path(name).parts:
            problems.append(f'manifest names an unsafe path: {name}')
        elif name in installed and _digest(target / name) != digest:
            problems.append(f'modified or missing: {name}')
    for name in ra.EXECUTABLE_PROGRAM_FILES + ra.DATA_PROGRAM_FILES:
        if name not in listed:
            problems.append(f'program file not covered by manifest: {name}')
    for name in sorted(_dir_files(target) - set(listed)):
        problems.append(f'unexpected file in installed program directory: {name}')
    return problems


def _dir_files(target: Path) -> set[str]:
    """Every file or link under the installed program directories (skills/, standards/, tests/)."""
    found: set[str] = set()
    for name in ra.DATA_PROGRAM_DIRS:
        base = target / name
        if base.is_symlink():
            found.add(name)  # a link where a program directory belongs is never expected
            continue
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            links = [d for d in dirnames if (Path(dirpath) / d).is_symlink()]
            for leaf in filenames + links:
                found.add((Path(dirpath) / leaf).relative_to(target).as_posix())
    return found


def _installed_names(target: Path, listed: dict[str, str]) -> set[str]:
    """The installed set: program files, manifest entries inside program directories, and files found there."""
    names = set(ra.EXECUTABLE_PROGRAM_FILES + ra.DATA_PROGRAM_FILES)
    names |= {n for n in listed if n.split('/', 1)[0] in ra.DATA_PROGRAM_DIRS}
    names |= _dir_files(target)
    # Generated release files (VERSION, PROGRAM.json) are installed too: a listed one must still match.
    names |= {n for n in listed if n in ra.GENERATED_FILES}
    return names


def diagnose(target: Path, *, unit_dir: Path, bin_dir: Path) -> tuple[list[str], list[str]]:
    """Return (failures, warnings) for the installed copy. Read-only.

    Failures: modified program files, a dangling link into this install, an interrupted
    rollback/incoming (updates are refused until it is inspected).
    Warnings: missing or foreign links, interrupted staged directories, a leftover drain
    request (workers park while it exists), a recorded memory failure.
    """
    failures = manifest_problems(target)
    warnings: list[str] = []
    for label, link in (('unit link', unit_dir / UNIT), ('CLI link', bin_dir / 'autocontroller')):
        if link.is_symlink() and not link.exists():
            (failures if _owned_link(link, target) else warnings).append(f'{label} is dangling: {link}')
        elif link.is_symlink() and not _owned_link(link, target):
            warnings.append(f'{label} points outside this install (left alone): {link}')
        elif not link.is_symlink():
            warnings.append(f'{label} is missing: {link}')
    for name in ra.DATA_PROGRAM_DIRS:
        if (target / f'.{name}.new').exists():
            warnings.append(f'interrupted install left .{name}.new; reinstall or remove it')
    if (target / 'rollback' / 'incoming').exists() or (target / 'rollback' / 'incoming').is_symlink():
        failures.append('rollback/incoming exists: an earlier update did not finish; inspect it before updating')
    if (target / 'rollback' / 'swap').exists() or (target / 'rollback' / 'swap').is_symlink():
        failures.append('rollback/swap exists: an earlier rollback did not finish; run `autocontroller rollback --recover`')
    request = target / 'state' / DRAIN_REQUEST
    if os.path.lexists(request):
        # S2: a request counts only while its updater holds lifecycle.lock. Without the lock it is
        # stale: the controller ignores it, and a human must remove it.
        if _lifecycle_lock_held(target / LIFECYCLE_LOCK):
            warnings.append('state/drain.request exists and an update holds lifecycle.lock: workers are draining')
        else:
            failures.append(f'stale state/drain.request (no operation holds lifecycle.lock): '
                            f'remove it with: rm {request}')
    if (target / 'state' / 'memory' / 'failure').exists():
        warnings.append('memory failure marker recorded (state/memory/failure); memory is blocked')
    return failures, warnings


def status_lines(target: Path, *, active: bool) -> list[str]:
    """Human-readable status. Read-only: takes the service state from the caller."""
    lines = [f'version: {installed_version(target) or "unknown"}',
             f'service: {"active" if active else "not active"}']
    rollback = _read_json(target / 'rollback' / 'previous' / 'VERSION')
    kept = rollback.get('tag') if isinstance(rollback, dict) else None
    lines.append(f'previous kept for rollback: {kept or "none"}')
    if (target / 'rollback' / 'incoming').exists():
        lines.append('rollback/incoming: present (an update did not finish)')
    projects = target / 'state' / 'projects'
    names = sorted(p.name for p in projects.iterdir() if p.is_dir()) if projects.is_dir() else []
    for name in names:
        cycle = _read_json(projects / name / 'cycle.json')
        phase = cycle.get('phase', 'unknown') if isinstance(cycle, dict) else 'idle'
        pending = ' (cycle.pending present)' if (projects / name / 'cycle.pending').exists() else ''
        lines.append(f'project {name}: phase {phase}{pending}')
    if not names:
        lines.append('projects: none with state')
    drain = [n for n in (DRAIN_REQUEST, DRAIN_READY) if (target / 'state' / n).exists()]
    lines.append(f'drain files: {", ".join(drain) or "none"}')
    failure = target / 'state' / 'memory' / 'failure'
    lines.append(f'memory failure marker: {"present" if failure.exists() else "none"}')
    history = _read_json(target / 'updates' / 'history.json')
    last = history[-1] if isinstance(history, list) and history else None
    if isinstance(last, dict):
        lines.append(f'last update: {last.get("action")} {last.get("from")} -> {last.get("to")} ({last.get("result")})')
    else:
        lines.append('last update: none recorded')
    return lines


# ---- remove one project -------------------------------------------------------------------
# Removes one entry from config.json. Memory, GitHub state and the checkout are kept unless
# --delete-local-data, which deletes only a clean, fully pushed managed clone directly under
# ~/repositories plus state/projects/<name>. state/memory is never touched.

REMOVE_REPO = re.compile(r'(?:https://github\.com/)?([A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+?)(?:\.git)?/?', re.I)
PROJECT_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}')
LOCAL_GIT_TIMEOUT = 120


def canonical_repo(value) -> str:
    """OWNER/REPOSITORY from a GitHub URL or the short form. Anything else refuses."""
    match = REMOVE_REPO.fullmatch(str(value).strip())
    if not match or match.group(1).split('/')[1] in ('.', '..'):
        raise LifecycleError('expected a GitHub repository URL or OWNER/REPOSITORY')
    return match.group(1)


def _same_repo(url: str, repo: str) -> bool:
    try:
        return canonical_repo(url).lower() == repo.lower()
    except LifecycleError:
        return False


def _config(target: Path) -> dict:
    doc = _read_json(target / 'config.json')
    if not isinstance(doc, dict) or not isinstance(doc.get('projects'), list):
        raise LifecycleError('config.json is unreadable; refusing to change it')
    return doc


def _project(doc: dict, repo: str) -> dict:
    found = [p for p in doc['projects'] if isinstance(p, dict) and str(p.get('repo', '')).lower() == repo.lower()]
    if not found:
        raise LifecycleError(f'{repo} is not configured')
    if len(found) > 1:
        raise LifecycleError(f'{repo} is configured more than once; refusing to guess which to remove')
    name = found[0].get('name')
    if not isinstance(name, str) or not PROJECT_NAME.fullmatch(name) or '..' in name:
        raise LifecycleError('configured project name is unsafe; refusing to touch its state')
    return found[0]


def in_flight(state: Path, name: str) -> list[str]:
    """cycle.json/cycle.pending: an unfinished task. Removing it without --force would strand work."""
    pdir = state / 'projects' / name
    return [n for n in ('cycle.json', 'cycle.pending') if (pdir / n).exists() or (pdir / n).is_symlink()]


def _git_out(checkout: Path, *args: str) -> str:
    try:
        proc = subprocess.run(['git', '-C', str(checkout), *args], capture_output=True, text=True,
                              timeout=LOCAL_GIT_TIMEOUT, check=False,
                              env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'})
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LifecycleError(f'git {args[0]} failed: {exc}') from exc
    if proc.returncode != 0:
        raise LifecycleError(f'git {args[0]} failed in checkout; refusing to delete it')
    return proc.stdout


def check_deletable(directory, repo: str, repositories: Path):
    """Refuse unless the checkout is a clean, fully pushed, managed clone directly under repositories.

    Runs before any change. Upstream state comes from the last fetch, so it is only as current
    as the local remote-tracking ref; no network is used.
    """
    if not isinstance(directory, str) or not Path(directory).is_absolute():
        raise LifecycleError('configured checkout path is not absolute; refusing to delete it')
    path = Path(directory)
    if path.is_symlink() or not path.is_dir():
        raise LifecycleError(f'checkout {path} is missing or a link; refusing to delete it')
    if path.parent.resolve() != repositories.resolve():
        raise LifecycleError(f'checkout is not directly under {repositories}; refusing to delete it')
    if (path / '.git').is_symlink() or not (path / '.git').is_dir():
        raise LifecycleError('checkout is not a git repository; refusing to delete it')
    origins = [v.strip() for v in _git_out(path, 'config', '--get-all', 'remote.origin.url').splitlines() if v.strip()]
    if len(origins) != 1 or not _same_repo(origins[0], repo):
        raise LifecycleError('checkout origin does not match the configured repository; refusing to delete it')
    if _git_out(path, 'status', '--porcelain', '--ignored').strip():
        raise LifecycleError('checkout has uncommitted, untracked or ignored files; refusing to delete it')
    if _git_out(path, 'stash', 'list').strip():
        raise LifecycleError('checkout has stashed changes; refusing to delete it')
    try:
        _git_out(path, 'rev-parse', '--abbrev-ref', '@{u}')
    except LifecycleError as exc:
        raise LifecycleError('checkout has no upstream branch; refusing to delete it') from exc
    if _git_out(path, 'rev-list', '--count', '@{u}..HEAD').strip() != '0':
        raise LifecycleError('checkout has commits not pushed to its upstream; refusing to delete it')
    # Every local branch, not only HEAD: a branch without upstream, or with commits no remote has,
    # would be destroyed with the checkout.
    branches = _git_out(path, 'for-each-ref', '--format=%(refname:short)%09%(upstream)', 'refs/heads/')
    for line in branches.splitlines():
        name, _, upstream = line.partition('\t')
        if not upstream.strip():
            raise LifecycleError(f'local branch {name} has no upstream; refusing to delete it')
    if _git_out(path, 'log', '--branches', '--not', '--remotes', '--oneline').strip():
        raise LifecycleError('a local branch has commits no remote has; refusing to delete it')
    # Linked worktrees live outside this checkout and would be left dangling when it is deleted.
    worktrees = [l[len('worktree '):] for l in _git_out(path, 'worktree', 'list', '--porcelain').splitlines()
                 if l.startswith('worktree ')]
    if [Path(w).resolve() for w in worktrees] != [path.resolve()]:
        raise LifecycleError('checkout has linked worktrees; refusing to delete it')


def remove(target: Path, repo_arg: str, *, environ, yes: bool = False, force: bool = False,
           delete_local_data: bool = False, ask=input, repositories: Path | None = None,
           drain_timeout: float = DEFAULT_DRAIN_TIMEOUT, sleep=time.sleep, clock=time.monotonic) -> str:
    refuse_agent(environ, 'remove')
    repo = canonical_repo(repo_arg)
    root = repositories if repositories is not None else Path.home() / 'repositories'
    with operation(target):
        doc = _config(target)
        entry = _project(doc, repo)
        name = entry['name']
        busy = in_flight(target / 'state', name)
        if busy and not force:
            raise LifecycleError(f'{repo} has an in-flight cycle ({", ".join(busy)}); let it finish or use --force')
        if busy and delete_local_data:
            # Deleting state/projects/<name> would destroy the cycle files that --force keeps.
            raise LifecycleError('--delete-local-data cannot run while a cycle is in flight')
        directory = entry.get('directory')
        if delete_local_data:
            check_deletable(directory, repo, root)
        confirm(f'Remove {repo} from AutoController' + (' and delete its local checkout' if delete_local_data else ''),
                yes=yes, ask=ask)
        was_active = _stop_after_drain(target, drain_timeout, sleep, clock)
        # B5: the drain may have taken minutes. A cycle or an unpushed commit that appeared after
        # confirmation must refuse now, before any config write, and the service comes back.
        try:
            # --force keeps a cycle's files, but never lets --delete-local-data remove the checkout under one:
            # the post-drain re-check therefore runs whenever local data would be deleted, even with --force.
            if (not force or delete_local_data) and in_flight(target / 'state', name):
                raise LifecycleError(f'{repo} started an in-flight cycle while draining; nothing was changed')
            if delete_local_data:
                check_deletable(directory, repo, root)
        except LifecycleError as exc:
            if was_active and not start_service():
                raise LifecycleError(f'{exc}; the service did not restart, run autocontroller start') from exc
            raise
        doc['projects'] = [p for p in doc['projects'] if p is not entry]
        try:
            _write_json(target / 'config.json', doc)
        except BaseException:
            # The old config is still in place: bring the service back as it was before raising.
            if was_active:
                start_service()
            raise
        if doc['projects']:
            if was_active and not start_service():
                raise LifecycleError(f'{repo} removed from config, but the service did not restart; run autocontroller start')
        elif systemctl('disable', '--now') != 0:
            raise LifecycleError(f'{repo} removed from config, but systemctl disable --now failed')
        if delete_local_data:
            shutil.rmtree(Path(directory))
            _remove(target / 'state' / 'projects' / name)
    if delete_local_data:
        return f'removed {repo}; deleted checkout {directory} and state/projects/{name}; memory and GitHub state kept'
    return f'removed {repo}; kept checkout {directory}, state/projects/{name}; memory and GitHub state kept'
