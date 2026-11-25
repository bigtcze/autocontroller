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
- **Disposable VM lifecycle (`vmtest/`)**: boots pinned official Debian/Ubuntu
  cloud images in QEMU (KVM, or TCG as a slower fallback) and runs the install,
  systemd user manager, lingering, reboot, Engram failure/rollback/crash and
  repeated-install scenario against real APT, systemd and a real Engram binary,
  with deterministic OpenCode and GitHub CLI stand-ins. It runs only in the
  scheduled/manual workflow `.github/workflows/vm-lifecycle.yml`, never as a
  required check, and only on disposable VMs. Its harness logic is unit-tested
  offline (`tests/test_vm_harness.py`), and workflow safety is checked by
  `tests/test_repository_contracts.py`. See [docs/VM_TESTING.md](../docs/VM_TESTING.md).
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

- Installer and systemd lifecycle on fresh VMs, reboot survival, lingering: covered
  only by the local/scheduled VM suite (`vmtest/`), which has not yet run on GitHub
  and whose stand-ins replace OpenCode and the GitHub CLI. arm64 is not covered.
- Live GitHub: device authorization, Project provisioning, branch protection,
  merge methods, real CI interaction, Renovate App behaviour.
- Live OpenCode/OMO sessions and provider quotas.
- Real Engram releases: the VM suite exercises first install, upgrade-failure rollback
  (download blackholed) and crash recovery with a real Engram binary. It does not perform
  a real upgrade between two real Engram versions, which remains a gap.
- Release workflow publication (never triggered during development).

These are tracked in `docs/VALIDATION.md` and dedicated Issues.
