# Backend: OpenAI Codex CLI

The reference reviewer backend. Impasse's protocol is backend-neutral; Codex is the first
(and, in v1, only) implementation. `scripts/impasse_lib.py` resolves it and
`scripts/impasse_run.py` supervises it.

## Requirements

- The Codex CLI (`@openai/codex`) installed and logged in (`codex login`). Auth/config load
  from `~/.codex` automatically.
- Python 3 (stdlib only) for the Impasse helpers.

## Resolution

Order: `IMPASSE_CODEX_BIN` / `CODEX_BIN` override → `PATH` → known install locations
(Homebrew, `/usr/local/bin`, `~/.local/bin`, `~/.npm-global/bin`, the macOS desktop app at
`/Applications/Codex.app/Contents/Resources/codex`, and the Git-Bash `%APPDATA%\npm` shim).
nvm/fnm installs are on `PATH` in a normal shell; from a stripped non-interactive `PATH`, set
`IMPASSE_CODEX_BIN` rather than let it guess a Node version.

## Invocation (what the runner does)

```
codex exec --json --output-last-message <file> \
  --sandbox read-only --color never --skip-git-repo-check --ephemeral \
  [-c model_reasoning_effort="low"] \
  "<reviewer instruction + the reviewer-response schema>"   # artifact piped on stdin, then EOF
```

Verified behaviors (on `codex-cli 0.144.0-alpha.4` — re-check with `codex exec --help`, these
are version observations, not a durable API):

- **stdin must reach EOF.** `codex exec` blocks indefinitely if stdin is an open, unwritten
  pipe. The runner writes the artifact and closes stdin (or uses `/dev/null`). This is why the
  artifact is piped, not passed as an argv element (also avoids `ARG_MAX`).
- **`--json`** streams JSONL events; **`--output-last-message`** writes the final answer to a
  file (the runner reads the answer there and treats the JSONL as telemetry).
- **NOT `--output-schema`.** It routes to OpenAI's structured-output mode, which requires a
  *restricted* schema (every property in `required`; no `oneOf`/`allOf`/`if-then`/`minLength`/
  `pattern`). The rich `reviewer-response.v1.json` doesn't qualify (the API returns
  `invalid_json_schema`). So the runner **embeds the schema in the instruction** and validates
  the returned JSON afterward, rather than relying on the CLI to enforce it.
- **Reasoning effort:** valid values are `none|low|medium|high|xhigh`; **`minimal` is rejected**.
  The runner allowlists these.
- This build's `codex exec` has **no `--ask-for-approval`**; access is controlled with
  `--sandbox` only.

## Data destination

The destination is `OPENAI_BASE_URL` (default `https://api.openai.com`). Consent is keyed to
the normalized endpoint, so a custom base URL (Azure, a gateway, localhost) requires its own
grant. See `docs/security-model.md`.
