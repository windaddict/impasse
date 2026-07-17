# Backend: OpenAI Codex CLI

The reference reviewer backend. Impasse's protocol is backend-neutral; Codex is the reference
implementation, alongside the `claude` fallback backend (`docs/backends/claude.md`).
`scripts/impasse_lib.py` resolves it and `scripts/impasse_run.py` supervises it.

Its independence tier is **host-relative**: to a Claude host (the usual case) it's the
`cross_provider` reviewer; to a Codex host (`IMPASSE_HOST=codex`) it's `same_provider` and the
runner says so — use `--backend claude` there instead. See `docs/environments.md`.

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
  --ignore-user-config --ignore-rules \
  [-m <model>] [-c model_reasoning_effort="low"] \
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
  The runner allowlists these. `-c model_reasoning_effort` still applies under `--ignore-user-config`.
- **Hermetic by default.** The runner adds `--ignore-user-config` (ignore `~/.codex/config.toml`)
  and `--ignore-rules` (ignore repo `AGENTS.md`): config can't reroute the endpoint away from the
  consented destination, and an artifact's own repo can't inject instructions into the read-only
  reviewer. Auth in `~/.codex/auth.json` survives `--ignore-user-config` (verified). Opt out with
  `IMPASSE_CODEX_RESPECT_CONFIG=1`.
- **`--output-schema` unreliability is real:** OpenAI's own issues report it is silently ignored
  when tools/MCP are active (openai/codex#15451) and doesn't apply to only the final message
  (#19816) — so the embed-schema-in-prompt-then-validate approach above is the right call; don't
  "fix" it back to `--output-schema`.
- This build's `codex exec` has **no `--ask-for-approval`**; access is controlled with
  `--sandbox` only.

## Data destination

The destination is `OPENAI_BASE_URL` (default `https://api.openai.com`). Consent is keyed to
the normalized endpoint, so a custom base URL (Azure, a gateway, localhost) requires its own
grant. See `docs/security-model.md`.

**Consent keying — hardened.** The runner launches Codex with `--ignore-user-config` by default, so
a custom `base_url`/provider in `~/.codex/config.toml` can't silently reroute data away from the
consented `OPENAI_BASE_URL` destination (auth in `~/.codex/auth.json` is unaffected — verified). If
you *rely* on `~/.codex/config.toml` (e.g. an enterprise gateway), set `IMPASSE_CODEX_RESPECT_CONFIG=1`
to honor it — then keep that config consistent with the endpoint you grant. (The `claude` backend
has the analogous concern and *refuses* under `CLAUDE_CODE_USE_BEDROCK/VERTEX`.)

## Model selection

The runner omits `-m` by default, so Codex uses its built-in default — and because
`--ignore-user-config` is on, a model pinned in `~/.codex/config.toml` is ignored, so pass it
explicitly if you want a specific one. Precedence: `--model <name>` (per run) > `IMPASSE_CODEX_MODEL`
env > a persisted default (`impasse_run.py set-model --backend codex <name>`, stored `0600` in
`settings.json`) > the backend default. The `claude` backend mirrors this (`IMPASSE_CLAUDE_MODEL`,
`set-model --backend claude`) — pinning a reviewer model *different* from the host's climbs a rung
on the independence ladder.

**No enumerable model list.** Codex has no `models` subcommand, and the valid set is
account-dependent (ChatGPT-account tier vs API key); an unsupported `-m` value fails only at call
time with a `400 … model is not supported`. So an interactive picker can offer a *curated*
candidate list plus a free-text "other" — it can't authoritatively enumerate.

## Reasoning effort

Mirrors model precedence: `--effort` (per run) > `IMPASSE_CODEX_EFFORT` env > a persisted default
(`impasse_run.py set-effort <effort>`, stored in `settings.json` next to the model default) > the
backend default. When nothing is configured the runner omits the flag and Codex uses its own
default (**medium**, as of current CLI builds). Values are allowlisted (`none|low|medium|high|
xhigh`) at every entry point: the CLI flag and `set-effort` by argparse choices, the persisted
value again on read (a hand-edited `settings.json` can't smuggle a bad value), and the env var at
resolution — an invalid `IMPASSE_CODEX_EFFORT` fails as a structured `backend_error` naming the
variable, never a traceback. The review result reports the resolved value in `effort` (`null`
means backend default). Higher effort means longer silent server-side reasoning — scale `--wall`
with it (see SKILL.md Timeouts). The claude backend has no effort equivalent: nothing resolves
for it (an `IMPASSE_CLAUDE_EFFORT` is ignored, never an error) and it reports `effort: null`.
The argv builder re-checks the allowlist before interpolating into `-c` (defense in depth).

## Failure handling (limits & outages)

On an API error Codex exits non-zero and puts the real error — `{"type":"error"|"turn.failed", …}`
carrying the HTTP status + message — in the **`--json` stream**, not stderr. The runner parses that
stream and classifies the failure: `rate_limited` (429 / "usage limit" / "quota"),
`service_unavailable` (5xx / "overloaded"), `auth_error` (401/403), else `backend_error` — each with
the real message and a `retryable` hint. A **transient** `service_unavailable` is auto-retried up to
twice with backoff; a rate/usage cap or auth failure is surfaced (it won't clear in seconds) for the
host to offer recovery — wait, switch `--model`, or the `--backend claude` fallback with disclosure.
Classification only trusts a real HTTP status or a structured error EVENT, so stderr noise that
merely contains "unavailable"/"rate limit" can't trigger pointless retries.

**Malformed reviewer output is retried once** (issue #1): an LLM's invalid JSON, wrong-shape JSON,
or empty final message is stochastic the way an outage is transient — an immediate identical
re-run usually succeeds — so the runner retries it once (no backoff, same wall-clock budget) and,
if it persists, fails `invalid_response` with `retryable: true`. The size-bound variants (stdout
capture cap, final message over the 2 MB byte bound) are **not auto-retried** — the costliest
class to re-spend on blindly, and often a systematic cause (artifact echoed back, a degenerate
loop) — but they still carry `retryable: true`: as with `rate_limited`, the hint means "recovery
is plausible, offer it to the operator", and the failure message names the remedy (shrink the
artifact, tighten the instruction, lower effort, or re-run unchanged — most plausible near the
bound). `retryable` is a recoverability hint, not an auto-retry marker.
