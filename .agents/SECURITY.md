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

- Work authorization: an Issue is schedulable only as a native Issue of the
  configured repository and a member of its configured GitHub Project with a
  nonterminal status. Onboarding imports only open Issues authored by
  `OWNER`/`MEMBER`/`COLLABORATOR`. The controller itself never adds Issues to the
  Project after onboarding. The orchestrator, per the work prompt, adds the
  necessary follow-up Issues it creates for an already-authorized task to the same
  repository's configured Project and verifies them; it never admits unrelated or
  external Issues.
- Renovate identity: login `renovate[bot]`, type `Bot` and the GitHub.com
  Renovate App user id, **and** head repository equal to the configured
  repository. Branch prefix or login text alone is never sufficient; the live
  PR is re-verified before any agent invocation.
- Linked, repair and release-preparation PRs are considered only when their
  head is in the same repository; fork PRs are ignored.
- Merges require the exact reviewed head SHA, green `required` and no admin
  bypass. Feature releases require an owner comment bound to tag and SHA.
- Prompts label GitHub content as untrusted data. A follow-up Issue may carry the release-blocker label only when it is a genuine blocker found during authorized work; the label never authorizes work.
- Least privilege: workflows default to `contents: read`; only the release
  `publish` job has `contents: write`. No `pull_request_target`, no secrets in
  PR workflows, `persist-credentials: false`, third-party actions pinned to
  full commit SHAs (Renovate maintains digests). CI and release container
  images are pinned to image digests, which Renovate also maintains.
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
