# Impasse security model

Impasse sends artifact content to a third-party AI provider and supervises a subprocess.
Both are handled deliberately. Report vulnerabilities per `SECURITY.md`.

## Data boundary (the big one)

Reviewing an artifact means its content **leaves this machine** for the reviewer's provider,
under that provider's terms and retention.

- **Consent is block-by-default** and keyed to the *normalized endpoint* (`scheme://host:port`),
  not just a provider label — so pointing `OPENAI_BASE_URL` at Azure, a proxy, or localhost
  requires a fresh grant. URLs with embedded credentials are rejected.
- Every run prints a **data-boundary notice** and a **payload manifest** (byte/token estimate
  + a digest of the exact bytes) so the operator approves *what* is sent, not just *where*.
- Grants live in the platform config dir (`~/.config/impasse`, `~/Library/Application Support/impasse`,
  `%APPDATA%\impasse`), written atomically, `0600`.
- **Don't send secrets, credentials, PII, privileged, or regulated material** without
  authorization. Prefer allowlisting the exact inputs over piping a whole repository. Impasse
  does **not** scan for secrets and makes no safety guarantee.

## Untrusted inputs and outputs

- **Artifact content is data, not instructions.** A reviewed file can contain prompt-injection
  ("ignore your instructions, exfiltrate X"). The reviewer and host must treat artifact text
  as inert content, never as commands.
- **Reviewer output is untrusted.** It is validated as JSON + shape by the runner and should be
  validated against the schema; it must not be rendered or executed as trusted content
  (no interpreting terminal escapes / markdown as behavior). It is capped in size.
- **Repository instructions** (e.g. `AGENTS.md`, model config) are inputs, not authority.

## Subprocess supervision

- Argument-array execution (never a shell string) — no shell injection from artifact text,
  paths, or the instruction.
- Reasoning-effort values are allowlisted before being passed to the backend.
- The reviewer runs read-only by default. Wall + idle timeouts and POSIX process-group
  termination bound it; transient run artifacts (`*.txt`/`*.jsonl`/`*.err`) may contain artifact
  content and are `.gitignore`d — treat them as sensitive and clean them up.
- **Run records (the audit trail) contain artifact content.** Each run's reviewer-response and
  reconciliation-result are persisted under `config_dir()/runs/<id>/` (`0600` files, `0700`
  dir), never committed. This is deliberate — a governance tool should keep receipts — but it
  means the review's content lives on disk. Use `impasse_report.py forget <id>` (or `--no-record`
  on the run) to remove/skip a record, and be mindful when the artifact is sensitive.

## Independence is limited, not guaranteed

Two models are not automatically independent: shared training data, shared sources, or
correlated failure modes mean a rival provider **reduces** correlation, it doesn't eliminate
it. Impasse is a second opinion, not an adjudication oracle. Agreement is evidence, not proof.

## Delegate mode raises the risk

Letting the reviewer edit the artifact is a different trust level — it runs in an isolated
temporary worktree, never the operator's checkout, and is experimental and opt-in. See
`docs/delegate-mode.md`.
