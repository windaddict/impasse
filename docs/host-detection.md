# Host auto-detection — compatibility matrix & trust model

Impasse computes independence *relative to the host* driving the protocol (see
[`environments.md`](environments.md)). `detect_host()` / `host_detection()` identify that host from
environment variables the driving agent leaves in the subprocess it spawns. This file records the
markers, how far to trust them, and how drift is managed.

## Markers (strict value — presence alone never counts)

The **Confidence** column is the value `host_detection()` actually emits (`strong | heuristic | none`),
not a subjective marker rating — so it matches the runtime `host_detection.confidence` field.

| Host | Marker rule | Emitted confidence | Verified (agent / version / OS) | Upstream source |
|---|---|---|---|---|
| `claude` | `CLAUDECODE == "1"` **or** `CLAUDE_SURFACE ∈ {cowork,chat,sandbox}` | **strong** | Claude Code CLI, observed live in a spawned subprocess, macOS (2026-07) | empirically confirmed |
| `claude` | a presence-style surface flag: `CLAUDE_CODE_ENTRYPOINT` / `CLAUDE_COWORK` / `CLAUDE_CHAT_SANDBOX` affirmatively set | **heuristic** | these accept any non-falsy value, so they can't mint a *strong* (silent) cross-provider claim — a resulting positive tier carries the soft notice | empirically confirmed |
| `gemini` | `GEMINI_CLI == "1"` | **strong** | documented detection hook; not yet live-verified in this repo's harness | Gemini CLI shell-tool docs — geminicli.com/docs/tools/shell |
| `cursor` | `CURSOR_AGENT == "1"` | **none** — non-attributable (operator-chosen model); detected only to sharpen the recommendation, never a positive tier. Marker is documented but was once dropped/re-added in a `cursor-agent` release | cursor.com/docs/agent/tools/terminal |
| `codex` | `CODEX_SANDBOX == "seatbelt"` **or** `CODEX_SANDBOX_NETWORK_DISABLED == "1"` | **heuristic** | sandbox-state signal, not a host flag; absent under sandbox bypass | openai/codex issues #30356, #5041; developers.openai.com/codex/environment-variables |

**Deliberately not used:** `TERM_PROGRAM=vscode` (shared across the VS Code family — Cursor, VS Code,
extensions), `CURSOR_TRACE_ID` (unconfirmed), and config *inputs* like `CODEX_HOME`, `GEMINI_API_KEY`,
`AI_AGENT` (operator-set, or generic names — not host-injected identity markers).

## Resolution (fail-safe)

`host_detection()` returns `{host, method, confidence}`. Let `A` = attributable hosts with a matching
strict marker (⊆ `{claude, codex, gemini}`); `cursor?` = `CURSOR_AGENT == "1"`.

1. `IMPASSE_HOST` — absent/empty → fall through; nonempty-invalid → `unknown`; recognized but
   disagreeing with an observed marker → `unknown`; recognized & consistent → that host (`asserted`).
2. `|A| ≥ 2` → `unknown`. `|A| == 1` **and** `cursor?` → `unknown` (can't tell the inner driver).
   `|A| == 1` → that host (`codex` → `heuristic`, else `strong`). `|A| == 0` & `cursor?` → `cursor`.
   Else → `unknown`.

`confidence == "none"` (cursor / unknown) never rides a positive tier. A positive `cross_provider`
built on a `heuristic` detection carries a soft `independence_notice`.

## Trust floor (why this is "honest," not "authenticated")

Environment variables are **unauthenticated inherited strings**. Any ancestor process, shell rc, CI
job, wrapper, or the operator can set an exact marker and spoof a host. The strict-value matching and
the conflict/ambiguity → `unknown` rules eliminate the *accidental* and *ambiguous* failure modes —
the realistic ones — but they **cannot** stop a deliberately or accidentally injected exact marker.
This residual is the same one the original Claude-only detection always carried; it is accepted and
disclosed, not hidden. The independence label is only as trustworthy as the environment's integrity.
For a firmer basis on a weak host (notably Codex under sandbox bypass), set `IMPASSE_HOST` explicitly.

## Drift

These are third-party env contracts that change between releases (Cursor's already did once). The
unit suite (`tests/test_helpers.py`) proves only Impasse's **mapping logic** — a 128-cell decision
matrix checked against an independently written truth table — so a green suite does **not** confirm
that a host still emits its marker. Detecting that requires observing real host behavior:

- **Minimum:** keep the "Verified" column above current; re-verify a row when you bump that host's
  version, and cite the exact upstream contract.
- **Recommended (follow-up):** a scheduled live smoke test that launches each supported host, captures
  the environment its child process sees, and flags a missing/renamed/leaking marker — with
  negative-control environments (plain shell, CI, unrelated tools) to catch false positives.
