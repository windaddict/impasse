# Plan: auto-detect the driving host (Claude, Codex/ChatGPT, Gemini, Cursor)

**Scope:** phase 2 of multi-host support. Extend `lib.detect_host()` so Impasse identifies the
agent *driving* the protocol without requiring the operator to export `IMPASSE_HOST`, for the four
most common coding-agent hosts. The tier math, notices, and `review_mode` already consume
`detect_host()`'s output; they need no change beyond one new provider entry for Gemini.

**Non-goal:** changing the independence *math* — `independence_tier(host, backend_provider)` is
unchanged. This plan only makes the *host* argument populate itself more often, and must do so
without ever **auto-manufacturing** a positive independence claim from ambiguous, conflicting, or
merely-inherited environment state (the fail-open class the phase-1 review already caught once).

## Threat model & the irreducible trust floor (read first)

Environment variables are **unauthenticated, inherited strings**. Any process that controls the
environment `detect_host()` runs in — an ancestor agent, a shell rc file, a CI job, a wrapper
script, or the operator — can set an exact marker (`GEMINI_CLI=1`, `CLAUDECODE=1`, …) and thereby
*spoof* a host. **No environment-variable scheme can fully defeat this**, and this is already true of
the shipped Claude auto-detection (`CLAUDECODE=1 → claude → cross_provider`). Therefore:

- The independence label produced by auto-detection is **only as trustworthy as the integrity of the
  environment the run executes in.** This is stated in the disclosures, not buried.
- The mitigations below (strict-value matching, conflict→`unknown`, ambiguity→`unknown`) exist to
  eliminate *accidental* collisions and honest ambiguity — the realistic failure modes. They do
  **not** claim to stop a deliberately or accidentally injected *exact* marker; that residual risk is
  documented (operator decision, 2026-07-19: keep auto-detection granting `cross_provider`, hardened,
  with this residual risk disclosed).
- The operator escape hatch (`IMPASSE_HOST`) is likewise operator-asserted provenance — trusted, but
  cross-checked against observed markers (see "Override handling").

The design goal is thus **"fail-safe against ambiguity and accident, honest about spoofing"**, not
"cryptographically authenticated host identity" (which env vars cannot provide).

## Why this matters

Today `detect_host()` auto-detects **only** Claude surfaces; a Codex, Gemini, or Cursor host that
forgets `IMPASSE_HOST` silently falls to `undetermined`, a real downgrade. Phase 1 shipped the
host-relative machinery with this as an explicit cliffhanger ("phase 1 of multi-host support").

## The markers (researched, with citations & confidence)

Detection uses **strict value equality**, never mere presence (see F004 rationale below).

| Host | Rule (strict) | Confidence | Source |
|---|---|---|---|
| `claude` | `CLAUDECODE == "1"` | HIGH — empirically confirmed live in a spawned subprocess | existing, unchanged |
| `gemini` | `GEMINI_CLI == "1"` | HIGH — documented detection hook, set for shell-tool commands | geminicli.com/docs/tools/shell ; google-gemini.github.io/gemini-cli/docs/tools/shell.html |
| `cursor` | `CURSOR_AGENT == "1"` | MED-HIGH — documented; **once dropped & re-added** in a `cursor-agent` release | cursor.com/docs/agent/tools/terminal ; forum.cursor.com/t/…/132427 |
| `codex` | `CODEX_SANDBOX == "seatbelt"` **or** `CODEX_SANDBOX_NETWORK_DISABLED == "1"` | **LOW — heuristic only** (see below) | github.com/openai/codex issues #30356, #5041 ; developers.openai.com/codex/environment-variables |

**Codex is a heuristic, not an identity contract.** OpenAI ships **no branded "I am Codex" flag**.
The `CODEX_SANDBOX*` vars signal an *execution condition* (running inside Codex's macOS seatbelt /
network-disabled sandbox), not "Codex is the top-level driver." They are **absent** when Codex runs
with `--dangerously-bypass-approvals-and-sandbox`, and the same vars appear for the Codex VS Code
extension / desktop app. Consequences, handled explicitly: Codex auto-detection is best-effort;
its absence is a **false-negative** (→ `undetermined`, safe); and because it is weak, Codex users
who need a guaranteed label should set `IMPASSE_HOST=codex` (documented).

**Deliberately rejected markers:**
- `TERM_PROGRAM=vscode` — shared by Cursor, VS Code, and every VS Code fork/extension. Cannot
  distinguish Cursor; would false-positive constantly.
- `CURSOR_TRACE_ID` — could not be confirmed to exist; not used.
- `CODEX_HOME`, `CODEX_API_KEY`, `GEMINI_API_KEY`, `AI_AGENT` — operator-set *inputs* or generic
  names, not host-injected identity markers. Not used.

## Detection algorithm (fail-safe against ambiguity)

`detect_host()` returns exactly one of `KNOWN_HOSTS` ∪ `{"unknown"}`. Let:

- **A** = the set of *attributable* hosts whose strict marker matches — a subset of
  `{claude, codex, gemini}` (each maps to a single provider).
- **cursor?** = `CURSOR_AGENT == "1"` (Cursor is **not** attributable — it wraps an operator-chosen
  model).

Resolution order:

1. **`IMPASSE_HOST` override (authoritative, conflict-checked, validated).** Read `IMPASSE_HOST`:
   - **Absent** (unset or empty string) → fall through to marker resolution (step 2).
   - **Nonempty but unrecognized** (not in `KNOWN_HOSTS` — e.g. a typo `cdoex`) → return
     **`"unknown"`**. A present invalid value is concrete evidence of operator misconfiguration;
     ignoring it and letting a weaker inherited marker grant a positive tier is fail-open, so we
     **refuse** rather than continue (F002, rev-2). *(This tightens the phase-1 behavior, which
     continued detection on an unrecognized value; the existing `IMPASSE_HOST=skynet → claude` test
     is updated to expect `unknown`.)*
   - **Recognized value *h*** but an observed attributable marker names a host **different from *h***
     (e.g. `IMPASSE_HOST=gemini` while `CODEX_SANDBOX=="seatbelt"` is present) → return
     **`"unknown"`** (override disagrees with the environment; fail-safe — operator decision
     2026-07-19).
   - **Recognized value *h*, no disagreement** → return *h*.

2. **No override — resolve from markers:**
   - `|A| ≥ 2` (two attributable markers) → **`"unknown"`** (conflict; cannot pick a driver).
   - `|A| == 1` **and** cursor? → **`"unknown"`** (ambiguous: cannot tell whether Cursor or the
     attributable agent is the *inner* driver — see F001 below).
   - `|A| == 1` **and not** cursor? → the single attributable host.
   - `|A| == 0` **and** cursor? → **`"cursor"`**.
   - `|A| == 0` **and not** cursor? → **`"unknown"`**.

### Why this is fail-safe against ambiguity

- **Environment inheritance flows downward**, and a child cannot mutate an already-running parent's
  environment. Impasse's own reviewer subprocess (`codex exec`) is a *child* of `impasse_run.py`, so
  its `CODEX_SANDBOX` never leaks up into the detector. The normal "Claude Code drives Impasse,
  Codex is the backend" path reads `A={claude}`, no cursor → `claude` cleanly. *(This "downward"
  argument covers only reviewer-child leakage; ancestor/wrapper/override state is handled by the
  conflict and ambiguity rules above and the trust-floor section, not by this argument.)*
- **F001 (reverse-nesting) is closed.** An unordered inherited marker set carries no nesting depth,
  so when an attributable marker and `CURSOR_AGENT` coexist we cannot know which agent is innermost.
  Rather than assume Cursor is the outer shell (the old, unsafe assumption), we return `unknown`.
  Cost: the common "Claude Code inside Cursor's terminal" now yields `undetermined` unless the
  operator sets `IMPASSE_HOST` — the safe direction.
- **F002 (single spurious marker) is mitigated, not eliminated.** Strict value matching removes
  accidental truthy-string collisions (`GEMINI_CLI=0` no longer counts). A deliberately or
  accidentally injected *exact* marker still spoofs; that residual is disclosed per the trust-floor
  section — it is the same risk the shipped Claude detection already carries.

## Independence implications (unchanged math, newly reachable states)

| Detected host | Provider | vs `codex` backend | vs `claude` backend |
|---|---|---|---|
| `claude` | Anthropic | cross_provider | same_provider |
| `codex` | OpenAI | same_provider | **cross_provider** (newly auto-reachable, heuristic) |
| `gemini` | Google | **cross_provider** | **cross_provider** (newly auto-reachable) |
| `cursor` | (none) | undetermined | undetermined |
| `unknown` | (none) | undetermined | undetermined |

The only newly-auto-reachable *positive* claims are `codex`/`gemini` host → cross_provider, both
subject to the trust floor and (for codex) the heuristic caveat.

### Runtime detection provenance (a heuristic must not look like an assertion)

A documentation caveat does not reach a consumer of the runtime result: `review()` currently returns
`independence: "cross_provider"` with `independence_notice: null` regardless of *how* the host was
determined, so a Codex host inferred from a weak sandbox heuristic is indistinguishable from a
Gemini host read off a branded flag or an explicit operator assertion (F001, rev-2). Fix — carry the
provenance into the result itself:

- Add a `host_detection` block to the `review()` / `mode` result: `{method, confidence}` with the
  **complete** domains `method ∈ {override, auto, none}` and
  `confidence ∈ {asserted, strong, heuristic, none}`, defined for **every** resolution branch:

  | Resolution branch | `method` | `confidence` |
  |---|---|---|
  | recognized `IMPASSE_HOST`, no disagreement → host *h* | `override` | `asserted` |
  | `claude`/`gemini` from strict branded marker | `auto` | `strong` |
  | `codex` from `CODEX_SANDBOX*` heuristic | `auto` | `heuristic` |
  | `cursor` from `CURSOR_AGENT` | `auto` | `none` (not provider-attributable) |
  | `unknown` from marker ambiguity / no marker | `auto` | `none` |
  | `unknown` from invalid override | `override` | `none` |
  | `unknown` from override↔marker conflict | `override` | `none` |

  (`confidence: none` never rides a positive tier — `cursor`/`unknown` are always `undetermined`.)
- When a **positive** tier (`cross_provider`) rests on a `heuristic` detection (today: only Codex via
  `CODEX_SANDBOX*`), emit a **soft `independence_notice`** instead of `null`: it states the
  cross-provider label was inferred from a sandbox-condition heuristic, that a sandbox-bypassed Codex
  run is invisible, and that `IMPASSE_HOST=codex` gives a firmer basis. This keeps the operator's
  "harden & keep auto cross_provider" decision (the tier is still granted) while ending the honesty
  gap the null notice created.

This touches result assembly, **not** the tier math: `independence_tier` still returns
`cross_provider` for those states; the change is purely additional disclosure fields.

## Code changes

`scripts/impasse_lib.py`:
1. `KNOWN_HOSTS` — add `"gemini"` (already has `codex`, `cursor`, `other`).
2. `_HOST_PROVIDERS` — add `"gemini": "Google"`. (`codex → OpenAI` already present.)
3. `_KNOWN_PROVIDERS` — **no change**: this tuple is consulted only for the *backend* provider, and
   the only backends are OpenAI (codex) and Anthropic (claude). Google never appears as a backend, so
   adding it would be dead code. `independence_tier` reads the host provider from `_HOST_PROVIDERS`
   (item 2), which suffices. *(Stated so the asymmetry isn't misread as a bug.)*
4. `detect_host()` — implement the algorithm above with **strict value equality** and the override
   conflict-check. Continue keying off genuine host markers only; do **not** consult
   `detect_environment()`/`IMPASSE_ENV` (a surface-policy knob must not manufacture a host identity —
   existing invariant, preserved).

5. `review()` / `review_mode()` result assembly — add the `host_detection` `{method, confidence}`
   block and, for a positive tier resting on a `heuristic` detection, emit the soft
   `independence_notice` described above (via the shared `independence_notice` formatter). **No change
   to `independence_tier`'s return values** — this is additive disclosure only.

## Tests (`tests/test_helpers.py`, extending the host block ~L702)

The decision table is security-relevant, so it is tested **exhaustively and table-driven**, not by a
hand-picked list (F003, rev-2): a branch-order or set-construction bug must not be able to reintroduce
a positive classification while the suite stays green. Each case runs in a `try/finally` that
saves/clears the relevant env vars (suite convention), and asserts **both** `detect_host()` output
**and** the resulting `independence_tier` against the `codex` and `claude` backends.

**Matrix — assert every cell, expectations from an independent truth table.** The three input axes
are *concrete values*, not state-dependent labels (the rev-2 matrix conflated the two):
- attributable markers present: all 8 subsets of `{claude, codex, gemini}` (0/1/2/3 attributable),
  using each host's strict marker string,
- `CURSOR_AGENT`: `{absent, "1"}`,
- `IMPASSE_HOST`: `{absent, "" (empty), "claude", "codex", "gemini", "cursor", "other",
  "zzinvalid" (nonempty-invalid)}` — i.e. absent, empty, every `KNOWN_HOSTS` value, and one invalid.

Expectations are computed by a **separately written** `expected_host(A, cursor, override)` truth
function (deliberately *not* sharing code with `detect_host`, so a bug in one can't mask the other),
encoding: override absent/empty → fall through; override nonempty-invalid → `unknown`; override
recognized *h* and some attributable marker names a host ≠ *h* → `unknown`; override recognized *h*,
no disagreement → *h*; else `|A|≥2` → `unknown`; `|A|==1 & cursor` → `unknown`; `|A|==1 & ¬cursor` →
that host; `|A|==0 & cursor` → `cursor`; `|A|==0 & ¬cursor` → `unknown`. (Note: `IMPASSE_HOST="other"`
with any *attributable* marker present → `unknown`, since `other` disagrees with that marker; `other`
is only returned when no attributable marker contradicts it.) For every cell assert **both**
`detect_host()` == `expected_host(...)` **and** that the resulting tier is **not `cross_provider`
whenever the expected host is `cursor` or `unknown`**.

**Strict-value negatives (F004):** `GEMINI_CLI=0→unknown`; `GEMINI_CLI=""→unknown`;
`CODEX_SANDBOX=1→unknown` (value is `seatbelt`, not `1`); `CODEX_SANDBOX=off→unknown`;
`CURSOR_AGENT=0→unknown`.

**Provenance (F001):** `host_detection.confidence == "strong"` for a `gemini`/`claude` marker,
`== "heuristic"` for a `codex` sandbox marker, `== "asserted"` for a valid `IMPASSE_HOST`; and a
`codex`-heuristic host + `claude` backend yields `cross_provider` **with a non-null soft
`independence_notice`**, whereas a `gemini` host + `codex` backend yields `cross_provider` with a
`null` notice.

**Override validation (F002):** `IMPASSE_HOST=skynet → unknown` (nonempty-invalid refused — updates
the phase-1 regression that expected `claude`); `IMPASSE_HOST="" ` + `CLAUDECODE=1 → claude` (empty
treated as absent).

**e2e via BOTH `review()` and `review_mode()` (F003, rev-3):** the heuristic-notice guarantee applies
to *both* result-producing paths, so assert it on each independently.
- `review()`: gemini host + codex backend → `cross_provider`, **null** notice,
  `host=="gemini"`, `host_detection.confidence=="strong"`; codex host (sandbox marker) + claude
  backend → `cross_provider`, `confidence=="heuristic"`, **non-null** soft notice.
- `review_mode()`: an explicit case with a Codex sandbox marker + claude backend available → mode
  `claude`, tier `cross_provider`, `host=="codex"`, `host_detection.confidence=="heuristic"`, and a
  **non-null** heuristic notice (guards a mode-specific assembly bug); plus `unknown`/`cursor` cases
  asserting `undetermined` with `confidence=="none"`. Also mirror the existing codex-host `review_mode`
  assertions for `host="gemini"`.

## Testing the *contracts*, and marker drift (F005, F006)

The unit tests above verify **only Impasse's mapping logic** — that given a string, the right host
comes out. They **cannot** detect that a host *stopped* emitting a marker, renamed it, or began
leaking it: those are third-party env contracts that drift (Cursor's `CURSOR_AGENT` already did once).
Stated plainly so a green suite is not misread as "hosts still behave as assumed." The strategy:

- **Minimum (v1):** a checked-in **compatibility matrix** (`docs/host-detection.md`) recording, per
  host: the marker rule, the exact upstream citation, and the host **version/OS/invocation mode**
  under which it was last verified — plus an explicit "re-verify on host upgrade" note and named
  ownership. This turns drift into an auditable, dated claim rather than an assumption.
- **Recommended (fast-follow, optional for v1):** a **scheduled live smoke test** that launches each
  supported host, captures the environment seen by a child process, and flags when an expected marker
  is missing or an unexpected one appears — with **negative-control** environments (plain shell, CI,
  unrelated tools) to catch false positives. This is the only mechanism that actually observes drift;
  the unit suite cannot. Marked optional for v1 because it needs all four CLIs installed in the
  harness, but its absence is disclosed as a known coverage gap, not silently omitted.

## Limitations (stated, not hidden)

- **Env detection cannot authenticate origin.** A spoofed exact marker yields a false positive; the
  label is only as trustworthy as the environment (trust-floor section). Same risk as shipped Claude
  detection; disclosed, accepted (operator decision 2026-07-19).
- **Codex detection is heuristic and best-effort.** Sandbox-bypassed Codex is undetectable →
  `undetermined` (safe). Guaranteed labeling requires `IMPASSE_HOST=codex`.
- **Cursor buys no independence.** Detecting it only sharpens recommendation text; tier stays
  `undetermined` (operator-chosen underlying model). Included for honest labeling, not a positive
  claim.
- **Nested / ambiguous hosts → `unknown`.** We refuse to guess innermost-driver from env vars; the
  operator resolves with `IMPASSE_HOST` (which is itself conflict-checked).
- **Marker drift.** Third-party contracts change; the unit suite cannot catch it. Mitigated by the
  compatibility matrix + optional live smoke test above.

## Docs

- `CHANGELOG.md` — `[Unreleased]`: "Host auto-detection (phase 2 of multi-host support)" — the four
  strict-value markers, conflict/ambiguity→`unknown`, override conflict-check **and invalid-value
  refusal**, the new `host_detection` provenance/confidence block + soft heuristic notice, the Codex
  heuristic caveat, and the env-spoofing trust floor. Note the phase-1 behavior change: a nonempty
  unrecognized `IMPASSE_HOST` now yields `unknown` instead of continuing detection.
- `README.md` "How independent is it?" — note Codex/Gemini/Cursor hosts are now auto-detected
  (best-effort for Codex; `IMPASSE_HOST` still authoritative and conflict-checked), and that env
  detection is trust-floored on environment integrity.
- `docs/environments.md` — the algorithm, precedence, conflict/ambiguity rules, and the
  `IMPASSE_HOST=codex` guidance for sandbox-bypassed Codex.
- `docs/host-detection.md` (new) — the compatibility matrix (F005/F006).
- `SKILL.md` — keep the "Claude Code host adapter" section; add that non-Claude hosts are
  auto-detected and overridable via `IMPASSE_HOST`.

## Rollout

Single commit to the dev clone (code + tests + docs + matrix), run the three gates
(`test_helpers.py`, `validate_schemas.py`, `ruff`), dogfood this change through Impasse before
pushing, then bump the `~/.claude` submodule pointer after push.
