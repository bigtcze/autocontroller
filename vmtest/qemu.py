"""QEMU argv construction (pure, testable offline) and accelerator selection."""
from __future__ import annotations

import os
from pathlib import Path

from . import cloudinit

MEMORY_MB = 2048
SMP = 2
ACCELS = ('auto', 'kvm', 'tcg')


def resolve_accel(requested: str, kvm_path: Path = Path('/dev/kvm')) -> str:
    if requested not in ACCELS:
        raise ValueError(f'accel must be one of {ACCELS}')
    usable = kvm_path.exists() and os.access(kvm_path, os.R_OK | os.W_OK)
    if requested == 'kvm':
        if not usable:
            raise RuntimeError('--accel kvm requested but /dev/kvm is not readable and writable')
        return 'kvm'
    if requested == 'tcg':
        return 'tcg'
    return 'kvm' if usable else 'tcg'


OVMF_CODE = Path('/usr/share/OVMF/OVMF_CODE_4M.fd')
OVMF_VARS_TEMPLATE = Path('/usr/share/OVMF/OVMF_VARS_4M.fd')


def build_argv(*, disk: Path, seed_port: int, ssh_port: int, accel: str,
               pidfile: Path, serial_log: Path, vars_file: Path,
               firmware: Path = OVMF_CODE, binary: str = 'qemu-system-x86_64') -> list[str]:
    if accel not in ('kvm', 'tcg'):
        raise ValueError('accel must be kvm or tcg')
    if not 1024 <= ssh_port <= 65535:
        raise ValueError('ssh port out of range')
    machine = 'q35,accel=kvm' if accel == 'kvm' else 'q35,accel=tcg'
    cpu = 'host' if accel == 'kvm' else 'max'
    netdev = (f'user,id=net0,restrict=off,'
              f'hostfwd=tcp:127.0.0.1:{ssh_port}-:22')
    return [
        binary,
        '-machine', machine,
        '-cpu', cpu,
        '-m', str(MEMORY_MB),
        '-smp', str(SMP),
        # Official Debian/Ubuntu cloud images are UEFI-only: SeaBIOS loops in GRUB.
        '-drive', f'if=pflash,format=raw,readonly=on,file={firmware}',
        '-drive', f'if=pflash,format=raw,file={vars_file}',
        '-drive', f'file={disk},if=virtio,format=qcow2,cache=writeback',
        '-netdev', netdev,
        '-device', 'virtio-net-pci,netdev=net0',
        '-smbios', cloudinit.smbios_argument(seed_port),
        '-display', 'none',
        '-monitor', 'none',
        '-serial', f'file:{serial_log}',
        '-pidfile', str(pidfile),
        '-no-user-config',
        '-nodefaults',
        '-device', 'virtio-rng-pci',
    ]
