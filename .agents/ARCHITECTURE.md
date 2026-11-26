# Architecture

## Components

| File | Role |
| --- | --- |
| `install.sh` | OS/systemd preflight, APT prerequisites, copies program files to the install root, links the user unit and CLI |
| `autocontroller.service` | systemd user unit (`NoNewPrivileges`, `UMask=0077`, private `RuntimeDirectory`) |
| `onboarding.py` | Interactive/resumable enrollment; CLI entry point |
| `autonomy-controller.py` | Main loop, `ProjectWorker` per repository, GitHub/CI/merge/release orchestration |
| `opencode-project-safe.py` | Wrapper around the OpenCode V2 CLI/API owning exactly one root session per task |
| `release_policy.py` | Pure release decisions (SemVer, approvals, maintenance eligibility, Renovate identity, release-blocker and evidence validation) |
| `release_artifacts.py` | Reproducible `autocontroller-<tag>.tar.gz` and `SHA256SUMS` from a git commit; validated extraction and verification; `qualify` CLI for the release workflow |
| `lifecycle.py` | Installed-controller lifecycle: agent guard, locks, drain handshake, verified update and rollback, remove, uninstall, status, doctor and memory inspect helpers |
| `followup.py` | Controller-authorized follow-up Issue admission: token-bound grant, `create` CLI, journal `followups.json`, idempotent steps (Issue, Project item, Todo, native dependencies), controller reconciliation without AI; the only `project item-add` writer outside onboarding. State: `followup-grant.json` (per-task token, removed when the task ends), `followups.json` (journal, each entry bound to repo, owner, project and the controller's numeric id), `followups.lock`. Identity and live-target checks run before journaling |
| `bootstrap_policy.py` | Golden Standard prompt and exact-commit contract verification |
| `memory.py` | Engram runtime manager and agent-facing learning adapter |
| `standards/`, `skills/` | Packaged trusted prompts/contracts |
| `tests/` | Offline unittest suite with simulated GitHub/OpenCode/Engram |

## Data flow

1. systemd starts `autonomy-controller.py --config <install>/config.json`.
2. The controller validates config, takes a global lock, starts Engram via
   `memory.Runtime` and one thread per enabled project.
3. Each worker: reconcile checkpoint → bootstrap gate → verified Renovate PRs →
   maintenance release check → OMO selection among Project Issues → task cycle.
4. A task cycle invokes the safe wrapper (`opencode run` as `orchestrator`),
   then polls GitHub for the linked PR, `required` checks on the exact head,
   merges without admin override, verifies push CI on the merge commit, closes
   the Issue and updates the Project.
5. Releases: owner approval comment (feature) or cadence (maintenance) → tag at
   exact SHA → native `release.yml` publishes → controller verifies.

## Storage

All outside repositories under the install root: `config.json`,
`state/projects/<name>/` (checkpoints, retry map, pending marker, logs, memory
grant), `state/memory/` (Engram generations, active pointer, failure marker),
`state/drain.request` and `state/drain.ready` (drain handshake between the lifecycle
commands and the running workers), `updates/` (staging and `history.json`),
`rollback/previous/` (the one kept earlier release), and `lifecycle.lock` /
`onboarding.lock` (operation locks).
The Engram socket lives in `/run/user/<uid>/autocontroller-memory/`.

## Deployment

User-level systemd service on a dedicated VM account. Program files are copied
by `install.sh`. The controller never updates itself. A user runs
`autocontroller update` or `rollback` from their own shell; agent sessions are
refused by an environment-marker guard (a logical guard, not a sandbox). Update
drains the workers, verifies the release, swaps the program files, and
health-checks the service; a failed check rolls back automatically. Source
checkouts (including this repository) are never executed as the production
instance.

## Trust boundaries

- **GitHub content** (Issues, PRs, comments, branch names, fork PRs): untrusted.
  Renovate work requires the verified `renovate[bot]` Bot identity and a
  same-repository head; linked/repair/release PRs from forks are ignored.
  Project membership plus a nonterminal status authorizes scheduling.
- **Agents**: run with the controller user's privileges; not sandboxed. The
  controller independently verifies GitHub state rather than trusting agent
  claims.
- **Memory adapter**: logical scoping per repository, token-bound grant; not an
  OS security boundary.
- **CI**: `required.yml` runs untrusted PR code only under `pull_request` with
  read-only `contents` permission and no secrets.

## Constraints

Stdlib only; atomic JSON writes with 0600/0700 permissions; bounded timeouts on
every subprocess; GitHub collections paginate with safety limits and fail
closed on truncation.
