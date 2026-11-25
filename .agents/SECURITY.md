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
  `OWNER`/`MEMBER`/`COLLABORATOR`; nothing is auto-added later.
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
- Tests are offline: no tokens, no network, no `gh`/`opencode`/`systemctl`,
  isolated `HOME`.

## Known limitations

Agents are not sandboxed and run with the controller user's privileges; the
memory scope is logical only. Use a dedicated account, trusted repositories and
no passwordless sudo.

## Vulnerability reporting

Report suspected vulnerabilities privately via GitHub Security Advisories
("Report a vulnerability") on `bigtcze/autocontroller`; do not open a public
Issue with exploit details. Do not change remote security settings or grant
permissions without owner authorization.
