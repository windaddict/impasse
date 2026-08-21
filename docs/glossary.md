# Glossary

The recurring vocabulary Impasse's docs and code reuse. Each entry gives the **mechanism** (what
the thing is) and its **role** (how it fits the whole). Terms marked *(coined)* are Impasse's own;
the rest are ordinary terms of art used in Impasse's specific sense. Link here on a term's first use
in a doc rather than re-defining it inline.

## Roles

- **host** — the AI agent that loads the skill and drives the protocol (Claude Code or the OpenAI
  Codex CLI). It runs the reviewer subprocess, verifies each finding, applies the fixes, and puts the
  deadlocks to the operator. *Independence is measured relative to the host* (see **independence tier**).
- **reviewer** *(coined, project sense)* — the second AI, run as a subprocess (`codex exec` or
  `claude -p`), that inspects the artifact **read-only** and returns findings. It argues; it never
  edits the artifact and never applies fixes (the critic never holds the pen).
- **backend** — the resolved reviewer CLI plus the data destination its choice implies. The `codex`
  backend defaults to `https://api.openai.com` (or wherever `OPENAI_BASE_URL` points — Azure, a
  gateway, localhost); the `claude` backend to Anthropic. The concrete tool that realizes the
  abstract "reviewer," and what **consent** is keyed to — at the *resolved* endpoint, not the default.
- **operator** — the human who owns the decision. They receive only the escalated **deadlocks**, as
  crisp questions — not the raw finding list.
- **artifact** — what's under review: a decision memo, an essay, a research claim, a dataset, or code.
  Its `kind` is chosen explicitly, never auto-detected.

## The protocol

- **CLAR — Cross-Lab Adversarial Review** *(coined)* — the practice of running a model from a
  *different lab* as an adversarial reviewer, so its blind spots are less likely to match the work's.
  Impasse is its reference implementation.
- **finding** — one issue the reviewer raises about the artifact, carrying a claim, a severity, and
  its **anchored evidence**. The unit the host verifies and dispositions.
- **anchored evidence** *(coined)* — an evidence item that pairs a specific **locator** in the
  artifact (a `file:line` range, a quoted span, a section, a JSON pointer) **with** an observation of
  what's wrong there. Every finding must carry at least one; a bare location is not evidence, and the
  schema enforces the pairing. It's what lets the host check a claim against the real artifact instead
  of debating tone. (`docs/protocol.md` also calls these *anchored observations* — same thing.)
- **reconciliation** *(project sense)* — the host's per-finding pass after the review: for each
  finding, verify it against the artifact, then assign a **disposition**.
- **disposition** *(coined)* — the state the host assigns a finding: **accepted** (host agrees, notes
  it), **rejected** (host refuted it with contradicting evidence), **resolved** (addressed/fixed, or
  an escalation the operator has since ruled on), **deadlocked** (neither side can settle it), or
  **withdrawn** (the reviewer retracted it). Drives the tally and what escalates.
- **deadlock / deadlocked** *(coined)* — a finding neither side can settle: an evidence conflict, a
  value/priority call that's the operator's to make, or a host objection it couldn't back with
  evidence (`dispute_kind: unverified_refutation`). The disposition normally queued for the operator
  via **escalation**; once they rule on it, the item becomes **resolved** (their ruling recorded as
  the resolution).
- **escalation** *(project sense)* — surfacing a deadlock to the operator as a question, carrying its
  `dispute_kind`, `stop_reason`, and `operator_question`. An operator ruling counts as an escalation
  regardless of channel.
- **converged / deadlocked / incomplete / failed** — run-level outcomes (distinct from a single
  finding's disposition): **converged** = every finding reached a terminal, non-deadlocked state;
  **deadlocked** = at least one escalated; **incomplete** = a round/budget cap stopped it;
  **failed** = a backend/timeout/consent/invalid-response error (never reported as converged).

## Independence

- **independence tier / ladder** *(coined)* — the reviewer's independence *relative to the host's
  provider*, ranked: **cross_provider** (different provider — the point) > **undetermined** > **
  same_provider** (shares the host's provider and blind spots — breadth, not independence) >
  **self_review** (the host model reviewing in its own context — last resort). Computed per run.
- **cross-provider / cross-lab** — a reviewer from a different provider (the tool's proxy) or lab
  (the intent) than the host. Where the blind-spot decorrelation comes from.
- **undetermined** *(coined)* — the tier when provider correlation can't be established (a
  mixed-model host like Cursor, or an unattributable endpoint). Never a positive cross-provider claim.
- **self-review** *(project sense)* — the host model reviewing the artifact in its own context.
  Near-zero independence; permitted only where no reviewer subprocess can run (chat sandbox / Cowork),
  and refused for `kind=code`.
- **host-detection provenance** *(coined)* — the recorded basis for a host label, carried on every
  result as `host_detection: {method, confidence}` (e.g. `auto`/`override`; `strong`/`heuristic`/
  `none`) so a heuristic guess is never presented as certainty.
- **fail-safe / fail-open / fail-closed** — a control's default when it's uncertain. Host detection
  is *fail-safe*: any ambiguity resolves to `unknown` (→ tier `undetermined`), never a guessed
  positive. An **allowlist** *fails closed* (nothing is permitted unless named); a **denylist** can
  only *fail open* (a new item is allowed until banned), so it's defense-in-depth only.

## Runtime & safety

- **consent gate / data boundary** *(project sense)* — reviewing sends the artifact to a third-party
  provider, so Impasse **blocks by default** until the operator approves the destination. Consent is
  keyed to the normalized endpoint (a gateway/proxy needs its own grant) and stored `0600`.
- **hermetic** — the codex reviewer runs isolated from the host's own config and repo rules
  (`--ignore-user-config --ignore-rules`), so neither `~/.codex/config.toml` nor a repo `AGENTS.md`
  can reroute the data or inject instructions into the read-only reviewer.
- **run record** *(coined)* — the persisted reviewer-response (and, once saved, the reconciliation)
  for one review, under `config_dir()/runs/<review_id>/`. The audit trail; holds artifact content, so
  it's kept `0600` and never committed.
- **supervisor** *(project sense)* — the process manager (`supervise()`) that runs the reviewer
  subprocess under a hard wall-clock cap **and** an idle (no-output) cap, bounds its output, and on an
  abnormal exit tears down the subprocess's **process group** (best-effort: a descendant that calls
  `setpgid`/`setsid` escapes the group, and one can briefly outlive a clean exit). The reviewer is
  untrusted and can hang or flood output, so it can't run unbounded.
- **wall / idle** — the two caps the supervisor enforces: **wall** is total elapsed time for the whole
  review (retries included), **idle** is time with no output. The reviewer reasons silently, so idle
  can't tell a hang from a long server-side wait — keep `--idle ≈ --wall` and treat wall as the real
  bound. Blowing the wall discards the entire review; nothing partial is kept.
- **timing store** *(coined)* — `config_dir()/metrics.jsonl`: one append-only row per run recording
  duration, payload size, outcome and time-to-first-byte — **no artifact content** (writes are
  filtered to a field allowlist and typed per field; the backend-supplied model/version strings are
  bounded at 200 characters). Its role is to make the **wall recommendation** reflect this
  machine's real history instead of a shipped constant. Separate from a **run record** and deleted
  separately (`impasse_report.py performance --forget`).
- **wall recommendation / `basis`** *(coined)* — the `--wall` Impasse suggests for a given payload
  (`impasse_run.py estimate`, and the `wall_advice` block on every result). `basis` states where the
  number came from and how much to trust it: **heuristic** = a shipped estimate padded for margin,
  not a measurement of this account; **empirical** = fitted from ≥5 of this machine's own completed
  runs for that backend+model. It is a recommendation, not a guarantee — no cap can bound a
  provider-side queue.
- **phase timeline** *(coined)* — the named, timestamped moments of one review (consent → spawn →
  first byte → exit → validated), returned as `telemetry.phases`. Its role is to make a timeout say
  *where* the time went: no bytes before the cap points at startup, authentication or a provider
  queue, not at the model reasoning over the artifact.
- **resolved vs requested model** — **requested** is what was asked for (a `--model` alias, or
  nothing at all for a backend default); **resolved** is the model the backend reports it actually
  ran. Only the claude backend reports one today, so a codex run shows `model_source: requested`.
  Keeping them distinct matters because two runs pooled under one alias may be different models.

## Reviewer controls (codex backend)

- **model** — which reviewer model runs (`--model` / `set-model` / `IMPASSE_CODEX_MODEL`).
- **effort** — Codex reasoning-effort level, `none`…`xhigh` (`--effort` / `set-effort` /
  `IMPASSE_CODEX_EFFORT`). How hard the model thinks. The `claude` backend has no such knob.
- **Fast mode / execution speed** — Codex's higher *service tier* (`--speed fast`, adding
  `service_tier="fast"` + `features.fast_mode=true`): faster serving at higher credit cost.
  Codex-only, and independent of **effort** (you can combine high effort with fast serving).
- **raw mode** *(coined)* — `--raw`: return the reviewer's **unverified** findings and skip the
  verify → reconcile → escalate protocol, recording nothing. A throwaway self-check on your own work,
  never a review you hand to the operator. (Distinct from **Fast mode** — `--raw` skips verification;
  Fast mode only changes serving speed.)
