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

- Test product behavior and meaningful correctness/security invariants, not
  incidental implementation details, documentation wording, test-double
  internals or test scaffolding. Test infrastructure itself only where a
  concrete failure could invalidate important evidence or isolation. Prefer
  existing fixtures and standard tools over bespoke testing frameworks.
- Preserve meaningful unit, regression, negative, integration and real
  acceptance coverage. Consolidate redundant or brittle tests only under
  the authorization described in GUARDRAILS.md; explain retained coverage
  and run the existing required checks. Do not optimize for test count.

- Deterministic, isolated fixtures; no network, paid services, real GitHub,
  OpenCode, providers, production credentials or the installed controller.
- Bug fixes and security fixes include a regression test that fails before
  the fix; include negative cases (impersonation, forks, truncation, closed
  without merge, missing evidence).
- No silent skipping: tests must not `skipTest` based on environment to hide
  failures. Full suite green is required for acceptance.
- Scope: the rules above govern the regression suite wherever it runs. The
  scheduled VM suite (`vm-lifecycle.yml`) uses real packages and network but no
  secrets (see `docs/VM_TESTING.md`). Release live acceptance (see
  `.agents/RELEASE_QUALITY.md`) is a separate, explicitly invoked path with
  isolated nonproduction sandbox credentials and repositories and bounded AI
  spend. It never runs in PR jobs or with privileged secrets.

## Inherited gaps (not covered by automation)

- Installer and systemd lifecycle on fresh VMs, reboot survival, lingering.
- Live GitHub: device authorization, Project provisioning, branch protection,
  merge methods, real CI interaction, Renovate App behaviour.
- Live OpenCode/OMO sessions, provider quotas and real Engram releases.
- Release workflow publication (never triggered during development).
- Managed `update` and `rollback` against real GitHub Releases: simulated only (fake GitHub API and `gh`, local archives). Digest presence on real release assets is not verified.
- Startup readiness (`state/ready.json`): live systemd verification is not automated. Offline tests use a fake service and simulated `/proc` start ticks; they prove the health rule, not that a real controller publishes the marker under systemd.

These are tracked in `docs/VALIDATION.md` and dedicated Issues.
