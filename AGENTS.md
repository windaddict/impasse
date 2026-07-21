# AGENTS.md — Impasse (for Codex / agent contributors)

Guidance for working **on** this repo (not for *using* the skill — that's `SKILL.md`). The full
contributor guide is [`CLAUDE.md`](CLAUDE.md); the essentials:

## Before every commit — all three must pass

```bash
python3 tests/test_helpers.py       # stdlib, no pytest
.venv/bin/python3 tests/validate_schemas.py   # jsonschema lives in the repo-root .venv, not on PATH
ruff check scripts/ tests/
```

## Non-negotiable invariants

- **stdlib-only** in `scripts/` (no pip deps; `jsonschema` is tests-only).
- Reviewer output is **untrusted** — don't render it raw or let it build a filesystem path
  (`_clean`, and `_safe_id` / `_run_dir`). The reviewer sets `review_id`.
- **Read-only** on the artifact; consent is block-by-default and the codex backend runs hermetic.
- A **rejection needs contradicting evidence** (schema-enforced); otherwise it's a `deadlocked`
  item with `dispute_kind: unverified_refutation`.
- Schema change ⇒ add a positive example **and** a negative fixture in `schemas/examples/invalid/`.

See [`CLAUDE.md`](CLAUDE.md) for layout, the independence ladder, model selection, and the release flow.
