#!/usr/bin/env python3
"""Launcher that runs the REAL safe wrapper `opencode-project-safe.py` for the offline E2E suite.

The controller's `run_wrapper` starts this file exactly where it would start the installed
wrapper: `python3 <this launcher> run --auto [--agent A] --title T PROMPT`, or
`--clear-unstarted-quota`, `--interrupt-owned`, `--checkpoint`. The launcher then loads
`opencode-project-safe.py` by path and calls its `main()` with the same argv.

Every wrapper code path runs unchanged: OpenCode API discovery and V2 checks, the guard
marker, the unique `[guard:...]` title, session checkpointing, the recovery turn
(`finish_recovery` -> `resume_turn`, which requires the nonce in the output), the
`--clear-unstarted-quota` verification and the exit codes (78 when blocked).

Only settle timing is shortened, and only inside this subprocess:

- `mod.QUIET_SECONDS` is set to 0 (the wrapper waits 60 s of quiet after a session);
- `time.sleep` is replaced by `min(requested, 0.05)` (the wrapper polls at 1 s and 5 s).

The OpenCode binary is `AUTONOMY_OPENCODE`, which the controller sets to the fake OpenCode V2
shim (`tests/e2e_opencode.py`). No real OpenCode, network or production path is involved.
"""
import importlib.util
import os
from pathlib import Path
import sys
import time

WRAPPER = Path(__file__).resolve().parent.parent / 'opencode-project-safe.py'
SETTLE_SLEEP_CAP = 0.05


def load_wrapper():
    spec = importlib.util.spec_from_file_location('opencode_project_safe_e2e', str(WRAPPER))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # reads AUTONOMY_* from the environment at import
    return module


def main(argv: list[str]) -> int:
    for name in ('AUTONOMY_PROJECT_ROOT', 'AUTONOMY_PROJECT_NAME', 'AUTONOMY_PENDING_MARKER',
                 'AUTONOMY_OPENCODE'):
        if not os.environ.get(name):
            print(f'E2E real-wrapper launcher: {name} is unset', file=sys.stderr)
            return 97
    real_sleep = time.sleep
    time.sleep = lambda seconds: real_sleep(min(float(seconds), SETTLE_SLEEP_CAP))
    module = load_wrapper()
    module.QUIET_SECONDS = 0
    sys.argv = [str(WRAPPER), *argv]
    module.main()
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
