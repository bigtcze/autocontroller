# Onboarding testing

This document describes how the offline suite exercises `onboarding.py` and the
controller's selection and admission rules, what the simulation assumes, and a
**designed but unimplemented** sandbox path for live onboarding (Issue #4).

Everything in the offline part runs with no network, no GitHub or OpenCode
credentials and no systemd. The live path is not implemented and has not been
run.

## 1. Simulation contract

The offline suite replaces three external programs with deterministic stand-ins.
Each is reached through a `PATH` shim, and the test environment contains no real
ones.

| Stand-in | File | Models | Refuses / fails closed |
| --- | --- | --- | --- |
| `gh` | `tests/fake_gh.py` | The `gh` invocations onboarding and the controller's Project code use: `auth`, `api` (repos, issues, user), `project` (list, create, link, view, field-list, item-list, item-add, item-edit). | Exits 4 on any unlisted invocation. Exits 5 if `GH_TOKEN`, `GITHUB_TOKEN` or `GH_ENTERPRISE_TOKEN` is set, and records the refusal in a sidecar `<state>.refused` file without reading or mutating state. |
| `opencode` | `tests/fake_opencode.py` | The read-only API probe (`api GET /api/info`, `/api/agent/{orchestrator,fixer,oracle}`). Every call is logged. | `run` (a paid model session) always exits 3. Unknown forms exit 4. |
| `systemctl` | `tests/fake_systemctl.py` | `--user` with `enable`, `disable`, `restart`, `start`, `stop`, `is-active`, `status`, for `autocontroller.service` only. | Anything else exits 4. Faults fail before any effect. |

State lives in JSON files inside the disposable world. `fake_gh.py` takes an
`fcntl` lock on a sidecar file and replaces state atomically. Every invocation
is appended to `state.calls` with its exit status, so tests can count mutations.

### Fault injection

A fault is `{"match": [tokens], "mode": "before" | "after", "rc": N, "once": bool, "skip": N}`.

- `match` tokens must appear in argv as a prefix-matched, in-order subsequence.
- `skip` lets a test target the Nth matching call.
- `before`: nothing is applied; the call fails with `rc`.
- `after`: the mutation is applied, and then the call fails with `rc` and the
  response is discarded. This models a lost response after the server accepted
  the request, which is what makes resume paths matter.

## 2. Disposable world

`tests/onboarding_harness.py` builds one `World` per test:

- **Install root**: a temporary copy of `onboarding.py`, `autonomy-controller.py`,
  `bootstrap_policy.py`, `release_policy.py`, `memory.py`, `config.example.json`
  and `standards/`. Onboarding writes `config.json` and `onboarding.lock` here,
  never in the checkout.
- **Remotes**: bare git repositories under a temporary directory. A global
  `url.<file>.insteadOf` rewrites `https://github.com/` to them. Clones, fetches
  and pushes therefore run against local bare repos.
- **HOME**: a temporary directory. Checkouts go to `$HOME/repositories/<key>`.
- **Environment**: built from scratch for each subprocess. It has a `PATH` with
  the shims first, `GIT_CONFIG_GLOBAL` pointing at a temporary file, and no
  inherited tokens.
- **Guards**: failure-path tests assert that the checkout directory and the install
  root's `config.json` are not created. The harness fails fast if a subprocess
  exceeds its timeout (120 s per call).

Every subprocess has a timeout. The whole integration module (32 tests) runs in about 48 s
on the development host.

## 3. Fault-injection phase matrix

`InterruptionResumeTests.test_interrupted_at_each_phase_then_resumes_to_exact_state`
fails one phase per subtest. The first run must exit nonzero with no
`Onboarded` line. A fault-free rerun must converge to the exact state: one
config entry, one Project titled `AutoController · <repo>`, one bootstrap
issue, trusted issues present once, the outsider absent, one seed commit, a
clean checkout on the default branch, and an `enable` or `restart` as the last
successful unit action.

| Phase | Injected fault |
| --- | --- |
| `auth login` | Not logged in, login aborts |
| `api repos/` | Repository lookup fails |
| `PATCH has_issues` | Issues disabled, PATCH applied then response lost |
| `git push seed` | Empty-repo seed push rejected once by a pre-receive hook |
| `project list` | Listing fails |
| `project create` | Create applied, response lost |
| `project link` | Link fails |
| `project field-list` | Field listing fails |
| `project item-list` | Item listing fails |
| `issues list` | Issue listing fails |
| `POST issue` | Bootstrap issue created, response lost |
| `first item-add` | Bootstrap item added, response lost |
| `later item-add` | A later item-add fails before effect |
| `final item-list` | Final presence check fails |
| `systemctl enable` | Enable fails |
| `systemctl restart` | Restart fails |

Other recovery and duplicate tests:

- A second successful run is a no-op: no mutating GitHub call and an identical config file.
- Lost local config, with remote Project and bootstrap intact: the rerun creates no Project, no item-add and no issue.
- A held `onboarding.lock` refuses the run before any call.
- Existing config entries and settings are preserved, and the config mode stays `0600`.
- Uncommitted work and a checkout ahead of its remote are refused and left untouched.

## 4. Scenario matrix

| Area | Scenarios (test names in `tests/test_onboarding_integration.py`) |
| --- | --- |
| Happy paths | Existing personal repo on `main`; existing org repo on `trunk`; empty personal and org repos get exactly one seed commit on `trunk`. |
| Merge modes | Preferred method is squash, then merge, then rebase; no supported method refuses before any mutation. |
| Permissions | Missing push refused; issues disabled and no admin refused; issues disabled with admin patched; org Project creation denied; another user's personal repo refused; missing `project` scope triggers refresh; refresh failure leaves no remote mutation. |
| OAuth handoff | Not logged in runs `auth login --web --scopes project,workflow` with exact arguments; login abort is nonzero with no mutation; `GH_TOKEN` in the onboarding environment is refused before any `gh` call; the token value never appears in output or config. |
| Interruption / resume | The 16-phase matrix above, run with the OWNER viewer. The `POST issue` phase is also run with a CONTRIBUTOR viewer (`test_post_issue_interruption_with_contributor_viewer_resumes_to_one_bootstrap`). |
| Duplicates and lock | Second run no-op; lost local config reuses remote state; lost config with a CONTRIBUTOR viewer creates no second bootstrap (`test_lost_config_rerun_with_contributor_viewer_creates_no_second_bootstrap`); concurrent lock refused. |
| Existing state | Config preserved; `insteadOf` resume accepted; uncommitted and ahead checkouts refused. |
| Admission and isolation | Only open, trusted, non-Renovate Issues are imported; foreign-repo items refuse before linking; a Project assigned to another configured repo is refused; a closed same-title Project is refused; no side effects outside the world. |
| Config shape | The entry onboarding writes equals the shared fixture `tests/onboarded_fixture.py` (drift guard). |

Controller-side admission and authority tests (`tests/test_admission_and_authority.py`)
use the same fixture as the bootstrap config. They cover failed bootstrap PR or
merged-main CI, `remote_errors` refusal, closed or terminal bootstrap without
proof, bootstrap priority, admission without Ready, empty boards, unlisted
selections, outsider approval text, fork Renovate impersonation, fork closing
references, outsider comments not waking the selector, and a static check that
the controller never calls `item-add`.

## 5. Defects found and fixed by this work

Each fix has a regression test that fails on the unfixed code.

| Defect | Fix | Regression test |
| --- | --- | --- |
| **Resume refused its own checkout.** `create_checkout` read `git remote get-url origin`, which applies `insteadOf`. A mirror or proxy rewrite made the origin look foreign. | Read `config --get remote.origin.url` instead. | `test_resume_with_global_insteadof_rewrite_accepts_checkout_it_cloned` |
| **Linked before refusing.** `project link` ran before the contamination check, so a Project holding another repository's Issue was linked to this repository before onboarding refused. | Check item contamination before any mutation. | `test_existing_project_with_foreign_repo_issue_refused_before_linking` |
| **Outsider bootstrap marker adopted (B1).** Any issue containing the marker text was adopted as the bootstrap, including outsider copies on public repositories. | Adopt only issues with a trusted `author_association`. | `test_outsider_marker_issue_is_not_adopted_as_bootstrap`, `test_outsider_marker_does_not_cause_multiple_bootstrap_refusal` (in `tests/test_onboarding.py`) |
| **Bootstrap not explicitly admitted (B2).** Onboarding relied on the author-association import rule to admit the bootstrap it had just created. A non-trusted creator's bootstrap was never admitted. | Add the bootstrap item explicitly, idempotently, before the final presence check. The explicit `item-add` runs only when the bootstrap is not already a Project item, so a resume adds it at most once. Tests assert exactly one bootstrap issue across the first run and the resume. | `test_bootstrap_issue_admitted_explicitly_even_when_import_rule_would_skip_it`, `test_bootstrap_item_add_is_idempotent_on_resume` (asserts one bootstrap issue and one `item-add`) |
| **Own bootstrap not re-found by a non-trusted account (B-1).** B1 required a trusted `author_association` to adopt a marker issue. A viewer with a non-trusted association (for example a private-org member shown as `CONTRIBUTOR`) could not find its own bootstrap on rerun, so a lost config or a lost `POST` response produced a second bootstrap issue. | `find_or_create_bootstrap(repo, number, user)` also adopts a marker issue whose `user.id` equals the authenticated account's numeric id. An outsider cannot reproduce that id, so the B1 protection still holds. | `test_post_issue_interruption_with_contributor_viewer_resumes_to_one_bootstrap` and `test_lost_config_rerun_with_contributor_viewer_creates_no_second_bootstrap` (fail on the unfixed code with `2 != 1` and a second POST), `test_own_marker_issue_adopted_by_account_id_when_association_untrusted`, `test_untrusted_marker_by_other_account_not_adopted_by_id` (in `tests/test_onboarding.py`) |
| **Ambiguous checkout origin.** `create_checkout` read the origin with `git config --get`, which silently returns the last of several values. | Read all values with `--get-all` and require exactly one, otherwise refuse before any mutation. | `test_checkout_with_two_origin_urls_refused_without_mutation` |
| **Outsider comments woke the selector (B3).** The selector signature hashed `updated_at` and comments, so any commenter started a new paid OMO selection after a NO_ACTION decision. | Signature is a board digest plus comment-independent per-issue digests. A pure `selector_wake` decides; newly seen issues merge without waking; legacy string signatures compare by equality. | `test_outsider_comment_after_no_action_does_not_start_ai` (in `tests/test_agent_backlog_selection.py`), plus the other `OutsiderCommentWake` tests and `test_outsider_comment_after_no_action_does_not_start_second_ai_session` in `tests/test_admission_and_authority.py` |

## 6. Fake semantics: assumptions to confirm live

The fake encodes assumptions about GitHub and the `gh` CLI. None of them has been
checked against live GitHub. A live run must confirm or correct each one.

1. `gh auth status --hostname github.com` exit codes and the `auth login --web --scopes` argv.
2. `gh api -i user` returns an `X-OAuth-Scopes` header, and `gh auth refresh --scopes` adds scopes.
3. `gh auth setup-git` is accepted without side effects on the rest of the environment.
4. `gh api repos/O/R` fields used by onboarding: `permissions.push`, `permissions.admin`, `has_issues`, `allow_*_merge`, `default_branch`, `owner.login`, `viewer_association`.
5. Disabled issues on a repository can be enabled by a PATCH with `has_issues=true` for admins, and refused otherwise.
6. `gh api --method POST repos/O/R/issues` returns the created issue with `author_association` and `user`.
7. `gh project list --owner … --format json --limit 1000 --closed` shape, and the rule that a personal owner can only create Projects for the authenticated user.
8. `gh project create` and `link` argument shapes, and the shape of `project item-list` rows (`content.url`, `content.type`, `status`, `totalCount`).
9. `gh project item-add` is idempotent for an Issue already in the Project. The fake treats a repeat as success; live GitHub may instead error.
10. `gh project item-edit --single-select-option-id` and the status field id.
11. The `--paginate --slurp` output is a list of pages. The fake returns one page, so pagination truncation is not exercised.
12. Token environment variables (`GH_TOKEN`, `GITHUB_TOKEN`, `GH_ENTERPRISE_TOKEN`) are the only ones the `gh` CLI reads that would bypass the device flow.
13. `git` insteadOf rewrites apply to clone, fetch and push but not to `remote.origin.url` config reads.
14. The `opencode api` probe contract (`api GET /api/info`) and the role endpoints.
15. `systemctl --user enable --now` and `restart` return nonzero on failure, and do not touch any unit other than `autocontroller.service`.
16. The `selector_signature` activity read (`repos/O/R/issues?state=all&sort=updated`) returns comments-independent fields the decider needs.
17. `gh project item-list` returns `content.body` for each Issue row. The controller's board digest (`body_sha256`) depends on it; without it, body edits would not wake the selector.
18. `POST repos/O/R/issues` returns `user.id` for the authenticated account. Bootstrap re-discovery for a non-trusted viewer depends on it (B-1).

**Security note.** Once a maintainer admits an outsider-authored Issue to the Project (for example by adding it manually), that author's later title, body and label edits can wake a new selection. Admission is the trust decision; the selector does not re-check the author.

**Selector upgrade.** The selector signature changed from a plain string to a dict snapshot. Existing skip files therefore cause one deliberate re-selection per project on the first iteration after upgrade. After that, snapshots compare as dicts and outsider comments do not wake.

**Selector window limitation.** Only the 100 most recently updated issues are read. An Issue outside that window when NO_ACTION was recorded is merged silently when it reappears, so its label, state, milestone or dependency changes wake triage only at the daily `review_after` recheck. Board-row changes are still caught through the board digest.

## 7. Live sandbox onboarding path: DESIGN ONLY

**Status: designed, not implemented, not executed.** Implementation and execution
are owned by Issue #4. Nothing described in this section exists in the
repository. No live GitHub, OpenCode or systemd call has been made for it.

Purpose: confirm the assumptions in section 6 against real GitHub without
touching production repositories or the owner's account.

Design constraints:

- **Owner-invoked manually.** A person starts it from a shell. Nothing starts it automatically.
- **Dedicated sandbox identity.** A GitHub account and an organization created for
  testing, with no access to production repositories, Projects, secrets or
  billing. Its credentials are supplied interactively at run time.
- **Disposable everything.** Repositories, Projects and the test VM are created for
  one run and deleted afterwards. The test uses a fresh HOME and install root on
  a disposable VM (see `vmtest/`), never the operator's host.
- **Allowlist and opt-in.** The runner refuses unless an explicit sandbox owner
  allowlist names the target owner, and an explicit opt-in variable is set for that
  invocation. Any other owner or repository is refused before the first call.
- **Never in automation.** It is not run by `required.yml`, `regression.yml`, any
  `pull_request` or `push` workflow, `scripts/offline-tests.sh`, or
  `scripts/check-syntax.sh`.
- **No secrets in the repository.** No token, device-flow state, sandbox owner
  name beyond a documented placeholder, or config is committed. Credentials stay in
  the operator's keyring or the VM.
- **Bounded spend.** A hard cap on runtime, on the number of created repositories
  and Projects, and on AI calls. The live path must make no OMO or model session.
  The cap is enforced before any paid action.
- **Cleanup.** A teardown step deletes or archives every resource it created, keyed by
  a run identifier recorded before creation. Teardown runs even after a failure.
  The result is reported with exact counts.
- **Evidence.** The run records command exit statuses, GitHub state before and after,
  and the list of created and deleted resources. It is pasted into the release
  Issue, not stored in the repository.

Open design questions for Issue #4: how the allowlist is supplied; whether the
sandbox organization is shared or per-run; and the exact cost ceiling.

## 8. Limits

- The suite does not prove GitHub's real responses. Section 6 lists what it assumes.
- No test runs `install.sh`, the real `autonomy-controller.py` service, or a real OpenCode session.
- Pagination, rate limits, SSO, branch protection and proxies are not simulated.
- Live onboarding, the sandbox path in section 7 and the VM lifecycle suite are separate; see [VALIDATION.md](VALIDATION.md).
