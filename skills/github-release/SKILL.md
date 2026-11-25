---
name: github-release
description: Prepare consistent, verified GitHub feature and maintenance release notes using SemVer and Keep a Changelog conventions; never publish without the external controller gate.
compatibility: Requires git and GitHub CLI (gh)
---

# GitHub release preparation

Adapted for autonomous OpenCode operation from GitHub's `github/awesome-copilot` `github-release` skill (MIT), plus GitHub's documented release-notes conventions.
Source: https://github.com/github/awesome-copilot/tree/main/skills/github-release

## Scope and authority

This skill prepares a release candidate **in the existing checkout**. It does NOT own scheduling, approval, merging, tags or publishing. AutoController owns those operations outside all repositories. Never ask for human input during preparation, and never impersonate approval. A feature release requires only the controller's explicit owner acceptance gate before publication. Maintenance patches do not require human acceptance.

Follow the project's AGENTS.md and release/engineering contracts first. Do not modify, weaken, or replace them with this generic skill. Use existing native versioning and release automation; do not introduce a new release pipeline or controller files into the repository.

## Release preparation

1. Verify authenticated `gh`, clean checkout, stable SemVer release tag and the most recent published release. Compare the **actual changes** since the previous release, including merged PR titles, bodies and the source diff; never infer behavior from commit titles alone.
2. Select the intended version: features use the release coordinator's authorized major/minor version; routine maintenance is a PATCH increment selected by the controller. Never silently promote a maintenance release to a feature release.
3. Prepare release notes in the repository's normal release notes location (for example `docs/releases/vX.Y.Z.md`). Keep sections consistent: `Highlights` when there are actual user-visible changes, `Fixes`, `Security`, `Dependencies`, and `Upgrade notes` when applicable. Omit empty headings. Explain user-facing impacts, not internal commit-message jargon. Give PR references, note meaningful breaking changes or upgrade/migration impacts, and link to the full compare range.
4. For a dependency-only technical patch, describe meaningful dependency/security fixes and update impact; never claim product features, vulnerability remediation, platform tests, or image artifacts without evidence. Where upgrade impacts are unknown, say so rather than inventing them.
5. Preserve the project's required production validation and release artifacts. Create or update the single release-preparation PR through normal branch protection and required CI. Obtain the independent OMO `oracle` review inside the orchestrator's normal task session, fix blockers through `fixer`, and leave any unresolved PR in Draft. The controller then verifies GitHub CI and performs the protected merge; the native tag-triggered workflow handles publication. If requirements cannot be verified, report blockers rather than declare success.

## Prohibited actions

- Do not push a release tag, run `gh release create`, publish artifacts, or mark a release issue Done.
- Do not modify controller configuration or add repo-local autonomy policies.
- Do not ask for arbitrary approvals of routine technical releases.
- Do not use GitHub auto-generated release notes as sole evidence of acceptance, testing or changes.
