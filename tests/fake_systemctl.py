#!/usr/bin/env python3
"""Recording stand-in for `systemctl --user` used by onboarding tests.

Only the user-unit verbs onboarding uses for autocontroller.service are
supported; anything else exits 4 (fail closed). State lives in
$FAKE_SYSTEMCTL_STATE as JSON, guarded by an fcntl lock. Faults use the same
{"match", "rc", "once", "skip"} shape as fake_gh.py and fail before any effect.
"""
import fcntl
import json
import os
import sys
import tempfile
from pathlib import Path

VERBS = {'enable', 'disable', 'restart', 'start', 'stop', 'is-active', 'status', 'daemon-reload', 'show'}
# N1: `show` models the four properties lifecycle reads. The model state lives under state['unit']:
#   {'ActiveState', 'SubState', 'MainPID', 'NRestarts'}; a test scripts an auto-restart by setting
#   ActiveState='activating', SubState='auto-restart', MainPID=0 and then NRestarts+1 and a new PID.
SHOW_PROPERTIES = ('ActiveState', 'SubState', 'MainPID', 'NRestarts')
DEFAULT_UNIT = {'ActiveState': 'inactive', 'SubState': 'dead', 'MainPID': 0, 'NRestarts': 0}
UNIT = 'autocontroller.service'
# Stateful unit: start/restart make it active, stop/disable --now make it inactive.
# is-active --quiet exits 0 only while active, 3 otherwise (systemd's convention).
ACTIVATES = {'start', 'restart'}
DEACTIVATES = {'stop'}


def in_order(tokens, argv):
    position = 0
    for token in tokens:
        while position < len(argv) and argv[position] != token:
            position += 1
        if position == len(argv):
            return False
        position += 1
    return bool(tokens)


def main(argv):
    path = Path(os.environ['FAKE_SYSTEMCTL_STATE'])
    with path.with_name(path.name + '.lock').open('a', encoding='utf-8') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'calls': [], 'faults': []}
        rc = 0
        verb = argv[1] if len(argv) >= 2 else ''
        # `show` takes the unit and then -p properties, so the unit is not always last.
        valid = verb in VERBS and (verb == 'daemon-reload' and len(argv) == 2 or UNIT in argv[2:])
        if len(argv) < 2 or argv[0] != '--user' or not valid:
            rc, message = 4, f'fake systemctl: unsupported invocation: {" ".join(argv)}'
            sys.stderr.write(message + '\n')
        elif verb == 'daemon-reload':
            pass
        else:
            for fault in state['faults']:
                if not in_order(fault['match'], argv):
                    continue
                fault['seen'] = int(fault.get('seen', 0)) + 1
                if fault['seen'] <= int(fault.get('skip', 0)) or (fault.get('once', True) and fault.get('fired')):
                    continue
                fault['fired'] = int(fault.get('fired', 0)) + 1
                rc = int(fault['rc'])
                sys.stderr.write(f'fake systemctl: injected failure for {" ".join(argv)}\n')
                break
            if rc == 0:
                unit = {**DEFAULT_UNIT, **state.get('unit', {})}
                if verb in ACTIVATES or verb == 'enable' and '--now' in argv:
                    state['active'] = True
                    # A started unit has a live process; a new PID each start keeps restarts visible.
                    unit.update(ActiveState='active', SubState='running', MainPID=int(state.get('pid', 1000)))
                elif verb in DEACTIVATES or verb == 'disable' and '--now' in argv:
                    state['active'] = False
                    unit.update(ActiveState='inactive', SubState='dead', MainPID=0)
                elif verb == 'show':
                    wanted = [a[2:] for a in argv if a.startswith('-p')]
                    if not set(wanted) <= set(SHOW_PROPERTIES):
                        rc = 1
                        sys.stderr.write(f'fake systemctl: unsupported property in {" ".join(argv)}\n')
                    else:
                        for key in wanted:
                            sys.stdout.write(f'{key}={unit[key]}\n')
                elif verb == 'is-active':
                    rc = 0 if state.get('active') else 3
                    # Plain `is-active` prints the state word, as systemd does; --quiet prints nothing.
                    if '--quiet' not in argv:
                        sys.stdout.write(('active' if state.get('active') else 'inactive') + '\n')
                if verb != 'show':
                    state['unit'] = unit
        state['calls'].append({'argv': argv, 'rc': rc})
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix='.fake-systemctl-')
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(state, stream, sort_keys=True)
        os.replace(tmp, path)
    return rc


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
