#!/usr/bin/env python3
"""Launcher that runs the REAL memory.cli() for the offline E2E suite.

The scripted agent (tests/e2e_agent.py) runs this file where a session would run
`python3 <repo>/memory.py --grant G --token T ACTION [QUERY]`. It is the only learning CLI the
E2E suite uses.

Only two things are redirected:

- `memory.ROOT` is set to $E2E_MEMORY_ROOT (the world's temp directory). The CLI requires the
  grant to live under `ROOT/state`, so this makes `ROOT/state` the world state directory.
- `memory.socket_path()` returns $E2E_ENGRAM_SOCKET, the private stand-in socket. The launcher
  refuses an unset value, a value that resolves under /run/user/, and a missing ROOT.

Everything else is the product code, unchanged: grant path shape, token comparison, selection
refusal, the `failure` marker, the 4096-byte stdin cap, the JSON-object check, the
`memory.operation()` budgets, the metrics counters, and the exception mapping of memory.py's
`__main__` (MemoryError, ValueError, KeyError, TypeError, OSError -> message on stderr, exit 1).
"""
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))  # memory.py lives at the repository root


LAUNCHER_REFUSED = 3  # distinct from memory.cli() exit 1 and argparse exit 2


def refuse(reason: str) -> int:
    print(f'E2E memory launcher refused: {reason}', file=sys.stderr)
    return LAUNCHER_REFUSED


def main() -> int:
    import memory

    root = os.environ.get('E2E_MEMORY_ROOT', '')
    sock = os.environ.get('E2E_ENGRAM_SOCKET', '')
    if not root:
        return refuse('E2E_MEMORY_ROOT is unset')
    if not sock:
        return refuse('E2E_ENGRAM_SOCKET is unset')
    if str(Path(sock).absolute()).startswith('/run/user/') or str(Path(sock).resolve()).startswith('/run/user/'):
        return refuse('E2E_ENGRAM_SOCKET resolves under /run/user/ (production socket)')
    memory.ROOT = Path(root).resolve()  # the controller writes resolved grant paths
    memory.socket_path = lambda: Path(sock)
    try:
        memory.cli()
    except (memory.MemoryError, ValueError, KeyError, TypeError, OSError) as exc:
        print(f'Memory operation refused: {exc}', file=sys.stderr)  # same mapping as memory.py __main__
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
