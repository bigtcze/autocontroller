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

VERBS = {'enable', 'disable', 'restart', 'start', 'stop', 'is-active', 'status', 'daemon-reload'}
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
        valid = verb in VERBS and (verb == 'daemon-reload' and len(argv) == 2 or argv[-1] == UNIT)
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
                if verb in ACTIVATES or verb == 'enable' and '--now' in argv:
                    state['active'] = True
                elif verb in DEACTIVATES or verb == 'disable' and '--now' in argv:
                    state['active'] = False
                elif verb == 'is-active':
                    rc = 0 if state.get('active') else 3
        state['calls'].append({'argv': argv, 'rc': rc})
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix='.fake-systemctl-')
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(state, stream, sort_keys=True)
        os.replace(tmp, path)
    return rc


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
