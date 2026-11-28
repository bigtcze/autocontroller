# End-to-end acceptance testing

Status:

- **The offline E2E suite is implemented and passes.** It is automated and runs
  in `scripts/offline-tests.sh`. It exercises the real `autonomy-controller.py`
  `ProjectWorker` against stand-ins for GitHub, the OMO/OpenCode wrapper and the
  Engram service. Nothing in it touches a live service.
- **The live smoke (`scripts/live-smoke.py`) is implemented, including its body,
  and has NEVER been executed against a real service.** Its tests only use fake
  `gh`, `git` and `opencode` shims. The agent that wrote it did not run it, and no
  workflow references it. The owner must run it once in a disposable sandbox
  before anyone treats the live path as verified.

This document maps each Issue #4 acceptance area to what the offline suite runs,
what it simulates, and what has been verified against an actual service (nothing).

## What runs, and how

Run the whole offline suite (the required entry point):

```sh
bash scripts/offline-tests.sh
```

It runs `python3 -m unittest discover -s tests -v` with an isolated temporary
`HOME`, no credentials and blocking shims for `gh`, `opencode`, `systemctl`,
`loginctl`, `sudo`, `curl` and `engram`.

To run one module by hand, use a temporary `HOME` and discover mode. Do not run
`python3 -m unittest tests.<module>`: `tests/` is not a package and is not on
`sys.path`, so that form fails. Without the shims in `scripts/offline-tests.sh`,
a bare run does not block `gh` or `opencode`, so use a temporary `HOME` and
`GIT_CONFIG_GLOBAL=/dev/null`:

```sh
H=$(mktemp -d /tmp/e2e-home-XXXX)
env HOME="$H" GIT_CONFIG_GLOBAL=/dev/null python3 -m unittest discover -s tests -p test_e2e_controller.py
rm -rf -- "$H"
```

| Module | Role |
| --- | --- |
| `tests/test_e2e_controller.py` | 46 scenario, guard and hardening tests driving the real `ProjectWorker`; the default World runs the scripted agent double as the wrapper |
| `tests/test_e2e_real_wrapper.py` | 3 tests driving the controller through the **real safe wrapper** `opencode-project-safe.py` (happy path with one resume and one exact-SHA merge, learning-instruction routing, unstarted-quota recovery) |
| `tests/test_e2e_memory_cli.py` | 13 tests running the **real `memory.cli()`** in a subprocess (refusals, stdin caps, failure marker, metrics counters, dead-backend marker) and the Engram backend identity probe |
| `tests/e2e_world.py` | Disposable world: temp state, bare remotes, real git clones, transport patch, memory socket guard, external-access guard. `World(tmp, real_wrapper=True)` selects the real wrapper; `E2E_ENGRAM_BINARY` selects the Engram binary |
| `tests/e2e_github.py` | Stateful, multi-repo, thread-safe **GitHub service double** patched at the `gh` argv level (loaded from `autonomy-controller.py` by path) |
| `tests/e2e_agent.py` | **Scripted step engine** (`perform_step`), shared by the scripted wrapper double and the fake OpenCode. Learning operations go through the real `memory.cli()` launcher |
| `tests/e2e_real_wrapper.py` | Launcher that loads `opencode-project-safe.py` by path and runs its `main()`. Only settle timing is shortened: `QUIET_SECONDS=0`, `time.sleep` capped at 0.05 s, and the `--interrupt-owned` drain deadline (`INTERRUPT_DRAIN_SECONDS`, 30 s in the product) set to 2 s |
| `tests/e2e_opencode.py` | **Fake OpenCode V2 CLI** used only by the real wrapper: `api` discovery, session list/get, interrupt, `run --title`, `run --session`, and a stateful active-session model (owned root, nested descendants, foreign roots, poll-driven lifetimes, interrupts, persisted API faults). Unmodelled forms exit 97 |
| `tests/test_e2e_active_sessions.py` | 15 tests on the real wrapper and real controller for owned-root adoption, descendant settling, interruption and restart, foreign and ambiguous refusal, API outage and `--interrupt-owned`. Described in the "Wrapper active-session paths" row below |
| `tests/e2e_memory_cli.py` | Launcher that imports `memory` from the repository root and calls the real `memory.cli()`. Only `memory.ROOT` and `memory.socket_path` are redirected; it refuses an unset socket or one under `/run/user/` |
| `tests/e2e_engram_standin.py` | **Engram stand-in** (not upstream Engram; the default backend): a SQLite-backed HTTP-over-Unix-socket server launched via the real `memory.Runtime.start_process` |

Every E2E test runs against a private temporary state directory. The memory
socket is replaced for the whole test by a path under a 0700 temp directory.
`E2EBase` also refuses, at the socket layer, any `connect` to a path under
`/run/user/`, and refuses `memory.Runtime.download`. Both are asserted by
`MemorySocketGuardTests`.

Incident during development of this PR: a test helper connected once (connect
only, no request) to the production socket path
`/run/user/<uid>/autocontroller-memory/engram.sock`. It was fixed by patching the
memory socket path to a private 0700 directory for every E2E test
(`guard_memory_socket`), and by refusing at the socket layer any `connect` to a
`/run/user/` AF_UNIX path (`guard_external_access`). Tripwires that keep this
from recurring: `MemorySocketGuardTests` asserts the resolved socket path is
private, that a `connect` to `/run/user/<current uid>/...` raises before any I/O,
that `Runtime.download` is refused, and that the agent's memory double refuses a
missing or `/run/user/` socket. The probe paths are built from `os.getuid()`, so
the tripwire also fires under a UID other than the developer's.

## Scenario to test mapping

Each Issue #4 scenario, the tests that cover it, and what is simulated.

| Scenario | Tests (`tests/test_e2e_controller.py`) | Simulated (service double) |
| --- | --- | --- |
| 1. Scheduler happy path | `SchedulerHappyPathTests.test_renovate_first_then_backlog_issue_with_exact_sha_merge`; `SchedulerHappyPathTests.test_native_omo_contract_in_every_coding_prompt` | GitHub (PRs, Issues, Project, checks, merge), OMO session |
| 2. Review and CI gate | `ReviewAndCiGateTests.test_draft_pending_and_failed_ci_block_merge_until_exact_green_head_then_main_red_repairs` | GitHub checks and main-branch runs |
| 3. Recurring known failure, lessons, backoff | `KnownFailureRecurrenceTests.test_recurring_failure_reuses_lesson_and_shared_promotion_needs_independent_repo`; `KnownFailureRecurrenceTests.test_same_repo_task_cannot_reach_shared_copy_so_no_same_repo_shared_promotion`; `CiFailureBackoffTests.test_repeated_required_ci_failure_backs_off_across_restarts_then_retries_once` | Engram stand-in; required-CI failure; persisted `retries.json` across restarts |
| 4. Concurrent projects and memory scope | `ConcurrentProjectsTests.test_two_projects_keep_separate_checkpoints_and_never_cross_merge`; `MemoryScopeTests.test_local_lesson_never_reaches_another_repo_and_budgets_hold`; `MemoryScopeTests.test_byte_budget_is_what_empties_the_search_once_the_grant_is_spent`; `MemoryScopeTests.test_each_repo_grant_only_sees_its_own_local_scope` | Two projects on threads; Engram stand-in; per-grant byte and query budgets |
| 5. Idle board and selection | `IdleAndSelectionTests.test_no_action_decision_stops_ai_until_the_board_changes`; `IdleBoardTests.test_empty_board_makes_no_session_and_no_mutation_over_several_iterations`; `IdleBoardTests.test_all_terminal_board_makes_no_session_and_no_mutation` | Board with no work; an all-Done board |
| 6. Engram death: before work, mid-flow, restart | `MemoryOutageTests.test_engram_process_death_blocks_new_work_before_any_ai_session`; `MemoryOutageTests.test_learning_survives_engram_restart_from_the_sqlite_store`; `MidFlowEngramTests.test_engram_death_at_ci_fails_closed_then_startup_restart_completes_without_repeating_ai` | Engram process killed; restart through real `Runtime.startup()` with the upgrade lookup patched to raise |
| 6. GitHub outage | `OutageTests.test_outage_after_selection_keeps_decision_and_restart_does_not_reselect`; `OutageTests.test_outage_before_any_ai_session_fails_closed_with_no_mutations`; `WorkerRunLoopOutageTests.test_outage_backs_off_persistently_without_ai_and_stops_cleanly` | GitHub returns HTTP 503 for every call; the real `run()` loop on a thread with a stop event |
| 7. Quota and crash recovery | `CheckpointAndQuotaTests.test_quota_429_on_one_project_pauses_all_ai_then_recovers`; `CheckpointAndQuotaTests.test_quota_during_coding_keeps_checkpoint_and_resumes_same_task_once`; `CrashRecoveryTests.test_lost_merge_response_then_restart_completes_without_second_merge`; `CrashRecoveryTests.test_restart_at_ci_and_postmerge_never_duplicates_ai_or_merge`; `RealWrapperUnstartedQuotaTests.test_unstarted_quota_is_cleared_by_the_real_wrapper_and_the_same_task_resumes_once` | Scripted quota exit (rc 78) in the scripted-wrapper tests; in the real-wrapper test, `HTTP 429: quota reached` from the fake OpenCode, with the real `--clear-unstarted-quota`; lost merge response |
| 1–3, 7 through the real wrapper | `RealWrapperHappyPathTests.test_backlog_issue_through_the_real_wrapper_resumes_once_and_merges_exact_sha`; `RealWrapperHappyPathTests.test_learning_instructions_reach_the_coding_env_and_recovery_prompt_but_never_selection` | The real `opencode-project-safe.py` (guard title, one recovery turn into the coding session, pending marker removed) against the fake OpenCode V2 CLI; GitHub is still the service double |
| Supporting: learning CLI through the real `memory.cli()` | `MemoryCliRefusalTests` (7 tests); `MemoryCliFailureTests.test_a_dead_backend_records_the_failure_marker`; `MemoryCliMetricsTests.test_scripted_learning_session_updates_every_operational_counter`; `EngramBackendIdentityTests` (4 tests) | Real `memory.cli()` in a subprocess; only `memory.ROOT` and `memory.socket_path` redirected; Engram backend per `E2E_ENGRAM_BINARY` |
| Issue #36. Follow-up admission | `FollowUpAdmissionTests.test_each_project_coding_prompt_names_only_its_own_project`; `FollowUpAdmissionTests.test_admitted_follow_up_is_offered_on_next_selection_and_unadmitted_issues_are_not`; prompt contract in `tests/test_omo_roles.py` (`FollowUpIssueContract`) | Prompt contract plus simulated admission (the test seeds the Project membership; no agent runs `item-add`). Live agent behaviour unverified |
| 8. Release gate | `ReleaseGateTests.test_feature_release_needs_owner_approval_then_tags_exact_sha`; `ReleaseGateTests.test_release_verify_waits_for_required_asset_then_completes`; `ReleaseGateTests.test_approval_for_a_different_sha_does_not_release_this_candidate`; `ReleaseGateTests.test_existing_tag_at_other_sha_fails_closed_and_is_not_moved`; `ReleaseGateTests.test_missing_blocker_label_prevents_tag_with_notice_and_never_starts_ai`; `ReleaseGateTests.test_custom_blocker_label_is_queried_and_blocks_for_other_repositories`; `MaintenanceDueTests.test_due_release_enters_maintenance_and_the_only_ai_call_is_release_preparation`; `MaintenanceDueTests.test_not_due_with_fewer_than_three_renovate_prs_makes_no_ai_call`; `MaintenanceDueTests.test_not_due_with_release_younger_than_fourteen_days_makes_no_ai_call`; `MaintenanceDueTests.test_a_non_technical_merged_pr_blocks_maintenance_even_with_three_renovate_prs` | GitHub releases, tags, assets and approval comments |
| 2. Independent oracle review evidence (issue #48) | `tests/test_e2e_review_evidence.py`: `ScriptedReviewEvidenceTests.test_green_ci_without_oracle_review_is_not_merged_until_a_review_is_recorded`; `ScriptedReviewEvidenceTests.test_review_of_an_old_head_does_not_merge_after_the_agent_pushes_a_new_commit`; `RealWrapperReviewEvidenceTests.test_real_wrapper_oracle_descendant_on_the_head_tree_allows_exact_sha_merge`; `RealWrapperReviewEvidenceTests.test_real_wrapper_interrupted_oracle_descendant_never_allows_merge`; `RealWrapperReviewEvidenceTests.test_real_wrapper_oracle_snapshot_on_a_different_tree_never_allows_merge` | GitHub PR head and git trees; OpenCode session store (fake V2 CLI). Verifies identity, freshness and independence only; not the review outcome or content (open release blocker, see VALIDATION.md) |
| Supporting: exit-code mapping of `gh pr checks` | `FakeChecksExitCodeTests.test_controller_reads_pending_failed_and_passed_from_the_fake` | The fake's exit codes: 8 pending, 1 failed, 0 passed |
| Supporting: memory socket and network guards | `MemorySocketGuardTests` (6 tests) | None; asserts the guards themselves |

### Maintenance due rule, as tested

The controller starts a maintenance release only when all of these hold (from
`release_policy.maintenance_due`, read, not changed):

- release is enabled and there is a latest release;
- the latest release is at least 14 days old;
- there is no active feature release;
- there are at least 3 merged, verified same-repository Renovate PRs since the release;
- no merged PR since the release is a non-technical change.

The due test asserts that the release-preparation session is the only AI call.
The "not due" negatives assert no maintenance session and no AI call at all.

## Simulated versus verified

Every item below is a stand-in unless marked otherwise. Nothing is verified
against a live service by the offline suite.

| Item | Status | What the offline suite does | What still needs live acceptance or an owner decision |
| --- | --- | --- | --- |
| GitHub (`gh`, REST, search, Projects, checks, merge, releases) | Simulated — needs live acceptance | `tests/e2e_github.py` at the `gh` argv seam | The live smoke (below), once run by the owner |
| OMO / OpenCode session | Simulated — needs live acceptance or owner decision | Scripted-wrapper mode (`tests/e2e_agent.py`) and real-wrapper mode (the real `opencode-project-safe.py` calling the fake OpenCode V2 CLI, `tests/e2e_opencode.py`). Prompts are checked, the model is not | A real session, through the live smoke, once run by the owner |
| Real safe wrapper `opencode-project-safe.py` | Real code, fake OpenCode | `RealWrapperBase` tests run the real wrapper unchanged (settle timing shortened only, see `tests/e2e_real_wrapper.py`) against the fake OpenCode V2 CLI | A real OpenCode server and model run through the wrapper (the live smoke) |
| Learning CLI `memory.cli()` | Real code, two seams redirected | `tests/e2e_memory_cli.py` runs the real `memory.cli()` in a subprocess. Only `memory.ROOT` (the world temp dir) and `memory.socket_path()` (the private stand-in socket) are redirected. Grant shape, token check, selection refusal, `failure` marker, stdin caps, JSON-object check, `operation()` budgets and `metrics.json` counters run unchanged inside `memory.cli()`. The launcher reproduces memory.py's `__main__` exception-to-exit mapping (MemoryError, ValueError, KeyError, TypeError, OSError → message on stderr, exit 1); memory.py's own `__main__` block and its `--health` path are not exercised | Live Engram behaviour (see the next row) |
| Engram backend | Stand-in by default; upstream binary when opted in | Default: `tests/e2e_engram_standin.py`, a SQLite-backed stand-in, launched through the real `memory.Runtime.start_process`. With `E2E_ENGRAM_BINARY` set, a copy of that binary is launched through the same path instead. `EngramBackendIdentityTests` records which backend answered (`service`, `status`, `version`) and, with a binary, asserts the version is not the stand-in's and matches `E2E_ENGRAM_VERSION`, which is then required. The real download is refused (`Runtime.download`) | Real-binary run: on 2026-11-26 the full `scripts/offline-tests.sh` passed locally with `E2E_ENGRAM_BINARY` naming a copy of upstream Engram v3.7.2 `linux_amd64` (checksum verified against upstream `checksums.txt`). This was a manual run, and CI uses the stand-in; the evidence is on PR #17. The VM suite is the other real-binary check: `vmtest/runner.py` lists "Engram release binary downloaded from GitHub by memory.py (checksum verified)", run by `.github/workflows/vm-lifecycle.yml`; see `docs/VM_TESTING.md` |
| Wrapper active-session paths | `tests/test_e2e_active_sessions.py` (15 tests) through the real `opencode-project-safe.py` and the real controller: the settle loop waiting on nested descendants and then on a late one; exactly one recovery turn plus one more for a descendant spawned during it; no second root; checkpoint and marker kept across a cycle-timeout kill (rc 124); restart adopting the same root (`Adopted interrupted active cycle`); no new selection until verified merge; foreign root refused, never interrupted; a foreign root alongside the adopted root, refused by settle (seeded before the run, not appearing mid-settle); a marker whose root differs from the active root; duplicate-title and legacy-two-root ambiguity; an active session in another directory does not block a new run; `--checkpoint` rc 2 when ambiguous and checkpointing a single root; API down during settle (exit 78, marker kept) and after recovery; `--interrupt-owned` root-first, descendants included, foreign untouched, ignored interrupt and API outage both fail closed; live previous guard refuses recovery | **Simulated.** The fake's active lifetimes are poll counts and interrupts take effect at once. The live OpenCode server, real subagent timing and real interrupt delivery are not verified. `tests/test_wrapper_recovery.py` still hard-codes the empty list and is not changed by this PR |
| Oracle review evidence (issue #48) | Real controller gate and real safe wrapper; the oracle is a stand-in session in the fake OpenCode store | Identity, freshness and independence only. Review outcome and content are NOT verified: this is an open release blocker (Issue #53, owner decision pending). The PR description is not bound. A same-user shell could forge sessions or controller state |
| Memory `failure` marker | Real code | `memory.cli()` refuses with `Memory blocked` when `<state>/memory/failure` exists, and a dead backend's `MemoryError` creates it (`MemoryCliFailureTests`). The E2E controller also checks it through `memory_gate` → `runtime.check()` | None beyond the live Engram row |

### Real-Engram run (opt-in, owner or CI)

The offline default uses the stand-in. To run the Engram-backed tests against
upstream Engram, download the release binary, verify it against the upstream
checksum list, and point the suite at a copy:

```sh
# Download the release archive and checksums.txt from the upstream release page.
sha256sum -c --ignore-missing checksums.txt        # must print "OK" for the archive
tar -xzf engram_3.7.2_linux_amd64.tar.gz engram
install -d -m 700 /path/to/engram-copy
cp engram /path/to/engram-copy/engram && chmod 700 /path/to/engram-copy/engram
E2E_ENGRAM_BINARY=/path/to/engram-copy/engram E2E_ENGRAM_VERSION=3.7.2 bash scripts/offline-tests.sh
```

The supported entry point is `scripts/offline-tests.sh`, which runs the suite with a
temporary `HOME` and the shims. Running the modules by hand with discovery is for
debugging only.

`E2E_ENGRAM_BINARY` is checked before anything is copied. It is resolved, then refused
(the suite fails loudly, never silently falling back to the stand-in) when it points
inside `~/scripts/autocontroller`, under `/run/user/`, or at anything that is not a
regular executable file. A symlink into a refused location is refused too.

Each test's generation folder gets a copy of the binary (`engram-real`) and an `engram`
shell shim. The shim runs the copy with `HOME` set to a private temporary directory,
so the upstream binary does not read or write the real HOME. The real binary still does
all the serving.

`E2E_ENGRAM_VERSION` is required whenever `E2E_ENGRAM_BINARY` is set. Without it the
identity test fails with a message asking you to set it. The test then requires the
probed version to equal `E2E_ENGRAM_VERSION` exactly. Socket directories are `tempfile`
defaults under `/tmp`, not a fixed path.

Use a copy, never the production Engram binary or database, and never
`~/scripts/autocontroller`.

### `memory.cli()` checks no longer covered only by a double

The scripted agent double does not reimplement the CLI any more. Every learning
operation in E2E runs through `tests/e2e_memory_cli.py`, so the checks listed in
`tests/test_e2e_memory_cli.py` (grant path shape, `failure` marker, 4096-byte stdin
cap, JSON-object requirement, `update_metrics` counters, socket refusal) are
exercised by the real code. The agent only parses the grant path and token out of
the instruction text it was given, substitutes `$first_returned`, and calls the
launcher. The token comparison itself happens inside `memory.cli()`.

## Live smoke

`scripts/live-smoke.py` is a separate, opt-in, one-shot check against ONE
disposable sandbox repository. It spends real AI tokens and creates real GitHub
objects. **Nothing runs it automatically.** No workflow, hook or test invokes it
for real; its tests use fake `gh`, `git` and `opencode` shims only.

### Invocation (owner only)

```sh
AUTOCONTROLLER_LIVE_SMOKE=1 GH_CONFIG_DIR=/path/to/dedicated/gh-config \
python3 scripts/live-smoke.py \
  --i-understand-this-spends-tokens \
  --owner OWNER --repo autocontroller-smoke-RUN --run-id RUN \
  --model=PROVIDER/MODEL \
  --opencode /absolute/path/to/opencode \
  --report /absolute/path/outside/this/checkout/report.json \
  --max-ai-sessions 1 --timeout-seconds 300 --max-prompt-bytes 4096 \
  --execute
```

Without `--execute` the script checks the static guards and stops. It makes no
external call at all: no `gh`, `git` or `opencode`. `--execute` is required to run
the smoke. `--model` is written as `--model=VALUE` so a leading dash is never parsed
as an option.

Set `GH_CONFIG_DIR` to a dedicated directory (not `~/.config/gh`) that holds a
`gh` login for the owner account only. Do not set `GH_TOKEN` or `GITHUB_TOKEN`.
The allowlist file `autocontroller-smoke-allowlist` in that directory lists one
owner login per line, as an alternative to `--allow-owner`.

### Guards (all checked before the first external call)

- `AUTOCONTROLLER_LIVE_SMOKE=1` and `--i-understand-this-spends-tokens`;
- the owner is allowlisted by `--allow-owner` and/or by a file named
  `autocontroller-smoke-allowlist` inside `GH_CONFIG_DIR` (one login per line);
- `GH_CONFIG_DIR` is set, exists and is not the default;
- the production controller root (`~/scripts/autocontroller`) does not exist on this
  host. This is an existence check only; its contents are never read;
- `GH_TOKEN`, `GITHUB_TOKEN` are unset; `CI` and `GITHUB_ACTIONS` are unset;
  `GITHUB_EVENT_NAME` is never `pull_request` or `pull_request_target`;
- `--repo` fullmatches `autocontroller-smoke-[a-z0-9-]+` and contains `--run-id`;
- `--model` fullmatches `[A-Za-z0-9._-]+/[A-Za-z0-9._:/-]+` and never starts with `-`;
- `--opencode` is an absolute path, is not under this checkout or the production
  root, and is not named `opencode-project-safe.py`, `autocontroller` or `wrapper`;
- `--max-ai-sessions` is exactly 1; `--timeout-seconds` is 30–900;
  `--max-prompt-bytes` is at most 4096.

After the static guards, the script runs one read-only `gh api user` and requires
the login to equal `--owner`.

### Flow

1. Existence probe: `gh repo view` on the sandbox name. Only "could not resolve",
   "HTTP 404" or "not found" means the repository is absent; any other failure
   fails closed.
2. `gh repo create --private --add-readme` (the repository is recorded as created
   only after this returns 0).
3. `gh issue create` for one Issue.
4. `gh repo clone` into a private temporary directory.
5. Read-only controller helpers: the Issue state, that no PR is linked before the
   session, and the default-branch CI state (recorded, not asserted green).
6. One `opencode run` session in its own process group (`--agent orchestrator`,
   the prompt built by `build_prompt`).
7. A check for a linked same-repository PR (not cross-repository, head repository
   equal to the sandbox).
8. Teardown in `finally`: close the PR, close the Issue, re-verify the repository
   owner with `gh api repos/OWNER/REPO`, then `gh repo delete --yes`. Each step is
   independent, so one failure does not skip the others.

### Sandbox environment and what it does NOT isolate

For `gh`, `git` and `opencode`, the script sets `GH_CONFIG_DIR` to the dedicated
directory, a temporary `GIT_CONFIG_GLOBAL` whose only credential source is
`gh auth git-credential`, `GIT_CONFIG_NOSYSTEM=1` and `GIT_TERMINAL_PROMPT=0`. It
removes `GH_TOKEN`, `GITHUB_TOKEN`, `SSH_AUTH_SOCK`, `GIT_ASKPASS`, `SSH_ASKPASS`,
`GIT_SSH*`, `GIT_CONFIG_PARAMETERS` and `GIT_CONFIG_COUNT`.

**HOME is NOT isolated.** `opencode` needs the operator's provider authentication,
which lives under the real HOME. The agent therefore runs as the operator's OS
account and can read any file that account can read. Run the live smoke only on a
disposable VM or account that holds nothing else. Do not claim that the real HOME
is untouched.

### Cost and time bound

The bound is: wall-clock `--timeout-seconds`, plus the size of the prompt, plus one
top-level run. The OMO orchestrator may make several model calls within that one
session, and those calls are not metered by this script. Tokens are not metered.
On timeout, the process group receives SIGTERM, then 30 s of grace, then SIGKILL,
so child processes are also stopped.

Interruption. The session runs in its own session (`start_new_session=True`), so
Ctrl-C or a terminal hangup does not reach it directly. Under `--execute`, SIGTERM
and SIGHUP are handled like Ctrl-C (they raise `KeyboardInterrupt`), and the previous
handlers are restored afterwards. Any `BaseException` raised while the session may be
running terminates its process group the same way as a timeout, and the exception is
re-raised. `run_smoke` catches the `KeyboardInterrupt`, sets the top-level report
status to `interrupted`, and re-raises. Teardown in `finally` then runs (the PR and
Issue are closed and the repository this run created is deleted after re-verification),
the report is written, and the exit code is 3.

Signal blocking for teardown. `terminate_group` blocks SIGINT, SIGTERM and SIGHUP with
`signal.pthread_sigmask` for its whole call, and sends SIGKILL to the group in a
`finally`, so a second interrupt during the SIGTERM grace wait cannot skip the SIGKILL.
The previous mask is restored afterwards. In `run_session`, the session's
`report.command` record and the pipe close are also in a `finally` around
`terminate_group` (m4), so they happen even if `terminate_group` raises. The
`run_smoke` teardown restores the mask in a `finally` even if recording the deferred
signals raises (m1; `test_m1_mask_is_restored_when_recording_deferred_signals_raises`). `run_smoke`'s `finally` blocks the same
signals around the teardown and the report write, and restores the mask after. Every
call inside has a timeout: `gh` and `git` calls use `GH_TIMEOUT` (60 s) or
`GIT_TIMEOUT` (120 s) through `run_timed`, and the local directory removal does not
block. Teardown is therefore bounded, but it is not instantaneous.

Worst case and what a signal does (m2):

- Worst case: about 300 s of teardown (five `GH_TIMEOUT` calls of 60 s each), plus
  60 s for `terminate_group` (its SIGTERM grace wait is 30 s, then SIGKILL and a second
  wait of up to 30 s). While this runs, Ctrl-C, SIGTERM and SIGHUP are deferred: they
  are held and handled once, after the mask is restored.
- SIGQUIT and SIGKILL are not blocked. They end the script immediately, and teardown
  does not run. The sandbox repository may then be left behind: use the operator
  instructions below.
- Single-thread assumption (m3): `pthread_sigmask` affects only the calling thread, and
  signal handlers run only on the main thread. So the mask covers the whole controller
  only in a single-threaded process. `main` refuses `--execute` (exit 2, before the
  report file is created) when `threading.active_count() != 1`.
- Inherited `SIG_IGN` for SIGHUP (m5): if SIGHUP was ignored when the script started
  (for example under `nohup`), `main` keeps it ignored for the run and restores it
  afterwards. It does not install its own handler there, so a hangup cannot start
  interrupting the run. SIGTERM and SIGINT still follow the handlers described above.

A signal that arrives during teardown is deferred, not lost. It stays pending while the
mask is blocked. After teardown completes and the report is written, the mask is
restored and the pending signal is handled once. The outcome is exit 3, with
`status: interrupted` (unless the status is already `teardown-failed` or
`teardown-incomplete`, which keep their precedence), and the names of the deferred
signals are recorded in `deferred_signals`. A run whose teardown finished cleanly is
never reported as `passed` when a handled signal arrived. A signal whose handler is
`SIG_IGN` (for example SIGHUP under `nohup`) stays pending while blocked but is dropped
on unmask, so it is not recorded and does not change the status or exit code.

Teardown calls and their bounds (checked by reading `scripts/live-smoke.py`):

- close PR: `gh pr close`, `run_timed`, `GH_TIMEOUT` (60 s);
- close Issue: `gh issue close`, `run_timed`, `GH_TIMEOUT` (60 s);
- existence probe (only when `gh repo create` did not return 0): `gh api`,
  `run_timed`, `GH_TIMEOUT` (60 s);
- ownership re-verify: `gh api`, `run_timed`, `GH_TIMEOUT` (60 s);
- repository delete: `gh repo delete --yes`, `run_timed`, `GH_TIMEOUT` (60 s);
- local directory removal, environment restore and report write: local, no network,
  no timeout needed.

`run_timed` passes `timeout=` to `subprocess.run`, which kills the direct child on
timeout. It does not kill grandchildren of `gh`. That is part of the residual risk
below.

Residual risk. A SIGKILL of the script itself, or a teardown call that times out, can
leave `OWNER/autocontroller-smoke-<run-id>` behind. The report records it in
`leftovers` when it is known. The operator instructions below were not executed by
this change or by any test. They are for an operator to run by hand, only after
checking the run's report:

```sh
# Operator instructions, NOT executed. Replace the placeholders first.
GH_CONFIG_DIR=/path/to/dedicated/gh-config gh repo view OWNER/autocontroller-smoke-RUN
# Only if the view shows the repository exists and its owner is the sandbox owner:
GH_CONFIG_DIR=/path/to/dedicated/gh-config gh repo delete OWNER/autocontroller-smoke-RUN --yes
```

What each test proves (in `tests/test_live_smoke_run.py`):

- `test_main_interrupt_during_session_tears_down_exits_three_and_reports_interrupted`:
  an interrupt raised from the session's `communicate` makes `main` exit 3 and the
  report status `interrupted`, runs teardown (the created repo is deleted), and leaves
  the session leader and child dead.
- `test_real_sigterm_during_session_tears_down_and_reports_interrupted`: a real SIGTERM
  sent to the process during the session goes through the handler `main` installs,
  with the same results as the test above.
- `test_second_interrupt_during_grace_wait_still_sends_sigkill_to_the_group`: a
  `KeyboardInterrupt` raised by the first grace `wait` in `terminate_group` still
  leads to SIGKILL, the leader and child are dead, the exception propagates, and the
  signal mask is restored.
- `test_non_object_json_from_the_ownership_check_is_a_leftover_never_a_delete`: a
  non-object JSON answer from the ownership re-verify makes the repository a leftover,
  and it is never deleted.
- `test_create_failing_after_server_side_create_is_deleted_when_verified`: a `repo
  create` that fails after the repo exists is verified and deleted.
- `test_create_failing_without_a_repo_is_recorded_as_not_created`: a `repo create`
  that fails before creating anything is recorded as `not_created`, and the status is
  `failed`, not `teardown-incomplete`.
- `test_m1_mask_is_restored_when_recording_deferred_signals_raises`: if recording the
  deferred signals raises, the controller still restores the signal mask, and the run
  exits 3 with the exception named on stderr.
- `test_m5_inherited_sighup_ignore_is_kept_during_the_run_and_restored`: a SIGHUP that was
  `SIG_IGN` on entry stays `SIG_IGN` during the run and afterwards.
- `test_ignored_sighup_during_teardown_is_not_recorded_or_reported_interrupted`: with
  SIGHUP ignored, a SIGHUP sent from the fake `gh repo delete` during teardown is not
  listed in `deferred_signals`; the run exits 0 with `status: passed`.
- `test_sigterm_during_teardown_is_deferred_then_handled_once` and
  `test_sigint_during_teardown_is_deferred_then_handled_once`: a real signal sent to
  the controller from inside the fake `gh repo delete` is held until teardown completes
  (PR and Issue closed, repo deleted, report written). The mask is restored, the signal
  is handled once after the block, exit is 3, the status is `interrupted`, and
  `deferred_signals` names the signal.
- `test_real_sigterm_during_session_...` and the `terminate_group` second-interrupt test
  above cover the session path. The mask is restored after each.

Guards tests (`tests/test_live_smoke_guards.py`) for the allowlist:

- `test_unreadable_allowlist_directory_in_place_of_file_is_a_refusal_with_exit_two`: a
  directory in place of the allowlist file refuses with exit 2. This works for root too.
- `test_permission_error_reading_the_allowlist_is_a_refusal_with_exit_two`: the
  `PermissionError` branch, forced by patching the read, refuses with exit 2.

### Exit codes

- `0`: the smoke passed.
- `2`: refused by a guard. Some refusals happen after read-only `gh` calls: the
  existing-repo check, and the login check.
- `3`: a smoke step failed, teardown left a repository behind, or an unexpected
  exception occurred. Non-smoke exceptions map to 3. For an interrupt, the stderr line
  names the outcome: "teardown ran" when teardown finished cleanly, "teardown did NOT
  complete cleanly (teardown-failed or teardown-incomplete)" when it did not, and "no run
  had started" when the interrupt came before the run began.

The report prints only the basename of `--opencode`. Statuses: `passed`,
`failed`, `refused`, `interrupted`, `teardown-failed`, `teardown-incomplete`.

Other behaviour:

- A `gh repo create` that raises (timeout, or failure after the server-side create)
  is checked in teardown. If the existence probe returns HTTP 404, the repository was
  never created: that is recorded under `not_created`, it is not a teardown error, and
  the status is not `teardown-incomplete`. The exit code stays 3, because create failed.
  Otherwise the repository is re-verified and deleted if it is owned by the run. If
  existence or ownership cannot be established, it is recorded as a leftover, never
  deleted.
- A refusal, or an import or login failure, that happens before the run starts
  removes the empty exclusive `--report` file. A non-UTF-8 or unreadable allowlist is a refusal (exit 2).
- Under `--execute`, the allowlist file `autocontroller-smoke-allowlist` must exist in
  `GH_CONFIG_DIR` and list the owner. `--allow-owner` alone is not enough, because it
  is self-asserted. The real controls are the dedicated `GH_CONFIG_DIR`, the login
  check (`gh api user`), and that file.

### What the live smoke proves, once it has been run

Only the steps that completed. Until the owner runs it, the live path has no
evidence.

## Limits

- No test in this suite contacts GitHub, OpenCode, a model provider, the network,
  systemd, or the production Engram socket or database.
- The default Engram backend is the stand-in, not upstream Engram. Its schema and HTTP
  surface follow `memory.py`, not the upstream project. The real binary runs only when
  `E2E_ENGRAM_BINARY` is set (see "Real-Engram run" above).
- `memory.cli()` runs for real, with two seams redirected: `memory.ROOT` and
  `memory.socket_path()`. Nothing else in `memory.py` is patched.
- The real safe wrapper runs for real in the `RealWrapper*` tests, but its OpenCode
  is the fake V2 CLI. Only its settle timing is shortened. The real OpenCode server and
  model are not used.
- The active-session model in the fake OpenCode is deterministic: a session is active for a
  fixed number of `GET /api/session/active` polls, or until interrupted. Real OpenCode timing,
  real subagent durations and real interrupt delivery are NOT simulated and need the live
  sandbox. `tests/test_e2e_active_sessions.py` proves the wrapper's and controller's decisions
  against that model only.
- The active-session tests shorten two product timings in the launcher: the settle quiet period
  (`QUIET_SECONDS=0`) and the `--interrupt-owned` drain deadline (2 s instead of 30 s). The
  deadline logic is the real one; only its length is shortened.
- `previous_guard_alive` checks a live pid whose `/proc/<pid>/cmdline` contains
  `opencode-project-safe`. The E2E test uses a stand-in process with that text in its argv. It
  does not prove the real launcher's cmdline, which is only verified on a live host.
- `tests/test_wrapper_recovery.py` still hard-codes an empty active list and is not changed by
  this work.
- The worker `run()` outage test (`WorkerRunLoopOutageTests`) sets `worker._validated = True`.
  The E2E checkouts sit outside `DEFAULT_ROOT`, so `validate()` would reject them. That
  test therefore does NOT cover checkout validation. The outage alone drives the
  failures. Checkout validation is not covered by the E2E suite.
- Timing is deterministic: `published_at` is set relative to the test clock, and
  the 14-day maintenance check uses the same clock. Retry backoff is shortened only
  through the persisted `until` value, not by patching product code.
- Shared promotion is only partly covered. `memory.operation()` promotes a lesson to
  `shared` on a shared-scope confirmation only when some origin is in another
  repository. The same-repository test shows that a same-repo task cannot reach the
  shared copy at all (search skips it, so the feedback is refused and the row stays
  `candidate`). It does NOT exercise the origin-rule branch. The cross-repository
  promotion is covered by `KnownFailureRecurrenceTests.test_recurring_failure_reuses_lesson_and_shared_promotion_needs_independent_repo`.
- Inherited, not introduced by this PR: `memory.py`'s `Runtime.startup` opens a
  read-only sqlite connection for `PRAGMA quick_check` with `with sqlite3.connect(...)`.
  The `with` commits or rolls back but does not close the connection, so the handle
  stays open until garbage collection. `ResourceWarning: unclosed database` appears in
  normal `scripts/offline-tests.sh` runs, not only under `python3 -X dev`. It is
  reported by whichever test happens to trigger garbage collection (it has been seen in
  `MidFlowEngramTests`, and in the pre-existing `test_memory` `RuntimeTests`), so the
  test it appears in depends on GC timing. It does not fail the suite. It is not fixed
  here because product code is out of scope for this PR.
- Inherited and latent, not introduced by this PR (product code, out of scope here):
  - `ProjectWorker.env()` (`autonomy-controller.py`) copies `os.environ`, then
    overwrites `AUTONOMY_PROJECT_ROOT`, `AUTONOMY_PROJECT_NAME`,
    `AUTONOMY_PENDING_MARKER` and `AUTONOMY_OPENCODE`. `run_wrapper` then overwrites
    `AUTONOMY_PHASE`. Other `AUTONOMY_*` names pass through unchanged.
  - An inherited `AUTOCONTROLLER_LEARNING_INSTRUCTIONS` passes through unchanged into the
    selection phase. It also passes into every phase when no memory runtime is
    configured. It carries another session's grant path and token.
  - The leak test (`test_leaked_learning_env_never_reaches_a_selection_session`) proves
    the harness scrub only. It is not a product guarantee, and the product code is
    unchanged.
  - The installed systemd unit does not set these variables, so the defect is latent.
    It is recommended as a separate Issue for the owner.
- Hermetic environment for the E2E tests: `scripts/offline-tests.sh` removes every
  `AUTOCONTROLLER_*`, `AUTONOMY_*`, `OPENCODE_*`, `OPENCODE` and `ENGRAM_*` variable
  before any test runs. `E2EBase.setUp` and the worker's iteration (`World.worker`) also
  remove them while a test runs, and restore them afterwards. The regression test
  `test_leaked_learning_env_never_reaches_a_selection_session` sets
  `AUTOCONTROLLER_LEARNING_INSTRUCTIONS` in its own body and checks that the selection
  session still gets no learning instructions. It fails if the per-iteration removal is
  removed. The live-smoke test classes do not spawn the controller or the wrapper; they
  call `main` through shims and pass their own `env`, so they do not need this removal.
