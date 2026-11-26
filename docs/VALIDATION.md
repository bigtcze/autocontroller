# Release readiness

Target: Debian 12, Debian 13, Ubuntu 24.04 LTS, Ubuntu 26.04 LTS, systemd, amd64/arm64. No other distribution or desktop OS is in scope. A fresh VM has only a working OpenCode V2 + OMO Slim runtime, provider authorization, a sudo-capable ordinary user and network access. The installer must take care of all other controller prerequisites.

## Automated coverage (every PR and `main` push)

The `required` check (`.github/workflows/required.yml` → `regression.yml`) runs:

- Syntax: byte-compilation of all Python files, `bash -n` of shell scripts, JSON validation.
- The full offline regression suite (`scripts/offline-tests.sh`) as an unprivileged user in clean `debian:12`, `debian:13`, `ubuntu:24.04` and `ubuntu:26.04` containers with the distribution `python3`, plus `mise run ci` on the GitHub runner.
- Repository contract checks: bootstrap contract files, workflow safety (no `pull_request_target`, no secrets, pinned actions, aggregate gate), no personal paths or tokens.
- Offline E2E acceptance: scenarios 1–8 against the real controller, with GitHub and OMO agent doubles. `tests/test_e2e_controller.py` (40 tests) uses a scripted wrapper double; `tests/test_e2e_real_wrapper.py` (3 tests) runs the real `opencode-project-safe.py` against a fake OpenCode V2 CLI; `tests/test_e2e_memory_cli.py` (13 tests) runs the real `memory.cli()`, and checks the Engram backend identity. Engram is the stand-in by default, or upstream binary when `E2E_ENGRAM_BINARY` is set. The scenario→test mapping and every stand-in limit are in [E2E_TESTING.md](E2E_TESTING.md). Nothing in this suite is verified against a live service.
- Live-smoke guard and body tests (`tests/test_live_smoke_guards.py`, 37 tests; `tests/test_live_smoke_run.py`, 25 tests). They exercise `scripts/live-smoke.py` only through fake `gh`/`git`/`opencode` shims. The live smoke itself is never run by automation.
- Release artifacts and qualification, offline: `tests/test_release_artifacts.py` (archive build, validated extraction, `SHA256SUMS` and `MANIFEST.sha256` checks), `tests/test_release_qualification.py` (tag-commit qualification: green push CI, open `release-blocker` Issues, evidence validation and ancestry), and `tests/test_release_workflow.py` (`release.yml` structure and safety). `tests/test_release_policy.py` also covers the blocker and evidence rules.
- Lifecycle commands, offline: `tests/test_lifecycle_core.py` (locks, program-file swap, snapshots, `history.json`), `tests/test_lifecycle_update.py` (`update` and `rollback` with a simulated GitHub and a fake `systemctl`: digest checks, downgrade refusal, health failure and automatic rollback), `tests/test_drain.py` and `tests/test_lifecycle_drain.py` (drain handshake with the workers and the timeout), `tests/test_lifecycle_status.py` (`status` and `doctor` helpers), `tests/test_lifecycle_cli.py` (CLI wiring, agent refusal, confirmation), `tests/test_lifecycle_uninstall.py`, `tests/test_lifecycle_remove.py` (safe removal and `--delete-local-data` refusals), and `tests/test_memory_inspect.py` (read-only inspection against a fake backend).
- Simulated only: none of these modules contacts GitHub, `systemctl`, `gh` or Engram. The release and update paths use local archives, a fake GitHub API and fake `systemctl`.

The suite simulates GitHub, OpenCode and Engram: scheduling, checkpoints and recovery, CI/merge gates, Renovate identity and fork rejection, onboarding import authorization, release policy, safe-wrapper recovery against a fake OpenCode API, and Engram runtime staging with fake binaries.

Simulated onboarding (`tests/test_onboarding_integration.py`) runs the real `onboarding.py` in a disposable install root, temporary HOME and bare remotes, against fake `gh`, `opencode` and `systemctl` stand-ins. It covers the happy paths, merge-mode and permission refusals, the OAuth handoff, a fault-injected interruption at each of 16 phases with exact-state resume, duplicate prevention, the lock, preserved config and admission isolation. The simulated GitHub contract, its fault model and its unverified assumptions are listed in [ONBOARDING_TESTING.md](ONBOARDING_TESTING.md). Simulated only: the required-check gate (classic protection and rulesets, the admin-consent ruleset add, fail-closed and ambiguity cases, and lost-response resume) is covered by `tests/test_onboarding.py` and `tests/test_onboarding_integration.py` against the fake `gh`; no live GitHub response was observed.

Follow-up admission is covered offline by `tests/test_followup_admission.py` (grant token binding, the active-checkpoint authority check, strict input validation, live parent and link validation, the five-per-parent bound, numeric-id identity, idempotent create and adoption after a lost response, native dependency links, Todo admission, forged-journal refusal, controller reconciliation with per-entry backoff, and a static AST check that no executable program file except `followup.py` and `onboarding.py` contains an `item-add` literal) and by `tests/test_followup_controller.py` (the #15/#6 scenario, an interrupted `item-add`, idle wake-up, bootstrap and Renovate precedence over a blocked follow-up, grant lifecycle in `run_wrapper`, and no admission mutation without a journal). Both run against an in-memory GitHub double, which models the gh shapes followup.py reads; the native dependency endpoint shape is not verified against live GitHub, and this is a known unknown listed in PRODUCT_SCOPE.md. Follow-up admission and link validation refuse a Project with more than 1000 items (`--limit 1000` plus a `totalCount` check), and a long-blocked intent entry can hit the listing page bound; recovery is in README.md.

Scheduling: only `Todo`/`To do` (new work) and `In Progress`/`Review` (recovery) are offered to the selector; blank, `Backlog`, `Blocked`, custom and terminal statuses are refused (`tests/test_admission_and_authority.py`, `tests/test_regressions.py`).

The selector is covered for comment-only changes: an outsider comment or `updated_at` bump after a no-action decision does not start a new OMO session, while body, label and status changes do (`tests/test_agent_backlog_selection.py`, `tests/test_admission_and_authority.py`). Known limits: only the 100 most recently updated issues are read, so an Issue outside that window changes its label, state, milestone or dependencies without waking triage until the daily recheck; and the first iteration after the signature upgrade causes one deliberate re-selection per project.

Reproducible release archives were checked locally, not in CI. The orchestrator built commit `1e2339006253abf3ab73ba7dc149990a82e850f6` (built as `v0.0.1`, using that commit's own `release_artifacts.py`) in `debian:12`, `debian:13`, `ubuntu:24.04` and `ubuntu:26.04` containers and on the host. All five archives verified and had identical SHA256 digests: `21cb554e1b3d4551c2fe0193608b7000332010250852b3cfdc4d2cda74b4a4ba`. This was a local run. No `release.yml` run has happened on GitHub.

These are simulated results. They do not show that real GitHub, `gh` or OpenCode behave as the stand-ins do. Live onboarding and live selector behaviour are not performed by any automated check. Containers prove OS/Python compatibility only. They do **not** prove systemd, lingering, APT installation, reboot survival, live GitHub/OpenCode behaviour or release publication; those remain in the live acceptance below.

## Scheduled and manual VM lifecycle suite

`.github/workflows/vm-lifecycle.yml` runs `vmtest/` weekly (cron) and on manual dispatch
(optional single-distro input), one matrix job per distribution (debian-12, debian-13,
ubuntu-24.04, ubuntu-26.04), using pinned official cloud images in QEMU. It covers a
clean install, Engram failure and recovery, logout and reboot persistence, an Engram
rollback, a crash and a repeated install. See [VM_TESTING.md](VM_TESTING.md) for the
scenario, the real/stand-in split and the limits.

- **Status:** the workflow has **not yet run on GitHub**. A `schedule` or `workflow_dispatch`
  trigger only exists once the workflow file is on the default branch, so its first green run
  is a post-merge follow-up, not part of this PR's checks. Local runs of the suite are
  recorded under "Local VM evidence" below. The workflow is not a required check, and
  `required.yml` and `regression.yml` do not reference it.
- **Local VM evidence:** on the final tree (`-final3`), all four distros passed 16/16 steps
  (KVM). Each result is recorded exactly as the harness reported it.
- **Still not covered by this suite:** arm64, live GitHub authentication and onboarding,
  real GitHub Project/PR/CI behaviour, live OpenCode/OMO sessions and provider behaviour,
  release publication. The OpenCode and GitHub CLI used by the controller are stand-ins.
- **TCG:** timings without `/dev/kvm` are unmeasured; see VM_TESTING.md.

### Local VM evidence

Local runs on a KVM host (QEMU 10.0.13, docker recipe in VM_TESTING.md). Each row is the
harness `report.json` overall result on the tree at that time.

| Distro | Run | Overall | Notes |
| --- | --- | --- | --- |
| debian-12 | final3 | passed (16/16 steps) | KVM, QEMU 10.0.13; POSIX dpkg-lock and apt-unit idle wait; artifacts `art-debian-12-final3`. |
| debian-13 | final3 | passed (16/16 steps) | KVM, QEMU 10.0.13; POSIX dpkg-lock and apt-unit idle wait; artifacts `art-debian-13-final3`. The wrapper log lacks the trailing rc lines (wrapper interrupted after teardown); the result is taken from `report.json` (step j collected, teardown reached). |
| ubuntu-24.04 | final3 | passed (16/16 steps) | KVM, QEMU 10.0.13; POSIX dpkg-lock and apt-unit idle wait; artifacts `art-ubuntu-24.04-final3`. |
| ubuntu-26.04 | final3 | passed (16/16 steps) | KVM, QEMU 10.0.13; POSIX dpkg-lock and apt-unit idle wait; artifacts `art-ubuntu-26.04-final3`. |

Earlier `final2` runs (before the dpkg-lock probe fix) also passed 16/16 on all four distros.
Earlier `final` runs (before the seed string change): debian-13, ubuntu-24.04 and ubuntu-26.04
passed 16/16; debian-12 failed at boot because cloud-init 22.4.2 ignored the `ds=nocloud` seed
and fell back to `DataSourceNone`. The `nocloud-net` name fixed it. Earlier history: ubuntu-24.04
run 2 and debian-13 run 9 passed on an earlier tree; ubuntu-24.04 run 1 failed at step h on a
readiness race, since fixed in the harness.


These are local runs. The scheduled workflow has not run on GitHub.

## Release and lifecycle paths: simulated only

The following have been exercised only by the offline suite or the local reproducibility check above. None of them has been executed live:

- `release.yml` publication: qualification, the draft release, asset digest verification and publish have not run on GitHub.
- Docker reproducibility builds in CI: the container builds have run only locally, as described above.
- Container images are not pinned: `regression.yml` and `release.yml` use the tag names `debian:12`, `debian:13`, `ubuntu:24.04` and `ubuntu:26.04`, not digests, so a rebuilt tag can change the base image. Digest pinning is deferred and not implemented.
- Real `update` against GitHub Releases: no published release exists, so `update` and `rollback` have only run against the simulated GitHub API and fake `systemctl`.
- Digest presence on real release assets: GitHub's digests for uploaded assets have not been observed.

Release-specific qualification status and evidence are tracked in the GitHub release Issues; the requirements are in `.agents/RELEASE_QUALITY.md`. Evidence files for feature releases are described in [docs/releases/README.md](releases/README.md).

## Live acceptance (not yet performed)

Release-specific qualification status and evidence are tracked in the GitHub release Issues; the requirements are in `.agents/RELEASE_QUALITY.md`.

- **Live smoke (implemented, NEVER executed):** `scripts/live-smoke.py` is an opt-in, bounded check for one disposable sandbox repository (see the automated-coverage section for its tests). The agent that wrote it did not run it against GitHub or OpenCode, and no workflow references it. The owner must run it once in a disposable sandbox before anyone treats it as verified. Invocation, flow, scope and cost bound are in [E2E_TESTING.md](E2E_TESTING.md#live-smoke).

- Fresh VM per supported distribution: install with no Git, gh, mise or Engram; complete GitHub device authorization and repository URL prompt; detach SSH; confirm continued service and reboot survival.
- Exercise an initialized and an empty test repository, personal and organization ownership, non-main default branch, existing protection and multiple merge methods. Confirm the required-check gate against real GitHub: classic protection and rulesets that already enforce `required` pass without a write; an unprotected default branch with an admin account adds only the `AutoController required check` ruleset after consent; a non-admin account stops with the owner action; the rulesets API response shapes and the plan limits match the assumptions in [ONBOARDING_TESTING.md](ONBOARDING_TESTING.md) section 6 (items 19 to 22). Confirm one isolated Project per repo. Interrupt each onboarding phase and retry without duplicate Project/issue or overwriting local work. Verify scope, no PAT prompt, safe config permissions, stable identities and no accidental merge before consent.
- Observe actual OMO bootstrap: agent reads existing contracts, creates real tests/mise tasks, required PR and push workflows, Renovate dashboard config, and native release pipeline. Verify CI for exact PR + merged SHA and a real failed CI that cannot merge. Confirm missing checks do not cause an indefinite bootstrap deadlock, and a manually closed bootstrap cannot unblock work.
- Enroll a second repo while the first has in-flight work; ensure checkpoints resume and memory scopes never mix. Verify routine task, Renovate, idle no AI calls, real learned entry and confirmed reuse. Verify memory fail-closed runtime behavior and recovery, and Engram boot update with safe rollback.
- Verify release authorization, actual artifact expectations, project permissions/SSO and failure messages; run a clean unattended reboot and check GitHub state. Test package-source paths, proxy restrictions, shell PATH, externally managed OpenCode service, and systemd user environment on each target.
- Review privacy, safety, supply chain, dependency and license provenance before publishing. No uncontrolled self-update of controller code. No credentials, runtime config, state, logs or personal paths in repository.

## Current known limitations

- Startup readiness (issue #20), offline: `tests/test_controller_readiness.py` covers current vs stale `state/ready.json` markers, failed Engram or worker initialization, idle workers, readiness timeout, rollback without readiness, and the zero-enabled-projects update (validate_config refuses zero enabled projects for a running controller, so that case is an inactive service: the update succeeds, the service is never started, and no readiness is consulted). Live systemd verification of readiness on a real VM remains a gap. Readiness proves startup only, not continuous task progress.
- Readiness compatibility: a `rollback` or `rollback --recover` to a release that predates `state/ready.json` cannot pass the readiness check. It times out, the current release is restored, and the reason is named; reinstall that release instead. An update started from a pre-readiness install is verified by that install's older lifecycle, which does not require readiness.
- Health window (S8, deferred, not implemented): after an update or rollback the service must stay active for `--health-seconds`, and its controller process must publish startup readiness (`state/ready.json`). The window reads `systemctl show` once per second and requires `ActiveState=active`, `SubState=running`, a nonzero `MainPID` and the same `MainPID` and `NRestarts` as the first poll. An auto-restart, a PID change or an `activating` state fails it; unreadable output fails closed. That is process liveness plus startup readiness only, not a heartbeat: a controller that is running but stuck after startup still passes.
- Role endpoint and GitHub APIs are documented but this onboarding implementation has not been exercised against live services. Agent quality and permissions are not sandboxed. A working API and role existence do not prove the provider can complete a task.
- Existing issues are imported once; future issues must be real Issues that a maintainer adds to the Project. Issues are never enrolled automatically (by design). The controller never updates itself by design; `update` and `rollback` are user-invoked. `remove`, `uninstall` and `memory inspect` exist as offline-tested commands, but none has run against a live installation. OS certification is not done.
- Installed git/gh/mise binaries are reused; Engram updates automatically per boot. OS package updates follow APT. `--install-only` preserves runtime configuration.
- Onboarding currently assumes repository-owner Project creation rights. It refuses closed/ambiguous bootstrap or Project records. If interrupted with an unexpected dirty checkout, it stops for inspection rather than resetting or discarding user work.
- New releases use the standard default cadence and empty asset allowlist; exact required artifact names must be established for products that publish assets before public release qualification.
