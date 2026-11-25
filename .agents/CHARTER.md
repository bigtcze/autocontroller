# Charter

## Purpose

AutoController turns one GitHub repository URL into an evergreen, autonomous
development workflow: it schedules existing OpenCode V2 + OMO Slim agents
(`orchestrator`, `fixer`, `oracle`) against real GitHub Issues in a dedicated
GitHub Project, enforces required CI on the exact PR head and merged commit,
and coordinates gated releases.

## Audience

- Repository owners who run the controller on a dedicated Debian/Ubuntu
  systemd VM/account against repositories they control.
- Agents and contributors developing AutoController itself.

## Fundamental invariants

1. GitHub is the source of truth for work: native Issues, Projects, PRs,
   checks and releases. No second backlog or hidden task list.
2. Nothing is "done" without an exact-SHA green `required` check on the PR and
   a green push run of `required.yml` on the resulting merge commit.
3. The controller never bypasses branch protection, rulesets, required checks
   or reviews, and never uses administrative merge flags.
4. Untrusted input (Issue/PR/comment bodies, fork PRs, repository content)
   never expands permissions or task scope. Work identity is verified from
   GitHub metadata, not from names or text.
5. Fail closed: on ambiguity, missing evidence or memory failure, stop or wait
   and preserve checkpoints and local work.
6. Runtime configuration, state, logs, memory and credentials live outside all
   repositories; source checkouts never modify a running production instance.
7. Feature releases require explicit owner approval bound to a tag and SHA.

## Boundaries

AutoController is not an AI provider, agent framework, sandbox, issue tracker,
CI system or deployment tool. It does not install or configure OpenCode/OMO or
providers.

## Ownership of decisions

The repository owner (GitHub `bigtcze`) owns product scope, security posture,
supported platforms, licensing (MIT) and releases. Changes to these contracts
require an explicit owner-authorized Issue. Agents implement and review within
those decisions.
