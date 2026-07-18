# Impasse

> **An independent second opinion for any high-stakes call — a decision, an essay, a research
> claim, a dataset, or code — from a cross-provider AI whose blind spots don't match your own.**

The independent **reviewer never edits your work** — it argues, with evidence. Keeping the critic
away from the pen is the point: fixes get applied by the host driving Impasse (or by you), never by
the model that's supposed to be checking you. And unlike a plain code reviewer, it doesn't hand you
a raw list to triage — it **verifies each finding, reconciles the two models, and escalates only
the genuine disagreement.** You get the verified problems, plus the one call that's actually yours.

```mermaid
flowchart TB
    A["Your artifact<br/>decision · essay · research · data · code"] --> R["🔎 Reviewer<br/>cross-provider AI · read-only"]
    R -->|"anchored findings"| V{"⚖️ Host verifies<br/>each finding vs. the real artifact"}
    V -->|"verified"| F["verified real<br/>host applies the fix"]
    V -->|"refuted with evidence"| X["dropped<br/>a confident miss"]
    V -->|"host disagrees,<br/>but has no evidence"| RB{"🔎 one rebuttal round<br/>reviewer substantiates<br/>or withdraws"}
    V -->|"value / priority call"| Q(["❓ one question<br/>→ you decide"])
    RB -->|"withdrawn / evidence found"| X
    RB -->|"neither side can win"| Q
    style R fill:#6366f1,color:#fff
    style RB fill:#6366f1,color:#fff
    style V fill:#0ea5e9,color:#fff
    style Q fill:#f97316,color:#fff
    style X fill:#e5e7eb,color:#111
```

The reviewer (indigo) proposes; the host (blue) verifies and applies the fixes they agree on; the
judgment calls come to you. The independent reviewer never edits — the critic and the editor stay
separate. A refutation only *drops* a finding when the host has contradicting evidence — a host
disagreement with no evidence isn't a rejection, so it goes back to the reviewer for one round,
then to you if neither side can win.

**Status: pre-release.** The open implementation of the pattern from the essay
[*AI's Second Opinion: When Rival Models Disagree*](https://www.movingavg.com/essays/ai-second-opinion-rival-model.html).
The Codex path, consent gate, and schemas are implemented and tested; verify → reconcile →
escalate is **directed by the host skill, not enforced in code** — a review is only as good as the
host's adherence to the protocol (see Guardrails). Dogfooding it on its own source caught a real
shipping bug before release. It pins to a fast-moving alpha of the Codex CLI and is best-effort —
expect rough edges.

## Example

Ask Claude Code in plain English:

> Use Impasse to get a second opinion on this build-vs-buy memo before I commit.

It runs a cross-provider reviewer, verifies each finding against your artifact, and hands back a
report — the problems worth acting on, the one the host threw out, and the single call that's yours:

```text
📊 Decisions: 4 finding(s) raised → 🤝 2 accepted · ❌ 1 rejected · ⚖️ 1 escalated to you
──────────────────────────────────────────────────────────────
F002  🟠 high  ❌ rejected
  🔎 Reviewer: the go-to-market is undifferentiated.
  ◀ Host:     the memo already concedes the mechanic is commodity and stakes its case on
              distribution — a rediscovered premise, not a gap. Rejected, with the quote.
F004  🟠 high  ⚖️ ESCALATED — needs your decision
  ❓ Enter Europe first to cut concentration risk, or protect the nine-month runway?
──────────────────────────────────────────────────────────────
📈 Your Impasse record — 9 reviews reconciled
   31 findings reviewed · 18 accepted · 7 refuted with evidence · 2 resolved · 4 escalated to you
```

*Example output. The reviewer never edits your work; the host applies the fixes it verifies, and
only the genuine disagreement reaches you. Your record is local to your machine and grows as you use it.*

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

## It reviews itself — the maintainer's ledger

The maintainer's practice is to put substantial changes through Impasse before shipping them.
Below is what that practice has produced so far — **a snapshot of every reconciled review record
on the maintainer's machine, all artifact kinds** (as of 2026-07-18). It is a count of what the
saved records contain — not an audit proving every change was reviewed, and not a complete count
of every event in every conversation:

| | |
|---|---|
| Reviews reconciled | 65 |
| Findings raised by the reviewer | 391 |
| … resolved (host addressed the finding) | 345 |
| … accepted (host agreed; noted or deferred) | 36 |
| … refuted — each with contradicting evidence, as the schema requires of a saved rejection | 10 |
| … withdrawn | 0 |

**Escalation counts are deliberately not reported yet.** An important operational metric is how
often findings need a human ruling — no reliable historical rate exists. The counting rule only
recently became channel-independent (an operator ruling that decides a disposition now counts as
an escalation whether it arrived through a formal deadlock or through conversation), and the
operator attests that more judgment calls reached him than the pre-rule records captured.
Historical events whose exact wording is no longer recoverable can't be amended in (the rule
requires the question as actually posed), so rather than publish a number known to undercount,
**the ledger will report escalations — numerator and denominator — after the next 50 reconciled
reviews under the corrected rule** (counting from 2026-07-18; the maintainer applies the rule).
The same capture caveat bounds the whole table: these are counts of what the saved
reconciliations contain — raw-mode runs, failed runs, and anything never reconciled are outside
them by construction. Review runs that fail outright produce no reconciliation and are not in
the 65 — at least one did, see below.

Four of these reviews covered **this codebase's own release cycle** (23 findings). Three times
the cross-provider reviewer **overturned the author's design decision** with an argument the
author then conceded: a fail-open host-identity fallback that overstated independence in exactly
the case it existed to prevent; a retryability spec the operator himself had written; and a
byte-vs-character bound that could let a silently truncated reviewer message pass as a complete
review. One review run also **failed outright** on malformed reviewer JSON — that failure became
[issue #1](https://github.com/windaddict/impasse/issues/1) and the retry logic that fixed it.
The [CHANGELOG](CHANGELOG.md) summarizes each episode; the resulting code and tests are in this
repository. The raw run records stay local **by design** — they can contain reviewed artifact
content (see the data-boundary section) — so what's public is the maintainer's summaries and the
diffs, not the reviewer transcripts. **With one deliberate exception:** for the fail-open case,
the full reviewer response and reconciliation are published verbatim in
[`docs/evidence/host-independence-review/`](docs/evidence/host-independence-review/) (the
reviewed artifact was this repo's own public code, so nothing sensitive rode along), with the
case narrated for non-developers in
[`docs/case-study-host-independence.md`](docs/case-study-host-independence.md).

**Weigh this for what it is:** author-run dogfooding on the author's own artifacts, reported by
the author. It's offered as a process record, not proof — and it says nothing yet about how
Impasse performs on *your* work.

## How it works

1. **Review** — an independent reviewer returns structured findings, each with *anchored
   evidence* (a location in the artifact **plus** an observation — a bare location isn't
   evidence).
2. **Verify** — the host checks each finding against the actual artifact before trusting it.
3. **Reconcile** — accept / reject (with evidence) / resolve each finding; one rebuttal round.
4. **Report + escalate** — you get the verified findings to act on, and *only* the deadlock —
   an evidence conflict, a value/priority judgment that's yours to make, or a host objection it
   couldn't back with evidence — comes to you as a crisp question:

   > **Question for you:** The reviewer argues that entering Europe first reduces concentration
   > risk; the memo argues it delays break-even by nine months. Which matters more here —
   > runway, or geographic diversification?

**See a full decision review** end to end — a build-vs-buy memo, not code — from rival finding
to the single call that needs a human: [`docs/walkthrough-decision.md`](docs/walkthrough-decision.md).

Full protocol: [`docs/protocol.md`](docs/protocol.md).

## What the reviewer checks for

The reviewer **observes and argues — it never edits your artifact; the critic never holds the
pen.** Every finding must carry *anchored evidence*: a specific location **and** an observation of
what's wrong there — never a bare "line 40 looks off." What it looks for adapts to the artifact:

- **Decision / strategy** — hidden assumptions, unpriced tradeoffs, and each *materially-affected
  stakeholder's* view (who executes it, who bears the downside, the customer, the regulator).
- **Document / essay** — unsupported claims, weak or self-contradicting arguments, structure.
- **Research** — a citation that doesn't support its claim, overgeneralization, missing counter-evidence.
- **Code** — correctness, security, edge cases, missing error handling.
- **Data / other** — whatever the artifact's own structure makes checkable.

Then the host does the half the reviewer can't be trusted to do alone: **verify** each finding
against the real artifact, **reject** the confident misses with evidence, and **escalate** only the
judgment call. The reviewer proposes, the host verifies and fixes, and you decide the rest.

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
- The [OpenAI Codex CLI](https://github.com/openai/codex) installed and logged in — the
  recommended, cross-provider reviewer backend — [`docs/backends/codex.md`](docs/backends/codex.md).
  **No Codex?** A same-provider **Claude fallback** (`--backend claude`) runs on Claude Code alone,
  no second vendor account — but it shares the host's blind spots, so it buys breadth, not
  independence — [`docs/backends/claude.md`](docs/backends/claude.md).
- Python 3 (standard library only — the shipped helpers have no pip dependencies).
- macOS or Linux. Windows via WSL; native Windows is a [roadmap](docs/windows.md).

### How independent is it?

Independence is a ladder, not a switch — and Impasse always tells you which rung you're on. A
different provider is the point; the fallbacks trade independence for reach.

```mermaid
flowchart TB
    B1["Different provider — Codex<br/>real independence · default"] --> B2["Same provider, fresh process<br/>Claude fallback · breadth, not independence"]
    B2 --> B3["Self-review<br/>last resort · sandbox/Cowork only · refused for code"]
    style B1 fill:#16a34a,color:#fff
    style B2 fill:#eab308,color:#111
    style B3 fill:#dc2626,color:#fff
```

For the usual Claude host, genuine independence needs a Codex login; the weaker rungs run on
Claude alone. The rungs are labeled **relative to the host** driving the protocol (the diagram
shows the Claude-host case): to a Codex host, the Claude backend is the different-provider rung,
and the runner computes and discloses the tier accordingly (`IMPASSE_HOST`). Detail:
[`docs/environments.md`](docs/environments.md).

## Install

Impasse is a Claude Code skill — the repository *is* the skill directory:

```bash
git clone https://github.com/windaddict/impasse ~/.claude/skills/impasse
```

Then ask Claude Code to use Impasse — for example, "Use Impasse to review this decision memo."

**Choosing the reviewer model:** by default the backend's own default is used. Ask Claude Code to
pick one and it presents the options (Codex can't enumerate models, so it's a curated list plus a
free-text "other" — availability depends on your account). Or set it directly: `--model <name>` per
run, `scripts/impasse_run.py set-model --backend codex <name>` to persist, or the
`IMPASSE_CODEX_MODEL` / `IMPASSE_CLAUDE_MODEL` env var. Precedence: flag > env > persisted > default.
Pinning a model *different* from the host's also climbs a rung on the independence ladder.

**Fast checks (`--raw`):** for a quick, low-stakes look at your own work, `review --raw` returns the
reviewer's findings and skips the verify → reconcile → escalate protocol (and doesn't record). The
findings are **unverified** — the host hasn't checked them — so use the full protocol when it matters.

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

Impasse is provided under the MIT License, **"AS IS", without warranty of any kind** — including no
warranty of merchantability, fitness for a particular purpose, or non-infringement. Its outputs
(and the reviewer's) may be wrong and are **not legal, financial, medical, tax, or other
professional advice**, nor a substitute for professional or human judgment. Verify important
conclusions; **you remain responsible for every decision and every change you make.** A second
model is not an independent source of truth — see the independence caveat in the security model.

To the maximum extent permitted by law, the authors are not liable for any damages arising from use
of the software, and are **not responsible for the third-party AI providers** (OpenAI, Anthropic) —
their availability, output, pricing, or handling of the data you choose to send them. Impasse is
**pre-release**: interfaces, storage formats, and behavior may change without notice.

## Acceptable use

These are reminders of your responsibilities under the law and the providers' terms — not
additional conditions Impasse places on the MIT license:

- **Don't send** secrets, credentials, personal or regulated data, or anyone else's confidential
  information without authorization — the tool doesn't scan for them, and a send leaves your machine
  for a third-party provider.
- **Comply with the backends' own terms** — the [OpenAI Usage Policies](https://openai.com/policies/usage-policies/)
  and the [Anthropic Usage Policy](https://www.anthropic.com/policies/usage-policy), and each
  provider's privacy and data-handling terms, govern what you send; you are responsible for your API
  keys, provider accounts, and any usage costs.
- **Don't rely on it for unlawful, harmful, or high-stakes automated decisions** without human
  review. Impasse routes the judgment calls to a human by design — keep it that way.
- **Export/sanctions:** you are responsible for complying with applicable export-control and
  sanctions laws, and with your providers' geographic restrictions.

Impasse stores **run records locally** (the config dir's `runs/`) — they hold whatever you sent, so
treat the local store as sensitive. Impasse itself sends your artifact only to the provider you
invoke. Delete Impasse's local records with `impasse_report.py forget <id>` or `prune` — this
removes only Impasse's local copies, not anything already sent to a provider.

## Related work

OpenAI ships an official [Codex plugin for Claude Code](https://github.com/openai/codex-plugin-cc)
with read-only and adversarial **code** review, an optional review gate, and delegated Codex
tasks. Impasse is a different layer: a **domain-general** review-and-reconciliation protocol
(decisions, documents, research, data, and code) that verifies each finding and reconciles the
two models, escalating only what they can't settle rather than returning the review to triage.
It uses the Codex CLI as its cross-provider reviewer, with a same-provider Claude fallback
(`claude -p`) for users without Codex — breadth, not independence; the protocol is backend-neutral.

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

MIT — see [`LICENSE`](LICENSE). Claude, Claude Code, Codex, OpenAI, and Anthropic are trademarks of
their respective owners, used here only for identification and comparison (nominative fair use).
Impasse is independent and is **not affiliated with, sponsored by, or endorsed by** them. See
[`NOTICE`](NOTICE).
