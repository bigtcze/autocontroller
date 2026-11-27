# Release quality

Status: no release has been published. Publication requires explicit owner
authorization.

## Contract

- Versions are stable SemVer tags `vMAJOR.MINOR.PATCH`. No pre-release tags or
  releases (release candidates, previews, etc.) are published, and no
  pre-release tag triggers publication.
- `.github/workflows/release.yml` runs on such tag pushes: it re-runs the full
  regression matrix (`regression.yml`), verifies the tagged commit is on
  `main`, then creates the GitHub Release with generated notes, or with
  `docs/releases/<tag>.md` when present. Only the `publish` job has
  `contents: write`.
- Tags are created only by the controller/owner at a commit whose push CI is
  green. Agents never run `gh release create` or push tags.
- Feature releases need the owner's explicit approval of tag and exact SHA;
  technical maintenance releases need no owner approval and follow controller
  cadence policy, but meet every per-release requirement below.
- Release notes follow Keep a Changelog sections and describe only merged,
  verified changes and known limitations.
- Every public stable release is published only for one exact candidate commit
  on `main` whose `required` push CI is green, and must have:
  - Reproducible release artifacts with a verified SHA256 manifest.
  - No open release-blocking Issues.
  - No partial releases, such as source code without verified artifacts.
- The initial release and every feature release additionally require:
  - Recorded real evidence for that candidate: regression, integration and
    disposable systemd VM tests on every supported distribution, and live
    sandbox acceptance of the GitHub, OpenCode/OMO lifecycle and Engram
    learning and recovery paths.
  - Owner approval of the exact tag and SHA, per the feature-release bullet
    above (the initial release is always a feature release; see
    `release_policy.is_feature_release`).
- Deterministic fixtures and simulations never substitute for required live
  acceptance. When live prerequisites (sandbox credentials, VMs) are
  unavailable, that is a release blocker, never a waived test.
- Release-specific scope, checklists and readiness status are tracked in GitHub
  Issues, not in this file.

## Implementation state (offline-tested; not yet run on GitHub)

- `.github/workflows/release.yml`, on a SemVer tag push:
  - `qualify` re-runs the regression matrix (`regression.yml`).
  - `candidate` checks the tag is on `main`, then `release_artifacts.py qualify` requires a completed, successful `required.yml` push run on the exact tag commit, no open `release-blocker` Issues, and, for feature and initial releases, `docs/releases/<tag>.evidence.json` (see `docs/releases/README.md`).
  - Evidence must name a candidate commit that is an ancestor of the tag commit. Only `docs/releases/` may change between them.
  - The evidence must reference a completed, successful `vm-lifecycle.yml` run on that candidate, with all four `vm (<distro>)` jobs successful.
  - `artifacts` builds `autocontroller-<tag>.tar.gz` and `SHA256SUMS` in `debian:12`, `debian:13`, `ubuntu:24.04` and `ubuntu:26.04` containers. The four builds must be byte-identical.
  - `publish` rebuilds on the runner, must match the artifact digest, creates a draft release with both assets, checks each asset's GitHub digest against the local SHA256 values, and only then publishes. It refuses to replace an existing published release.
  - The publish logic lives in `release_artifacts.py publish`; the workflow only calls it with `--notes-file` or `--generate-notes`. The blocker check is repeated just before publishing, so a blocker opened after qualification keeps the draft unpublished and deletes it.
- Controller: blocks tag creation while `release-blocker` Issues are open (label from `release.blocker_label`). The label must exist: GitHub lists no Issues for a missing label, so a missing label or unreadable label listing blocks the release. AutoController's own release must use the canonical `release-blocker` label, which onboarding creates when absent.
- Installed-controller lifecycle (`onboarding.py update|rollback|remove|uninstall`):
  - Run lifecycle commands from a normal terminal. They are refused when `OPENCODE_SESSION_ID` or any `AUTONOMY_*` marker is set. This is a logical guard against an agent running them, not a sandbox.
  - `rollback --recover` resolves an interrupted rollback. A leftover `rollback/swap` blocks `update` and `rollback`, and `doctor` reports it as a failure. Recovery restores the release the rollback replaced and verifies it against `MANIFEST.sha256` before the marker is removed. When the rollback had already replaced the old store, recovery finishes the commit. A leftover `rollback/previous.old` with no swap is deleted by the same command; `doctor` warns about it until then.
  - A drain request counts only while an operation holds `lifecycle.lock`. A `state/drain.request` without that lock is stale: workers ignore it and `doctor` fails with the `rm` command to remove it. Remove it by hand only after confirming no update is running.
  - Restart risk (not tested live): onboarding enables `autocontroller.service`. A swap stops the unit first, but if the machine reboots while a swap is in progress, the enabled unit can start on a partly replaced tree. `doctor` reports a leftover `rollback/swap` or `rollback/incoming`, and `rollback --recover` restores the pre-rollback tree from `swap`. A leftover `incoming` (an interrupted update) is not restored by any command yet; it blocks further updates until a human inspects it. Nothing here prevents a boot-time start.
- `required_assets` entries may use the `{tag}` placeholder.

Still required, and still release blockers:

- Owner approval of the exact tag and SHA for feature releases. The controller enforces this.
- Live evidence content (sandbox acceptance reports) is recorded by the owner. Automation validates only its structure and the report hashes, not the reports themselves.
- None of the release workflow, the container builds, or `update` against a published GitHub release has run on GitHub yet.
- Live acceptance is still a release blocker. Simulated results never substitute for it.
