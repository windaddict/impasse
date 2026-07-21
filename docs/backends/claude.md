# Backend: Claude Code CLI (same-provider fallback)

**A fallback for users without Codex — not the recommended default.** The host is Claude Code;
this backend makes the *reviewer* Claude too. That removes the cross-provider independence that is
Impasse's whole point: the reviewer shares the host's training and blind spots. Use it for its
**breadth / adversarial-second-pass** value when Codex isn't available — never as if it were an
independent second opinion.

## Where it sits on the independence ladder

**The ladder is host-relative** (`independence_tier()`): everything below assumes the usual case —
a **Claude host** (Claude Code). To a *Codex* host (`IMPASSE_HOST=codex`), this backend **is** the
different-provider reviewer: the runner labels it `cross_provider` and emits no downgrade notice,
while the codex backend gets the `same_provider` notice instead. See `docs/environments.md`.

1. **Different provider (Codex)** — the default; lowest correlation of blind spots. Prefer it.
2. **Same provider, different model** — some independence (pin a reviewer model ≠ the host's).
3. **Same provider, same model, fresh context + adversarial stance** — what this backend gives
   you by default. Catches anchoring/momentum errors, arithmetic, unsupported claims; misses the
   capability blind spots both share.

The runner enforces the anti-self-preference stance on every review (see
`compose_full_instruction`), which matters most here — a same-provider reviewer is the case most
prone to giving its own style the benefit of the doubt. Even so, **agreement from this backend is
weak evidence.** `review()` returns an `independence_notice` when the backend is same-provider;
the host must surface it to the operator.

## Requirements

- [Claude Code](https://claude.com/claude-code) installed and logged in (the `claude` CLI). No
  second vendor account — this is the point: it runs for anyone who already has the host.
- Python 3 (stdlib only) for the helpers.

## Resolution

`resolve_claude_command()`, order: `IMPASSE_CLAUDE_BIN` / `CLAUDE_BIN` override → `PATH` → known
locations (Homebrew, `/usr/local/bin`, `~/.local/bin`, `~/.npm-global/bin`, the Git-Bash
`%APPDATA%\npm` shim). Select it with `--backend claude`.

## Invocation (what the runner does)

```
claude -p "<reviewer instruction + schema>" \
  --output-format text \
  --permission-mode default \
  --strict-mcp-config \
  --allowed-tools "" \
  --disallowed-tools Edit Write NotebookEdit Bash WebFetch WebSearch Task   # artifact on stdin, then EOF
```

Verified behaviors (on `claude` 2.1.197 — re-check with `claude --help`, these are version
observations, not a durable API):

- **stdin is context.** The artifact is piped on stdin and reaches EOF via the supervisor (same
  mechanism as codex); the instruction is the prompt argument.
- **Final message is on STDOUT.** `claude -p` has no `--output-last-message` file; the runner reads
  the final answer from stdout. Because a chat-style backend sometimes wraps the JSON in a code
  fence or a line of prose, the runner parses tolerantly (`_parse_reviewer_json`: strip a fence,
  else a string-aware balanced-brace scan). It also rejects a stdout that hit the capture cap
  (`stdout_truncated`) rather than trying to parse a cut-off object.
- **Read-only is fail-closed, and it is NOT a process sandbox.** Unlike codex's
  `--sandbox read-only` (a real OS-level sandbox), the Claude reviewer runs in your normal Claude
  Code process. Its read-only posture is instead: an **empty allowlist** (`--allowed-tools ""`) so
  *no* tool is permitted (the artifact is on stdin — the reviewer needs none); `--strict-mcp-config`
  so no MCP servers load; and a pinned `--permission-mode default` so it can't inherit a permissive
  ambient mode (`acceptEdits`/`bypassPermissions`) or a `settings.json` that pre-allows network
  tools. An allowlist fails *closed* as Claude Code adds tools; the `--disallowed-tools` list is
  defense-in-depth (it also names the exfiltration vectors `WebFetch`/`WebSearch` and the spawn
  tool `Task`). Verified on 2.1.197: under this config the reviewer's attempts to `Read` a local
  file and to `WebFetch` are both blocked, yet it still answers from stdin. **Caveat:** do not run
  this backend under an override that weakens the permission gate.
- **Runs in a scratch dir, not your project (F003).** The reviewer subprocess is launched with its
  CWD set to the run's own throwaway scratch directory (under the impasse config dir), **not** the
  operator's project directory. This matters because `claude -p` discovers a project's `CLAUDE.md`
  and `.claude/` hooks by walking up from CWD — and here the "project" is the *artifact under
  review*, whose instructions are untrusted. Running in scratch keeps the reviewed artifact's own
  `CLAUDE.md`/hooks out of the reviewer, closing an artifact-controlled prompt-injection /
  independence-leak vector. This became load-bearing when a Codex host made `claude` the
  *cross-provider* reviewer. **Residual:** CWD isolation does not neutralize your **user-global**
  `~/.claude/CLAUDE.md` / `~/.claude/settings.json` hooks, which `claude -p` loads regardless of
  CWD — but those are *your own* config, not artifact-controlled, so they are an independence/noise
  consideration, not an injection vector. If you need the reviewer fully clean of user-global
  context, run it under a scratch `CLAUDE_CONFIG_DIR`. The codex backend is unaffected (its
  `--ignore-user-config --ignore-rules` already excludes config and `AGENTS.md`).
- **No reasoning-effort knob.** There is no `model_reasoning_effort` equivalent; a configured
  effort (`--effort`, `IMPASSE_CLAUDE_EFFORT`, or a persisted `set-effort` default) is ignored
  for this backend.
- **Model.** The runner omits `--model` (account default). Pinning a reviewer model *different*
  from the host's buys a rung of independence (ladder step 2) — worth doing if you know the host.

## Data destination

The destination is `ANTHROPIC_BASE_URL` (default `https://api.anthropic.com`). Consent is keyed to
the normalized endpoint, so a Bedrock/Vertex base URL or a gateway requires its own grant:

```
python3 scripts/impasse_consent.py grant https://api.anthropic.com --backend-type claude-cli
```
