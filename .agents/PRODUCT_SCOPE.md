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
- **Release artifacts** (`release_artifacts.py`, `.github/workflows/release.yml`;
  offline-tested, not yet run on GitHub): reproducible `autocontroller-<tag>.tar.gz`
  and `SHA256SUMS` built from a git commit; the tag commit is qualified (green push
  CI, no open `release-blocker` Issues, evidence for feature releases), built in the
  four supported containers, and published only after asset digest verification.
- **Follow-up admission** (`followup.py`; offline-tested, never run against real
  GitHub): an admitted Issue task may create at most 5 necessary follow-up Issues
  per parent through a token-bound CLI, admitted to `Todo` with native dependencies.
  Before anything is journaled, the parent and every link target must be a live
  open Issue of the repository in the Project with a non-blank, nonterminal Status
  (link validation only, not scheduling authority). Authorship
  is by the controller account's numeric id recorded at intent time; adoption of an
  interrupted attempt lists the repository's recent Issues (REST, paginated, `since`
  with a 24h clock-skew margin) and matches the controller account's numeric id and
  the body marker, never the login (`creator=` is not used). The controller completes unfinished follow-ups without AI
  before new selection and also defers maintenance while one is blocked. Each
  journal entry must match its worker's repository and Project or it is refused.
  Authority rule: `.agents/SECURITY.md` ("Work authorization"); this is a logical
  control, not a sandbox.
- **Known unknown:** follow-up admission and link validation refuse a Project with more
  than 1000 items (`project item-list --limit 1000` plus a `totalCount` check), and a
  long-blocked intent entry can hit the repository listing page bound; recovery is in
  the README ("Recovering a blocked or foreign follow-up").
- **Known unknown:** the native dependency endpoint
  (`repos/{repo}/issues/{n}/dependencies/blocked_by`, POST with the database `id`)
  is modelled offline only. It has not been exercised against live GitHub, so a
  real `blocks`/`blocked_by` link may behave differently. Live acceptance is required
  before this is relied on.
- **Lifecycle CLI** (`lifecycle.py`; offline-tested, never run against a real
  release): `update` and `rollback` (verified release, drain, one kept previous
  release, health check, automatic rollback), `remove`, `uninstall`, `status`,
  `doctor`, `memory inspect`, and the refusal inside agent sessions.

## Non-goals

Web UI, database service, Docker runtime requirement, own model/provider,
sandboxing agents, managing non-GitHub forges, desktop OSes or other Linux
distributions, unrestricted automatic adding of Issues to the Project (admission
is explicit; see `.agents/SECURITY.md`), bypassing protections.

## Explicit unknowns / not yet available

- Live acceptance on fresh VMs and real GitHub/OpenCode services
  (`docs/VALIDATION.md`) has not been performed.
- Managed `update` and `rollback` have not run against a real release, because no
  public release exists yet. Their GitHub download and digest checks are simulated.
- The controller never updates itself; this is by design, not a gap.
- The agent refusal in lifecycle commands is a logical guard, not a sandbox.

## Compatibility obligations

- Python ≥ 3.11 standard library only; distribution `python3` on the four
  supported OS versions.
- Config keys in `config.example.json`, the install layout
  (`~/scripts/autocontroller`), the `autocontroller.service` unit, the memory
  grant CLI used by agents, PR markers (`Autocontroller-Repair-For:`,
  `Autocontroller-Release:`), selector result lines and the aggregate check
  name `required` are interfaces of running installations; change them only
  with migration and an authorized Issue.
- Lifecycle and release interfaces: the startup readiness marker `state/ready.json`
  (bound to the controller pid and start ticks; required by update/rollback health checks),
  and the drain state files `state/drain.request`
  and `state/drain.ready` (the same names in `lifecycle.py` and
  `autonomy-controller.py`); the config keys `update_repository` and
  `release.blocker_label`; the archive layout `autocontroller-<tag>/` with its
  `VERSION` and `MANIFEST.sha256` files; the `release-blocker` label; and
  `docs/releases/<tag>.evidence.json` schema 1.
