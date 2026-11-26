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

VERBS = {'enable', 'disable', 'restart', 'start', 'stop', 'is-active', 'status'}
UNIT = 'autocontroller.service'


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
        if len(argv) < 3 or argv[0] != '--user' or argv[1] not in VERBS or argv[-1] != UNIT:
            rc, message = 4, f'fake systemctl: unsupported invocation: {" ".join(argv)}'
            sys.stderr.write(message + '\n')
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
        state['calls'].append({'argv': argv, 'rc': rc})
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix='.fake-systemctl-')
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(state, stream, sort_keys=True)
        os.replace(tmp, path)
    return rc


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
