# Environments & the independence ladder

Impasse's reviewer backends are subprocesses — `codex exec` and `claude -p`. Running a subprocess
needs a real shell, so **where Impasse runs decides how much independence it can offer.** Claude
Code is the best environment; everywhere else the tool degrades along a ladder, and the rule is:
*degrade gracefully, never silently.*

## The host, and why the ladder is relative to it

Independence is a **relation between the host's provider and the reviewer's provider**, not a
property of a backend: to a Claude host, Codex is the cross-provider reviewer and `claude -p` the
same-provider fallback; to a Codex host, the ladder inverts and **`--backend claude` is the
cross-provider choice**. The runner computes every tier relative to the detected host
(`independence_tier()`).

Host identity (`detect_host()` / `host_detection()`): `IMPASSE_HOST` is authoritative
(`claude | codex | gemini | cursor | other`), and the four common hosts are **auto-detected** from
genuine, **strict-value** env markers — deliberately not from `detect_environment()`, whose
`IMPASSE_ENV` override is a surface-policy knob and must not be able to manufacture a host identity:

| Host | Marker (strict value) | Confidence | Provider |
|---|---|---|---|
| `claude` | `CLAUDECODE=1` (or a genuine Cowork/chat-sandbox surface marker) | strong | Anthropic |
| `gemini` | `GEMINI_CLI=1` | strong | Google |
| `cursor` | `CURSOR_AGENT=1` | — | none (operator-chosen model) |
| `codex` | `CODEX_SANDBOX=seatbelt` or `CODEX_SANDBOX_NETWORK_DISABLED=1` | **heuristic** | OpenAI |

Detection is **fail-safe**: markers are matched by exact value (an inherited `GEMINI_CLI=0` doesn't
count); ≥2 attributable markers, or one attributable marker plus `CURSOR_AGENT`, resolve to
`unknown` (an unordered inherited env set carries no nesting depth, so the driver is genuinely
ambiguous); and `IMPASSE_HOST` is **validated and conflict-checked** — a nonempty unrecognized value,
or a value that disagrees with an observed marker, yields `unknown` rather than silently letting a
weaker marker win. An undeclared/ambiguous host is `unknown` → `undetermined`, **never a positive
cross-provider claim**. `cursor`/`other` run an operator-selected model, so they too are
`undetermined` in either direction, as is a backend routed through an unattributable endpoint (a
custom gateway).

Provenance rides on the result as `host_detection: {method, confidence}`. **Codex is a heuristic:**
its sandbox-state vars are absent under `--dangerously-bypass-approvals-and-sandbox`, so a
sandbox-bypassed Codex run is invisible (a safe false-negative → `undetermined`), and when the Codex
heuristic *does* grant a positive `cross_provider` tier the result carries a **soft
`independence_notice`** saying so. **Codex users who need a guaranteed label should set
`IMPASSE_HOST=codex`.** Env markers are unauthenticated inherited strings — the label is only as
trustworthy as the environment's integrity; see [`host-detection.md`](host-detection.md) for the
compatibility matrix and the spoofing trust floor.

## The ladder (strongest → weakest)

| Tier | Meaning | Where |
|---|---|---|
| `cross_provider` | reviewer from a **different provider** than the host — different blind spots | any shell surface |
| `undetermined` | provider correlation can't be established (mixed-model host, or unattributable endpoint) | any shell surface |
| `same_provider` | reviewer **shares the host's provider**, fresh process — breadth, shared blind spots | any shell surface |
| `self_review` | the host model, in its own context — near-zero, self-critique | chat sandbox / Cowork only |

Each step down is disclosed: the runner emits an `independence_notice` for `same_provider` and
`undetermined` (naming the host); the `self_review` tier emits a louder `self_review_notice`.

## Environments

- **Claude Code** — full shell. Resolves and runs a backend (Codex by default; Claude fallback if
  Codex is absent). Among *Claude* surfaces, the only one that runs a reviewer subprocess — hence
  the only Claude surface that yields genuine independence (a non-Claude host with its own shell
  can run one too; see the host section above). Self-review is **refused here** — degrading to the
  host's own context would throw away the independence you actually have.
- **Claude chat sandbox** (claude.ai Skills) — a code container with no CLI installs and no
  reliable way to spawn a fresh isolated reviewer. No subprocess backend runs. Self-review is
  permitted, with full disclosure.
- **Claude Cowork** — treated like the sandbox: if it can't run a reviewer subprocess, it self-
  reviews with disclosure. (If a future Cowork *can* run a backend, `review_mode` prefers it
  automatically — the policy is capability-first.)
- **Unknown** — fail safe. Self-review is **not** permitted; `review_mode` returns `refuse` rather
  than silently degrade on a surface we can't confirm is a sandbox.

## The policy (one source of truth)

`impasse_lib.review_mode(kind, environment=..., codex_available=..., claude_available=...,
host=..., detection=...)` returns `{mode, tier, allowed, notice, recommendation, reason, host,
host_detection}` where `mode ∈ {codex, claude, self_review, refuse}` and `host_detection` is
`{method, confidence}`. Pass `detection` (a `host_detection()` record) to use a caller's snapshot
verbatim — preserving its confidence — instead of re-deriving from `host`. Capability-first (prefer the available subprocess
backend most independent of the host — `cross_provider > undetermined > same_provider`, ties keep
codex first for its hermetic OS sandbox), then env-gated (self-review only in
`chat_sandbox`/`cowork`, and never for `code`). The pre-flight mirrors the actual run: tiers are
computed against each backend's **configured endpoint** (custom base URLs degrade to
`undetermined`), a backend `get_backend()` would refuse (claude under Bedrock/Vertex routing) is
never recommended, a downgraded tier carries its `independence_notice` here too, and the
`recommendation` is host-aware (only a Claude host is pointed at Claude Code). CLI:

```bash
python3 scripts/impasse_run.py mode --kind decision [--host codex]
```

Environment auto-detection (`detect_environment()`) keys off env markers and is overridable with
`IMPASSE_ENV` (authoritative). When it can't tell, it returns `unknown` — which does not permit
self-review.

## Self-review, if you use it

Only in the chat sandbox or Cowork, only for non-code artifacts, and only with the
`self_review_notice` prepended verbatim — it says plainly that this is *not* an independent
opinion, that agreement is near-zero evidence, and that **Claude Code is the best environment for a
real review.** Self-review still catches arithmetic slips, unsupported claims, and internal
contradictions — the errors that come from momentum, not from a shared blind spot. It does not
catch what the host already got wrong for capability reasons. Treat it as a spell-check on
reasoning, not a second opinion.
