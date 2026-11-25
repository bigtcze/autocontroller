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

## Not yet defined (tracked separately)

Release archives, SHA256 manifests, install/upgrade/rollback verification from
a release artifact and managed update are not implemented. Until they are, a
release is source-only and must not be advertised as a certified installer.
Before the first public release the live acceptance in `docs/VALIDATION.md`
must be completed.
