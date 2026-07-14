# Impasse

> An independent second opinion for any high-stakes call — business, strategy, writing,
> research, or code — from a cross-provider AI whose blind spots don't match your own.
> The host verifies and reconciles the review under a defined protocol, then hands you the
> problems worth acting on and the disagreements that need your decision.

**Status: pre-release.** Impasse is the open reimplementation of the workflow described in the
essay [*AI's Second Opinion: When Rival Models Disagree*](https://www.movingavg.com/essays/ai-second-opinion-rival-model.html).
The Codex CLI review path, the consent helper, and the schemas are implemented and tested; the
verify → reconcile → escalate reasoning is **directed by the host skill** (the agent follows the
protocol below), not enforced by the software — so a review is only as good as the host's
adherence to the protocol, not a guarantee in code. Running it on its own source caught a real
bug before release (a schema incompatibility with the Codex CLI's structured-output mode). It
pins to a fast-moving alpha of the Codex CLI and is maintained on a best-effort basis: expect
rough edges, and pin versions if you build on it.

## Why

The value of a second AI is *independence*, not a smarter answer. A reviewer trained by a
different provider may fail in different places, so a disagreement is a useful signal for where
a human should look. Impasse runs that cross-provider review, **verifies each finding**,
reconciles the two models, and reports the verified problems plus the disagreements that need
your judgment — not a raw list to triage. Agreement is evidence, not proof.

It is **domain-general** — the same protocol reviews:

- a **decision / strategy** memo (hidden assumptions, unpriced tradeoffs),
- a **document / essay** (unsupported claims, weak or self-contradicting arguments),
- **research** (a citation that doesn't support its claim, overgeneralization),
- **code** (correctness, security, missing error handling),
- a **dataset** or other artifact.

## How it works

1. **Review** — an independent reviewer returns structured findings, each with *anchored
   evidence* (a location in the artifact **plus** an observation — a bare location isn't
   evidence).
2. **Verify** — the host checks each finding against the actual artifact before trusting it.
3. **Reconcile** — accept / reject (with evidence) / resolve each finding; one rebuttal round.
4. **Report + escalate** — you get the verified findings to act on, and *only* the deadlock —
   an evidence conflict, or a value/priority judgment that's yours to make — comes to you as a
   crisp question:

   > **Question for you:** The reviewer argues that entering Europe first reduces concentration
   > risk; the memo argues it delays break-even by nine months. Which matters more here —
   > runway, or geographic diversification?

**See a full decision review** end to end — a build-vs-buy memo, not code — from rival finding
to the single call that needs a human: [`docs/walkthrough-decision.md`](docs/walkthrough-decision.md).

Full protocol: [`docs/protocol.md`](docs/protocol.md).

## What a run surfaces

The output is what *survived* scrutiny — the reviewer's findings, with a disposition on each. On
a decision artifact (a market-entry memo, not code), a run can produce all three outcomes:

- **Accepted** — the reviewer flags that the revenue model leans on a churn rate cited nowhere
  in the memo. The host checks, confirms the number is unsupported, and accepts it. → a real
  gap to fix.
- **Refuted with evidence** — the reviewer calls the go-to-market "undifferentiated." The host
  points to the paragraph where the memo already concedes the mechanic is commodity and stakes
  its case on distribution — the reviewer rediscovered a stated premise, not a hole. Rejected,
  with the quote. → the verify step catching a confident miss, so it never reaches you.
- **Escalated** — the reviewer wants Europe first to cut concentration risk; the memo wants to
  protect a nine-month runway. Neither is a fact. It comes to you as one question. → routed,
  not decided.

That mix — most findings verified, some refuted on evidence, a few escalated — is what a run is
for: an independent model checks the work, the host filters its misses where it can, and the
genuine judgment calls come to you. Each `show` closes with a running tally across your reviews.

## Requirements

- [Claude Code](https://claude.com/claude-code) (the host).
- The [OpenAI Codex CLI](https://github.com/openai/codex) installed and logged in (the
  reference reviewer backend) — [`docs/backends/codex.md`](docs/backends/codex.md).
- Python 3 (standard library only — the shipped helpers have no pip dependencies).
- macOS or Linux. Windows via WSL; native Windows is a [roadmap](docs/windows.md).

## Install

Impasse is a Claude Code skill — the repository *is* the skill directory:

```bash
git clone https://github.com/windaddict/impasse ~/.claude/skills/impasse
```

Then ask Claude Code to use Impasse — for example, "Use Impasse to review this decision memo."

## Data boundary & consent

Reviewing an artifact sends its content to a third-party provider. Impasse **blocks by
default** until you approve the destination, and shows a payload manifest so you approve *what*
is sent, not just *where*:

```bash
python3 scripts/impasse_consent.py grant https://api.openai.com --backend-type codex-cli
```

Consent is keyed to the normalized endpoint (a custom `OPENAI_BASE_URL` needs its own grant),
stored `0600` in your platform config dir. **Don't send secrets or regulated data without
authorization.** See [`docs/security-model.md`](docs/security-model.md).

## Structured output

Reviews and reconciliations are JSON, validated against
[`schemas/reviewer-response.v1.json`](schemas/reviewer-response.v1.json) and
[`schemas/reconciliation-result.v1.json`](schemas/reconciliation-result.v1.json). Domain
generality comes from an evidence *anchor* union (`file_range | text_quote | section |
structured_path | generic`) plus an optional external-source citation — see the worked
[`schemas/examples/`](schemas/examples/).

## Disclaimer

Impasse is provided under the MIT License, **"AS IS", without warranty of any kind**. Its
outputs may be inaccurate and are **not legal, financial, medical, tax, or other professional
advice**. Verify important conclusions; you remain responsible for your decisions. A second
model is not an independent source of truth — see the independence caveat in the security
model.

## Related work

OpenAI ships an official [Codex plugin for Claude Code](https://github.com/openai/codex-plugin-cc)
with read-only and adversarial **code** review, an optional review gate, and delegated Codex
tasks. Impasse is a different layer: a **domain-general** review-and-reconciliation protocol
(decisions, documents, research, data, and code) that verifies each finding and reconciles the
two models, escalating only what they can't settle rather than returning the review to triage.
It uses the Codex CLI as its **one backend today**; the protocol is backend-neutral by design,
but no second backend is wired up yet.

## Repository layout

```
SKILL.md              the skill (how the host drives Impasse)
schemas/              reviewer-response + reconciliation-result + examples
scripts/              stdlib-Python helpers (consent, supervised runner, lib)
docs/                 protocol, security model, backend, delegate mode, platform support
tests/                schema validation + helper tests (CI)
```

## Audit trail & reports

Every run is recorded — the reviewer's findings, and (once you save it) the reconciliation —
under your config dir, and `scripts/impasse_report.py show <review_id>` renders it: the
**reviewer↔host back-and-forth** on each finding, the **decision** made, a **tally** (raised /
resolved / accepted / rejected / escalated), and the questions escalated to you. `list` shows
past runs (flagging which still have open escalations); `forget` deletes one. `open` surfaces
runs with decisions you haven't answered yet; `prune --older-than N` cleans up old records
(keeping any with open escalations unless `--include-open`). Records contain artifact content —
they're kept `0600` and never committed.

Every `show` closes with a **running recap across your reconciled runs** — findings reviewed,
accepted, refuted with evidence, and escalated to you — a plain reminder of what independent
review has surfaced. Deeper longitudinal reporting (trends over time, per-artifact history) is
still roadmap; each run is fully inspectable on its own.

## Who builds this

Impasse is a working artifact from [Moving Average](https://www.movingavg.com/), an AI advisory
practice for CEOs and founders. The pattern behind it — running a rival model as an independent
reviewer and routing only the real disagreements to a human — is written up in the essay
[*AI's Second Opinion*](https://www.movingavg.com/essays/ai-second-opinion-rival-model.html).
Wiring model-to-model governance into how a team actually decides is the kind of thing the
[AI Workshop for CEOs](https://www.movingavg.com/ai-workshop-for-ceos.html) works through with a
group of executives. If that's the problem you're facing, start there.

## License & trademarks

MIT — see [`LICENSE`](LICENSE). Claude, Claude Code, Codex, OpenAI, and Anthropic are
trademarks of their respective owners; **Impasse is not affiliated with or endorsed by them.**
