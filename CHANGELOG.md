# Changelog

All notable changes to Impasse are documented here. This project adheres to
[Semantic Versioning](https://semver.org/) for its schemas and skill.

## [Unreleased]

### Added
- Schemas: `reviewer-response.v1.json` and `reconciliation-result.v1.json` — the
  reviewer emits observations with anchored evidence; reconciliation records the
  per-finding disposition and inline escalated deadlocks. Domain-general via an
  evidence *anchor* union (`file_range | text_quote | section | structured_path |
  generic`) plus an optional `external_source` citation. Invariants are enforced
  (evidence needs anchor+observation; approve ⇒ 0 findings; failed ⇒ failure object).
- Stdlib-only helpers under `scripts/`:
  - `impasse_lib.py` — config dir, cross-platform codex resolution, artifact hashing,
    endpoint normalization.
  - `impasse_consent.py` — block-by-default data-boundary consent, keyed to the
    normalized endpoint (a changed destination re-prompts), with a payload manifest
    and atomic consent-file writes.
  - `impasse_run.py` — supervised reviewer invocation: stdin-EOF, wall + idle
    timeouts, POSIX process-group termination with bounded reap, size-capped capture,
    and JSON/shape classification of reviewer output (never reports failure as success).
    Reliable process-group kill is POSIX-only; Windows is a roadmap.

### Pre-publish hardening (five-reviewer pass: Codex prose + security/bug/coverage agents)
- Docs: reframed the core promise so it matches read-only behavior (you get the verified
  findings *and* the escalated deadlocks, not "only" the deadlock); marked reconciliation as
  host-directed (not script-enforced); absolute skill-root paths; accurate OpenAI-plugin
  comparison; softened the independence claim; a concrete escalation example.
- Code: `review()` output-file creation moved inside the cleanup scope and uses
  `shutil.rmtree`; reliable reader-drain on clean completion (dropped a misleading
  `stdout_truncated` from the `review()` result); `_read_limited` reads `limit+1` (no TOCTOU);
  consent CLI normalizes destinations to match runtime keys; `_provider_label` uses an exact
  host suffix; `with open()` for the consent read.
- Tests: negative schema fixtures under `schemas/examples/invalid/` (the enforced invariants
  are now proven to *reject*); positive `approve`/`failed` fixtures; consent-integrity tests
  (malformed/wrong-version/symlink → block; notice-version drift); supervisor spawn-error +
  truncation; `review()` timeout + no-final classification; ruff lint in CI.

### Ship-decision improvements (four-lens rubric: CEO buyer, brand strategist, adversarial skeptic, Codex)
- Reframed as the open, pre-release **reimplementation of the essay's workflow**; made the
  top-line present-tense-accurate (verify/reconcile/escalate is *host-directed*, not a standalone
  engine) — closes the claim-vs-code seam.
- Added an advisory funnel: a "Who builds this" section linking the essay + AI Workshop for CEOs.
- Added `docs/walkthrough-decision.md` — a full **business-decision** review end to end (not
  code), reconciling the shipped decision fixtures; aligned `decision.reconciliation-result.json`
  with the reviewer fixture so the two tell one story.
- Qualified "backend-neutral" → "backend-neutral by design; one backend (Codex CLI) today".

### Run records + reports (the audit trail)
- Runs are now **persisted** under `config_dir()/runs/<id>/` (reviewer-response + reconciliation,
  `0600`/`0700`, gitignored). The runner auto-records the reviewer's findings (`--no-record` to
  skip); the host saves the reconciliation via `impasse_report.py save-reconciliation`.
- New `scripts/impasse_report.py`: `list` / `show <id>` / `save-reconciliation <file>` /
  `forget <id>`. `show` renders a scannable report — the **reviewer↔host back-and-forth** per
  finding, the **decision** made, a **tally** (raised/resolved/accepted/rejected/escalated), the
  verification, and the **escalated questions** — with emojis for context.
- `impasse_lib`: `runs_dir` / `save_run_doc` (atomic) / `list_runs` / `load_run` / `forget_run`.
- Closes the "governance tool with no audit trail" gap the ship-review flagged. Cumulative
  cross-run "what it caught" reporting remains a documented roadmap item (not built).
- Housekeeping: `impasse_report.py open` (runs with unresolved escalations + their questions),
  `prune --older-than N` (keeps runs with open escalations unless `--include-open`), and an
  open-count marker on `list`. SKILL guidance to proactively surface unanswered decisions and
  offer to clean up sensitive records.
