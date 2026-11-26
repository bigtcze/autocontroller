# Security

## Threat model

AutoController runs on a dedicated VM account with persistent GitHub CLI OAuth
credentials and executes AI agents that run arbitrary code from managed
repositories. Relevant threats:

1. **Public-repository impersonation**: outsiders open Issues, fork PRs or
   branches named like bots (`renovate/...`), or link fork PRs with
   `Closes #n`, to get agent work, merges or blocking state.
2. **Prompt injection** via Issue/PR/comment/repository content.
3. **Malicious dependencies/actions** (supply chain) in this repo and in CI.
4. **Credential exposure**: GitHub token, provider credentials, memory grants.
5. **Cross-project leakage** through checkouts, state or memory.
6. **Production interference** by development work on this repository.

## Required controls

- Work authorization (single authoritative rule): an Issue may be offered to
  the selector only when it is a native Issue of the configured repository, is
  a member of its configured GitHub Project, and its Project Status is `Todo`
  or `To do` (new work), or `In Progress` or `Review` (recovery of work the
  controller already admitted). Blank, `Backlog`, `Blocked`, custom and terminal
  (`Done`, `Completed`, `Closed`, `Cancelled`, `Archived`) statuses are
  non-authorizing. Python never sorts candidates; ordering and dependencies are
  OMO's. Onboarding imports only open Issues authored by
  `OWNER`/`MEMBER`/`COLLABORATOR`, sets each imported blank row to `Todo` and
  never overrides a status a maintainer set. Nothing else is added to the
  Project by the controller except through `followup.py`, the only permitted
  `project item-add` writer outside onboarding (see the follow-up rule below).
- Follow-up admission: an admitted Issue task may create at most 5 NECESSARY
  follow-up Issues per parent, only through `followup.py create`, authorized by a
  token-bound `followup-grant.json` that the controller issues only for `kind=issue`
  phases `code` and `repair`. Each follow-up carries the marker
  `<!-- autocontroller-followup parent=<P> key=<K> -->`, is created by the
  controller account, is admitted to Todo, and is journaled in `followups.json`.
  The CLI acts only while the granted parent is the live pending Issue checkpoint
  of the same project directory (`cycle.pending` and `cycle.json` with `kind=issue`
  and `number` equal to the grant's parent). Before journaling, the parent and every
  `blocks`/`blocked_by` target must be live, open, non-pull-request Issues of the
  granted repository, members of the granted Project, with a non-blank, nonterminal
  Status. This is link validation only; it does not grant scheduling authority, which
  is the Work authorization rule above.
  Authorship of an adopted or created follow-up is decided by the controller
  account's **numeric id** recorded at intent time, never by login text. The
  controller reconciles journal entries without AI, refuses any entry whose
  repository or Project differs from its worker's (case-insensitive owner and
  repository, exact project number) before any `gh` call, and defers selection
  and maintenance while a follow-up is blocked.
- Logical control, not a sandbox: an agent holding the controller account's
  credentials could call `gh` directly. This rule governs what the controller
  schedules, and an Issue outside `Todo` is never scheduled, but it does not stop
  a process that has the credentials from creating an Issue.
- Renovate identity: login `renovate[bot]`, type `Bot` and the GitHub.com
  Renovate App user id, **and** head repository equal to the configured
  repository. Branch prefix or login text alone is never sufficient; the live
  PR is re-verified before any agent invocation.
- Linked, repair and release-preparation PRs are considered only when their
  head is in the same repository; fork PRs are ignored.
- Merges require the exact reviewed head SHA, green `required` and no admin
  bypass. Feature releases require an owner comment bound to tag and SHA.
- Prompts label GitHub content as untrusted data.
- Least privilege: workflows default to `contents: read`; only the release
  `publish` job has `contents: write`. No `pull_request_target`, no secrets in
  PR workflows, `persist-credentials: false`, third-party actions pinned to
  full commit SHAs (Renovate maintains digests).
- Runtime files 0600/0700, `UMask=0077`, `NoNewPrivileges=true`; the memory
  socket directory must be private; Engram downloads are checksum-verified and
  size-bounded.
- The regression suite (locally and in PR/push CI) runs offline: no tokens, no
  network, no `gh`/`opencode`/`systemctl`, isolated `HOME`. Other test paths:
  see the scope rule in `.agents/TEST_STRATEGY.md`.

## Known limitations

Agents are not sandboxed and run with the controller user's privileges; the
memory scope is logical only. Use a dedicated account, trusted repositories and
no passwordless sudo.

## Vulnerability reporting

Report suspected vulnerabilities privately via GitHub Security Advisories
("Report a vulnerability") on `bigtcze/autocontroller`; do not open a public
Issue with exploit details. Do not change remote security settings or grant
permissions without owner authorization.
