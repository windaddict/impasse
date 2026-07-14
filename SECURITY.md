# Security policy

Impasse sends artifact content to a third-party AI provider and supervises a subprocess.
Please read [`docs/security-model.md`](docs/security-model.md) for the trust boundaries
(data boundary, prompt-injection, untrusted reviewer output, independence limits).

## Reporting a vulnerability

Please report suspected vulnerabilities **privately** — do not open a public issue for a
security problem. Use GitHub's private vulnerability reporting on this repository (Security →
Report a vulnerability), or contact the maintainer directly.

Include: what you found, how to reproduce it, and the impact. We'll acknowledge and work a fix
before any public disclosure.

## Scope notes

- Impasse does **not** scan for secrets and makes no guarantee that sensitive data is kept out
  of a review. Approving a send is the operator's responsibility; prefer allowlisting inputs.
- Reviewer output and reviewed artifacts are **untrusted**. Reports about the tool
  interpreting them as trusted content (prompt injection, escape-sequence rendering, executing
  cited paths) are in scope.
- Delegate mode is experimental and outside the trusted path; treat findings there accordingly.
