# Test strategy

## Commands

```sh
mise run ci                       # = syntax + test
bash scripts/check-syntax.sh      # py_compile all Python, bash -n, JSON validation
bash scripts/offline-tests.sh     # full unittest suite, hermetic
```

`scripts/offline-tests.sh` requires Python ≥ 3.11, uses a temporary `HOME` and
Git config, removes GitHub/provider tokens, and puts failing shims for `gh`,
`opencode`, `systemctl`, `loginctl`, `sudo`, `curl` and `engram` first on
`PATH`, so an accidental real call fails loudly instead of touching services.

## Layers

- **Unit / policy**: pure functions (`release_policy`, `bootstrap_policy`,
  selection verdicts, Renovate identity, config validation).
- **Simulated integration**: `ProjectWorker` cycles with patched GitHub
  responses; real temporary Git repositories; the real safe wrapper against a
  fake OpenCode API process; Engram runtime with fake binaries and temporary
  SQLite stores and a private temporary socket.
- **Repository contracts**: `tests/test_repository_contracts.py` checks this
  repository's own bootstrap contract files, workflow safety properties
  (no `pull_request_target`, no secrets, pinned actions, aggregate `required`)
  and absence of personal paths/tokens.
- **CI matrix**: `.github/workflows/regression.yml` runs syntax checks, the
  suite as an unprivileged user in clean `debian:12`, `debian:13`,
  `ubuntu:24.04`, `ubuntu:26.04` containers, and `mise run ci`. Containers
  prove OS/Python compatibility of the code and tests only; they are **not** a
  systemd/VM installation test.

## Rules

- Deterministic, isolated fixtures; no network, paid services, real GitHub,
  OpenCode, providers, production credentials or the installed controller.
- Bug fixes and security fixes include a regression test that fails before
  the fix; include negative cases (impersonation, forks, truncation, closed
  without merge, missing evidence).
- No silent skipping: tests must not `skipTest` based on environment to hide
  failures. Full suite green is required for acceptance.

## Inherited gaps (not covered by automation)

- Installer and systemd lifecycle on fresh VMs, reboot survival, lingering.
- Live GitHub: device authorization, Project provisioning, branch protection,
  merge methods, real CI interaction, Renovate App behaviour.
- Live OpenCode/OMO sessions, provider quotas and real Engram releases.
- Release workflow publication (never triggered during development).

These are tracked in `docs/VALIDATION.md` and dedicated Issues.
