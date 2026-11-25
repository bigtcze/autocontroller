"""Scenario steps a–f: preconditions, installer negatives, clean install, stand-in
project, Engram first-install failure, logout and reboot persistence.

Guest commands run through vmtest.ssh.GuestSsh as `tester` (controller user) or
`vmadmin` (harness control). Assertions raise AssertionError-like ScenarioAbort.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .runner import ScenarioAbort

GUEST_USER = 'tester'                  # controller user inside the disposable guest
GUEST_HOME = '/home/' + GUEST_USER     # built from parts: keeps literal home paths out of the repo
REPO_SRC = f'{GUEST_HOME}/src'         # tar-extracted source tree
INSTALL = f'{GUEST_HOME}/scripts/autocontroller'
PROJECT_DIR = f'{GUEST_HOME}/repositories/example-repo'
BOOT_ID = '/proc/sys/kernel/random/boot_id'
BOOT_ID_CMD = ['cat', BOOT_ID]
SERVICE = 'autocontroller.service'
SUPPORTED_OS = {'debian-12': ('debian', '12'), 'debian-13': ('debian', '13'),
                'ubuntu-24.04': ('ubuntu', '24.04'), 'ubuntu-26.04': ('ubuntu', '26.04')}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ScenarioAbort(message)


def ssh_out(ctx, user: str, argv: list[str] | str, timeout: float = 120) -> str:
    return ctx.ssh.run(user, argv, timeout=timeout).stdout.decode('utf-8', 'replace')


def save(ctx, name: str, text: str) -> None:
    path = ctx.artifacts / 'guest' / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8', errors='replace')


def root_run(ctx, argv: list[str], timeout: float = 120):
    """vmadmin with sudo. Used only for harness checks and injections, never for the controller."""
    return ctx.ssh.run('vmadmin', ['sudo', '-n', *argv], timeout=timeout, check=False)


def sudo_out(ctx, argv: list[str], timeout: float = 120) -> str:
    """Read output of a command that needs root, e.g. stat on the mode-700 tester home."""
    return ssh_out(ctx, 'vmadmin', ['sudo', '-n', *argv], timeout=timeout)


def step_preconditions(ctx) -> str:
    osr = ssh_out(ctx, 'vmadmin', ['cat', '/etc/os-release'])
    fields = dict(re.findall(r'^([A-Z_]+)="?([^"\n]*)"?$', osr, re.M))
    expect = SUPPORTED_OS[ctx.distro]
    require((fields.get('ID'), fields.get('VERSION_ID')) == expect,
            f'os-release {fields.get("ID")} {fields.get("VERSION_ID")} != {expect}')
    require(ssh_out(ctx, 'vmadmin', ['ps', '-p', '1', '-o', 'comm=']).strip() == 'systemd', 'PID 1 is not systemd')
    for tool in ('git', 'gh', 'mise', 'engram', 'opencode'):
        rc = ctx.ssh.run('vmadmin', ['sh', '-c', f'command -v {tool} >/dev/null 2>&1'],
                         timeout=30, check=False).returncode
        if tool == 'opencode':
            require(rc != 0, 'opencode must be absent before the stand-in is installed')
        else:
            require(rc != 0, f'{tool} must be absent before install')
    for pkg in ('git', 'gh', 'mise'):
        rc = ctx.ssh.run('vmadmin', ['dpkg', '-s', pkg], timeout=30, check=False).returncode
        require(rc != 0, f'package {pkg} must not be installed before install')
    uid = ssh_out(ctx, 'tester', ['id', '-u']).strip()
    require(uid != '0', 'tester must be non-root')
    ctx.data['tester_uid'] = int(uid)
    return f'{ctx.distro} os-release ok; git/gh/mise/engram/opencode absent; tester uid {uid}'


def step_installer_negatives(ctx) -> str:
    # Root refused: installer must exit 2 before any mutation.
    rc = root_run(ctx, ['bash', f'{REPO_SRC}/install.sh'], timeout=60).returncode
    require(rc == 2, f'install.sh as root exited {rc}, expected 2')
    rc = ctx.ssh.run('tester', ['bash', f'{REPO_SRC}/install.sh', '--bogus'], timeout=60, check=False).returncode
    require(rc == 2, f'bad argument exited {rc}, expected 2')
    out = ctx.ssh.run('tester', ['env', '-i', f'HOME={GUEST_HOME}', 'PATH=/usr/bin:/bin',
                                 'bash', f'{REPO_SRC}/install.sh'], timeout=60, check=False)
    require(out.returncode == 2, f'install without opencode exited {out.returncode}, expected 2')
    require(b'Install/configure OpenCode V2' in out.stderr, 'missing OpenCode refusal message')
    return 'root, bad-argument and missing-opencode refusals exit 2'


def step_install_fake_opencode(ctx) -> str:
    ctx.ssh.run('tester', ['mkdir', '-p', f'{GUEST_HOME}/.opencode/bin'], timeout=30)
    script = (ctx.repo_root / 'vmtest' / 'guest' / 'opencode').read_bytes()
    ctx.ssh.run('tester', ['sh', '-c', f'cat > {GUEST_HOME}/.opencode/bin/opencode && chmod 755 {GUEST_HOME}/.opencode/bin/opencode'],
                timeout=60, stdin=script)
    return 'fake opencode installed to ~/.opencode/bin/opencode'


def step_clean_install(ctx) -> str:
    env = 'GH_TOKEN=vmtest-placeholder-not-a-credential'
    proc = ctx.ssh.run('tester', ['sh', '-c', f'cd {REPO_SRC} && {env} bash install.sh </dev/null'],
                       timeout=1500, check=False)
    save(ctx, 'install-clean.stdout.txt', proc.stdout.decode('utf-8', 'replace'))
    save(ctx, 'install-clean.stderr.txt', proc.stderr.decode('utf-8', 'replace'))
    require(proc.returncode == 1, f'clean install exited {proc.returncode}, expected 1 (fail-closed GH_TOKEN refusal)')
    require(b'Unset temporary GH_TOKEN' in proc.stderr, 'missing GH_TOKEN refusal message')
    for pkg in ('git', 'gh', 'mise'):
        require(ctx.ssh.run('vmadmin', ['dpkg', '-s', pkg], timeout=30, check=False).returncode == 0,
                f'{pkg} not installed from packages')
    linger = ssh_out(ctx, 'vmadmin', ['loginctl', 'show-user', 'tester', '-p', 'Linger']).strip()
    require(linger == 'Linger=yes', f'lingering not enabled: {linger}')
    require(ctx.ssh.run('tester', ['test', '-L', f'{GUEST_HOME}/.config/systemd/user/{SERVICE}'],
                        timeout=30, check=False).returncode == 0, 'unit symlink missing')
    cfg_mode = sudo_out(ctx, ['stat', '-c', '%a', f'{INSTALL}/config.json']).strip()
    require(cfg_mode == '600', f'config.json mode {cfg_mode}, expected 600')
    dir_modes = sudo_out(ctx, ['stat', '-c', '%a', f'{INSTALL}', f'{INSTALL}/state']).split()
    require(dir_modes == ['700', '700'], f'runtime dirs modes {dir_modes}, expected 700')
    return 'APT git/gh/mise installed; linger yes; unit linked; GH_TOKEN refusal exit 1 before GitHub contact'
