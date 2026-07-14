# Impasse

> An independent second opinion for any high-stakes call — business, strategy, writing,
> research, or code — from a cross-provider AI whose blind spots don't match your own.
> It verifies and reconciles the review, then hands you the problems worth acting on and
> the disagreements that need your decision.

**Status: pre-release.** The Codex CLI review path, the consent helper, and the schemas are
implemented and tested; verification, reconciliation, and escalation are currently directed by
the host skill rather than enforced end to end. Expect rough edges.

## Why

The value of a second AI isn't a smarter answer — it's *independence*. A reviewer trained by a
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

Full protocol: [`docs/protocol.md`](docs/protocol.md).

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
It uses the Codex CLI as its reference backend; the protocol is backend-neutral.

## Repository layout

```
SKILL.md              the skill (how the host drives Impasse)
schemas/              reviewer-response + reconciliation-result + examples
scripts/              stdlib-Python helpers (consent, supervised runner, lib)
docs/                 protocol, security model, backend, delegate mode, platform support
tests/                schema validation + helper tests (CI)
```

## License & trademarks

MIT — see [`LICENSE`](LICENSE). Claude, Claude Code, Codex, OpenAI, and Anthropic are
trademarks of their respective owners; **Impasse is not affiliated with or endorsed by them.**
