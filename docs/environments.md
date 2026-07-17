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

Host identity (`detect_host()`): `IMPASSE_HOST` is authoritative
(`claude | codex | cursor | other`); a Claude host is auto-detected from its **genuine** env
markers only — deliberately not from `detect_environment()`, whose `IMPASSE_ENV` override is a
surface-policy knob and must not be able to manufacture a host identity. **A non-Claude host
adapter MUST export `IMPASSE_HOST`** — a subprocess cannot identify a driver that won't identify
itself, so an undeclared/unrecognized host is `unknown` and gets `undetermined`, **never a
positive cross-provider claim** (a human at the CLI can export `IMPASSE_HOST` if the driver is
known). Hosts that run an operator-selected underlying model (`cursor`, `other`) are likewise
`undetermined` — provider correlation can't be established in either direction — as is a backend
routed through an endpoint whose provider can't be attributed (a custom gateway).

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
host=...)` returns `{mode, tier, allowed, notice, recommendation, reason, host}` where
`mode ∈ {codex, claude, self_review, refuse}`. Capability-first (prefer the available subprocess
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
