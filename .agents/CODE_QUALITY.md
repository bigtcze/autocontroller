# Code quality

- Python ≥ 3.11, standard library only. Scripts in Bash with
  `set -euo pipefail`; must pass `bash -n`.
- Prefer the smallest maintainable solution that meets actual requirements.
  Reuse established tools and interfaces before adding custom mechanisms;
  avoid speculative abstractions, parallel implementations and infrastructure
  without a concrete need.
- Reuse existing helpers (`command`, `gh_json`, `gh_api_pages`, `json_atomic`,
  `json_read`) instead of ad-hoc subprocess/JSON handling. Keep identity and
  policy predicates in one place (e.g. Renovate identity in
  `release_policy.py`) rather than duplicating them.
- Every subprocess and network call has an explicit timeout; never `shell=True`
  with untrusted values; pass argv lists.
- Fail closed with `ControllerError`/`MemoryError`; never swallow errors that
  decide merge, release or scheduling. Log a concise reason.
- Persist state atomically with restrictive permissions; never store state in
  checkouts.
- Pure decision logic stays side-effect free and unit-testable (see
  `release_policy.py`, `bootstrap_policy.py`).
- Preserve external interfaces listed in `.agents/PRODUCT_SCOPE.md`.
- Static analysis: `scripts/check-syntax.sh` (byte-compile, `bash -n`, JSON
  validation). No linter is configured yet; do not introduce one without an
  Issue, and do not reformat unrelated code.
- Comments explain safety reasons and invariants, not history.
