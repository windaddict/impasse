# The Impasse protocol

Backend-neutral. The host drives it; a reviewer backend answers; the operator decides
what the two can't settle. Reconciliation is **per-finding**, not one global loop — item A
can be resolved while item B is still contested.

## States (per finding)

```
prepared
  → reviewed              reviewer returns anchored observations (reviewer-response schema)
  → finding_triage        host classifies each finding
  → evidence_verification host checks the evidence against the real artifact/facts
  → host_response         accepted | rejected (with evidence) | contested | resolved
  → reviewer_rebuttal     one round, on contested findings only
  → resolved | deadlocked
```

The overall run ends in `converged`, `deadlocked`, `incomplete` (a bound was hit), or
`failed` (backend/timeout/consent/invalid-response). **A failure is never reported as
success.**

## Verification methods (strongest to weakest)

Recorded per verification, so the basis of a decision is auditable:

- `artifact_inspection` — the host read the artifact directly.
- `command` — a deterministic check/test was run.
- `source_retrieval` — an external source was fetched and read.
- `operator_confirmation` — the operator confirmed a fact.
- `model_inference` — another model opinion. The **weakest**; not a verification.

## Bounds (keep it terminating and honest)

- **One rebuttal round per finding** by default.
- **Stop when neither side brings new evidence** — trading opinions never converges; trading
  evidence does.
- A **total review deadline** and a **byte/token budget** (the runner enforces wall + idle
  timeouts; see `backends/codex.md`).
- These are the reasons a run can end `incomplete` rather than converged.

## Escalation — the whole point

A finding reaches the operator when it deadlocks. Two independent dimensions are recorded:

- **`dispute_kind`** — *what kind* of disagreement: `evidence_conflict`, `evidence_gap`,
  `assumption_difference`, `interpretation_difference`, `value_or_priority_tradeoff`,
  `policy_or_authority_required`.
- **`stop_reason`** — *why the loop stopped*: `no_new_information`, `round_limit`,
  `budget_limit`, `verification_unavailable`, `operator_authority_required`.

**Not every disagreement should be settled.** `value_or_priority_tradeoff` and
`policy_or_authority_required` are judgment calls that belong to the operator — the models
must not "resolve" them. Each deadlock carries a crisp `operator_question`.

## Honest claim

Impasse cannot guarantee it escalates *only* genuine, evidence-backed disagreements. What it
guarantees is that it **filters** disagreements through a documented process and **labels**
their evidentiary status — so the operator spends judgment on the calls that survived that
filter, not on a raw findings dump.

## Schemas

- `schemas/reviewer-response.v1.json` — the reviewer's observations for one pass.
- `schemas/reconciliation-result.v1.json` — per-finding disposition + inline escalations.

Both enforce their invariants (evidence needs anchor+observation; `approve` ⇒ no findings;
`converged` ⇒ no deadlocked item; a deadlocked item ⇒ an escalation; `failed` ⇒ a failure
object). See `tests/validate_schemas.py`.
