# Release quality

Status: no release has been published. Publication requires explicit owner
authorization.

## Contract

- Versions are stable SemVer tags `vMAJOR.MINOR.PATCH`; no pre-release tags
  trigger publication.
- `.github/workflows/release.yml` runs on such tag pushes: it re-runs the full
  regression matrix (`regression.yml`), verifies the tagged commit is on
  `main`, then creates the GitHub Release with generated notes, or with
  `docs/releases/<tag>.md` when present. Only the `publish` job has
  `contents: write`.
- Tags are created only by the controller/owner at a commit whose push CI is
  green. Agents never run `gh release create` or push tags.
- Feature releases need the owner's explicit approval of tag and exact SHA;
  technical maintenance follows controller policy.
- Release notes follow Keep a Changelog sections and describe only merged,
  verified changes and known limitations.
- Every public stable release is published only for one exact candidate commit
  on `main` whose `required` push CI is green, and must have:
  - Reproducible release artifacts with a verified SHA256 manifest.
  - No open release-blocking Issues.
  - No pre-release tags or releases (release candidates, previews, etc.) and
    no partial releases, such as source code without verified artifacts.
  - No automatic deployment to any installed controller.
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

## Not yet implemented (tracked in GitHub Issues)

Release archives, SHA256 manifests, artifact install/upgrade/rollback
verification, managed update and automated enforcement of the qualification
above are not implemented. Consequently, no public release can qualify until
they are. The qualification evidence, artifact and release-blocking-Issue
requirements above are not enforced by automation today; `release.yml` only
re-runs `regression.yml`, verifies the SemVer tag and main ancestry and creates
the GitHub Release.
