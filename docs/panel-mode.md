# Panel mode (proposal — NOT built)

**Status: design proposal.** Nothing here is implemented. This captures the design and, more
importantly, the constraints that keep panel mode from quietly breaking Impasse's thesis. Run
this proposal through Impasse (`kind=decision`) before building any of it.

## Motivation

The default path is one adversarial reviewer → verify → reconcile → escalate. That's the right
shape for *independence*, but it under-serves two things on high-stakes **decisions**:

1. **Breadth of perspective.** A decision's quality is often about *who it affects*. A single
   "find what's wrong" critic misses "you never considered what the ops team, the customer, or
   the regulator would say."
2. **Generative alternatives.** The critic finds flaws; it doesn't propose a *better option from
   an adjacent domain*. (This is the `codex-collaborator` skill's "brainstorm mode," which Impasse
   deliberately excludes to stay tight — a reviewer judges, it doesn't ideate.)

Panel mode fans out multiple agents to cover both — **as an explicit opt-in mode beside the core,
never replacing the single-reviewer default.**

## The constraint that governs everything: breadth ≠ independence

A panel of same-provider (e.g. all-Claude) agents multiplies *perspectives*, not *independence* —
they share training data and blind spots. **Panel mode must never wear the independence claim.**
The honest framing is a division of labor:

- **Independence** comes from the cross-provider skeptic (Codex), exactly as today.
- **Breadth** comes from the (possibly same-provider) panel.

The strongest configuration is therefore a **hybrid**: a Claude panel for breadth + one Codex
skeptic for independence — "perspectives from the room, the sharp disagreement from the outsider."

## Roles

| Agent | Job | Output | Independence |
|---|---|---|---|
| **Stakeholder agents** (N) | Evaluate the decision from one materially-affected party's vantage | findings (reviewer-response) | breadth only |
| **Adversarial skeptic** (1) | The current blunt critic — what's wrong/unsupported/risky | findings (reviewer-response) | **cross-provider (Codex)** |
| **Creative / cross-domain** (1) | Propose better alternatives from adjacent domains | *alternatives*, NOT findings | breadth only |
| **Meta / completeness critic** (1) | What did the panel miss — uncovered stakeholder, unverified claim, contradiction | coverage gaps | catches gaps, not new blind spots |

Notes:
- **Creative output is not findings.** Generating ("here's a better approach") and reviewing
  ("what's wrong") dilute each other in one pass — keep the creative agent's output on a separate
  track (advisory alternatives), not run through per-finding verify/reconcile.
- **The meta agent** is the "completeness critic" pattern. Same-provider, it mostly catches
  coverage gaps and inconsistency — useful, but not a source of new independence.

## Orchestration — this is a Workflow, not the core protocol

Panel mode is a fan-out/synthesize pipeline, which is what the Workflow layer is for. It must feed
back into the **same** reconciliation machinery so the human still gets verified findings + routed
deadlocks:

```
select stakeholders  →  fan out: [stakeholder×N, skeptic(Codex), creative, meta]
                     →  collect findings  →  META-DEDUP across agents (new step)
                     →  host verify each surviving finding
                     →  reconcile → escalate only the deadlocks
                     →  (creative alternatives surfaced separately, unverified)
```

The **meta-dedup step is the real new engineering.** One reviewer yields ~10 findings the host can
verify; a panel yields a larger, partly-contradictory pile that must be deduped and de-conflicted
*before* the host verifies — otherwise the reconciliation layer (Impasse's actual differentiator)
gets swamped and the "only deadlocks reach the human" promise breaks.

## Costs / when NOT to use it

- **Latency + tokens multiply** by the agent count. The single-reviewer path stays the default for
  "quick blunt second opinion."
- Panel mode is for **consequential decisions** where breadth is worth the spend — not code review,
  not documents, not a fast check.

## Open questions (resolve before building)

1. **Stakeholder selection** — host-inferred from the artifact, or operator-specified? (Bad
   stakeholder list = confident blind spots dressed up as coverage.)
2. **Bounding N** — cap stakeholder agents; `log()` any that were dropped (no silent truncation).
3. **Dedup strategy** — by (claim, anchor)? A cross-agent judge? This determines whether the
   reconciliation load stays tractable.
4. **Meta agent provider** — same-provider is cheap but blind-spot-correlated; is a second
   cross-provider pass worth it?
5. **Does the creative track belong in Impasse at all**, or is it a distinct "brainstorm" skill?

## Relationship to the Claude fallback backend

Panel mode and the [Claude fallback backend](backends/) are independent but complementary: the
fallback lets Impasse run with no second vendor install (breadth/independence ladder — see the
Guardrails independence caveat), and panel mode is where a same-provider Claude fleet earns its
keep as *breadth* while a cross-provider skeptic supplies *independence*. Build the fallback
backend first (smaller, on-thesis); panel mode only if decisions are the proven use.
