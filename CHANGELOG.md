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

### Configurable reasoning effort (codex backend)
- `--effort` now resolves like `--model`: per-run flag > `IMPASSE_CODEX_EFFORT` env > persisted
  default (`impasse_run.py set-effort <none|low|medium|high|xhigh>`, `--clear` to unset) > the
  codex CLI's own default (currently medium; Impasse omits the flag and reports `effort: null` —
  backend-controlled). The review result reports the resolved value in `effort`.
- Allowlisted at every entry point — argparse choices on the flag and `set-effort`, a read-path
  filter on the persisted value (a hand-edited `settings.json` can't smuggle a bad value), a
  structured `backend_error` naming `IMPASSE_CODEX_EFFORT` when the env var is invalid, and a
  defense-in-depth re-check in `build_codex_argv` itself before the value is interpolated into
  a codex `-c` config expression (Impasse's own cross-provider review of this change caught the
  last two gaps — see the dogfooding note in CLAUDE.md).
- Effort resolves **only for the codex backend**; claude has no effort knob, so nothing resolves
  there (an irrelevant `IMPASSE_CLAUDE_EFFORT` can neither fail a claude run nor masquerade in
  the result as applied configuration — its `effort` is always `null`).
- SKILL guidance: scale `--wall` with the resolved effort, and run long reviews in the
  background where the host's shell tool caps foreground commands (Claude Code: 10 min).

### Host-relative independence (phase 1 of multi-host support)
- Independence is now computed as a **relation between the host's provider and the reviewer's**
  (`lib.independence_tier`), not hardcoded per backend: to a Codex host, `--backend claude` is
  correctly labeled `cross_provider` (no downgrade notice), and the codex backend gets the
  `same_provider` notice — previously the labels assumed a Claude host and inverted the truth
  for anyone driving the protocol from Codex.
- Host identity: `IMPASSE_HOST` env (`claude|codex|cursor|other`, authoritative; unrecognized
  values ignored) with auto-detection of a Claude host from its **genuine** env markers only —
  `IMPASSE_ENV` (a surface-policy override) cannot manufacture a host identity.
- New `undetermined` tier (with its own `independence_notice`), and it is the **fail-safe
  floor**: an undeclared/unrecognized host, a mixed-model host (`cursor`/`other`, whose
  underlying model is operator-selected), and a backend routed through an endpoint whose
  provider can't be attributed (custom gateways) all get `undetermined` — a positive
  cross-provider claim requires both sides to be attributable. Non-Claude host adapters MUST
  export `IMPASSE_HOST`; a human at the CLI can too.
- `review_mode()` is host-aware and mirrors the actual run: it prefers the available backend
  most independent of the host (`cross_provider > undetermined > same_provider`; ties keep
  codex first for its hermetic OS sandbox), computes tiers against each backend's **configured
  endpoint**, never recommends a backend `get_backend()` would refuse (claude under
  Bedrock/Vertex), carries the downgrade `independence_notice` itself (shared formatter with
  `review()`), and gives host-aware recommendations (only a Claude host is pointed at Claude
  Code). `mode` CLI gains `--host`; review results carry `host`.
- Impasse's own cross-provider review of this change drove the hardening: the initial
  implementation fell back to the historical (Claude-host) labels for an undeclared host —
  fail-open at exactly the boundary the change exists to close — and let `IMPASSE_ENV` imply a
  host identity; both were caught, accepted, and fixed, along with the pre-flight/disclosure
  gaps above.

### Retry malformed reviewer output (issue #1)
- `invalid_response` from stochastically malformed reviewer output — invalid JSON, wrong-shape
  JSON, or an empty final message — is now **auto-retried once** (no backoff, inside the same
  wall-clock budget), the same way a transient outage is; a persistent failure surfaces with
  `retryable: true` so a host can distinguish "retry likely helps" from "structurally broken".
- The size-bound variants (stdout capture cap, final message over the 2 MB byte bound) fail
  immediately with **no auto-retry** but carry `retryable: true` — consistent with the
  `rate_limited` precedent, the hint means "recovery is plausible, offer it", not "the runner
  re-spent for you" — and their messages name the remedy (shrink/tighten/lower effort, or an
  unchanged re-run near the bound). This reversed the issue's original `retryable: false` spec
  after a cross-provider decision review showed the hint and the auto-retry policy are separate
  dimensions of the existing failure contract; operator-ratified 2026-07-16.
- The retry loop now tracks outage and output retries separately (`_MAX_TRANSIENT_RETRIES = 2`,
  `_MAX_OUTPUT_RETRIES = 1`); regression tests prove recovery, the exact retry count per
  failure class, budget independence (outage then malformed output still recovers), per-attempt
  truncation of the final-message file (a retry never re-reads stale output), and that oversize
  output is never retried.
- Fixed alongside (caught by the dogfood review of this fix): the 2 MB final-message bound was
  checked on decoded **characters**, not bytes — a multi-byte UTF-8 message over the byte bound
  slipped past it, and the tolerant parser could then accept a complete JSON object out of a
  silently truncated prefix. The bound is now enforced on bytes, before decoding, with a
  UTF-8 regression test.
- The issue's optional "self-repair round" (feed the reviewer its own broken output to re-emit)
  is deliberately not implemented: a full re-run rescues the common case at the same cost, one
  invocation, without a new echo-untrusted-output-back path.

### Final pre-commit hardening (Fable + Impasse dual review of the full changeset)
- `review_mode` no longer offers a backend whose configured base URL fails to normalize
  (malformed / embedded credentials) — `get_backend()` would refuse it; the raw endpoint value
  is never echoed into labels or notices.
- `_read_limited` (CLI instruction/artifact input) now enforces its 4 MB limit on **bytes**,
  decoding only after the bound passes — the input-side mirror of the final-message byte fix.
- Size-bound failure messages are backend-aware: "lower `--effort`" is suggested only for
  codex (claude has no effort knob); host metadata now rides on backend-resolution failures too.
- Test hardening: the claude transport path (stdout, capture cap) now carries the same
  retry/size assertions as the codex file path; a review-CLI end-to-end test; a cross-feature
  matrix test (codex host + env effort + output retry → identical argv on both attempts); the
  suite clears ambient Impasse/backend env vars at startup so a user's own configuration can't
  break assertions. Docs sweep removed the remaining static-provider independence claims.
