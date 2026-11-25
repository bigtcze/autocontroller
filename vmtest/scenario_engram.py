"""Scenario steps g–i (Engram upgrade rollback, crash recovery, repeated install) and
step j (log collection, always runs). Companion of scenario.py / scenario_persist.py.
"""
from __future__ import annotations

import hashlib
import json
import time

from .runner import ScenarioAbort
from .scenario import (GUEST_HOME, INSTALL, REPO_SRC, SERVICE, require, root_run, save, ssh_out,
                       sudo_out)
from .scenario_persist import (MEMORY, _hosts_blackhole, _hosts_restore, _service_show)

GH_HOSTS = ['github.com']  # release download host; api.github.com stays reachable


def _sha256(ctx, path: str) -> str:
    return sudo_out(ctx, ['sha256sum', path], timeout=60).split()[0]


def _wait_service(ctx, want_state: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    state = ''
    while time.monotonic() < deadline:
        state = _service_show(ctx, 'ActiveState')
        if state == want_state:
            return
        time.sleep(5)
    raise AssertionError(f'service did not reach {want_state} (last {state!r}) within {timeout:.0f}s')


def _upgrade_failures(ctx) -> int:
    """upgrade_failure_total from metrics.json (0 when the file has no such key)."""
    metrics = ssh_out(ctx, 'tester', ['cat', f'{MEMORY}/metrics.json']).strip()
    return int(json.loads(metrics).get('upgrade_failure_total', 0))


def step_engram_upgrade_rollback(ctx) -> str:
    """Force an upgrade attempt: latest() succeeds, staging fails (github.com blackholed)."""
    ctx.ssh.run('tester', ['systemctl', '--user', 'stop', SERVICE], timeout=180, check=False)
    _wait_service(ctx, 'inactive', 200)
    active_path = f'{MEMORY}/active.json'
    original = ssh_out(ctx, 'tester', ['cat', active_path]).strip()
    generation_before = json.loads(original)['generation']
    ctx.data['upgrade_original_active'] = original
    helper = (ctx.repo_root / 'vmtest' / 'guest' / 'set_active_version.py').read_bytes()
    ctx.ssh.run('tester', ['mkdir', '-p', f'{GUEST_HOME}/.local/state/vmtest'], timeout=30)
    ctx.ssh.run('tester', ['sh', '-c', f'cat > {GUEST_HOME}/.local/state/vmtest/set_active_version.py'],
                timeout=60, stdin=helper)
    ctx.ssh.run('tester', ['sh', '-c', f'rm -f {MEMORY}/checked-boot'], timeout=30)
    ctx.ssh.run('tester', ['python3', f'{GUEST_HOME}/.local/state/vmtest/set_active_version.py',
                           active_path, 'v0.0.1'], timeout=60)
    failures_before = _upgrade_failures(ctx)
    # Guest clock at step start bounds the journal window, so older lines cannot satisfy the checks.
    since = ssh_out(ctx, 'tester', ['date', '+%Y-%m-%d %H:%M:%S']).strip()
    _hosts_blackhole(ctx, GH_HOSTS)
    try:
        ctx.ssh.run('tester', ['systemctl', '--user', 'start', SERVICE], timeout=120)
        time.sleep(60 * ctx.data.get('wait_factor', 1))
    finally:
        _hosts_restore(ctx)
    _wait_service(ctx, 'active', 300)
    after = ssh_out(ctx, 'tester', ['cat', active_path]).strip()
    require(json.loads(after)['generation'] == generation_before, 'active generation changed on failed upgrade')
    gens = ssh_out(ctx, 'tester', ['sh', '-c', f'ls -1 {MEMORY} | grep -c "^gen-" || true']).strip()
    require(gens in ('0', '1'), f'leftover gen-* directories: {gens}')
    failures_after = _upgrade_failures(ctx)
    require(failures_after - failures_before == 1,
            f'upgrade_failure_total delta {failures_after - failures_before}, expected exactly +1')
    journal = ssh_out(ctx, 'tester', ['sh', '-c', f'journalctl --user -u {SERVICE} --no-pager --since "{since}" || true'])
    require('Engram upgrade failed; previous generation required' in journal,
            'upgrade attempt (rollback journal line) missing inside step window')
    save(ctx, 'journal-upgrade-rollback.txt', journal)
    # Restore the original pin; the version is re-checked on next boot.
    ctx.ssh.run('tester', ['python3', f'{GUEST_HOME}/.local/state/vmtest/set_active_version.py',
                           active_path, json.loads(original)['version']], timeout=60)
    return f'upgrade failed as designed; generation {generation_before} unchanged; metrics and journal recorded'


def _wait_engram_ready(ctx, *, pid: str, deadline: float) -> None:
    """Bounded wait until the restarted controller (MainPID `pid`) logged 'Engram ready' and
    cleared the marker. Filtering on _PID keeps old lines from earlier processes from counting.
    The marker check goes through sudo (state is in the mode-700 tester home)."""
    last = 'no output yet'
    while time.monotonic() < deadline:
        marker = sudo_out(ctx, ['sh', '-c', f'test -e {MEMORY}/failure && echo yes || echo no'],
                          timeout=30).strip()
        ready = sudo_out(ctx, ['sh', '-c', f'journalctl _PID={pid} --no-pager '
                                           f'| grep -c "Engram ready" || true'], timeout=60).strip()
        last = f'marker={marker} ready_lines_recent={ready}'
        if marker == 'no' and ready not in ('', '0'):
            return
        time.sleep(5)
    raise ScenarioAbort(f'Engram not ready after restart within bound: {last}')


def step_engram_crash_recovery(ctx) -> str:
    """Kill the Engram process; controller must exit non-zero and systemd must restart it."""
    before_pid = _service_show(ctx, 'MainPID')
    before_restarts = int(_service_show(ctx, 'NRestarts') or 0)
    engram_pid = ssh_out(ctx, 'tester', ['pgrep', '-u', 'tester', '-x', 'engram']).split()[0:1]
    engram_pid = engram_pid[0] if engram_pid else ''
    require(engram_pid.isdigit(), 'no engram serve process found')
    ctx.ssh.run('tester', ['kill', '-KILL', engram_pid], timeout=30)
    scale = ctx.data.get('wait_factor', 1)
    deadline = time.monotonic() + 600 * scale
    while time.monotonic() < deadline:
        pid = _service_show(ctx, 'MainPID')
        restarts = int(_service_show(ctx, 'NRestarts') or 0)
        if restarts > before_restarts and _service_show(ctx, 'ActiveState') == 'active' and pid not in ('', '0', before_pid):
            break
        time.sleep(15)
    else:
        raise AssertionError('controller not restarted after Engram crash')
    require(restarts >= before_restarts + 1, 'NRestarts did not increase')
    # Readiness race: MainPID and ActiveState change at ExecStart, but memory.py clears the
    # failure marker only after startup() finishes. Wait for that completion (logged as
    # "Engram ready" after the restart, and no marker) before probing health.
    _wait_engram_ready(ctx, pid=pid, deadline=time.monotonic() + 300 * scale)
    health = ssh_out(ctx, 'tester', ['sh', '-c', f'{GUEST_HOME}/.local/bin/autocontroller memory'], timeout=120)
    require('"status": "ok"' in health or '"status":"ok"' in health, 'Engram not healthy after recovery')
    ctx.data['main_pid_after_crash'] = pid
    return f'Engram killed; controller restarted (MainPID {before_pid} -> {pid}, NRestarts {restarts})'


def step_repeated_install(ctx) -> str:
    """install.sh while active is refused; --install-only leaves config/active untouched."""
    cfg = f'{INSTALL}/config.json'
    proc = ctx.ssh.run('tester', ['sh', '-c', f'cd {REPO_SRC} && bash install.sh </dev/null'], timeout=120, check=False)
    require(proc.returncode == 2 and b'Stop the existing AutoController' in proc.stderr,
            f'install while active: rc={proc.returncode}, expected 2 with stop message')
    ctx.ssh.run('tester', ['systemctl', '--user', 'stop', SERVICE], timeout=180, check=False)
    _wait_service(ctx, 'inactive', 200)
    cfg_before = _sha256(ctx, cfg)
    active_before = _sha256(ctx, f'{MEMORY}/active.json')
    proc = ctx.ssh.run('tester', ['sh', '-c', f'cd {REPO_SRC} && bash install.sh --install-only </dev/null'],
                       timeout=600, check=False)
    require(proc.returncode == 0, f'--install-only exited {proc.returncode}')
    require(_sha256(ctx, cfg) == cfg_before, 'config.json changed by --install-only')
    require(_sha256(ctx, f'{MEMORY}/active.json') == active_before, 'active.json changed by --install-only')
    require(_service_show(ctx, 'ActiveState') == 'inactive', '--install-only started the service')
    proc = ctx.ssh.run('tester', ['sh', '-c', f'cd {REPO_SRC} && bash install.sh </dev/null'], timeout=900, check=False)
    save(ctx, 'install-repeat-final.stdout.txt', proc.stdout.decode('utf-8', 'replace'))
    require(proc.returncode == 0, f'full repeated install exited {proc.returncode}')
    require(b'Existing configuration found' in proc.stdout, 'full repeat did not take existing-config path')
    _wait_service(ctx, 'active', 300)
    require(_sha256(ctx, cfg) == cfg_before, 'config.json changed by repeated install')
    return 'running install refused (exit 2); --install-only left config/active.json untouched; repeat install active'


def _user_bus(ctx, *args: str) -> list[str]:
    """systemctl --user as tester via runuser, with its runtime dir (root has no user bus)."""
    uid = ctx.data.get('tester_uid', 1000)
    return ['runuser', '-u', 'tester', '--', 'env', f'XDG_RUNTIME_DIR=/run/user/{uid}',
            'systemctl', '--user', *args]


def _collect(ctx, name: str, argv: list[str], *, mandatory: bool, notes: list[str]) -> None:
    """Run one collection command as vmadmin+sudo. Record rc, stderr and empty mandatory output
    in the artifact and in the step notes; never silently accept a failed read."""
    proc = ctx.ssh.run('vmadmin', ['sudo', '-n', *argv], timeout=120, check=False)
    out = proc.stdout.decode('utf-8', 'replace')
    err = proc.stderr.decode('utf-8', 'replace').strip()
    save(ctx, name, out + (f'\n[collector rc={proc.returncode} stderr: {err[-400:]}]\n' if proc.returncode or err else ''))
    if proc.returncode != 0:
        notes.append(f'{name}: rc={proc.returncode}')
    elif mandatory and not out.strip():
        notes.append(f'{name}: empty')


def step_collect_logs(ctx) -> str:
    """Always runs. Every read of tester-owned data goes through sudo; failures are reported in
    the step detail (and the step fails if a mandatory file is missing or empty)."""
    notes: list[str] = []
    t = ctx.data.get('tester_uid', 1000)
    plan = [
        # User-manager units log with _SYSTEMD_USER_UNIT, not the system unit name.
        ('journal-user-unit.txt', ['journalctl', f'_SYSTEMD_USER_UNIT={SERVICE}', '--no-pager', '-n', '2000'], True),
        ('journal-tester-uid.txt', ['journalctl', f'_UID={t}', '--no-pager', '-n', '2000'], True),
        ('systemctl-user-status.txt', _user_bus(ctx, 'status', SERVICE, '--no-pager'), True),
        ('memory-state.txt', ['sh', '-c', f'ls -la {MEMORY}; for f in metrics.json checked-boot active.json failure; '
                                          f'do echo "== $f"; if [ -e {MEMORY}/$f ]; then cat {MEMORY}/$f; '
                                          f'else echo "(absent)"; fi; done; true'], True),
        ('gh-calls.log', ['cat', f'{GUEST_HOME}/.local/state/vmtest/gh-calls.log'], False),
        ('opencode-calls.log', ['cat', f'{GUEST_HOME}/.local/state/vmtest/opencode-calls.log'], False),
        ('cloud-init.log', ['sh', '-c', 'cat /var/log/cloud-init.log /var/log/cloud-init-output.log'], True),
        ('boot.txt', ['sh', '-c', 'cat /proc/sys/kernel/random/boot_id; uptime; systemctl --failed --no-pager'], False),
    ]
    for name, argv, mandatory in plan:
        _collect(ctx, name, argv, mandatory=mandatory, notes=notes)
    if notes:
        raise ScenarioAbort('collection incomplete: ' + '; '.join(notes))
    return 'collected all artifacts; no collection errors'
