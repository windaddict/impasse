# Proposal: Cursor as a first-class Impasse host

**Status: design proposal — NOT built.** Revised after an Impasse review
(`impasse-cursor-host-adapter-review-001`, `--backend claude`, 2026-08-20). This file records
how to make Impasse work under Cursor Agent (Desktop + CLI) without overclaiming independence.
Evidence for the limitations and workarounds comes from Cursor's public skill docs (fetched
2026-08-20), Impasse's existing host-detection code, and a live probe in this Cursor session.

**Scope:** (1) a Cursor host adapter in `SKILL.md` / install docs so an agent following the skill
can run the existing review path; (2) an independence workaround for Cursor's mixed-model host,
with **assertion provenance always disclosed**; (3) optional later backends (notably xAI Grok) so
a Cursor session has a rival reviewer that is not OpenAI or Anthropic. The verify → reconcile →
escalate protocol is unchanged.

**Non-goals:** rewriting Impasse as a Cursor-only plugin; treating Cursor's in-IDE Task /
subagent models as Impasse backends (different trust model — see "Anti-patterns"); inventing a
provider for Cursor from `CURSOR_AGENT` alone; shipping a Grok backend in the same change as the
skill adapter.

## Why this matters

Impasse is already an [Agent Skills](https://agentskills.io) package. Cursor implements that
standard and discovers skills from `.cursor/skills/`, `~/.cursor/skills/`, **and** (compat) the
Claude/Codex locations (`~/.claude/skills/`, `~/.codex/skills/`) — per Cursor's docs. A machine
that already has Impasse under Claude Code **may** load the skill in Cursor, but that path was
**not** dogfooded in the probe below (only the review scripts were). The skill text still names
only Claude Code and Codex as tested hosts, and the independence labeling for Cursor is
deliberately weak.

A live probe in this Cursor Agent shell (2026-08-20):

```text
CURSOR_AGENT=1          # present (host marker works in this Desktop session)
claude, codex on PATH   # reviewer CLIs available
grok, gemini            # not installed
```

```bash
python3 scripts/impasse_run.py mode --kind decision
# → host=cursor, mode=codex, tier=undetermined, notice present

IMPASSE_HOST=claude python3 scripts/impasse_run.py mode --kind decision
# → host=claude, mode=codex, tier=cross_provider, notice=null
#    BUT host_detection={method: override, confidence: asserted}
```

So the **review path already runs** under Cursor. What fails is the *honest cross-provider
claim* until the operator (or the skill) asserts which model is driving the session — and even
then, today's runner suppresses `independence_notice` on an asserted `cross_provider`, leaving
provenance only in `host_detection` (easy to miss if the host only prints the notice field).

## Limitations researched (Cursor)

| Limitation | Evidence | Impact on Impasse |
|---|---|---|
| **Host is not a single provider** | Cursor model picker exposes Anthropic, OpenAI, Google, xAI Grok, and Cursor's own Composer family in one IDE (`cursor-agent --list-models`). Docs + Impasse `host-detection.md`: `CURSOR_AGENT=1` → host `cursor`, confidence `none`, never a positive tier. | Without an override, every review is `undetermined` even when the reviewer is a different lab from the model in *this* chat. |
| **No skill-root env var** | Claude Code exports `CLAUDE_SKILL_DIR`. Cursor skill docs list discovery paths but no equivalent "you are running from this skill directory" variable. | Skill text must teach path resolution; a wrong `IMPASSE_ROOT` mainly breaks the *commands the skill prints* (scripts self-locate schemas via `__file__`). |
| **`CURSOR_AGENT` marker drifts** | Forum report: CLI once dropped `CURSOR_AGENT=1`; Cursor re-added it. Matrix in `docs/host-detection.md` already flags this. | Auto-detection can false-negative → `unknown`/`undetermined` (safe). Not a blocker; override still works. |
| **Agent shells also set `CI=1`** | Cursor `cursor-agent-exec` injects `CI=1` with `CURSOR_AGENT=1` (forum reports). | Impasse's own scripts do not key off `CI`, but **spawned reviewer CLIs** (`codex`, `claude`) inherit it and may change auth, interactivity, or output. Phase A dogfood must check. |
| **Long command lifetime is soft** | Forum: no user-facing "disable terminal timeout"; some agents support `block_until_ms` / backgrounding (reports of ~15+ min); Composer reportedly weaker on that knob. **Not measured in this Cursor session.** | Same *class* of risk as Claude Code's foreground kill. Treat timeout guidance as provisional until dogfood; prefer backgrounding + collect JSON over hoping the harness holds. |
| **Reviewer CLIs are still required** | Impasse backends today: `codex` and `claude` only (`get_backend` rejects anything else). | Cursor alone is not a reviewer. Operator must install at least one backend CLI. |
| **Grok is available inside Cursor but not as an Impasse backend** | Model list includes `cursor-grok-4.5-*` and `cursor-grok-4.6-*`. Separate xAI **Grok Build CLI** (`grok -p`) exists with headless JSON output; **does not read stdin** (prompt via `-p` / `--prompt-file`). Not installed on the probe machine. | Tempting to call a Cursor Task with a Grok model "independent review" — that bypasses consent, supervision, and schemas. A real Grok *backend* is a later, separate change. |

## Workarounds

### 1. Independence: assert the host model for this session (ship with the adapter)

This is the **primary** fix and needs no new backend — but it must not look like auto-detection.

Cursor cannot tell Impasse whether *this* chat is Claude, GPT, Gemini, Grok, or Composer.
The operator (or the skill, asking once) can. The existing override already works:

| Model family driving **this** Cursor chat | Set | Strongest rival among **shipped** backends |
|---|---|---|
| Claude / Fable / Sonnet / Opus | `IMPASSE_HOST=claude` | `--backend auto` → **codex** (`cross_provider`) |
| GPT / Codex / Sol / Luna | `IMPASSE_HOST=codex` | `--backend auto` → **claude** (`cross_provider`) |
| Gemini | `IMPASSE_HOST=gemini` | either shipped backend is `cross_provider` vs Google (today both OpenAI and Anthropic are "different") |
| Grok (xAI) inside Cursor | `IMPASSE_HOST=other` today → still `undetermined` | Phase B: add `grok` to `KNOWN_HOSTS` / `_HOST_PROVIDERS` |
| Composer / Auto / unknown | leave unset or `IMPASSE_HOST=cursor` | stay `undetermined`; still run a review, surface the notice |

**Skill rule (proposed):**

1. At the start of **every** Cursor-hosted Impasse *invocation* (not once per chat session), if
   `detect_host()` is `cursor` or `unknown`, establish which provider is driving **this** turn:
   **prefer asking the operator**. Reading the chat's model badge is allowed only when the badge
   unambiguously names a family in the mapping table — and the host must still tell the operator
   what it asserted. Never invent `IMPASSE_HOST` from `CURSOR_AGENT` alone, and never assert a
   provider just to upgrade the tier without an operator-visible basis.
2. If the operator changes the Cursor model mid-chat, **re-ask or clear** `IMPASSE_HOST` before
   the next review. Auto / silent fallback under a named selection → leave `undetermined`.
3. **Always surface assertion provenance.** Today's runner sets
   `host_detection={method: override, confidence: asserted}` but leaves `independence_notice`
   null on asserted `cross_provider`. The Cursor adapter **must** print `host_detection` (and,
   ideally, Phase A includes a small runner change: a soft notice when
   `confidence == "asserted"`, parallel to the existing heuristic soft notice). Success is not
   "notice=null"; success is "tier is honest *and* the operator can see it was asserted."

This is **operator-visible assertion**, not trust in Cursor's routing plane. Anti-pattern 3 still
forbids treating two Cursor-routed models as two labs.

**Phase B preview (optional same PR):** add `"grok"` to `KNOWN_HOSTS` with
`_HOST_PROVIDERS["grok"] = "xAI"`. Verified by simulation against current `independence_tier`
(2026-08-20): with that table entry, `independence_tier("grok", "OpenAI"|"Anthropic")` →
`cross_provider` because those backends are in `_KNOWN_PROVIDERS`; `"xAI"` need not be in
`_KNOWN_PROVIDERS` until a grok *backend* exists. Add an explicit unit test so the asymmetry is
documented, not only inferred.

### 2. Skill root: resolve without `CLAUDE_SKILL_DIR`

Proposed resolution order for Cursor (document in `SKILL.md`):

1. Absolute path of the skill directory Cursor attached when the skill was invoked (prefer
   whatever the host surfaces in available-skills / skill-path context — same pattern as Codex).
2. **Else the absolute path of the directory containing the `SKILL.md` the agent just read**
   (observable; preferred over guessing among install locations).
3. Else, only as a last resort and with a warning if more than one exists:
   `$HOME/.cursor/skills/impasse`, `$HOME/.agents/skills/impasse`, then
   `$HOME/.claude/skills/impasse` / `$HOME/.codex/skills/impasse`.

Do **not** prefer `~/.claude/skills/impasse` merely because the directory exists — that condition
("loaded via Claude compat") is not observable without a skill-root variable, and dual installs
are expected once a Cursor-native symlink is offered.

Scripts already self-locate bundled schemas relative to `__file__`, so a wrong `IMPASSE_ROOT`
mainly breaks printed commands — still fix the documented path.

**Install options:**

- **Compat path (documented, not yet proven primary):** Cursor docs say it loads
  `~/.claude/skills/` and `~/.codex/skills/`. Treat as a candidate until Phase A dogfood shows
  Impasse invocable by name/description from that path alone.
- **Cursor-native symlink (recommended until compat is proven):**
  `ln -s <repo> ~/.cursor/skills/impasse` (and optionally `~/.agents/skills/impasse`). Optional
  `scripts/install-cursor.sh` (symlink-only, refuse to clobber a real directory), mirroring
  `install-codex.sh`.

### 3. Long reviews: harness, not silent death

Document a Cursor-specific timeout section in `SKILL.md`, marked **provisional until dogfood**:

- Prefer `block_until_ms` ≥ `--wall` × 1000 + buffer **when the host tool exposes that knob**
  (do not claim every Cursor model supports it).
- Prefer running long reviews in the **background** and collecting JSON on exit — same *idea* as
  Claude Code's `run_in_background`. The ~550s figure is Claude Code's 10‑min foreground kill
  heuristic, **not** a measured Cursor limit; use `estimate` and observed harness behavior.
- If the harness kills the command with **no** Impasse `failure` JSON, treat it as host
  interruption, never approval — that is the silent-approval hazard.
- Prefer shortening the *work* (`estimate`, lower `--effort` on codex, smaller artifact) or
  switching to a host model that can hold the command open. If time must be bounded and the
  harness cannot be trusted, an Impasse `--wall` timeout (observable `failure` JSON) is **less
  bad** than an opaque harness kill — but it is still a truncated review, not a pass.

### 4. Operator Q&A and consent without `AskUserQuestion`

Claude Code's `AskUserQuestion` is named in `SKILL.md` for model/effort prompts and
escalations. Consent does **not** depend on it today: the runner blocks, the host shows the
notice + manifest, and the operator approves via `--approve-send <endpoint>` or
`impasse_consent.py grant` (prose in chat is enough).

**Cursor adapter must say all of:**

- **Consent:** on `consent_denied`, paste the runner's notice + manifest to the operator; obtain
  yes/no in plain chat (or the host's question tool); then `--approve-send` or a persistent
  grant for the **exact** endpoint in the manifest. Same block-by-default gate as every other
  host — only the UI channel changes.
- **Escalations / model knobs:** use the host's structured question tool if present; otherwise
  plain chat; record answers verbatim into reconciliation `escalation` (already allowed:
  "regardless of channel").

### 5. Rival reviewers: shipped today vs later

**Available now (no new Impasse backend):**

| Host session (asserted) | Preferred reviewer CLI | Why |
|---|---|---|
| Claude in Cursor | `codex` | Different lab (OpenAI) |
| GPT/Codex in Cursor | `claude` | Different lab (Anthropic) |
| Gemini in Cursor | `codex` or `claude` | Both cross vs Google |
| Composer / Auto | either, with `undetermined` notice | Honest; still useful as second pass |

**Candidate later backends (separate proposals / PRs):**

| Backend | Provider | CLI shape | Fit for Impasse |
|---|---|---|---|
| **Grok Build (`grok`)** | xAI | `grok -p` / `--prompt-file`, `--output-format json`, sandbox/deny tools, **no stdin** | Strong independence vs Claude *and* GPT hosts; needs prompt-file path, consent endpoint for xAI, read-only flags. Highest-value new backend for Cursor users on Claude or GPT. |
| **Gemini CLI** | Google | Host marker already exists (`GEMINI_CLI=1`); headless reviewer shape TBD | Useful when host is Claude/GPT and operator prefers Google. |
| Cursor Task / subagent models | mixed | In-process Cursor models (`cursor-grok-4.6-high`, etc.) | **Not** an Impasse backend. No consent gate, no supervisor, no schema enforcement at the runner; shared IDE routing ≠ two labs. Keep out of scope. |

**Grok model IDs worth tracking (Cursor pool, 2026-08-20 probe — names rotate):**
`cursor-grok-4.5-high`, `cursor-grok-4.5-high-fast`, `cursor-grok-4.6-{low,medium,high,xhigh}` (+ `-fast` variants). These are **host** models for asserting "this chat is Grok," not reviewer backends.

## Proposed implementation phases

### Phase A — Cursor host adapter (this proposal's shipping unit)

**Not "docs only."** The central deliverable changes how a positive independence tier is
*obtained and disclosed* under Cursor (operator assertion + mandatory provenance surfacing).
Docs/skill text are the bulk; a small runner change for an asserted soft notice is in scope if
we want the notice field itself to carry provenance (recommended).

1. **`SKILL.md` — "Running it (host adapter)"**
   - Add **Cursor** beside Claude Code and Codex; needs Python 3 + `codex` and/or `claude`.
   - Document `IMPASSE_ROOT` resolution (SKILL.md-just-read first; no silent `~/.claude` prefer).
   - Document host-model assertion **per invocation**, re-check on model switch, and **always
     surface `host_detection`** (and soft notice if implemented).
   - Document consent channel (manifest → chat approve → `--approve-send` / grant).
   - Document provisional timeout / backgrounding rules.
2. **Optional but recommended runner tweak:** soft `independence_notice` when
   `confidence == "asserted"` on a positive tier (mirror heuristic notice). Tests in
   `tests/test_helpers.py`.
3. **`README.md` Install** — Cursor section: Cursor-native symlink recommended until compat
   discovery is dogfooded; compat paths documented as candidates.
4. **`docs/environments.md` / `docs/host-detection.md`** — "using Impasse from Cursor"; update
   Verified column for `CURSOR_AGENT=1` from dogfood (agent / version / OS).
5. **Optional `scripts/install-cursor.sh`** — symlink-only, refuse real dirs.
6. **Dogfood (gates the "works in Cursor" claim):**
   - Skill invocable by name/description (compat path and/or Cursor-native path — record which).
   - Asserted host → rival backend → completed review + run record.
   - Provenance visible (`host_detection` and/or soft notice).
   - Consent flow via chat + `--approve-send` / grant.
   - Spot-check `codex`/`claude` under injected `CI=1` (no surprise interactive/auth failure).
   - One long-ish review with backgrounding / `block_until_ms` as available — record what worked.

**Tests:** required if the asserted soft notice (or installer) lands; otherwise the dogfood
checklist above is the acceptance test for pure skill-text changes. Do not waive coverage for
the assertion-disclosure path if code changes.

### Phase B — Host table: name Grok as an attributable host

Small code change in `impasse_lib.py`:

- Add `grok` to `KNOWN_HOSTS` with `_HOST_PROVIDERS["grok"] = "xAI"`.
- Tests: `independence_tier("grok", "OpenAI"|"Anthropic") == "cross_provider"` (behavior
  confirmed by simulation 2026-08-20 before the table edit); `IMPASSE_HOST=grok` → asserted;
  conflict-matrix cells updated.
- Docs: Cursor skill asks "Claude / GPT / Gemini / Grok / Other(Composer)" → `IMPASSE_HOST`.

Still **no** Grok backend — a Grok-driven host reviewing via Codex/Claude is already a real
cross-lab pair.

### Phase C — Optional `grok` reviewer backend (separate proposal)

Only after Phase A (and preferably B). Hard requirements before merge:

- Temp prompt file: mode `0600`, exclusive create, always deleted in `finally` (artifact on disk
  is a different posture than stdin backends — treat it as sensitive as a run record).
- Consent destination **pinned** the same way other backends pin (normalize endpoint; refuse
  when env would mis-key consent — follow `get_backend("claude")` routing refusals).
- `get_backend("grok")` → provider `xAI`; invocation via `--prompt-file` + `--output-format json`
  + strictest read-only / tool-deny / sandbox flags available.
- Auto-ladder policy: default stay on current `auto` unless Grok is the only cross-provider
  available (decide with evidence, not preference).
- Docs: `docs/backends/grok.md`; three gates + dogfood.

Defer until someone will maintain the CLI contract (xAI headless flags move).

## Anti-patterns (do not ship)

1. **Silent `cross_provider` from `CURSOR_AGENT=1` alone** — already forbidden; keep it that way.
2. **"Review with Cursor Task model X"** as a substitute for `impasse_run.py review` — skips
   consent, supervisor, schema shape-check, and run records.
3. **Using two Cursor-routed models** (e.g. Composer host + `cursor-grok-*` Task) and labeling the
   pair cross-provider — shared IDE routing is not two labs' CLIs. (Operator-asserted
   `IMPASSE_HOST` for the *host* side is different: it names the chat's provider so a *subprocess
   CLI* from another lab can be labeled honestly.)
4. **Treating a silent harness kill as a completed review** — no Impasse `failure` JSON means
   interruption, not approval. Prefer lengthening the harness or shortening the work; if you must
   cut time short, an Impasse `--wall` (observable failure) beats an opaque kill — and neither
   is a pass. Do not lower `--wall` merely to "fit" a short harness while still presenting the
   run as a normal review.

## Success criteria (Phase A)

- From Cursor Agent, an operator can invoke Impasse and get a completed review with a run record
  (dogfood records *which* install path worked).
- When they assert `IMPASSE_HOST` to the model driving the chat, `mode` reports `cross_provider`
  for the rival backend (Claude↔Codex), **and** assertion provenance is visible
  (`host_detection.method=override` / soft notice) — not merely `independence_notice is null`.
- When they do not assert, the undetermined notice is surfaced — never hidden.
- Consent works via chat + `--approve-send` / grant under Cursor.
- `SKILL.md` documents Cursor skill-root, consent, timeouts (as provisional), and Q&A without
  inventing unsupported APIs.
- No schema break; three gates green if any script/runner change lands.

## Open questions for the operator

1. **Phase A only first, or A+B together?** B is small and makes Grok-in-Cursor hosts attributable
   without a Grok backend — recommended with A if you already expect Grok chat sessions.
2. **Ship the asserted soft notice in Phase A?** Recommended so provenance is not only in a
   nested `host_detection` field. Slightly more than skill text.
3. **Default rival when host is Gemini or Grok (asserted):** keep `auto` = current ordering of
   `{codex, claude}`, or add `IMPASSE_PREFERRED_BACKEND`? Current `auto` is enough for Phase A.
4. **Schedule Phase C (Grok backend)?** Only if you want a third lab when the host is already
   Claude *or* GPT and you refuse to use the other. Otherwise A+B cover Cursor.

## Rollout

1. Land Phase A (and optionally B) on a feature branch; three gates if code changes.
2. Dogfood from Cursor with asserted host + Claude-or-Codex reviewer (checklist above).
3. Impasse-review the landing diff: Cursor host with asserted provider, cross-provider backend.
4. Push; bump `~/.claude` submodule if that install tracks main; symlink/update Cursor path if used.

## Impasse review of this proposal

- **Review id:** `impasse-cursor-host-adapter-review-001`
- **Backend:** `claude` / `claude-opus-5` (host was Cursor → independence `undetermined` for that
  run; expected without `IMPASSE_HOST`)
- **Assessment:** `needs_attention` (12 findings)
- **Reconciliation:** see run record after `save-reconciliation`; summary: 11 resolved into this
  revision, 1 rejected (F007's "trusts Cursor routing" half — contradicted by the assertion rule)

## References

- Cursor Agent Skills: <https://cursor.com/docs/skills> (discovery paths, Claude/Codex compat,
  frontmatter, scripts/).
- Impasse host detection: [`docs/host-detection.md`](../host-detection.md),
  [`docs/environments.md`](../environments.md).
- Prior multi-host work: [`docs/proposals/multi-host-autodetection.md`](multi-host-autodetection.md).
- xAI Grok headless: <https://docs.x.ai/build/cli/headless-scripting> (no stdin; `--prompt-file` /
  `-p`; `--output-format json`).
- Live probe (this session): `CURSOR_AGENT=1`; `mode` undetermined → `cross_provider` under
  `IMPASSE_HOST=claude` with `host_detection.confidence=asserted` and `notice=null`;
  `cursor-agent --list-models` includes Grok 4.5/4.6 family; `grok` CLI absent.
