"""Disposable QEMU guest lifecycle: overlay disk, NoCloud seed server, process control.

Everything is bounded and everything that is started is stopped in `close()`.
"""
from __future__ import annotations

import http.server
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import qemu
from .ssh import SshError, run_checked

OVERLAY_SIZE = '10G'


class VmError(RuntimeError):
    pass


class SeedServer:
    """Serves NoCloud user-data/meta-data on 127.0.0.1 only, from memory."""

    def __init__(self, files: dict[str, bytes]):
        allowed = {f'/{name}': body for name, body in files.items()}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 (stdlib naming)
                body = allowed.get(self.path)
                if body is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):  # keep the harness output quiet
                pass

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True,
                                       name='vmtest-seed')

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def create_overlay(base: Path, base_format: str, overlay: Path, *, timeout: float = 300) -> None:
    """Copy-on-write overlay; the cached base image is opened read-only and never written."""
    if overlay.exists():
        raise VmError(f'overlay already exists: {overlay}')
    run_checked(['qemu-img', 'create', '-q', '-f', 'qcow2', '-F', base_format,
                 '-b', str(base), str(overlay)], timeout=timeout)
    run_checked(['qemu-img', 'resize', str(overlay), OVERLAY_SIZE], timeout=timeout)


class Guest:
    """One qemu-system-x86_64 process. `close()` always kills it."""

    def __init__(self, *, overlay: Path, seed_port: int, ssh_port: int, accel: str,
                 workdir: Path, artifacts: Path):
        self.workdir = workdir
        self.pidfile = workdir / 'qemu.pid'
        self.serial_log = artifacts / 'serial.log'
        for firmware in (qemu.OVMF_CODE, qemu.OVMF_VARS_TEMPLATE):
            if not firmware.is_file():
                raise VmError(f'UEFI firmware missing: {firmware} (install the ovmf package)')
        self.vars_file = workdir / 'OVMF_VARS.fd'
        shutil.copyfile(qemu.OVMF_VARS_TEMPLATE, self.vars_file)  # per-run NVRAM, never shared
        self.argv = qemu.build_argv(disk=overlay, seed_port=seed_port, ssh_port=ssh_port,
                                    accel=accel, pidfile=self.pidfile,
                                    serial_log=self.serial_log, vars_file=self.vars_file)
        self.proc: subprocess.Popen | None = None
        self.ssh_port = ssh_port

    def start(self) -> None:
        if self.proc is not None:
            raise VmError('guest already started')
        self.serial_log.parent.mkdir(parents=True, exist_ok=True)
        self.stderr_path = self.workdir / 'qemu-stderr.log'
        with self.stderr_path.open('wb') as err:
            self.proc = subprocess.Popen(self.argv, stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=err,
                                         start_new_session=True)
        time.sleep(1)
        if self.proc.poll() is not None:
            err_text = self.stderr_path.read_text(errors='replace')[-800:]
            raise VmError(f'qemu exited immediately rc={self.proc.returncode}: {err_text}')

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def close(self, grace: float = 20) -> None:
        """Terminate, then kill. Safe to call repeatedly or when never started."""
        proc = self.proc
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=grace)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=grace)
        finally:
            self.proc = None


def wait_for_reboot(read_boot_id, *, old_boot_id: str, timeout: float) -> str:
    """After `systemctl reboot`, poll until SSH answers with a different boot_id."""
    deadline = time.monotonic() + timeout
    last = ''
    while time.monotonic() < deadline:
        try:
            new_boot = read_boot_id()
        except SshError as exc:
            last = str(exc)[-200:]
        else:
            if new_boot != old_boot_id:
                return new_boot
            last = 'boot_id unchanged'
        time.sleep(5)
    raise VmError(f'guest did not reboot within {timeout:.0f}s: {last}')
