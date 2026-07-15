# Environments & the independence ladder

Impasse's reviewer backends are subprocesses — `codex exec` (cross-provider) and `claude -p`
(same-provider fallback). Running a subprocess needs a real shell, so **where Impasse runs decides
how much independence it can offer.** Claude Code is the best environment; everywhere else the tool
degrades along a ladder, and the rule is: *degrade gracefully, never silently.*

## The ladder (strongest → weakest)

| Tier | Reviewer | Independence | Where |
|---|---|---|---|
| `cross_provider` | Codex, a subprocess | real — different provider, different blind spots | Claude Code |
| `same_provider` | Claude in a fresh process (`claude -p`) | breadth; shares the host's blind spots | Claude Code |
| `self_review` | the host model, in its own context | near-zero — self-critique | chat sandbox / Cowork only |

Each step down is disclosed: the runner emits an `independence_notice` for `same_provider`; the
`self_review` tier emits a louder `self_review_notice`.

## Environments

- **Claude Code** — full shell. Resolves and runs a backend (Codex by default; Claude fallback if
  Codex is absent). Today the only surface that runs a reviewer subprocess, hence the only one that
yields genuine independence. Self-review is **refused
  here** — degrading to the host's own context would throw away the independence you actually have.
- **Claude chat sandbox** (claude.ai Skills) — a code container with no CLI installs and no
  reliable way to spawn a fresh isolated reviewer. No subprocess backend runs. Self-review is
  permitted, with full disclosure.
- **Claude Cowork** — treated like the sandbox: if it can't run a reviewer subprocess, it self-
  reviews with disclosure. (If a future Cowork *can* run a backend, `review_mode` prefers it
  automatically — the policy is capability-first.)
- **Unknown** — fail safe. Self-review is **not** permitted; `review_mode` returns `refuse` rather
  than silently degrade on a surface we can't confirm is a sandbox.

## The policy (one source of truth)

`impasse_lib.review_mode(kind, environment=..., codex_available=..., claude_available=...)` returns
`{mode, tier, allowed, notice, recommendation, reason}` where `mode ∈ {codex, claude, self_review,
refuse}`. Capability-first (prefer any subprocess backend, on any surface), then env-gated
(self-review only in `chat_sandbox`/`cowork`, and never for `code`). CLI:

```bash
python3 scripts/impasse_run.py mode --kind decision
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
