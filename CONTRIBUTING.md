# Contributing to Impasse

Thanks for your interest. Impasse is intentionally small and solo-maintained, so please open
an issue to discuss before a large change.

## Ground rules

- **Keep the shipped helpers stdlib-only.** No runtime pip dependencies in `scripts/`.
  `jsonschema` is a dev/CI dependency (used by `tests/validate_schemas.py`) — that's the line.
- **Schemas are a contract.** Changes go through a new version file (`*.v2.json`), not an
  edit that breaks existing validators. Add example fixtures under `schemas/examples/` and
  keep them valid.
- **The review path stays read-only.** Anything that edits an artifact belongs in delegate
  mode ([`docs/delegate-mode.md`](docs/delegate-mode.md)), which is experimental and isolated.
- **Honesty over polish.** Don't claim platform support, provider neutrality, or safety the
  code doesn't actually provide — document limitations instead.

## Before a PR

```bash
pip install jsonschema        # dev only
python tests/validate_schemas.py
python tests/test_helpers.py
```

Both must pass (CI runs them). Please describe what you changed and why, and note any schema
or security-model implications.

## Contributor terms

By submitting a contribution (a pull request, patch, or otherwise), you agree it is licensed
under the same [MIT License](LICENSE) that covers this project ("inbound = outbound"), and you
certify you wrote it or otherwise have the right to submit it under that license. You keep your
copyright.

**Sign your work (optional but appreciated).** You may certify the above with a
[Developer Certificate of Origin](https://developercertificate.org/) sign-off — add a
`Signed-off-by` line via `git commit -s`. It isn't required for this pre-release project.
