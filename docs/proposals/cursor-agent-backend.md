# Proposal: `cursor-agent` as an Impasse reviewer backend

**Status: design proposal — NOT built.** Written 2026-08-21 in response to an operator question:
*should Impasse under Cursor be able to run the review on one of the models Cursor already provides,
instead of shelling out to the `codex` or `claude` CLI?* Evidence below comes from inspecting
`cursor-agent --help` and `cursor-agent --list-models` on the probe machine (Cursor Agent CLI,
macOS, 2026-08-21). No code has been written.

## The short answer

**Yes, this is feasible and legitimate — as a real backend, i.e. a supervised `cursor-agent`
subprocess.** It is *not* the thing the Cursor host-adapter proposal rules out. That anti-pattern is
about calling Cursor's **in-IDE Task/subagent** tool, which bypasses the consent gate, the
supervisor, the schema shape-check and the run record. A `cursor-agent -p` subprocess passes through
all four, exactly like `codex exec` and `claude -p` do.

## Why an operator would want it

**Cost, and it is a real argument.** Impasse today requires a second vendor relationship: to review
from a Cursor session you must also have a working `codex` or `claude` CLI, with its own
subscription or API billing. An operator already paying for Cursor may be paying twice to get a
second opinion, and which arrangement is cheaper depends entirely on their plans. Letting the review
run on the Cursor subscription they already hold removes that duplication.

Secondary benefit: **it works where no reviewer CLI is installed.** Today a Cursor session with
neither `codex` nor `claude` on `PATH` gets `refuse` — no review at all.

## What the CLI actually supports

| Need | Flag | What was actually established |
|---|---|---|
| Headless, non-interactive | `-p` / `--print` | `--help`, 2026-08-21 |
| Machine-readable output | `--output-format json` (also `stream-json`) | `--help` |
| A specific model | `--model <id>` (e.g. `claude-opus-5-thinking-high`) | `--help` + `--list-models` |
| Read-only execution *(claimed by `--help`, NOT established)* | `--mode ask` (Q&A, read-only) or `--mode plan` | **flag exists**; effective capabilities unverified — see open question 1 |
| Endpoint override | `-e` / `--endpoint`, `CURSOR_API_ENDPOINT` (default `https://api2.cursor.sh`) | `--help` |

### The hazard that shapes the whole design

`cursor-agent -p` documents itself as having **"access to all tools, including write and shell"** by
default. An Impasse reviewer that could write files or run shell commands would break the invariant
this project is built on — *the reviewer is read-only on the artifact; the critic never holds the
pen*.

So `--mode ask` is not a nicety, it is a **hard requirement**, and it must be pinned in code the way
`build_claude_argv` pins an empty `--allowed-tools` and `build_codex_argv` pins
`--ignore-user-config --ignore-rules`. Merge gate: a test asserting the argv contains a read-only
mode and that removing it fails the build — the same shape as the existing "fails closed" argv tests.

**But pinning the flag is not the same as proving the property.** `--help` establishes that a mode
named `ask` exists and is *described* as read-only; it establishes nothing about what tools that mode
actually retains. Pinning detects *syntactic* drift (the flag vanishing or being renamed), not
*semantic* drift (the mode quietly gaining a write path) and not retained file-read tools that could
wander outside the artifact. Treat "`--mode ask` is read-only" as an unverified vendor claim until an
empirical probe says otherwise — see open question 1, which is **merge-blocking**.

## Requirements before this could merge

1. **Read-only, fail-closed.** Pin `--mode ask`. If a future CLI drops or renames that flag, the
   backend must refuse to run rather than fall back to a write-capable default.
2. **Refuse `auto`.** The reviewer's provider is derived from the selected model, so `--model auto`
   is unattributable and cannot carry a tier. Refuse it explicitly rather than reporting
   `undetermined` — an operator asking for a *review backend* wants a known reviewer, and silently
   accepting a router repeats the Auto hazard on the reviewer side.
3. **The tier must rest on the model that ACTUALLY RAN, not the one requested.** Issue #11 already
   forced this distinction on the other backends: `model_requested` is an alias the operator sent,
   `model_resolved` is what the backend confirmed, and only the latter can carry a provider claim.
   Three ways this backend could silently diverge — `--model` losing to the CLI default recorded in
   `~/.cursor/cli-config.json`, a quota fallback to a cheaper model, or an `auto` selection resolving
   per request — each invalidates the tier with no error raised. **If the response cannot report the
   model that served the request, this backend must report `undetermined` regardless of `--model`.**
   Merge-blocking, alongside read-only.
4. **Model → provider mapping, with drift handling.** `claude-*` → Anthropic, `gpt-*` → OpenAI,
   `gemini-*` → Google, `cursor-grok-*` → xAI, `composer-*` → Anysphere. **Model IDs rotate**
   (the 2026-08-21 pool already differs from the 2026-08-20 one), so an unrecognized ID must yield
   `undetermined`, never a guess. Prefix matching is a heuristic and should be labeled as one.
5. **Consent to a DIFFERENT destination, disclosed as adding a party.** Traffic goes to
   `https://api2.cursor.sh` (normalize `CURSOR_API_ENDPOINT` the way the other backends normalize
   theirs). This is the sharpest thing to get right, and it is **not settled by the evidence gathered
   here**: `--help` and `--list-models` establish flags and offered model names, not contractual
   recipients, subprocessors, or the actual data path. What IS established is that traffic goes to an
   Anysphere-operated endpoint rather than directly to the model's lab. The reasonable inference —
   that the artifact therefore reaches Anysphere **and** whichever lab serves the chosen model, where
   a direct `claude -p` reaches only Anthropic — must be **confirmed against Cursor's published
   privacy/subprocessor terms before any consent copy asserts it.** Until then the manifest should
   state what is known (destination `api2.cursor.sh`, an intermediary) and not enumerate recipients
   it cannot source.
6. **Retention.** The model list annotates some entries — e.g. `claude-fable-5-thinking-high
   (NO ZDR)` — which reads as "no zero-data-retention" but whose scope the annotation itself does
   **not** define: it does not say whether the retention is Cursor's, the model provider's, or both,
   nor for how long. Surface the annotation verbatim, attributed as Cursor's own label rather than
   restated as a fact Impasse established, and resolve its meaning from Cursor's terms before consent
   copy characterizes it — it must not be paraphrased into something more definite than it is.
7. **Tier semantics, stated precisely.** Two different questions, and they have different answers:
   - *Independence* (blind-spot correlation) follows the **model**: a Cursor-routed
     `claude-opus-5-*` reviewer correlates with Anthropic.
   - *Data boundary* follows the **route**: Anysphere plus Anthropic.
   Conflating them would let a doc claim "same as running Claude directly," which is false on the
   second axis.
8. **A useful inversion to allow:** on a Cursor host running **Auto** (host `undetermined`), a
   `cursor-agent --model <named>` reviewer is still *attributable* even though the host is not. The
   tier stays `undetermined` — correctly, since one side is unknown — but the operator at least
   knows exactly who reviewed. Report the reviewer's provider even when the tier cannot be positive.

## Non-goals

- Making `cursor-agent` part of `--backend auto`. It should be opt-in (`--backend cursor`) at least
  until the flag contract is proven stable; `auto` picks for *independence*, and this backend's
  independence depends on a `--model` the operator chose.
- Using Cursor's in-IDE Task/subagent tool. Still forbidden, for the reasons in
  `cursor-host-adapter.md` — no consent gate, no supervisor, no schema check, no run record.
- Treating "host on Cursor + reviewer on Cursor" as two labs when both resolve to the same provider.
  The existing `independence_tier` comparison handles this correctly *provided* the model→provider
  mapping is right, which is why requirement 4 matters.

## Open questions

1. **Is `--mode ask` sufficiently read-only for an untrusted-artifact review**, or does it retain
   file-read tools that could wander outside the artifact? Needs an empirical probe, not a docs
   reading — this is the one requirement that cannot be settled from `--help`.
2. **Does `--output-format json` wrap the model's text in an envelope** (like `claude -p`'s) that
   also reports the resolved model? If so it feeds `model_resolved` for free.
3. **Does the CLI honor a per-invocation `--model`** independently of the IDE's selection, or does
   it read `~/.cursor/cli-config.json`? The config file records `model.displayModelId`, so the
   precedence needs establishing before a tier can rest on `--model`.
4. **Rate limits and quota semantics** under a Cursor plan — a review that silently degrades to a
   cheaper model would invalidate the tier. Needs a probe.

## Recommendation

Worth building, **after** the Cursor host adapter has been dogfooded, and as its own change with its
own review — it is a new backend, a new consent destination, and a new read-only enforcement path,
which is more than a follow-on edit. **Two merge-blocking prerequisites, both empirical:** open question 1 (is `--mode ask` actually
read-only, and which file-read tools does it retain?) and requirement 3 (can the response report the
model that actually served the request?). If `--mode ask` cannot be shown read-only in practice, this
proposal does not proceed in this shape. If the resolved model cannot be recovered it may still
proceed, but only reporting `undetermined` — which removes most of its appeal and should be weighed
against the cost saving that motivates it.
