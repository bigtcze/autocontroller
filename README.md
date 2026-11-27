# AutoController

**One repository URL. An autonomous, evergreen development workflow.**

AutoController coordinates existing OpenCode V2 and Oh My OpenCode Slim agents against native GitHub Issues, Projects and pull requests. It is not an AI provider, a new agent framework, or a second issue tracker. No Docker, web UI, separate database service or additional model is required by the controller itself.

> **Unreleased development version — not a public release.** The controller core and simulated integrations are regression tested. Fresh-VM onboarding, live GitHub provisioning, and the first complete bootstrap PR have not yet passed live acceptance testing. Use a disposable trusted repository first. Licensed under the [MIT License](LICENSE).

## Quick start (development)

Target platforms: Debian 12/13 and Ubuntu 24.04/26.04 LTS on an amd64 or arm64 VM with systemd. These are the intended support matrix, not yet a certified clean-VM test matrix. Use a normal, sudo-capable account with OpenCode V2, OMO Slim (`orchestrator`, `fixer`, `oracle`) and a working, persistent provider configuration. The controller does not install, modify or upgrade OpenCode or OMO.

From a published release archive (`autocontroller-vX.Y.Z.tar.gz` and `SHA256SUMS`, built by `release.yml`; no public release has been published yet; the publish step is `release_artifacts.py publish`, which creates a draft, verifies it, and only then publishes, and re-checks open release blockers before publishing):

```sh
sha256sum -c SHA256SUMS
tar -xzf autocontroller-vX.Y.Z.tar.gz
cd autocontroller-vX.Y.Z
bash install.sh
```

From a source checkout or an extracted source package, the same `bash install.sh` applies.

The installer provisions missing system tools (Git, Python, GitHub CLI, mise), enables user lingering, installs the user service and starts guided onboarding. GitHub authorization uses the official CLI device flow; follow its on-screen browser instruction. Enter the URL of a repository you control and confirm autonomous changes. You authorize the controller to create and merge reviewed PRs under the account's permissions, including for existing open issues. It cannot bypass required GitHub checks or rules using administrative merge flags. Existing branch protection is not removed. An empty repository gets a minimal initial commit to establish a base branch.

Before the config is written or the service is restarted, onboarding verifies that the default branch requires the `required` status check, for this account. It reads classic branch protection and the repository's active rulesets (read-only). If neither enforces the check, onboarding stops unless a repository admin is at the terminal. An admin can then consent, separately from the first confirmation, to adding one new repository ruleset named `AutoController required check` on the default branch: it requires the `required` check from GitHub Actions and has no bypass actors. Existing protection and rulesets are never edited or removed. After the add, onboarding re-reads GitHub and proceeds only if the branch now enforces the check. A non-admin, a declined prompt, or an ambiguous or truncated response fails closed with an owner action and no further mutation. Rerunning onboarding is safe: an enforced branch is only read, and a ruleset from an earlier run is recognised rather than duplicated. Repositories enrolled before this check existed are **not** re-verified retroactively. When onboarding is rerun for an already enrolled repository, it performs a read-only check and prints a warning to stderr if the required check is no longer enforced; it never prompts or adds protection there, and the resume itself is unchanged. The controller does not re-check branch protection continuously.

Repositories that cannot use branch protection or rulesets (for example private repositories on a GitHub plan that does not offer them) cannot be onboarded until the plan allows requiring the check.

AutoController clones the repository into its own managed checkout, provisions a dedicated GitHub Project, imports existing open Issues authored by the repository owner, members or collaborators (excluding pull requests and Renovate issues), creates a mandatory repository bootstrap issue, writes the local config and enables the background service. The bootstrap agent adapts the versioned [Golden Standard](standards/GOLDEN_STANDARD.md) to actual repository requirements. Routine work is blocked until its exact PR passes the required CI and the exact merged commit passes push CI. The installer does not run a paid smoke prompt; model/provider and real delegation are verified by the first actual work.

Engram installs on first service start; its stable release is checked at each VM boot. Memory is mandatory; if it becomes unusable, development stops and the recovery mechanism preserves checkpoints. No independent backup job is installed. The updater stages a transient generation of the database to test migrations before activating the new binary; it is not a retained backup.

After setup, open the displayed GitHub Project to create or add **real repository Issues**, not draft Project cards. No manual Ready transition is required. Install the [Renovate GitHub App](https://github.com/apps/renovate) for the repository after the bootstrap has prepared its configuration. App authorization must be done by the owner. The controller never adds Issues to the Project automatically after onboarding; a maintainer admits each Issue by adding it to the Project (by design, not a missing feature). The only exception is agent follow-ups for an authorized task, which the orchestrator adds and verifies in the same repository's Project; see [.agents/SECURITY.md](.agents/SECURITY.md).

## Daily use

```sh
autocontroller status
autocontroller logs
autocontroller doctor
autocontroller memory                     # Engram health check
autocontroller memory inspect OWNER/REPO [--query TEXT] [--shared]
autocontroller add                        # enroll another repository
autocontroller remove <repo> [--force] [--delete-local-data] [--drain-timeout S]
autocontroller update [--tag vX.Y.Z] [--yes] [--health-seconds N] [--drain-timeout S]
autocontroller rollback [--yes] [--health-seconds N] [--drain-timeout S] [--recover]
autocontroller uninstall [--purge] [--drain-timeout S]
autocontroller stop
autocontroller start
```

If `~/.local/bin` is not on your interactive PATH, use `~/.local/bin/autocontroller`. Each repository has a separate Project, checkout, checkpoint, retry state and local memory. Adding a repository restarts the service gracefully to load configuration. The software is installed under `~/scripts/autocontroller`; `config.json`, `state/` and logs are private runtime data and must never be committed to the source repository.

`status` shows the installed version, service state, the kept previous release, each project's cycle phase, drain and memory-failure markers, and the last update result. `doctor` verifies the installed program files against `MANIFEST.sha256` (release installs), the unit and CLI links, and leftover interrupted operations. It reports problems and exits nonzero; it does not repair anything.

### Managed update, rollback and removal

- `update` takes the latest stable release from `update_repository` (or `--tag vX.Y.Z`). It asks for confirmation unless `--yes` is given. It refuses downgrades. Before anything is stopped, it downloads the archive and `SHA256SUMS`, and checks each against the digest GitHub reports for the asset and against the archive's `MANIFEST.sha256`.
- Then it drains the workers (they park at a safe point; the drain timeout is 3600 seconds by default; if they do not park in time, the service is left running and nothing is replaced), stops the service, and swaps in the new program files. The config, `state/` and checkouts are not touched.
- The service must pass `--check`, stay active for `--health-seconds` (30 by default), and its own controller process must publish startup readiness (`state/ready.json`, written only after Engram and every enabled project's worker have initialized, bound to that process's PID and start time). Readiness must appear within `max(--health-seconds, 180s)`, measured from the first service-state read after `--check` (not from the service start), otherwise the update fails with a readiness reason. Idle and CI-waiting workers count as ready without completing a task. This is startup readiness, not proof of continuous task progress. If any check fails, the controller stops the new code, restores the previous release, and starts it again. The update exits with an error that names the failed check. Once readiness has been seen, `state/ready.json` must stay present for the rest of the health window: a withdrawn marker fails the check and triggers the automatic restore, and if the restore is unhealthy too, its reason is recorded as `restore_reason` in `updates/history.json`.
- Compatibility: a `rollback` or `rollback --recover` to a release that predates the readiness marker cannot pass the readiness check. It times out, the current release is restored, and the reason is named; reinstall that release instead. An update started from a pre-readiness install is verified by that install's older lifecycle, which does not require readiness.
- A successful update keeps exactly one previous release in `rollback/previous/`. `rollback` swaps back to it under the same drain and health checks, and that swap can itself be rolled back.
- `remove <repo>` deletes one project's entry from `config.json` and restarts the service (or disables it when no project remains). It refuses while that project has an unfinished cycle unless `--force` is given. The checkout, GitHub state and memory are kept. `--delete-local-data` also deletes the checkout, but only when it is a clean, fully pushed managed clone directly under `~/repositories`: every local branch must have an upstream, no local branch may hold commits that no remote has, and the checkout must have no linked worktrees. The same checks run again after the service drains, so a cycle or unpushed commit that appears during the drain refuses the removal and the service is restarted. Memory is never deleted by `remove`.
- `uninstall` stops the service, removes the unit and CLI links, and removes the program files. It keeps `config.json`, `state/` and the checkouts. `--purge` also deletes `config.json` and `state/`, and asks you to type `purge`. Checkouts under `~/repositories` are never deleted.

The controller never updates itself. Its own lifecycle commands refuse to run inside an agent session (any `AUTONOMY_*` variable, `AUTOCONTROLLER_LEARNING_INSTRUCTIONS`, or `OPENCODE_SESSION_ID` set). Run `update`, `rollback`, `remove` and `uninstall` from a normal terminal, not from inside an OpenCode session. This refusal is a logical guard for the controller's own sessions, not a sandbox: it does not stop an agent that can run arbitrary shell commands as your user.

- `--drain-timeout SECONDS` (update, rollback, remove, uninstall) sets how long to wait for workers to park before giving up. Default 3600.
- `rollback --recover` resolves an interrupted rollback. A leftover `rollback/swap` blocks `update` and `rollback`, and `doctor` fails until it is resolved. Recovery restores the release that the rollback replaced and checks it against `MANIFEST.sha256`; if that check fails, the service stays stopped and the swap is kept. If the rollback had already replaced the old store, recovery finishes the commit; a leftover `rollback/previous.old` with no swap is deleted by the same command, and `doctor` warns about it until then.
- A leftover `rollback/incoming` (an interrupted update) blocks further updates and is reported by `doctor`. It is not restored by any command yet; inspect it before retrying.
- A stale drain request (`state/drain.request` with no update holding `lifecycle.lock`) is ignored by the controller and reported by `doctor` as a failure. Confirm no update is running, then remove the file with the `rm` command `doctor` prints.

`--install-only` applies to source installs only: it copies the program files from a source checkout or extracted source package without changing configuration or starting work. Stop the controller, run `bash install.sh --install-only`, then start it again. Release archives are updated with `autocontroller update`, not with `--install-only`. Existing in-flight projects are not automatically re-bootstrapped.

## Operating model

- One active task per repository; unrelated repositories can work concurrently.
- Existing work/recovery checkpoints are reconciled before new work. New projects finish the mandatory bootstrap first. After that, Renovate PRs have priority (only PRs from the verified `renovate[bot]` GitHub App identity whose branch lives in the same repository; branch names or fork PRs never qualify), then the native OMO orchestrator chooses the next actionable Issue from the dedicated Project. It is not an issue-number sorter and does not require a human-managed Ready queue.
- Empty backlogs produce no AI calls. An unchanged non-actionable backlog is polled with deterministic state and a low-frequency safety recheck. CI waits consume no AI tokens. Rate limits and task/infrastructure retries persist across restarts.
- Native OMO roles handle analysis, fixer implementation, and independent oracle review. The agent must leave unresolved work as Draft. The controller verifies actual GitHub required checks, an exact ready head SHA, and successful push CI on the merged commit; it does not independently prove that an LLM review occurred. It does not bypass branch protection.
- Release automation uses native tag-triggered workflows. Feature releases require the configured GitHub account's explicit approval of the version and exact commit; technical maintenance follows the configured cadence. New onboarding defaults to 14 days and at least three merged Renovate PRs, with no fixed product-specific required asset names until configured. Product-specific publication behavior must be reviewed during bootstrap.
- Engram memory is scoped by repo with filtered shared knowledge. Learning is opportunistic in existing sessions; no separate reflection/curator runs, embedding models or per-PR evaluation. Its evidence is agent-provided; deterministic policy checks structure, limits, identity and independent task reuse, not the causal correctness of evidence. The adapter uses logical scopes, not an OS security boundary.

## Security boundary

Use a dedicated VM/account and trusted repositories. Agents execute application code and shell commands under the controller user's privileges. A malicious issue, repo, dependency, prompt, agent or tool may attempt to access that user's files, GitHub credentials and other checkouts. Scope enforcement in the memory adapter is not a sandbox. Do not grant passwordless unrestricted sudo or expose production credentials to agents. Onboarding requires a normal user with temporary sudo for package installation and lingering. GitHub CLI's OAuth credentials are persistent; use an appropriately restricted account and review organization policies. External SSO, required reviews by other users, GitHub Actions policies and branch/ruleset restrictions may legitimately require an owner action. AutoController must stop or wait, never weaken protections to proceed. Onboarding verifies the default branch's required `required` check as described above; it adds a ruleset only with separate admin consent and never removes or weakens existing rules. The ruleset path has been exercised only against the offline fake GitHub. It has not been run against live GitHub, and that remains part of sandbox acceptance (see [docs/VALIDATION.md](docs/VALIDATION.md)).

Public repositories: membership in the configured GitHub Project (with a nonterminal status) is what authorizes an Issue; the controller never adds Issues to the Project by itself after onboarding. Agent follow-up Issues for an authorized task are added to that same Project by the orchestrator and verified (see [.agents/SECURITY.md](.agents/SECURITY.md)). Issue, PR and comment text is treated as untrusted data and cannot expand permissions or task scope. PRs from forks are never treated as Renovate, linked, repair or release PRs. A closing PR merged before its Issue's latest reopen is treated as historical, so a reopened Issue gets a fresh attempt; ambiguous chronology fails closed for manual reconciliation.

## Development and acceptance

Contributor and agent rules live in [AGENTS.md](AGENTS.md) and `.agents/`. A source checkout is never the running controller: do not run the installer, onboarding or service commands from it on a host with a production installation.

```sh
mise run ci                   # syntax checks + full offline regression suite
bash scripts/check-syntax.sh  # same checks without mise
bash scripts/offline-tests.sh
```

The tests use a simulated GitHub/OpenCode/Engram backend, an isolated temporary `HOME`, no tokens and blocking shims for `gh`, `opencode`, `systemctl`, `loginctl`, `sudo`, `curl` and `engram`; they spend no AI tokens and never write to GitHub. Onboarding is tested against simulated `gh`/OpenCode/systemctl stand-ins; see [docs/ONBOARDING_TESTING.md](docs/ONBOARDING_TESTING.md) for the simulation contract, the fault-injection matrix and the designed (unimplemented) sandbox live-test path. End-to-end acceptance tests (`tests/test_e2e_controller.py`, 45 tests, about a minute; plus `tests/test_e2e_real_wrapper.py` and `tests/test_e2e_memory_cli.py`) drive the real controller against stateful GitHub and OMO agent doubles, offline and hermetic, through the real safe wrapper and the real `memory.cli()`; Engram is a stand-in unless `E2E_ENGRAM_BINARY` names a verified upstream copy; see [docs/E2E_TESTING.md](docs/E2E_TESTING.md) for the scenario mapping, what is simulated and what is verified (nothing is verified against live services yet). `scripts/live-smoke.py` is an opt-in live smoke for one disposable sandbox repository: its body is implemented and exercised offline through fake `gh`/`git`/`opencode` shims, but it has never been executed against a real service, is never run by CI, and spends tokens only when run by the owner with explicit flags. The `required` GitHub check runs them on clean Debian 12/13 and Ubuntu 24.04/26.04 containers (OS/Python compatibility, not a systemd VM test). A separate scheduled/manual VM suite (`vmtest/`, see [docs/VM_TESTING.md](docs/VM_TESTING.md)) boots disposable pinned cloud images in QEMU to exercise real systemd, lingering and reboot; it runs on a disposable VM only, never on a production host. See [validation and limitations](docs/VALIDATION.md) for what still requires live VM/GitHub acceptance before a public release.

## License

MIT — see [LICENSE](LICENSE). Third-party material and notices: see [THIRD_PARTY.md](THIRD_PARTY.md).
