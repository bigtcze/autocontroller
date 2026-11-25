# Product scope

Status: unreleased; under development. No public release has been published.

## Implemented and authorized

- **Installer** (`install.sh`): Debian 12/13, Ubuntu 24.04/26.04 with systemd;
  installs Git, Python, GitHub CLI, mise from distribution/vendor APT sources;
  installs the user service; `--install-only` updates program files only.
- **Onboarding** (`onboarding.py`, CLI `autocontroller`): GitHub CLI device
  authorization, managed checkout, one dedicated GitHub Project per repository,
  one-time import of existing open Issues authored by the owner, members or
  collaborators, mandatory bootstrap Issue, config write, service enablement;
  `status`, `logs`, `doctor`, `memory`, `start`, `stop`, `add`.
- **Controller** (`autonomy-controller.py`): one worker per repository, one
  active task per repository; checkpoint recovery; mandatory bootstrap gate;
  verified Renovate PRs first; OMO-driven backlog selection from Project
  members only; persistent retry/quota backoff; exact-SHA PR and merged-commit
  CI verification; repair PRs; Project status sync.
- **Safe wrapper** (`opencode-project-safe.py`): runs one OpenCode session per
  task, durable pending markers, crash/restart recovery of the owned session.
- **Release policy** (`release_policy.py`): stable SemVer, feature-release owner
  approval bound to tag+SHA, technical maintenance cadence after verified
  Renovate updates, native tag-triggered publication verification.
- **Memory** (`memory.py`): controller-owned Engram runtime with staged,
  checksum-verified updates; scoped, bounded learning adapter for agents.
- **Bootstrap policy** (`bootstrap_policy.py`, `standards/GOLDEN_STANDARD.md`):
  versioned repository contract verified at the exact merged commit.

## Non-goals

Web UI, database service, Docker runtime requirement, own model/provider,
sandboxing agents, managing non-GitHub forges, desktop OSes or other Linux
distributions, unrestricted automatic adding or enrollment of Issues into the
Project (admission is an explicit maintainer action; onboarding's one-time
import of owner/member/collaborator Issues is the only exception), bypassing
protections.

## Explicit unknowns / not yet available

- Live acceptance on fresh VMs and real GitHub/OpenCode services
  (`docs/VALIDATION.md`) has not been performed.
- Release archives, checksums, managed stable update with drain/rollback
  (including controller self-update), project removal, uninstall and memory
  inspection are not implemented (tracked in GitHub Issues).

## Compatibility obligations

- Python ≥ 3.11 standard library only; distribution `python3` on the four
  supported OS versions.
- Config keys in `config.example.json`, the install layout
  (`~/scripts/autocontroller`), the `autocontroller.service` unit, the memory
  grant CLI used by agents, PR markers (`Autocontroller-Repair-For:`,
  `Autocontroller-Release:`), selector result lines and the aggregate check
  name `required` are interfaces of running installations; change them only
  with migration and an authorized Issue.
