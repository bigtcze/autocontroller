# Release evidence

Initial and feature releases need `docs/releases/<tag>.evidence.json` at the tag
commit. Patch releases need no evidence. Release notes may also live in
`docs/releases/<tag>.md`.

## Ownership

- One file per feature or initial release, at `docs/releases/<tag>.evidence.json`.
- The owner writes and commits it after the live acceptance runs and after a
  successful `vm-lifecycle.yml` run on the exact candidate commit.
- Agents must never create, edit or fabricate this file. Nothing in the
  repository can produce valid live evidence.

## Schema 1

Exactly these top-level keys, and no others:

| Field | Rule |
| --- | --- |
| `schema` | the integer `1` |
| `tag` | the release tag, for example `v1.1.0` |
| `candidate_sha` | the 40-character hex commit whose tests and VM run were evaluated |
| `vm_lifecycle_run` | a positive integer GitHub Actions run id of `.github/workflows/vm-lifecycle.yml` |
| `live_acceptance` | an object with exactly `github`, `opencode_omo` and `engram` |

Each `live_acceptance` entry is an object with:

| Field | Rule |
| --- | --- |
| `result` | the string `passed` |
| `report_sha256` | 64 hex characters: the SHA256 of the owner's report file, which stays outside this repository |
| `date` | an ISO date, `YYYY-MM-DD` |
| `summary` | 1 to 500 characters |

Shape only. The values below are placeholders, not a valid record:

```json
{
  "schema": 1,
  "tag": "v1.1.0",
  "candidate_sha": "<40 hex characters>",
  "vm_lifecycle_run": 1,
  "live_acceptance": {
    "github": {"result": "passed", "report_sha256": "<64 hex characters>", "date": "YYYY-MM-DD", "summary": "<text>"},
    "opencode_omo": {"result": "passed", "report_sha256": "<64 hex characters>", "date": "YYYY-MM-DD", "summary": "<text>"},
    "engram": {"result": "passed", "report_sha256": "<64 hex characters>", "date": "YYYY-MM-DD", "summary": "<text>"}
  }
}
```

## What the release workflow checks

- The file exists at the tag commit, is valid JSON and matches the schema above.
- `candidate_sha` is an ancestor of the tag commit. Every change between them is under `docs/releases/`.
- `vm_lifecycle_run` is a completed, successful run of `.github/workflows/vm-lifecycle.yml` whose `head_sha` equals `candidate_sha`.
- The jobs `vm (debian-12)`, `vm (debian-13)`, `vm (ubuntu-24.04)` and `vm (ubuntu-26.04)` each succeeded in that run.

## Limits

- The check is structural. It does not read the reports, judge the acceptance results or prove the owner's approval.
- Changing the schema is an owner-authorized change to `release_policy.py`.
