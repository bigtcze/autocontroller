"""Host-side driver for the disposable-VM lifecycle suite. Thin wiring only.

  python3 -m vmtest.harness validate-manifest
  python3 -m vmtest.harness run --distro debian-13 --artifacts DIR [--cache DIR] [--accel auto|kvm|tcg]

Never run this on a production host: it boots a QEMU guest with NOPASSWD sudo
for disposable-VM users. See docs/VM_TESTING.md.
"""
from __future__ import annotations

import argparse
import json
import secrets
import shutil
import socket
import sys
import tempfile
from pathlib import Path

from . import cloudinit, fetch, manifest, qemu, runner, scenario, scenario_engram, scenario_persist, source, ssh, vm

ROOT = Path(__file__).resolve().parents[1]
# Seconds to first SSH answer, bounded per accelerator. TCG is software emulation (roughly
# several times slower); 1800s keeps a worst-case TCG boot inside the 150-minute CI job limit.
BOOT_TIMEOUT = {'kvm': 900, 'tcg': 1800}
# Scale for fixed scenario waits (reboot, readiness, sleeps). KVM-tuned numbers are the baseline;
# TCG is several times slower, so waits scale instead of false-failing. Each wait stays bounded.
WAIT_FACTOR = {'kvm': 1, 'tcg': 3}
DEFAULT_CACHE = Path.home() / '.cache' / 'vmtest'


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def wait_ssh(ctx) -> str:
    ctx.ssh.wait('vmadmin', timeout=ctx.data['boot_timeout'])
    return f'SSH answered on 127.0.0.1:{ctx.ssh.port} with the pinned host key'


def transfer(ctx) -> str:
    archive = source.build_archive(ctx.repo_root)
    ctx.ssh.put('tester', scenario.REPO_SRC, archive, timeout=300)
    return f'{len(archive)} bytes of source tree to {scenario.REPO_SRC}'


def steps() -> list[runner.Step]:
    S = runner.Step
    return [
        S('boot: SSH as vmadmin with pinned host key', wait_ssh),
        S('transfer source tree to tester', transfer),
        S('a preconditions', scenario.step_preconditions),
        S('b installer negative injections', scenario.step_installer_negatives),
        S('b install fake opencode', scenario.step_install_fake_opencode),
        S('c clean full install', scenario.step_clean_install),
        S('d stand-in project and fake gh', scenario_persist.step_stand_in_project),
        S('d Engram first-install failure injection', scenario_persist.step_engram_first_install_failure),
        S('d Engram recovery and controller checks', scenario_persist.step_engram_recovered_and_healthy),
        S('d Engram failure marker cleared after recovery', scenario_persist.step_engram_marker_cleared),
        S('e logout persistence', scenario_persist.step_logout_persistence),
        S('f reboot persistence', scenario_persist.step_reboot_persistence),
        S('g Engram upgrade rollback', scenario_engram.step_engram_upgrade_rollback),
        S('h Engram crash recovery', scenario_engram.step_engram_crash_recovery),
        S('i repeated install', scenario_engram.step_repeated_install),
        S('j collect logs', scenario_engram.step_collect_logs, collect=True),
    ]


def _fail(report, report_path, name: str, exc: Exception) -> int:
    report.results.append(runner.StepResult(name, 'failed', 0.0, f'{type(exc).__name__}: {exc}'[:2000]))
    report.write(report_path)
    return 1


def _qemu_version() -> str:
    """First line of `qemu-system-x86_64 --version`; never raises (recorded as unavailable)."""
    try:
        out = ssh.run_checked(['qemu-system-x86_64', '--version'], timeout=20).stdout.decode('utf-8', 'replace')
        return out.splitlines()[0].strip() if out.strip() else 'unavailable: empty output'
    except Exception as exc:  # recorded in report.json, not silent
        return f'unavailable: {exc}'[:200]


def run(distro: str, artifacts: str, cache: str, accel: str) -> int:
    art = Path(artifacts)
    art.mkdir(parents=True, exist_ok=True)
    report = runner.Report(distro=distro)
    report_path = art / 'report.json'
    try:
        base = fetch.ensure_base_image(distro, Path(cache))
        image = manifest.image(distro)
        resolved = qemu.resolve_accel(accel)
    except Exception as exc:  # setup failures are recorded, never silent
        return _fail(report, report_path, 'setup: image and accelerator', exc)
    report.accel = resolved
    report.qemu_version = _qemu_version()
    (art / 'run.json').write_text(json.dumps({'distro': distro, 'accel': resolved, 'image': image,
                                              'base_image': base.name,
                                              'qemu_version': report.qemu_version}, indent=2, sort_keys=True) + '\n')
    ok = False
    with tempfile.TemporaryDirectory(prefix='vmtest-run-') as tmp:
        work = Path(tmp)
        try:
            vm.create_overlay(base, image['format'], work / 'disk.qcow2')
            client_priv, client_pub = ssh.generate_keypair(work, 'client')
            host_priv, host_pub = ssh.generate_keypair(work, 'host')
            seed = {'user-data': cloudinit.user_data(client_pubkey=client_pub, host_private=host_priv.read_text(),
                                                     host_public=host_pub).encode(),
                    'meta-data': cloudinit.meta_data(f'vmtest-{secrets.token_hex(6)}').encode()}
            with vm.SeedServer(seed) as server:
                guest = vm.Guest(overlay=work / 'disk.qcow2', seed_port=server.port, ssh_port=free_port(),
                                 accel=resolved, workdir=work, artifacts=art)
                try:
                    guest.start()
                    gs = ssh.GuestSsh.create(guest.ssh_port, client_priv, host_pub, work)
                    ctx = runner.Context(distro=distro, repo_root=ROOT, artifacts=art, ssh=gs, guest=guest)
                    ctx.data['boot_timeout'] = BOOT_TIMEOUT[resolved]
                    ctx.data['wait_factor'] = WAIT_FACTOR[resolved]
                    ok = runner.execute(steps(), ctx, report, report_path)
                except Exception as exc:
                    _fail(report, report_path, 'setup: start guest', exc)
                finally:
                    guest.close()
        except Exception as exc:
            _fail(report, report_path, 'setup: prepare overlay and seed', exc)
        finally:
            stderr = work / 'qemu-stderr.log'
            if stderr.exists():
                shutil.copy(stderr, art / 'qemu-stderr.log')
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='python3 -m vmtest.harness')
    sub = parser.add_subparsers(dest='cmd', required=True)
    run_p = sub.add_parser('run', help='boot one disposable VM and run the lifecycle scenario')
    run_p.add_argument('--distro', required=True, choices=manifest.SUPPORTED)
    run_p.add_argument('--artifacts', required=True)
    run_p.add_argument('--cache', default=str(DEFAULT_CACHE))
    run_p.add_argument('--accel', default='auto', choices=qemu.ACCELS)
    sub.add_parser('validate-manifest', help='validate vmtest/images.json offline')
    args = parser.parse_args(argv)
    if args.cmd == 'validate-manifest':
        try:
            data = manifest.load()
        except manifest.ManifestError as exc:
            print(f'images.json INVALID: {exc}', file=sys.stderr)
            return 2
        print('images.json OK: ' + ', '.join(sorted(data['images'])))
        return 0
    return run(args.distro, args.artifacts, args.cache, args.accel)


if __name__ == '__main__':
    sys.exit(main())
