# AutoController

**One repository URL. An autonomous, evergreen development workflow.**

AutoController coordinates existing OpenCode V2 and Oh My OpenCode Slim agents against native GitHub Issues, Projects and pull requests. It is not an AI provider, a new agent framework, or a second issue tracker. No Docker, web UI, separate database service or additional model is required by the controller itself.

> **Development alpha — not a public release.** The controller core and simulated integrations are regression tested. Fresh-VM onboarding, live GitHub provisioning, and the first complete bootstrap PR have not yet passed live acceptance testing. Use a disposable trusted repository first. Licensed under the [MIT License](LICENSE).

## Quick start (alpha)

Target platforms: Debian 12/13 and Ubuntu 24.04/26.04 LTS on an amd64 or arm64 VM with systemd. These are the intended support matrix, not yet a certified clean-VM test matrix. Use a normal, sudo-capable account with OpenCode V2, OMO Slim (`orchestrator`, `fixer`, `oracle`) and a working, persistent provider configuration. The controller does not install, modify or upgrade OpenCode or OMO.

From an extracted source package:

```sh
bash install.sh
```

The installer provisions missing system tools (Git, Python, GitHub CLI, mise), enables user lingering, installs the user service and starts guided onboarding. GitHub authorization uses the official CLI device flow; follow its on-screen browser instruction. Enter the URL of a repository you control and confirm autonomous changes. You authorize the controller to create and merge reviewed PRs under the account's permissions, including for existing open issues. It cannot bypass required GitHub checks or rules using administrative merge flags. Existing branch protection is not removed. An empty repository gets a minimal initial commit to establish a base branch.

AutoController clones the repository into its own managed checkout, provisions a dedicated GitHub Project, imports existing open Issues authored by the repository owner, members or collaborators (excluding pull requests and Renovate issues), creates a mandatory repository bootstrap issue, writes the local config and enables the background service. The bootstrap agent adapts the versioned [Golden Standard](standards/GOLDEN_STANDARD.md) to actual repository requirements. Routine work is blocked until its exact PR passes the required CI and the exact merged commit passes push CI. The installer does not run a paid smoke prompt; model/provider and real delegation are verified by the first actual work.

Engram installs on first service start; its stable release is checked at each VM boot. Memory is mandatory; if it becomes unusable, development stops and the recovery mechanism preserves checkpoints. No independent backup job is installed. The updater stages a transient generation of the database to test migrations before activating the new binary; it is not a retained backup.

After setup, open the displayed GitHub Project to create or add **real repository Issues**, not draft Project cards. No manual Ready transition is required. Install the [Renovate GitHub App](https://github.com/apps/renovate) for the repository after the bootstrap has prepared its configuration. App authorization must be done by the owner. Future issues are managed explicitly through the Project; automatic import of newly created issues outside the Project is not yet implemented.

## Daily use

```sh
autocontroller status
autocontroller logs
autocontroller doctor
autocontroller memory     # Engram health check
autocontroller add       # enroll another repository
autocontroller stop
autocontroller start
```

If `~/.local/bin` is not on your interactive PATH, use `~/.local/bin/autocontroller`. Each repository has a separate Project, checkout, checkpoint, retry state and local memory. Adding a repository restarts the service gracefully to load configuration. The software is installed under `~/scripts/autocontroller`; `config.json`, `state/` and logs are private runtime data and must never be committed to the source repository. A service `--install-only` update does not change existing configuration or auto-start work: stop the running controller, run `bash install.sh --install-only`, then start it again. Existing in-flight projects are not automatically re-bootstrapped.

## Operating model

- One active task per repository; unrelated repositories can work concurrently.
- Existing work/recovery checkpoints are reconciled before new work. New projects finish the mandatory bootstrap first. After that, Renovate PRs have priority (only PRs from the verified `renovate[bot]` GitHub App identity whose branch lives in the same repository; branch names or fork PRs never qualify), then the native OMO orchestrator chooses the next actionable Issue from the dedicated Project. It is not an issue-number sorter and does not require a human-managed Ready queue.
- Empty backlogs produce no AI calls. An unchanged non-actionable backlog is polled with deterministic state and a low-frequency safety recheck. CI waits consume no AI tokens. Rate limits and task/infrastructure retries persist across restarts.
- Native OMO roles handle analysis, fixer implementation, and independent oracle review. The agent must leave unresolved work as Draft. The controller verifies actual GitHub required checks, an exact ready head SHA, and successful push CI on the merged commit; it does not independently prove that an LLM review occurred. It does not bypass branch protection.
- Release automation uses native tag-triggered workflows. Feature releases require the configured GitHub account's explicit approval of the version and exact commit; technical maintenance follows the configured cadence. New onboarding defaults to 14 days and at least three merged Renovate PRs, with no fixed product-specific required asset names until configured. Product-specific publication behavior must be reviewed during bootstrap.
- Engram memory is scoped by repo with filtered shared knowledge. Learning is opportunistic in existing sessions; no separate reflection/curator runs, embedding models or per-PR evaluation. Its evidence is agent-provided; deterministic policy checks structure, limits, identity and independent task reuse, not the causal correctness of evidence. The adapter uses logical scopes, not an OS security boundary.

## Security boundary

Use a dedicated VM/account and trusted repositories. Agents execute application code and shell commands under the controller user's privileges. A malicious issue, repo, dependency, prompt, agent or tool may attempt to access that user's files, GitHub credentials and other checkouts. Scope enforcement in the memory adapter is not a sandbox. Do not grant passwordless unrestricted sudo or expose production credentials to agents. Onboarding requires a normal user with temporary sudo for package installation and lingering. GitHub CLI's OAuth credentials are persistent; use an appropriately restricted account and review organization policies. External SSO, required reviews by other users, GitHub Actions policies and branch/ruleset restrictions may legitimately require an owner action. AutoController must stop or wait, never weaken protections to proceed.

Public repositories: membership in the configured GitHub Project (with a nonterminal status) is what authorizes an Issue; the controller never adds Issues to the Project by itself after onboarding. Issue, PR and comment text is treated as untrusted data and cannot expand permissions or task scope. PRs from forks are never treated as Renovate, linked, repair or release PRs.

## Development and acceptance

Contributor and agent rules live in [AGENTS.md](AGENTS.md) and `.agents/`. A source checkout is never the running controller: do not run the installer, onboarding or service commands from it on a host with a production installation.

```sh
mise run ci                   # syntax checks + full offline regression suite
bash scripts/check-syntax.sh  # same checks without mise
bash scripts/offline-tests.sh
```

The tests use a simulated GitHub/OpenCode/Engram backend, an isolated temporary `HOME`, no tokens and blocking shims for `gh`, `opencode`, `systemctl`, `loginctl`, `sudo`, `curl` and `engram`; they spend no AI tokens and never write to GitHub. The `required` GitHub check runs them on clean Debian 12/13 and Ubuntu 24.04/26.04 containers (OS/Python compatibility, not a systemd VM test). A separate scheduled/manual VM suite (`vmtest/`, see [docs/VM_TESTING.md](docs/VM_TESTING.md)) boots disposable pinned cloud images in QEMU to exercise real systemd, lingering and reboot; it runs on a disposable VM only, never on a production host. See [validation and limitations](docs/VALIDATION.md) for what still requires live VM/GitHub acceptance before a public release.

## License

MIT — see [LICENSE](LICENSE). Third-party material and notices: see [THIRD_PARTY.md](THIRD_PARTY.md).
