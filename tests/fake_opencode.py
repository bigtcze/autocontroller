#!/usr/bin/env python3
"""Deterministic stand-in for the OpenCode V2 CLI used by onboarding preflight tests.

Answers only the read-only API contract onboarding uses. Every invocation is
appended to $FAKE_OPENCODE_LOG so tests can prove no AI session was started.
`run` (a paid model session) is always refused.
"""
import json
import os
import sys

ROLES = ('orchestrator', 'fixer', 'oracle')


def main(argv):
    log = os.environ.get('FAKE_OPENCODE_LOG')
    if log:
        with open(log, 'a', encoding='utf-8') as stream:
            stream.write(json.dumps(argv) + '\n')
    if argv[:3] == ['api', 'GET', '/api/info']:
        print(json.dumps({'data': {'version': '2.1.0'}}))
        return 0
    if len(argv) == 3 and argv[:2] == ['api', 'GET'] and argv[2].startswith('/api/agent/') \
            and argv[2].rsplit('/', 1)[1] in ROLES:
        print(json.dumps({'data': {'id': argv[2].rsplit('/', 1)[1]}}))
        return 0
    if argv[:1] == ['run']:
        sys.stderr.write('fake opencode: model sessions are refused in onboarding tests\n')
        return 3
    sys.stderr.write(f'fake opencode: unsupported invocation: {" ".join(argv)}\n')
    return 4


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
