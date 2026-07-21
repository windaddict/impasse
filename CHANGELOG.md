# Changelog

All notable changes to Impasse are documented here. This project adheres to
[Semantic Versioning](https://semver.org/) for its schemas and skill.

## [Unreleased]

### Multi-host support — Impasse now runs turnkey under Claude Code *and* OpenAI Codex
Both hosts implement the open [Agent Skills standard](https://agentskills.io); one installation serves
either, because the code is host-relative at runtime (the host is detected per run, never persisted)
and the shared per-user config dir holds no host-specific state — consent is keyed by *endpoint* (a
Claude host's OpenAI grant and a Codex host's Anthropic grant coexist), model/effort defaults are
keyed *per backend*, and run records are keyed by `review_id`.

- **Host-aware default backend.** `review --backend` now defaults to **`auto`**, which selects the
  most host-independent *available* backend via `review_mode()`: to a Claude host that's `codex`, to a
  **Codex host** it's `claude` (the ladder inverts). So a bare review on a Codex host gets a genuine
  cross-provider reviewer instead of a silent same-provider one; when the cross-provider backend is
  unavailable it degrades honestly to `same_provider` (never a false `cross_provider`), and with no
  backend it fails closed. Explicit `--backend codex|claude` still forces.
- **Reviewer hermeticity.** The reviewer subprocess now runs with its CWD set to the run's scratch
  dir, not the operator's project — so `claude -p` can't pick up the *reviewed* project's
  `CLAUDE.md`/hooks (an artifact-controlled injection / independence leak), newly load-bearing now
  that `claude` is the cross-provider reviewer for a Codex host. (Residual, documented: user-global
  `~/.claude` config — see `docs/backends/claude.md`.)
- **Backend discovery.** The Codex desktop app rebranded its bundle to `ChatGPT.app`;
  `resolve_codex_command()` gained that path (legacy `Codex.app` kept). Without it, Impasse couldn't
  find the backend after the app updated.
- **Turnkey install + docs.** New `scripts/install-codex.sh` — a **symlink-only** installer (safe by
  construction: it never deletes real data — it replaces only a verified symlink and refuses a
  physical destination), which detects the Codex skills root. `SKILL.md` generalized from a
  Claude-Code-only adapter to a host-neutral one (host-relative backend guidance, per-host consent
  endpoints, the Codex sandbox-escalation prompt distinguished from Impasse's endpoint consent).
- **Detection provenance + a closed composition fail-open.** `host_detection()` returns
  `{method, confidence}`; a positive `cross_provider` tier resting on a *heuristic* detection carries a
  soft notice. A holistic review of the assembled feature caught a fail-open no per-change review
  could: a presence-style Claude surface flag (any non-falsy value) had yielded *strong* confidence,
  so a stray one on a sandbox-bypassed Codex host produced a **silent** false `cross_provider`. Those
  flags now yield *heuristic* (notice-bearing); only strict `CLAUDECODE=1` / the `CLAUDE_SURFACE`
  allowlist stay *strong*.

#### Hardening (a full-source cross-provider review of the assembled feature)
Reviewing the whole thing surfaced latent issues, several pre-existing, now fixed:
- **Consent boundary:** `IMPASSE_CODEX_RESPECT_CONFIG` (which honors `~/.codex/config.toml`, able to
  reroute data) now **refuses** unless `OPENAI_BASE_URL` is pinned, so consent is never keyed to a
  destination the config could silently override. An explicitly-empty base URL is treated as the
  default (preflight and run now agree).
- **Audit records:** each run reserves a **unique** record directory (atomic `mkdir`, `-2/-3…` on
  collision) — a reused or untrusted `review_id`, or two hosts sharing one config dir, can no longer
  silently overwrite another run's record.
- **Settings writes** run under an interprocess lock, so concurrent `set-model`/`set-effort` from two
  hosts can't lose an update. `set-effort` is now codex-only (Claude has no effort knob — was dead
  config).
- **Supervisor:** the process-group id is captured before the leader is reaped, so descendant teardown
  works on a clean exit (previously the post-reap `getpgid` failed silently, leaking strays).
- **Self-review gate:** `detect_environment()` now matches presence-style Claude markers affirmatively
  (a stray `CLAUDE_COWORK=0` can't manufacture a sandbox surface that would permit self-review).
- **Robustness:** a bad `--wall`/`--idle` becomes a structured failure, not a traceback; every early
  failure path reports host provenance; `Backend.independence` (a vestigial duplicate) removed.
- Coverage: the bash installer is now driven by the suite (refuse-physical-dir, symlink, idempotent,
  dry-run); the presence-style/allowlist confidence branches, `review_mode(host="unknown")`, and the
  `other` host tier are all asserted. **One installation safely serves both a Claude Code and a Codex
  host** — verified: no host-specific persisted state; consent keyed by endpoint; settings per backend.

#### Host auto-detection (the detection core)
- `detect_host()` now **auto-detects four hosts** from genuine, strict-value env markers, not just
  Claude: `CLAUDECODE=1` → `claude`, `GEMINI_CLI=1` → `gemini` (new provider **Google**),
  `CURSOR_AGENT=1` → `cursor`, and `CODEX_SANDBOX=seatbelt` / `CODEX_SANDBOX_NETWORK_DISABLED=1` →
  `codex`. A non-Claude host no longer has to export `IMPASSE_HOST` to get an honest tier — though it
  still can, and that remains authoritative.
- **Fail-safe by construction.** Markers match by exact value (an inherited `GEMINI_CLI=0` doesn't
  count); ≥2 attributable markers, or one attributable marker plus Cursor, resolve to `unknown`
  (ambiguous inner-driver — an unordered env set has no nesting depth); and `IMPASSE_HOST` is now
  **validated and conflict-checked** — a nonempty unrecognized value, or one that disagrees with an
  observed marker, yields `unknown` instead of silently falling through. *Behavior change:* a nonempty
  unrecognized `IMPASSE_HOST` previously continued detection; it now returns `unknown`.
- **`codex` is a heuristic, not a contract.** OpenAI ships no branded host flag; the sandbox-state
  vars are absent under `--dangerously-bypass-approvals-and-sandbox` (a safe false-negative). New
  `host_detection: {method, confidence}` provenance rides on `review()`/`mode` results, and a positive
  `cross_provider` tier resting on the Codex heuristic carries a **soft `independence_notice`** so a
  guess can't read as a confirmed claim. Guaranteed labeling: `IMPASSE_HOST=codex`.
- **Trust floor, disclosed.** Env markers are unauthenticated inherited strings; the mitigations
  eliminate accidental collisions and ambiguity but cannot stop a deliberately/accidentally injected
  *exact* marker. New [`docs/host-detection.md`](docs/host-detection.md) carries the per-host
  compatibility matrix (marker, citation, verified version/OS) and the spoofing caveat. The unit
  suite proves the mapping logic (a 128-cell decision matrix vs an independent truth table); it cannot
  detect upstream marker drift — a periodic live smoke test is the documented follow-up.
- Plan of record: [`docs/proposals/multi-host-autodetection.md`](docs/proposals/multi-host-autodetection.md),
  hardened across three cross-provider Impasse reviews (6 findings → 3 → 0 fail-open paths).

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

### Escalation semantics: operator rulings count regardless of channel
- SKILL reconciliation guidance: an operator ruling that decides an item's disposition is
  recorded with an `escalation` object whether it arrived via a formal deadlock,
  `AskUserQuestion`, or prose in conversation — the metric is "questions that decided a
  disposition," and the channel is a UI detail. `operator_question` must be the question as
  actually posed (verbatim/excerpt, not a reconstruction); who initiated the decisive exchange
  is recorded in the positions; a low escalation count is explicitly not a goal.
- Amendments to past records must append amendment metadata (date, reason, what changed, prior
  state) to the item's `resolution` — never a silent rewrite. Applied once: the
  size-bound-retryability ruling (operator-ratified 2026-07-16, conversation path) now carries
  its escalation object with an amendment note.
- Cross-provider decision review of this rule change surfaced the audit-integrity requirements
  above (amendment provenance, verbatim questions, observable metric definition, anti-gaming
  language) — the initial proposal had none of them. Roadmap noted for a possible v1.1 schema:
  optional escalation-channel and amendment-provenance fields (`additionalProperties: false`
  currently confines provenance to structured resolution text).
- **Historical escalation counts are withheld from the public ledger** until 50 reconciled
  reviews accumulate under the corrected rule (from 2026-07-18): the operator attests that more
  judgment calls reached him than pre-rule records captured, and historical events whose exact
  wording is no longer recoverable cannot be amended in without violating the verbatim-question
  requirement. (The one amended record was eligible precisely because its exact wording — the
  operator's question and the delivered ruling — remained available in the retained
  conversation; events without recoverable wording stay uncounted, which is why the historical
  number is a known undercount.)

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
