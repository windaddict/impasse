# Contributing to Impasse

Thanks for your interest. Impasse is intentionally small and solo-maintained, so please open
an issue to discuss before a large change.

## Ground rules

- **Keep the shipped helpers stdlib-only.** No runtime pip dependencies in `scripts/`.
  `jsonschema` is a dev/CI dependency (used by `tests/validate_schemas.py`) — that's the line.
- **Schemas are a contract.** Edit `schemas/*.v1.json` in place for an additive or
  invariant-preserving change, pairing it with a new **positive** example under
  `schemas/examples/` and a **negative** fixture under `schemas/examples/invalid/` that proves
  the new invariant fails. Bump to a new version file (`*.v2.json`) only for a **breaking**
  change — one that would invalidate existing stored records, or make new output that current
  validators reject. See CLAUDE.md's "Changing a schema".
- **The review path stays read-only.** Anything that edits an artifact belongs in delegate
  mode ([`docs/delegate-mode.md`](docs/delegate-mode.md)), which is experimental and isolated.
- **Honesty over polish.** Don't claim platform support, provider neutrality, or safety the
  code doesn't actually provide — document limitations instead.

## Before a PR — the three gates

```bash
python3 tests/test_helpers.py                 # stdlib, no pytest: supervisor, consent, backends, env policy, records
.venv/bin/python3 tests/validate_schemas.py   # jsonschema lives in the repo-root .venv, not on PATH
.venv/bin/ruff check scripts/ tests/          # ruff too
```

All three must pass (CI runs them). `jsonschema` and `ruff` live in the repo-root `.venv`, not
on your PATH — invoke them through `.venv/bin/…` as shown, and run the stdlib helper test with
`python3`. Please describe what you changed and why, and note any schema or security-model
implications.

Project vocabulary: [`docs/glossary.md`](docs/glossary.md).

## Contributor terms

By submitting a contribution (a pull request, patch, or otherwise), you agree it is licensed
under the same [MIT License](LICENSE) that covers this project ("inbound = outbound"), and you
certify you wrote it or otherwise have the right to submit it under that license. You keep your
copyright.

**Sign your work (optional but appreciated).** You may certify the above with a
[Developer Certificate of Origin](https://developercertificate.org/) sign-off — add a
`Signed-off-by` line via `git commit -s`. It isn't required for this pre-release project.
