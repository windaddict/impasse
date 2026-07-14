# Impasse

> An independent second opinion for any high-stakes call — business, strategy, writing,
> research, or code — from a rival AI whose blind spots don't match your own.
> **Only the disagreement they can't settle reaches you.**

**Status: pre-release.** The schemas, the stdlib helpers, and the skill are in place and
tested; the README and docs describe the intended shape. Expect rough edges.

## Why

The value of a second AI isn't a smarter answer — it's *independence*. A reviewer trained by a
different provider fails in different places, so where the two models **can't agree** is
exactly the spot that still needs a human. Impasse runs that rival review, reconciles the two
models, and surfaces only the residue: the evidence-backed deadlock, or the genuine judgment
call. Agreement is evidence, not proof.

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
4. **Escalate only the deadlock** — what the two models can't settle (an evidence conflict, or
   a value/priority judgment that's yours to make) is put to you as a crisp question.

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

Then ask Claude Code for a second opinion on an artifact, or invoke `/impasse`.

## Data boundary & consent

Reviewing an artifact sends its content to a third-party provider. Impasse **blocks by
default** until you approve the destination, and shows a payload manifest so you approve *what*
is sent, not just *where*:

```bash
python3 scripts/impasse_consent.py grant https://api.openai.com --endpoint https://api.openai.com --backend-type codex-cli
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
that runs Codex as a **code** reviewer and returns findings for a human to triage. Impasse is a
different layer: **domain-general** (not just code) and it **reconciles** the two models,
escalating only what they can't settle. It uses the Codex CLI as one backend; the protocol is
backend-neutral.

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
