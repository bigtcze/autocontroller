"""Host-side SSH helpers for the disposable guest. argv lists only, explicit
timeouts, pinned host key, no agent, no password auth, no host shell.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

GUEST_HOST = '127.0.0.1'


class SshError(RuntimeError):
    pass


def generate_keypair(directory: Path, name: str) -> tuple[Path, str]:
    """Ephemeral ed25519 keypair. Returns (private path, public key line)."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    private = directory / name
    if private.exists() or private.with_suffix('.pub').exists():
        raise SshError(f'refusing to overwrite existing key {private}')
    run_checked(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', f'vmtest-{name}',
                 '-f', str(private)], timeout=60)
    os.chmod(private, 0o600)
    return private, public_line(private.with_suffix('.pub'))


def public_line(path: Path) -> str:
    text = Path(path).read_text(encoding='utf-8').strip()
    fields = text.split()
    if len(fields) < 2 or not fields[0].startswith('ssh-'):
        raise SshError(f'not an OpenSSH public key: {path}')
    return ' '.join(fields[:2])


def run_checked(argv: list[str], *, timeout: float, stdin: bytes | None = None,
                cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run a local argv with a hard timeout; raise SshError on non-zero exit."""
    try:
        proc = subprocess.run(argv, input=stdin, capture_output=True, timeout=timeout,
                              check=False, cwd=cwd)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SshError(f'{argv[0]} failed: {exc}') from exc
    if proc.returncode != 0:
        tail = proc.stderr.decode('utf-8', 'replace')[-800:]
        raise SshError(f'{argv[0]} rc={proc.returncode}: {tail}')
    return proc


@dataclass
class GuestSsh:
    """Pinned SSH client for one guest port. Host key comes from known_hosts only."""
    port: int
    key: Path
    known_hosts: Path

    @classmethod
    def create(cls, port: int, key: Path, host_public: str, workdir: Path) -> 'GuestSsh':
        known = workdir / 'known_hosts'
        # Non-default port: OpenSSH keys the entry as [host]:port.
        known.write_text(f'[{GUEST_HOST}]:{port} {host_public}\n', encoding='utf-8')
        os.chmod(known, 0o600)
        return cls(port=port, key=key, known_hosts=known)

    def _argv(self, user: str, remote: list[str] | str) -> list[str]:
        remote_cmd = remote if isinstance(remote, str) else shlex.join(remote)
        return ['ssh', '-F', '/dev/null', '-i', str(self.key),
                '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
                '-o', 'StrictHostKeyChecking=yes',
                '-o', f'UserKnownHostsFile={self.known_hosts}',
                '-o', 'GlobalKnownHostsFile=/dev/null',
                '-o', 'PasswordAuthentication=no', '-o', 'KbdInteractiveAuthentication=no',
                '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=15',
                '-o', 'ServerAliveCountMax=3', '-o', 'LogLevel=ERROR',
                '-p', str(self.port), f'{user}@{GUEST_HOST}', remote_cmd]

    def run(self, user: str, remote: list[str] | str, *, timeout: float,
            stdin: bytes | None = None, check: bool = True) -> subprocess.CompletedProcess:
        argv = self._argv(user, remote)
        try:
            proc = subprocess.run(argv, input=stdin, capture_output=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SshError(f'ssh {user}: {exc}') from exc
        if check and proc.returncode != 0:
            tail = proc.stderr.decode('utf-8', 'replace')[-800:]
            raise SshError(f'ssh {user} rc={proc.returncode}: {tail}')
        return proc

    def hold(self, user: str, seconds: int) -> subprocess.Popen:
        """Open a real interactive-style login session that stays up until it is terminated.

        The guest side is `sleep` with a bounded lifetime, so the session cannot outlive
        the test even if the caller forgets to close it. Caller must `.kill()` / `.wait()`."""
        argv = self._argv(user, ['sleep', str(int(seconds))])
        return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)

    def put(self, user: str, remote_dir: str, data: bytes, *, timeout: float) -> None:
        """Stream a gzip tar archive into remote_dir (created first)."""
        self.run(user, ['mkdir', '-p', '--', remote_dir], timeout=timeout)
        self.run(user, ['tar', '-xzf', '-', '-C', remote_dir], timeout=timeout, stdin=data)

    def wait(self, user: str, *, timeout: float, interval: float = 5.0) -> None:
        """Bounded wait until `true` succeeds over SSH."""
        deadline = time.monotonic() + timeout
        last = ''
        while True:
            try:
                self.run(user, ['true'], timeout=20)
                return
            except SshError as exc:
                last = str(exc)[-200:]
            if time.monotonic() >= deadline:
                raise SshError(f'ssh {user} not reachable within {timeout:.0f}s: {last}')
            time.sleep(interval)
