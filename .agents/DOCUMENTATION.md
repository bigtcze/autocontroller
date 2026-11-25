# Documentation

- `README.md`: user-facing overview, install, daily use, operating model,
  security boundary, development commands, license. Must describe actual
  behavior and the alpha status/limitations honestly.
- `docs/VALIDATION.md`: release-readiness matrix — what automated tests and CI
  prove, and what still requires live VM/GitHub acceptance.
- `AGENTS.md` and `.agents/*`: engineering contracts (owner-authorized changes
  only).
- `THIRD_PARTY.md`: provenance and license notices of adapted third-party
  material; `LICENSE`: MIT.
- `config.example.json`: every supported config key default.

Update documentation in the same PR as the behavior change. Never document
planned features as implemented; mark unknowns explicitly. No personal paths,
credentials or real runtime data in examples.
