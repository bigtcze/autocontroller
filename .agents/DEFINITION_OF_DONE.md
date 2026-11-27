# Definition of done

An Issue is the unit of work. It is done only when all hold:

1. Every acceptance criterion of the Issue is implemented, or an explicit owner
   decision on the Issue removes it. Unimplemented gates are never claimed.
2. Behavior changes and bug fixes have offline regression tests that fail
   without the change; negative/security cases are covered where relevant.
3. `bash scripts/check-syntax.sh` and `bash scripts/offline-tests.sh` (or
   `mise run ci`) pass locally with reported real output.
4. An independent review (OMO `oracle`) assessed the real diff against the
   acceptance criteria and these contracts, including unnecessary complexity,
   duplication and test relevance; avoidable overengineering introduced by the
   PR is a blocking finding, and blocking findings are fixed.
5. One PR containing `Closes #<n>`; the `required` check is green on the exact
   head SHA; the PR is ready (not draft) only after review.
6. After merge, the `required.yml` push run on the exact merge commit is green.
7. README/docs/contracts are updated for user-visible or contract changes;
   compatibility obligations are preserved or migrated.
8. No release, tag or deployment unless the Issue is an authorized release.
