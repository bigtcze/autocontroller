# Disposable VM lifecycle suite

`vmtest/` boots a fresh, pinned, official cloud image of Debian or Ubuntu inside a
QEMU guest and runs the controller's install and systemd lifecycle against it:
clean install, Engram failure and recovery, logout and reboot persistence, an Engram
upgrade rollback, a crash, and a repeated install. It exists to cover what the
offline suite and container matrix cannot: real systemd user managers, lingering,
APT installation and reboot survival.

**Disposable VM only.** The guest runs with passwordless `sudo` for the harness users.
Never run `vmtest` on a production host, on a host with an installed controller, or
against real GitHub credentials. The harness never reads or writes `~/scripts/autocontroller`.

## Status

Results come from local runs. The scheduled and manual workflow
(`.github/workflows/vm-lifecycle.yml`) has **not yet run on GitHub**. GitHub only offers
`schedule` and `workflow_dispatch` for a workflow once that file is on the default branch, so
its first green run is a post-merge follow-up, not part of this PR's checks. Until it has, treat
the suite as a local, reproducible check, not as CI evidence.

## Prerequisites

- Linux x86_64 host with `qemu-system-x86` and `qemu-utils` (`qemu-img`), `ovmf`
  (UEFI firmware), `openssh-client`, `python3` ≥ 3.11 and `git`.
- Network access on the first run, to download the pinned images (cached by digest afterwards).
  Downloads start at `https://cloud.debian.org` or `https://cloud-images.ubuntu.com`. Those hosts may
  redirect to vendor mirrors; integrity comes from the pinned digest, not from the host name.
  Any image that fails the digest is discarded.
- About 12 GB of free disk space and 2 GB of guest RAM plus host overhead.
- KVM is optional. Without `/dev/kvm`, the harness uses TCG (software emulation), which is much slower.

## Run locally

```sh
python3 -m vmtest.harness validate-manifest     # offline: validate vmtest/images.json
python3 -m vmtest.harness run --distro debian-13 \
  --artifacts /tmp/vmtest/art-debian-13 --cache ~/.cache/vmtest --accel auto
```

`--distro` is one of `debian-12`, `debian-13`, `ubuntu-24.04`, `ubuntu-26.04`.
`--accel` is `auto` (KVM if `/dev/kvm` is readable and writable, else TCG), `kvm` or `tcg`.
The exit code is 0 only if every step passed; `report.json` has the per-step detail.

### Docker recipe (used for the recorded local runs)

When the host has no QEMU, no `kvm` group membership and no `sudo`, run the suite in a
disposable container. Only the checkout is mounted read-only:

```sh
docker run --rm --device /dev/kvm \
  -v "$PWD":/src:ro -v /tmp/vmtest:/work debian:13 bash -c '
    set -euo pipefail; export DEBIAN_FRONTEND=noninteractive
    apt-get update -q
    apt-get install -y -q --no-install-recommends qemu-system-x86 qemu-utils \
      openssh-client python3 git ca-certificates ovmf
    cd /src && python3 -m vmtest.harness run --distro debian-13 \
      --artifacts /work/art-debian-13 --cache /work/cache --accel auto'
```

Drop `--device /dev/kvm` to force TCG. The harness copies the source tree at the start of
the run, so editing the checkout while a run is in progress is safe.

## Accelerator: KVM versus TCG

| | KVM | TCG |
|---|---|---|
| Requires | `/dev/kvm` readable and writable | nothing extra |
| Observed boot to SSH | 28–35 s (local KVM runs) | not measured |
| Observed full run | about 6 minutes on debian-13 run 8 (local KVM) | not measured; expected much longer |
| Boot budget | 900 s | 1800 s |

TCG is software emulation and is expected to be several times slower. Its runtime
has **not** been measured, so the 150-minute job limit may not fit a full run on
TCG. If a TCG run exceeds the limit, the job fails and the artifacts are still
uploaded. The job limit must not be raised silently. Record real TCG timings here
before relying on them.

## Scenario

Steps run in order. The first failure skips later steps. `j` always runs.

| Step | What it proves |
|---|---|
| boot | SSH as `vmadmin` answers using the pinned host key injected through NoCloud |
| transfer | The source tree (git-tracked and untracked files minus state, config and secrets) reaches `tester` |
| a preconditions | Expected OS release, PID 1 is systemd, git/gh/mise/Engram/OpenCode absent before install |
| b installer negatives | `install.sh` refuses root, bad arguments and a missing OpenCode with exit 2 |
| b fake opencode | The deterministic OpenCode stand-in is installed on PATH |
| c clean install | Full APT install of git/gh/mise, linger enabled, unit linked, `GH_TOKEN` refused with exit 1 before any GitHub contact |
| d stand-in project | Harness-written `config.json` (no `bootstrap_issue`) and stand-in project, gh installed |
| d first-install failure | Engram cannot be staged (`api.github.com` blackholed); the service fails closed, the failure marker exists, and no controller-origin gh calls happen during the window |
| d recovery | After restoring DNS, Engram is healthy, the controller runs as `tester` with `NoNewPrivileges`, and controller-origin gh calls (including `project view`) appear |
| d marker cleared | `startup()` removes the failure marker after successful startup |
| e logout | Terminates every `Class=user` login session of `tester` (a harness-held login is used); linger is kept, the controller PID is unchanged |
| f reboot | After a real reboot the boot ID changed, no tester login session is open, the service is active through lingering, and the checked-boot record matches |
| g upgrade rollback | With `api.github.com` blackholed and a newer version forced, the upgrade fails, the previous generation stays active, and the upgrade-failure metric and journal record it |
| h crash recovery | Killing the Engram process makes the controller exit; systemd restarts it, the startup finishes, and `memory --health` is healthy again |
| i repeated install | `install.sh` while active is refused; `--install-only` leaves `config.json` and `active.json` byte-identical |
| j collect | Gathers the journal, unit status, memory state, stand-in logs and cloud-init. Any failed or empty mandatory file fails the step |

## Real versus stand-in

| Component | Status in the VM |
|---|---|
| APT packages (git, python3, sudo, systemd user manager) | Real |
| GitHub CLI | Real from the vendor APT repository (`cli.github.com`) for the installer; a deterministic **stand-in** replaces the controller's `gh` on PATH after install |
| mise | Real, from the vendor APT repository (`mise.jdx.dev`) enabled via extrepo |
| systemd user manager, lingering, logout, reboot | Real |
| Engram release binary | Real, downloaded from GitHub by `memory.py` with checksum verification |
| OpenCode V2 API | **Stand-in**: answers `/api/info` and three agent endpoints, refuses `run` with exit 3, no model calls |
| GitHub API used by the controller | **Stand-in** `gh`: answers the Project and pull-request reads it uses, refuses everything else with exit 4 |
| GitHub authentication, onboarding against live GitHub | **Not exercised** |

The installer's capability probe runs `gh project link --help` and
`gh pr checks --help | grep -- --json`. The stand-in answers both, so the installer
keeps the stand-in and does not replace it with real gh from APT. The guest log
`gh-calls.log` separates installer probes (`cli` origin) from controller calls
(`svc` origin, set when `INVOCATION_ID` is present). Only `svc` lines count as
controller activity.

## Injected failures

- `api.github.com` in `/etc/hosts` (between `# vmtest-begin` and `# vmtest-end`) to force
  the first Engram install failure (step d). It is removed afterwards.
- `github.com` in `/etc/hosts` for the upgrade rollback (step g; the release download host).
  `api.github.com` stays reachable in step g. It is removed afterwards.
- Killing the Engram process with `SIGKILL` (step h).
- A real `systemctl reboot` (step f).
- `loginctl terminate-session` on `tester` login sessions (step e).
- Rewriting only the `version` field of `memory/active.json` through the guest helper
  `set_active_version.py` to force an upgrade attempt (step g). The generation pointer is never touched.

## Artifact layout

`--artifacts DIR` receives:

- `report.json`: `distro`, `accel` (resolved `kvm` or `tcg`), `qemu_version`, the
  `integration` real/stand-in lists, `overall`, and per step `name`, `status`
  (`passed`/`failed`/`skipped`), `duration` and `detail`.
- `run.json`: the resolved image entry, base image file name, accelerator and qemu version.
- `serial.log`: guest serial console (one `Linux version` line per boot).
- `qemu-stderr.log`: qemu's stderr, copied on exit.
- `failures/NN-<step>.txt`: exception and traceback for each failed step.
- `guest/`: collected guest state (journals, unit status, memory state, stand-in
  logs, install output, cloud-init log, `boot.txt`).

The harness does not scan artifacts automatically. Before sharing them, check them for
keys and tokens yourself (the recorded local runs were scanned by hand for common key and
token patterns with no hits).

## Image pin refresh

1. Pick a new official build from `cloud.debian.org` or `cloud-images.ubuntu.com`. Do not
   use other mirrors.
2. Download it and compute its digest, using the algorithm listed in `images.json`
   (SHA-512 for Debian, SHA-256 for Ubuntu). Compare it with the vendor's published checksums.
3. Update `url`, `algorithm`, `digest`, `format` and `os_release` in `vmtest/images.json`.
4. Run `python3 -m vmtest.harness validate-manifest` and `bash scripts/offline-tests.sh`.
5. Run the affected distro end to end and record the result in `docs/VALIDATION.md`.

The cache is keyed by digest, so a changed pin never reuses an old file.

## Safety

- Disposable VM only. Passwordless `sudo` exists only inside the guest, for `tester` and
  `vmadmin`; it is never configured on the host.
- Host commands are limited to `qemu-system-x86_64`, `qemu-img`, `ssh-keygen`, `ssh`, `git`
  (source archive) and one `urllib` HTTPS download of the pinned image, all as argv lists with timeouts.
- The guest SSH forward is bound to `127.0.0.1` only. QEMU has no display and no
  remote graphics. The seed server listens on `127.0.0.1` and serves one fixed
  NoCloud payload. The guest learns the seed URL from the SMBIOS serial
  `ds=nocloud-net;s=http://10.0.2.2:<port>/`. The `nocloud-net` name is required by
  cloud-init 22.4.x (debian-12); newer releases accept it as an alias.
- **Network exposure:** the user-mode network runs with `restrict=off`, so the guest can reach
  host loopback services through `10.0.2.2`. While it runs, the guest executes internet-sourced
  code with passwordless sudo. Run the suite only on a disposable host or container with no
  sensitive local services. A stricter `restrict=on` with a guestfwd for the seed server
  was not adopted, because apt and Engram downloads need general outbound access.
- Ephemeral keys are generated per run in a temporary directory. No passwords are set.
- The source archive excludes `.git`, `state/`, `config.json`, lock files, logs and `.env` files.

## Limits

- amd64 only. arm64 is not covered.
- Live GitHub authentication, Project provisioning, branch protection, real CI, and
  the Renovate App are not covered.
- Live OpenCode and OMO sessions, provider quotas and model behaviour are not covered.
- Onboarding against live GitHub is not covered. The harness writes `config.json`
  directly instead of running onboarding.
- The stand-ins are deterministic and only answer the calls the scenario needs.
- TCG timings are unmeasured (see above).
- Results are local runs; the scheduled workflow has not yet run on GitHub.
