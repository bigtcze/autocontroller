# Release readiness

Target: Debian 12, Debian 13, Ubuntu 24.04 LTS, Ubuntu 26.04 LTS, systemd, amd64/arm64. No other distribution or desktop OS is in scope. A fresh VM has only a working OpenCode V2 + OMO Slim runtime, provider authorization, a sudo-capable ordinary user and network access. The installer must take care of all other controller prerequisites.

## Automated coverage (every PR and `main` push)

The `required` check (`.github/workflows/required.yml` → `regression.yml`) runs:

- Syntax: byte-compilation of all Python files, `bash -n` of shell scripts, JSON validation.
- The full offline regression suite (`scripts/offline-tests.sh`) as an unprivileged user in clean `debian:12`, `debian:13`, `ubuntu:24.04` and `ubuntu:26.04` containers with the distribution `python3`, plus `mise run ci` on the GitHub runner.
- Repository contract checks: bootstrap contract files, workflow safety (no `pull_request_target`, no secrets, pinned actions, aggregate gate), no personal paths or tokens.

The suite simulates GitHub, OpenCode and Engram: scheduling, checkpoints and recovery, CI/merge gates, Renovate identity and fork rejection, onboarding import authorization, release policy, safe-wrapper recovery against a fake OpenCode API, and Engram runtime staging with fake binaries. Containers prove OS/Python compatibility only. They do **not** prove systemd, lingering, APT installation, reboot survival, live GitHub/OpenCode behaviour or release publication; those remain in the live acceptance below.

## Scheduled and manual VM lifecycle suite

`.github/workflows/vm-lifecycle.yml` runs `vmtest/` weekly (cron) and on manual dispatch
(optional single-distro input), one matrix job per distribution (debian-12, debian-13,
ubuntu-24.04, ubuntu-26.04), using pinned official cloud images in QEMU. It covers a
clean install, Engram failure and recovery, logout and reboot persistence, an Engram
rollback, a crash and a repeated install. See [VM_TESTING.md](VM_TESTING.md) for the
scenario, the real/stand-in split and the limits.

- **Status:** the workflow has **not yet run on GitHub**. Local runs of the suite are
  recorded under "Local VM evidence" below. The workflow is not a required check, and
  `required.yml` and `regression.yml` do not reference it.
- **Local VM evidence:** the debian-13 scenario passed end to end (KVM) in one local run;
  the ubuntu-24.04 run is recorded below. Each run's pass or fail detail is recorded
  exactly as the harness reported it.
- **Still not covered by this suite:** arm64, live GitHub authentication and onboarding,
  real GitHub Project/PR/CI behaviour, live OpenCode/OMO sessions and provider behaviour,
  release publication. The OpenCode and GitHub CLI used by the controller are stand-ins.
- **TCG:** timings without `/dev/kvm` are unmeasured; see VM_TESTING.md.

### Local VM evidence

Local runs on a KVM host (QEMU 10.0.13, docker recipe in VM_TESTING.md). Each row is the
harness `report.json` overall result on the tree at that time.

| Distro | Run | Overall | Notes |
| --- | --- | --- | --- |
| ubuntu-24.04 | r2 | passed (16/16 steps) | Run 1 failed at step h because of a readiness race in the harness, since fixed; run 2 is on the final tree. |
| debian-13 | r8 | passed (16/16 steps) | Predates the step-h readiness wait and the gh method tightening. |
| debian-13 | r9 | passed (16/16 steps) | Final tree; KVM, QEMU 10.0.13. |
| debian-12, ubuntu-26.04 | — | not run locally | Covered only by the scheduled workflow once it has run on GitHub. |

These are local runs. The scheduled workflow has not run on GitHub.

## Live acceptance (not yet performed)

- Fresh VM per supported distribution: install with no Git, gh, mise or Engram; complete GitHub device authorization and repository URL prompt; detach SSH; confirm continued service and reboot survival.
- Exercise an initialized and an empty test repository, personal and organization ownership, non-main default branch, existing protection and multiple merge methods. Confirm one isolated Project per repo. Interrupt each onboarding phase and retry without duplicate Project/issue or overwriting local work. Verify scope, no PAT prompt, safe config permissions, stable identities and no accidental merge before consent.
- Observe actual OMO bootstrap: agent reads existing contracts, creates real tests/mise tasks, required PR and push workflows, Renovate dashboard config, and native release pipeline. Verify CI for exact PR + merged SHA and a real failed CI that cannot merge. Confirm missing checks do not cause an indefinite bootstrap deadlock, and a manually closed bootstrap cannot unblock work.
- Enroll a second repo while the first has in-flight work; ensure checkpoints resume and memory scopes never mix. Verify routine task, Renovate, idle no AI calls, real learned entry and confirmed reuse. Verify memory fail-closed runtime behavior and recovery, and Engram boot update with safe rollback.
- Verify release authorization, actual artifact expectations, project permissions/SSO and failure messages; run a clean unattended reboot and check GitHub state. Test package-source paths, proxy restrictions, shell PATH, externally managed OpenCode service, and systemd user environment on each target.
- Review privacy, safety, supply chain, dependency and license provenance before publishing. No uncontrolled self-update of controller code. No credentials, runtime config, state, logs or personal paths in repository.

## Current known limitations

- Role endpoint and GitHub APIs are documented but this onboarding implementation has not been exercised against live services. Agent quality and permissions are not sandboxed. A working API and role existence do not prove the provider can complete a task.
- Existing issues imported once; future issues must be real Issues added to the Project. No automatic enrollment of future issues outside it yet. No automated controller self-update, removal command, uninstaller, OS certification or memory browser yet.
- Installed git/gh/mise binaries are reused; Engram updates automatically per boot. OS package updates follow APT. `--install-only` preserves runtime configuration.
- Onboarding currently assumes repository-owner Project creation rights. It refuses closed/ambiguous bootstrap or Project records. If interrupted with an unexpected dirty checkout, it stops for inspection rather than resetting or discarding user work.
- New releases use the standard default cadence and empty asset allowlist; exact required artifact names must be established for products that publish assets before public release qualification.
