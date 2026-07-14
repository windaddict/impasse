# Walkthrough: reviewing a business decision

Impasse is domain-general, so this walkthrough is deliberately *not* about code. It runs one
review end to end over a **build-vs-buy decision memo** and shows what actually reaches the
human at the end. The structured output is real and schema-valid — the two files referenced
below are shipped fixtures you can validate:
[`schemas/examples/decision.reviewer-response.json`](../schemas/examples/decision.reviewer-response.json)
and [`schemas/examples/decision.reconciliation-result.json`](../schemas/examples/decision.reconciliation-result.json).

## The artifact

A decision record — `adr-2026-014-payments-orchestration` — recommends **building** an in-house
payments-orchestration layer rather than buying one. It has options, per-option assumptions and
risks, and a weighted-criteria scoring table that produces the recommendation. The operator
wants an independent second opinion before ratifying it.

## 1. Review — a rival model, evidence required

An independent reviewer (a different provider than the host) returns two findings, each with
*anchored evidence* — a specific place in the record plus an observation, not a vibe:

- **F001 (high, unaddressed risk).** The Build option assumes gateway integrations migrate in a
  single quarter with **no dual-run period**, stated as fact with no fallback — and there's no
  matching entry in the option's risk list. Anchored by JSON-pointer into
  `/options/1/assumptions/0` and `/options/1/risks`. If a dual-run is actually needed (the norm
  for payments cutover), the omitted cost could invert the build-vs-buy comparison.
- **F002 (medium, value tradeoff).** The framing calls **speed-to-market the top priority this
  year**, but the weights table scores **cost 0.40 vs time-to-market 0.20** — the scoring
  silently trades away the priority the memo declares most important. Anchored to the "Decision
  Criteria and Weights" section.

## 2. Verify — examine before trusting

The reviewer is useful and sometimes wrong, so the host checks each finding against the *actual*
record before accepting it:

- **F001** — confirmed the assumption and the missing risk by reading the record, then **re-ran
  the record's own cost model** under a one-quarter dual-run. Build's margin over Buy narrows
  from 18% to 4% — real, but it doesn't flip.
- **F002** — confirmed the contradiction: the weights genuinely invert the stated strategic
  priority.

## 3. Reconcile

- **F001 → resolved.** A real, verified risk with a factual fix. The host adds a migration-
  slippage risk with a dual-run contingency and records the dual-run cost scenario. The
  recommendation (Build) stands, now with the risk visible. **This never needed the operator** —
  the two models agreed, the evidence was checkable, and it was fixed.
- **F002 → deadlocked.** Both models agree the mismatch is real. But the *fix* is not a fact.
  Raising the time-to-market weight versus keeping cost higher is a **priority call**: it depends
  on whether the business intends to protect runway or maximize speed this year — and it changes
  which option wins. Neither model has the authority to decide that.

## 4. Escalate — only the call that needs a human

One thing reaches the operator. Not the raw findings list — the single decision the two models
can't settle, phrased as a crisp question:

> **The memo calls speed-to-market the top priority this year, but its scoring weights cost
> higher. Which should actually govern the decision — protect runway (weight cost), or maximize
> speed-to-market (weight time-to-market)? Your answer changes which option wins.**

## The point

Of two findings from an independent review, one was a **factual risk** — caught, verified, and
fixed without the operator's attention — and one was a **value judgment** that only the operator
can make, surfaced with the evidence laid out on both sides. That second one is the deliverable.
Independence found it; evidence made it legible; but the decision is, correctly, still yours.

This is what "domain-general" means in practice: the same protocol that finds an off-by-a-cent
bug in code finds an unpriced assumption and a buried priority tradeoff in a strategy memo — and
routes only the part that needs judgment to the person accountable for it.
