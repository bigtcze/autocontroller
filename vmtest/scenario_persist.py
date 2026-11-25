"""Scenario steps d–f: stand-in project, first-Engram-install failure, logout and
reboot persistence. Companion of vmtest/scenario.py (a–c).
"""
from __future__ import annotations

import json
import time

from . import vm
from .scenario import (BOOT_ID_CMD, GUEST_HOME, INSTALL, PROJECT_DIR, REPO_SRC, SERVICE,
                       require, root_run, save, ssh_out, sudo_out)

STATE = f'{INSTALL}/state'
MEMORY = f'{STATE}/memory'
GH_LOG = f'{GUEST_HOME}/.local/state/vmtest/gh-calls.log'
OC_LOG = f'{GUEST_HOME}/.local/state/vmtest/opencode-calls.log'
BLACKHOLE_BEGIN = '# vmtest-begin'
BLACKHOLE_END = '# vmtest-end'


def _read_boot_id(ctx) -> str:
    return ssh_out(ctx, 'vmadmin', BOOT_ID_CMD, timeout=30).strip()


def _service_show(ctx, prop: str) -> str:
    out = ctx.ssh.run('tester', ['systemctl', '--user', 'show', SERVICE, '-p', prop, '--value'],
                      timeout=60, check=False).stdout.decode().strip()
    return out


def tester_login_sessions(ctx) -> list[str]:
    """logind *login* sessions of tester (Class=user), read as root, no tester SSH needed.

    Lingering creates a Class=manager session for user@UID; that is not a login and is
    excluded explicitly. Any session whose class cannot be read is counted as a login
    (fail closed)."""
    out = sudo_out(ctx, ['loginctl', 'list-sessions', '--no-legend', '--no-pager'])
    logins = []
    for line in out.splitlines():
        fields = line.split()
        if len(fields) < 3 or fields[2] != 'tester':
            continue
        cls = sudo_out(ctx, ['loginctl', 'show-session', fields[0], '-p', 'Class', '--value']).strip()
        if cls != 'manager':
            logins.append(f'{fields[0]} class={cls or "unknown"}')
    return logins


def _marker_exists(ctx) -> bool:
    """memory.py Runtime.failed: state/memory/failure, touched by main() on a startup failure."""
    rc = sudo_out(ctx, ['sh', '-c', f'test -e {MEMORY}/failure && echo yes || echo no'], timeout=30).strip()
    return rc == 'yes'


def _gh_svc_count(ctx) -> int:
    """Number of controller-originated (svc) gh calls, from the stand-in log.

    Log lines are `<seq> <time> <origin> <args>`. Installer probes are origin `cli`
    and do not count. Reads use sudo -n so a mode-700 tester home is readable."""
    out = sudo_out(ctx, ['sh', '-c', f'awk \'$3 == "svc"\' {GH_LOG} 2>/dev/null | wc -l'], timeout=30)
    return int(out.strip() or 0)


def _hosts_blackhole(ctx, hosts: list[str]) -> None:
    lines = ''.join(f'0.0.0.0 {h}\n' for h in hosts)
    script = (f'set -e; sed -i "/{BLACKHOLE_BEGIN}/,/{BLACKHOLE_END}/d" /etc/hosts; '
              f'printf "{BLACKHOLE_BEGIN}\\n{lines}{BLACKHOLE_END}\\n" >> /etc/hosts')
    require(root_run(ctx, ['sh', '-c', script], timeout=30).returncode == 0, 'cannot blackhole hosts')


def _hosts_restore(ctx) -> None:
    script = f'set -e; sed -i "/{BLACKHOLE_BEGIN}/,/{BLACKHOLE_END}/d" /etc/hosts'
    root_run(ctx, ['sh', '-c', script], timeout=30)


def step_stand_in_project(ctx) -> str:
    """Local bare origin + checkout under ~/repositories, a config entry like onboarding writes,
    and the fake gh installed only now (after the real gh package exists)."""
    script = ' && '.join([
        f'mkdir -p {GUEST_HOME}/.local/bin {GUEST_HOME}/repositories {GUEST_HOME}/vmtest-origin',
        f'rm -rf {GUEST_HOME}/vmtest-origin/example-repo.git {PROJECT_DIR}',
        f'git init -q --bare -b main {GUEST_HOME}/vmtest-origin/example-repo.git',
        f'git clone -q {GUEST_HOME}/vmtest-origin/example-repo.git {PROJECT_DIR}',
        f'cd {PROJECT_DIR} && printf "# example-repo\\n" > README.md && '
        f'git add README.md && git -c user.name=vmtest -c user.email=vmtest@example.invalid commit -q -m init '
        f'&& git push -q origin HEAD:main',
    ])
    ctx.ssh.run('tester', ['sh', '-c', script], timeout=120)
    gh = (ctx.repo_root / 'vmtest' / 'guest' / 'gh').read_bytes()
    ctx.ssh.run('tester', ['sh', '-c', f'cat > {GUEST_HOME}/.local/bin/gh && chmod 755 {GUEST_HOME}/.local/bin/gh'],
                timeout=60, stdin=gh)
    config = {
        'agent': 'orchestrator', 'poll_seconds': 30, 'post_cycle_seconds': 5, 'ci_poll_seconds': 30,
        'retry_max_seconds': 600, 'cycle_timeout_seconds': 600, 'projects': [{
            'name': 'example-repo', 'enabled': True, 'repo': 'example-owner/example-repo',
            'directory': PROJECT_DIR, 'base_branch': 'main', 'project_owner': 'example-owner',
            'project_number': 7, 'required_check': 'required', 'required_workflow': 'required.yml',
            'merge_method': 'squash', 'release': {'enabled': False},
        }],
    }
    cfg_text = json.dumps(config, indent=2) + '\n'
    ctx.ssh.run('tester', ['sh', '-c', f'umask 077 && cat > {INSTALL}/config.json && chmod 600 {INSTALL}/config.json'],
                timeout=60, stdin=cfg_text.encode())
    return 'stand-in project, fake gh and config.json (no bootstrap_issue) installed'


def step_engram_first_install_failure(ctx) -> str:
    """Repeated-install path with an existing config, while api.github.com is unreachable:
    the service must fail closed (no usable Engram), not run without memory."""
    _hosts_blackhole(ctx, ['api.github.com'])
    try:
        svc_before = _gh_svc_count(ctx)
        proc = ctx.ssh.run('tester', ['sh', '-c', f'cd {REPO_SRC} && bash install.sh </dev/null'],
                           timeout=600, check=False)
        save(ctx, 'install-repeat-blackholed.stdout.txt', proc.stdout.decode('utf-8', 'replace'))
        save(ctx, 'install-repeat-blackholed.stderr.txt', proc.stderr.decode('utf-8', 'replace'))
        require(b'Existing configuration found' in proc.stdout, 'repeated install did not take the existing-config path')
        # memory.startup() raises with no usable Engram; main() returns 70.
        deadline = time.monotonic() + 120
        status = ''
        while time.monotonic() < deadline:
            status = _service_show(ctx, 'ExecMainStatus')
            if status == '70':
                break
            time.sleep(5)
        require(status == '70', f'controller did not exit 70 without Engram (ExecMainStatus={status!r})')
        active = _service_show(ctx, 'ActiveState')
        require(active != 'active', 'service active without usable Engram')
        # autonomy-controller.main() catches the startup() exception and touches
        # state/memory/failure (memory.py Runtime.failed), then exits 70.
        require(_marker_exists(ctx), 'failure marker missing during blackhole window')
        health = ctx.ssh.run('tester', ['sh', '-c', f'{GUEST_HOME}/.local/bin/autocontroller memory'],
                             timeout=120, check=False)
        require(health.returncode != 0, 'memory health reports usable while Engram is unavailable')
        # "No worker started" proof: zero controller-originated (svc) gh calls in the window.
        svc_after = _gh_svc_count(ctx)
        require(svc_after == svc_before,
                f'controller made {svc_after - svc_before} svc gh call(s) while Engram was unavailable')
        ctx.data['svc_gh_before_recovery'] = svc_after
        ctx.data['failed_first_install_state'] = active
        ctx.data['marker_seen_in_blackhole'] = True
    finally:
        _hosts_restore(ctx)
    ctx.ssh.run('tester', ['systemctl', '--user', 'reset-failed', SERVICE], timeout=60, check=False)
    ctx.ssh.run('tester', ['systemctl', '--user', 'restart', SERVICE], timeout=120, check=False)
    return (f'service fail-closed while blackholed (state={ctx.data["failed_first_install_state"]}); '
            'failure marker present during blackhole')


def step_engram_marker_cleared(ctx) -> str:
    """After a successful startup() the failure marker must be gone (memory.py unlinks it)."""
    _wait_for(lambda: _service_show(ctx, 'ActiveState') == 'active', 900, 'service active after recovery')
    require(not _marker_exists(ctx), 'failure marker still present after successful startup')
    return 'failure marker removed after successful startup()'


def _wait_for(predicate, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        require(time.monotonic() < deadline, f'{what} not reached within {timeout:.0f}s')
        time.sleep(15)


def step_engram_recovered_and_healthy(ctx) -> str:
    deadline = time.monotonic() + 900
    while True:
        state = _service_show(ctx, 'ActiveState')
        if state == 'active' and ctx.ssh.run('tester', ['test', '-f', f'{MEMORY}/active.json'],
                                             timeout=30, check=False).returncode == 0:
            break
        require(time.monotonic() < deadline, f'service not active with Engram within 900s (state={state})')
        time.sleep(15)
    health = ssh_out(ctx, 'tester', ['sh', '-c', f'{GUEST_HOME}/.local/bin/autocontroller memory'], timeout=120)
    require('"status": "ok"' in health or '"status":"ok"' in health, f'memory health not ok: {health[:200]}')
    pid = _service_show(ctx, 'MainPID')
    user = ssh_out(ctx, 'vmadmin', ['ps', '-o', 'user=', '-p', pid]).strip()
    require(user == 'tester', f'controller MainPID {pid} runs as {user!r}, expected tester')
    require(_service_show(ctx, 'NoNewPrivileges') == 'yes', 'NoNewPrivileges not yes')
    ctx.data['main_pid'] = pid
    # Positive attribution proof: the worker issued controller (svc) gh calls, including
    # project validation, so the zero-svc assertion in step d is meaningful.
    svc_lines = sudo_out(ctx, ['sh', '-c', f'awk \'$3 == "svc"\' {GH_LOG} 2>/dev/null || true'])
    require(_gh_svc_count(ctx) > 0, 'no svc gh calls after recovery; attribution or worker validation failed')
    require('project view' in svc_lines, 'controller did not validate the project (no svc project view)')
    gh_log = sudo_out(ctx, ['sh', '-c', f'cat {GH_LOG} 2>/dev/null || true'])
    oc_log = sudo_out(ctx, ['sh', '-c', f'cat {OC_LOG} 2>/dev/null || true'])
    # The stand-in refuses `run`; any run request is recorded as a REFUSED line.
    require(' run' not in oc_log, 'fake opencode was asked to run')
    save(ctx, 'gh-calls.log', gh_log)
    save(ctx, 'opencode-calls.log', oc_log)
    return f'Engram healthy; controller MainPID {pid} runs as tester; NoNewPrivileges=yes'


def step_logout_persistence(ctx) -> str:
    """Terminate every logind session of tester (not terminate-user)."""
    # Open a real login session for tester that stays up (harness SSH calls are short-lived
    # and leave no session behind), then prove it is a Class=user login before ending it.
    held = ctx.ssh.hold('tester', seconds=600)
    try:
        deadline = time.monotonic() + 60
        tester_sessions: list[str] = []
        while time.monotonic() < deadline and not tester_sessions:
            tester_sessions = [s.split()[0] for s in tester_login_sessions(ctx)]
            time.sleep(2)
        require(tester_sessions, 'held tester login session never appeared in logind')
        # Only Class=user login sessions are terminated; the lingering manager is left alone.
        for session in tester_sessions:
            root_run(ctx, ['loginctl', 'terminate-session', session], timeout=30)
    finally:
        held.kill()
        held.wait(timeout=30)
    time.sleep(10)
    require(ssh_out(ctx, 'vmadmin', ['loginctl', 'show-user', 'tester', '-p', 'Linger']).strip() == 'Linger=yes',
            'linger lost after logout')
    require(_service_show(ctx, 'MainPID') == ctx.data['main_pid'], 'controller PID changed across logout')
    require(_service_show(ctx, 'ActiveState') == 'active', 'service not active after logout')
    return f'{len(tester_sessions)} tester session(s) terminated; linger yes; same MainPID active'


def step_reboot_persistence(ctx) -> str:
    old_boot = _read_boot_id(ctx)
    root_run(ctx, ['systemctl', 'reboot'], timeout=30)
    scale = ctx.data.get('wait_factor', 1)
    new_boot = vm.wait_for_reboot(lambda: _read_boot_id(ctx), old_boot_id=old_boot, timeout=600 * scale)
    ctx.data['boot_id'] = new_boot
    ctx.ssh.wait('vmadmin', timeout=300 * scale)
    require(new_boot != old_boot, 'boot_id did not change')
    # Lingering must start the user manager and the service with no tester login.
    # Checked via vmadmin only, so no tester SSH session is opened before the proof.
    deadline = time.monotonic() + 600 * scale
    uid = ctx.data['tester_uid']
    while True:
        # Root has no user bus; run systemctl --user as tester with its runtime dir, via sudo.
        # This opens no login session (sudo -u does not create a logind session).
        state = sudo_out(ctx, ['runuser', '-u', 'tester', '--', 'env', f'XDG_RUNTIME_DIR=/run/user/{uid}',
                               'systemctl', '--user', 'show', SERVICE,
                               '-p', 'ActiveState', '--value']).strip()
        if state == 'active':
            break
        require(time.monotonic() < deadline, f'service not active after reboot (state={state})')
        time.sleep(15)
    tester_logins = tester_login_sessions(ctx)
    require(not tester_logins, f'tester login session present at reboot check: {tester_logins}')
    checked = sudo_out(ctx, ['cat', f'{MEMORY}/checked-boot'], timeout=30).strip()
    require(checked == new_boot, f'checked-boot {checked} != new boot {new_boot}')
    return 'boot_id changed; service active via lingering with no tester login; checked-boot matches'
