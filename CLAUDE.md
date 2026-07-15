# CLAUDE.md — Impasse (contributor guide)

Guidance for working **on** this repo with Claude Code. This is *not* how to use Impasse — that's
[`SKILL.md`](SKILL.md) (the runtime protocol) and [`README.md`](README.md). It loads by working
directory, so it applies only to contributors, never to users who invoke the installed skill.

## What this is

A Claude Code **skill** that runs a rival-provider AI (OpenAI Codex, or a Claude fallback) as an
independent reviewer of any artifact, then verifies, reconciles, and escalates only the real
disagreements. The repo *is* the skill directory. Backend-neutral protocol; stdlib-only helpers.

## Before every commit — the three gates

```bash
python3 tests/test_helpers.py       # stdlib, no pytest: supervisor, consent, backends, env policy, records
python3 tests/validate_schemas.py   # needs jsonschema (dev/CI only): positive + negative fixtures
ruff check scripts/ tests/          # lint
```

All three must pass. Docs also follow the repo's understated, honest, no-hype voice — read the
existing docs for tone; no marketing superlatives, and pair any failure mode with its mitigation.

## Invariants a change MUST preserve

- **stdlib-only in `scripts/`.** No pip dependencies in shipped code (`jsonschema` is confined to tests).
- **Reviewer output is untrusted.** Never render it raw (terminal escapes → `_clean` in
  `impasse_report.py`) and never let it build a filesystem path (traversal → `_safe_id` / `_run_dir`
  in `impasse_lib.py`). The reviewer sets `review_id`.
- **Consent is block-by-default**, keyed to the normalized endpoint. The codex backend runs
  hermetic (`--ignore-user-config --ignore-rules`) so config/`AGENTS.md` can't reroute data or
  inject instructions into the read-only reviewer.
- **Read-only on the artifact.** The review path never edits it.
- **A rejection needs contradicting evidence** (schema-enforced). An evidence-less refutation is a
  `deadlocked` item with `dispute_kind: unverified_refutation`, not a `rejected` one.

## Changing a schema

Edit `schemas/*.v1.json`, then add/adjust a **positive** example in `schemas/examples/` **and** a
**negative** fixture in `schemas/examples/invalid/` that proves the new invariant fails.
`validate_schemas.py` discovers both by filename suffix.

## Layout

- `scripts/` — stdlib helpers: `impasse_lib` (config, backends, run records, environment policy),
  `impasse_consent` (consent store), `impasse_run` (process supervisor + `review()`), `impasse_report`.
- `schemas/` — `reviewer-response` + `reconciliation-result` + `examples/` (+ `invalid/`).
- `docs/` — `protocol`, `security-model`, `environments`, `backends/{codex,claude}`, proposals.
- Independence ladder: cross-provider (Codex) > same-provider (`claude -p`) > self-review
  (sandbox/Cowork only, refused for code). Model choice: `--model` / `IMPASSE_{CODEX,CLAUDE}_MODEL`.

## Never commit

Run records and consent (`runs/`, `consent.json`) hold artifact content — they're gitignored. Don't add them.

## Dogfooding

Impasse reviews itself: bundle the scripts as an artifact and run `impasse_run.py review --kind
code --backend codex …`. The cross-provider reviewer has caught real bugs here (path traversal,
terminal-escape injection, supervisor teardown) — run it before shipping substantial changes.

## Maintainer note

This repo is also vendored as a git submodule in a consuming config repo (`~/.claude`). After
pushing here, bump that submodule pointer so the installed skill tracks the new commit.

**Two working trees, one remote.** If you keep both a dev clone and an installed submodule checkout
of this repo, they're separate working trees of the same remote — a session may edit files in both.
Before pushing, consolidate every edit into one clone so a single commit carries them; push; then in
the other checkout discard its local copy (`git checkout -- <path>`), `git fetch`, and
`git checkout <sha>` to the new commit before bumping any submodule pointer.
