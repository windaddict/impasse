# Changelog

All notable changes to Impasse are documented here. This project adheres to
[Semantic Versioning](https://semver.org/) for its schemas and skill.

**Versioning, precisely.** Three contracts are versioned INDEPENDENTLY here, and conflating them
would be a mistake: the **skill** (the `VERSION` file — what this changelog numbers), the **stored
schemas** (`schemas/*.v1.json`), and the **on-disk formats** (`CONSENT_VERSION`, `NOTICE_VERSION`).
A skill release does not imply a schema bump, and vice versa. While the skill is **pre-release**, the
`0.y.z` line is deliberate: it says the CLI and protocol surfaces may still change without a major
bump. The `VERSION` file is the single source of truth; `SKILL.md`'s header line and frontmatter are
checked against it by the test suite, so they cannot drift.

## [Unreleased]

### Reconciliation write/read integrity — issues #16, #17, #18

*(Implemented from a plan that was independently reviewed before any code was written — 8 findings,
all accepted — and then reviewed again as a finished diff — 7 findings, all accepted. The
plan-review caught a `--partial` flag that would have re-created the bug behind an opt-in; the
implementation-review caught that the guard had been put in the command rather than the writer, so
the bug was still reachable. Both are described below.)*

#### Fixes from the implementation review
- **The guard moved from the command to the WRITER.** The first implementation validated inside
  `save-reconciliation` and reduced `save_run_doc` to a docstring saying "don't use me for
  reconciliations". Advice is not an invariant: one call to the public writer still created a
  complete orphan directory, outside the per-run lock — i.e. **issues #17 and #18 were still
  reproducible**. `save_run_doc` now raises on a `reconciliation-result` write unless it carries a
  private module sentinel that only `save_reconciliation_doc` holds (a sentinel object, not a
  boolean, so it cannot arrive from JSON, a flag, or reviewer output).
- **The report no longer crashes on the records it exists to describe.** Every reader did
  `rec.get("items") or []` and then `it.get(...)`, so a malformed collection raised `AttributeError`
  **before** the unverifiable banner could print — `show` and `list` died on exactly the corrupt
  records they were being taught to report honestly. One total accessor,
  `lib.reconciliation_items()`, now serves them all. The same class of bug in `load_run` (a file
  that parses but isn't an object) was found and fixed alongside it.
- **A sibling file is not a usable reviewer-response.** A response with no findings list, or findings
  without string ids, could certify a pair. Scoped deliberately to the pairing invariant: an
  `assessment` check was tried and withdrawn, because whether a response carries one has no bearing
  on whether the two halves belong together, and whole-document conformance is CI's job.
- **A record whose outcome says it never finished no longer signs off as complete.** The closing line
  branched on unverifiable and pending deadlocks but never on the outcome, so a `failed` or
  `incomplete` record still printed "Nothing needed you — the models settled all N" — the same shape
  as the original #16 bug.
- **Two totality holes**, both reachable from a hand-edited file: `len()` over a truthy non-sized
  `items` raised `TypeError` out of the branch whose job is a controlled refusal, and `repr()` of a
  deeply nested value raised `RecursionError` from inside a diagnostic string.

#### Fixes from the same-provider depth review
A third review — a stronger model, but **same-provider, so breadth rather than independence** — went
after concept and residual honesty rather than correctness. It found the following, and corrected the
record on a limitation this changelog had previously overstated.

- **The lock discipline was one-sided, so #17 was NOT fully closed.** `save_reconciliation_doc` took
  a per-run lock; `forget_run` — reachable from `forget`, `prune` and the runner's cleanup — did not.
  A delete landing between the writer's pair validation and its write let `makedirs(exist_ok=True)`
  recreate the directory holding a reconciliation **alone**: issue #17's orphan, produced by two
  commands each behaving exactly as documented. Reproduced, then closed three ways — `forget_run`
  takes the lock, the lock is **reentrant within a process** (taking it twice on two fds otherwise
  self-deadlocks, and a deadlock in a records tool is worse than the race), and the writer re-checks
  the sibling immediately **before and after** the write, undoing its own file if the pair broke
  mid-write.
- **Correction: this was testable all along.** The previous entry said a concurrency test "needs
  multi-process orchestration this stdlib suite has no harness for." That was wrong, and it
  functioned as a justification for not testing the property. A single-process test that patches the
  write to interleave a `forget_run` exercises the race deterministically — it now exists, and it
  fails against the pre-fix code.
- **A corrupt reconciliation reported itself as never recorded.** `load_run` mapped both "no file"
  and "file present but unreadable" to `None`, but those are opposite facts: absent means the step
  never happened, unreadable means it did and the evidence is damaged. So `show` printed
  "reconciliation not yet recorded" for a **recorded, converged** record — a false statement of the
  exact class this change exists to eliminate — while `list` called the same run an orphan and the
  lifetime recap dropped it without disclosure, contradicting its own stated rule.
- **The ⚠️ signal was being spent on healthy runs.** A not-yet-reconciled run printed
  "partial: only 0 of N dispositioned" directly above the correct "not yet recorded" footer.
- **The validator's enums are a second copy of the schema's**, gating every write and quarantining
  every read, with nothing checking them against it. A test now asserts they match, so adding an
  outcome or state to the schema fails loudly instead of silently refusing every new-format record.

- **Completing a partial reconciliation no longer requires `--force`.** The finished record
  conflicted with the operator's own interim one, so the sanctioned `--partial` workflow ended in the
  flag that exists to mark a dangerous replace — and a guard everyone types by default guards
  nothing. A save now *supersedes* without a flag when both hold: the existing record does not claim
  to be finished (`outcome` isn't `converged`), and the new one dispositions every finding the old
  one did, so it can only move forward. It reports `superseded` (distinct from both `saved` and
  `replaced`) and still writes a backup. `--force` is now reserved for the two real clobbers —
  replacing a finished record, or dropping dispositions — and the refusal says which, naming the
  finding ids at risk.
  Two corrections came out of reviewing that relaxation, both closing holes it opened:
  **identity by id is not identity of work** — a bare `{finding_id, state}` item is an id-superset of
  one carrying an operator's ruling and a paragraph of verification notes, so the predicate also
  requires that no shared item LOSES human-written content (`escalation`, `host_position`,
  `resolution`, `verification`); gaining content and answering a deadlock remain ordinary forward
  steps. And **an unreadable existing record is never superseded** — a corrupt collection degrades to
  an empty item list, which would make the superset test hold vacuously, so the more damaged the old
  record the easier it would have been to overwrite unflagged.
  Stated rather than hidden: the interim test reads the existing record's own `outcome`, which is
  self-reported. The content and coverage checks are what actually protect the work, and they do not
  rest on self-reporting.

**Known limitation, still not fixed:** no test injects a failure *between* the backup and the
primary replacement, so crash-safety in that window remains verified by inspection. Unlike the race
above, that one does need process-level fault injection.

#### The original three

One defect seen from three sides: a reconciliation could become separated from the reviewer-response
it claims to reconcile, and nothing anywhere noticed. All three were hit in one real session, from an
ordinary mistake — inventing a `review_id` instead of copying the one the runner assigned.

- **`save-reconciliation` validates before writing, instead of accepting almost anything.**
  Previously it checked only "is a dict with a truthy `review_id`", then wrote via `save_run_doc`,
  which creates the run directory if it doesn't exist. An unknown `review_id` therefore silently
  created an orphan directory holding a reconciliation with no findings behind it; a fabricated
  `finding_id` was accepted and later rendered as a real resolved finding; duplicate `finding_id`s
  were silently collapsed; and partial coverage went unremarked. It now refuses (non-zero exit,
  nothing written) on all four, naming the specific problem. New `lib.reconciliation_problems()` is
  the shared check — a hand-written structural validator, not a `jsonschema` dependency (stdlib-only
  in `scripts/` is a hard invariant here; full schema conformance stays in
  `tests/validate_schemas.py`, where a validation engine belongs). New `lib.save_reconciliation_doc()`
  is now the only sanctioned way to write a reconciliation — the guard lives at this storage boundary,
  not only in the CLI, so any other caller gets it too.
- **Partial coverage is a flag, not silent.** Saving before every raised finding is dispositioned is
  refused unless you pass `--partial` — a deliberately partial reconciliation mid-protocol is
  legitimate. It can never pair with `outcome: converged`, though: that combination is the original
  bug (9-of-13 findings, stored as converged) behind a flag instead of behind a typo. The success line
  always reports `N of M findings dispositioned`, so a partial record identifies itself.
- **Re-saving over an existing reconciliation is refused without `--force`.** Previously it was
  replaced with no prompt, no backup, and an identical `saved:` line either way. With `--force`, the
  previous reconciliation is kept as `reconciliation-result.<n>.json` in the same run directory
  (`0600`, permanent — removed only when the whole run is forgotten). Findings can be re-derived from
  the reviewer-response; a human's verification notes and dispositions cannot, which is why this is a
  kept copy rather than a discarded one.
- **`show` no longer invents a denominator.** It used to compute the "findings raised" tally as
  `len(findings) if findings else len(items)` — when the reviewer-response was missing, that count
  silently became the host's own dispositions, so an under-covered record read as complete. It now
  renders `?` and a prominent banner instead, and — since the denominator was only half of it — also
  suppresses the stored `outcome: converged` line (shown as `⚠️ unverifiable (stored: converged)`) and
  the "nothing needed you" footer, so a broken record can no longer read as a passed gate.
- **The same validator gates every other surface that reads a reconciliation.** `list` marks an
  orphan `⚠️ orphan (unverifiable)`; `open` won't surface a deadlock from a record it can't verify
  (you'd be asked to rule on a question that might not even name the finding it claims to); `prune`
  discloses how many of the records it inspected were invalid; `escalations` now checks pairing even
  when there is nothing currently deadlocked to render (previously it skipped that check whenever
  there were zero open escalations, which let a fabricated but non-deadlocked reconciliation pass
  silently); and the lifetime recap on `show` excludes an unverifiable run's items from its totals
  rather than counting them, disclosing how many records were excluded.
- **Risk, stated plainly: this moves numbers you've already seen.** Existing on-disk records that
  don't pass the new validator — most commonly a mismatched `review_id` — will now render as
  unverifiable instead of contributing a confident tally, and the lifetime recap's totals will drop
  by however many records that affects. That is the fix working as intended, not a regression, but it
  is a visible change to numbers you've already looked at.

### README accuracy — the defects a Cursor-hosted review found
Four inaccuracies, all raised by an independent review of the README against the code and verified
against the live files before fixing.

- **Requirements contradicted Install about whether Cursor is a host.** Requirements named only
  Claude Code and Codex while Install documented Cursor and shipped `install-cursor.sh`. Requirements
  now lists all three and marks Claude Code and Codex as the *tested* ones, which is the distinction
  that was actually being made.
- **The independence-ladder diagram omitted `undetermined`.** It showed three rungs where
  `INDEPENDENCE_TIERS` has four, so the diagram described an ordering the code does not implement.
  `undetermined` is now shown in its real position — **second, above same-provider** — with a note on
  why: it means *unknown*, and an unknown pairing may well be cross-provider, whereas same-provider
  is a known correlation. It is also the rung a Cursor session occupies until the operator asserts a
  host.
- **`save-reconciliation` and `escalations` were undocumented.** The Audit trail section listed six
  reporting subcommands and omitted the two that carry the protocol: without `save-reconciliation` a
  run stores only the reviewer's raw findings and never what you decided, and `escalations` is what
  refuses to let you be asked to rule on a question stripped of its context.
- **The install comments asserted a destination the installers only default to.** Both detect the
  skills root, may choose `~/.agents/skills`, honor `CODEX_HOME`/`--root`, and refuse when the choice
  is ambiguous.

### Host-error removal, from a second real Cursor run
Two defects and one guidance gap, all surfaced by watching an actual Cursor-hosted run rather than
by inspection.

- **The runner now stamps `artifact.revision` itself.** `SKILL.md` asked the *host* to set it from
  the consent digest; nothing enforced that, so a host that forgot left the **reviewer's invented
  value** in the permanent record — the observed run stored
  `{"algorithm": "other", "value": "caller-provided-bundle-2026-08-21"}` and only noticed afterwards,
  patching the stored file by hand. That field exists to stop findings being reconciled against
  changed content, so a fabricated value defeats it silently. The runner already computed the digest
  for the consent manifest; it now writes it into the stored response, overwriting whatever the
  reviewer claimed, and returns it as `artifact_revision` on every result — success and failure
  alike, from one computed value rather than two sources — so a host copies it rather than deriving
  it. `kind` is stamped the same way and for the same reason: the operator sets it explicitly and the
  reviewer only echoes it, so a disagreement means the reviewer is wrong.
  **Bounded precisely:** the runner corrects fields the reviewer *cannot know*; it does **not** invent
  ones the reviewer never sent. A response missing `artifact`, or carrying a non-object there, is left
  exactly as received — `artifact` is schema-required while the runner's shape-check does not demand
  it, so filling it in would turn a response that must fail validation into one that passes, silently,
  inside data whose whole premise is that it is untrusted.
- **`lib.revision_from_digest()`** turns a manifest's `"sha256:<hex>"` into the schema's
  `{algorithm, value}`, validating length **per algorithm** (sha256 exactly 64 hex; git a full SHA-1
  or SHA-256 object id) — a shared range would accept `sha256:aaaaaaa`, an abbreviation, which is the
  one thing an "immutable identity" must not be. The observed run guessed at three different key names and then gave up and
  recomputed the hash from a temp file; this is the one supported way across, and it returns `None`
  for junk rather than minting an identity for reviewed content.
- **"An analysis you could perform yourself is work too", and the ORDER to do it in.** The
  artifact-selection guidance covered *"do X and review it"* but not *"work out whether X is true"* —
  which has no separate deliverable, so it reads as "the review IS the task". In **one observed local
  run** (a Cursor session, reported by the operator; no run record is kept in this repo) the host
  bundled both sides correctly — the previous fix working — and then let the reviewer do all the
  thinking, accepting 4 of 4 findings with nothing refuted. That is the anecdote that prompted the
  change, not evidence for a general rule.
  The guidance now prescribes an order: **form your view, send the EVIDENCE not your conclusions,
  compare afterwards.** The obvious-looking alternative — pasting your findings into the instruction
  and asking the reviewer to challenge them — sounds more rigorous and is strictly worse: it
  discloses your hypotheses before the reviewer forms its own, so what returns is a critique of your
  framing rather than an independent look. That is anchoring, and it forfeits the property Impasse
  exists to provide. An adversarial pass on your reasoning is legitimate as a **second** review after
  the blind one, reported as anchored.
  Deliberately **not** shipped: a "no disagreement means only one analysis happened" heuristic. Two
  genuine analyses can simply agree, and a rule treating agreement as suspicious would push a host to
  manufacture disagreement. What unanimity means is that a run produced no signal to adjudicate —
  worth saying plainly, not dressing up as corroboration.

## [0.5.0] — 2026-08-21

First numbered release. Everything below was previously unreleased work on `main`; the entries are
unchanged, and the version exists so that an operator — or an agent — can tell which Impasse is
running without being handed a command.

### Skill versioning, surfaced where it is already read
The failure this fixes was observed, not hypothetical: two install paths (`~/.claude/skills/impasse`
and a Cursor-native symlink) served **different code**, and nothing said so. A review ran against a
stale copy and looked entirely normal.

- **`VERSION` at the skill root is the single source of truth**, surfaced in the places a reader
  reaches for free: the `SKILL.md` header (every host loads it into context on invoke, so the agent
  simply knows it), the frontmatter `metadata.version`, and machine output.
- **`SKILL.md` now instructs the host to state its version when a review begins** — unprompted. That
  removes the "run this obscure command" step. **Stated exactly:** this surfaces *which copy
  answered*; it does not detect a stale install by itself. Nothing enumerates or compares the
  discoverable install paths, so a stale copy states its own version quite happily — it takes a
  reader who knows what to expect to catch it. The smaller claim is the true one.
- **`impasse_version` on every machine surface**: `mode` (which a host runs *first*), `estimate`,
  every review result including failures, every timing-store row, and a `run-meta.json` stamped
  beside each run record. Records are kept for months; "which code produced this" is a question a
  stored record should answer about itself.
- **A `+<commit>` suffix when running from a git checkout** (`0.5.0+48f2b1e-dirty`), because the
  common dev setup symlinks a working clone — where a bare release number could be any of a hundred
  commits. The suffix decorates the runtime value only and never the documented one, so docs stay
  checkout-independent. Two limits, both deliberate: `git -C` searches **parent** directories, so a
  skill copied into an unrelated checkout (a dotfiles repo — `~/.claude` is one) would otherwise
  report that repo's commit as its own; the provenance is therefore refused unless the repository
  top level *is* the skill root **and** `VERSION` is tracked in it. And `-dirty` is a boolean —
  it says tracked files differ, never which ones — so it narrows the candidates rather than
  identifying exact code.
- **The redundancy is gated, not trusted.** A version copied into several files is a new way to be
  wrong, so the suite asserts the `VERSION` file, the header line and the frontmatter agree —
  verified to fail on exactly the release mistake it exists to catch (bump one, forget the others).
  Degradation is pinned too: a missing, non-UTF-8, or otherwise malformed `VERSION` yields
  `"unknown"` rather than a guess or a traceback — which matters because `version()` is called from
  the failure path, where raising would replace a real diagnosis with a stack trace. **Scope of the
  gate:** it runs in the tree the tests run in — the working tree locally, and the committed tree in
  CI. A copied or packaged install is not gated, so a hand-edited copy in the field can still
  disagree with itself.

### Cursor host adapter, and `grok` as an attributable host (proposal Phases A + B)
Cursor implements the Agent Skills standard, and a probe in a Cursor shell confirmed the scripts run
there (`impasse_run.py mode` returned `host=cursor`). **No full review has been run from Cursor**, so
"the review path works there" remains inference from that probe plus Cursor's published docs, not an
observation. What is certain is the independence problem: Cursor is not one provider. Its model picker spans Anthropic, OpenAI, Google, xAI and Cursor's own Composer family,
and no environment marker reveals which is driving a session, so every Cursor review reported
`undetermined`. `CURSOR_AGENT=1` identifies the IDE, not the lab.

- **Operator assertion, always disclosed.** The route to a positive tier under Cursor is the operator
  naming the model driving *this* session (`IMPASSE_HOST`). That already worked; what was missing was
  disclosure. A `cross_provider` tier resting on `confidence: asserted` now carries a soft
  `independence_notice` — parallel to the existing heuristic one — saying the label was taken on the
  operator's word, was never verified, and **goes stale silently if they switch models mid-session**.
  Success for an asserted tier is explicitly *not* "notice is null"; it is "the tier is honest AND the
  operator can see it was asserted." **This is a behavior change:** three existing tests asserted
  `independence_notice is None` for an asserted cross-provider tier and were updated.
- **Auto is the hazard, and the docs now lead with it.** Cursor's default picks a model **per
  request**, so no assertion can be truthful — and because Auto's pool contains *both* reviewer
  providers (`gpt-5.3-codex-*`, `claude-opus-5-*`), asserting one anyway risks labeling a
  same-provider reviewer as independent. SKILL.md, README and environments now open the Cursor
  guidance with "turn off Auto and pick a named model", and the mapping table uses real model-ID
  prefixes.
- **`composer` is nameable as a host** (Anysphere). Cursor's own model is a different organization
  from OpenAI and Anthropic, so a Codex or Claude reviewer is genuinely cross-provider against it —
  previously it collapsed to `undetermined`. Recorded caveat: Composer's base-model provenance is
  **not fully public**, so that claim is sound on organizational separation and less firmly
  established on training correlation than claude-vs-codex. `composer` (a model) and `cursor` (the
  Auto router) are deliberately separate hosts, and a test pins that Auto never inherits Composer's
  attributability.
- **`grok` is nameable as a host** (`_HOST_PROVIDERS["grok"] = "xAI"`). It has no marker and is never
  auto-detected — assertion is the only way in — but naming it lets a Grok-driven session label a
  Codex or Claude reviewer as genuinely cross-provider instead of settling for `undetermined`. xAI is
  deliberately a known *host* provider and not a known *backend* provider: no Grok backend ships, and
  the asymmetry is now pinned by a test so it reads as a decision rather than an oversight.
- **`scripts/install-cursor.sh`** — symlink-only, refuses to clobber a real directory, idempotent,
  same safety contract as `install-codex.sh` (re-tested rather than assumed, since they can drift).
  It also tells the operator at install time that they must assert the host model.
- **Docs**: a "Cursor host adapter" section in `SKILL.md` (skill-root resolution without a
  `CLAUDE_SKILL_DIR` equivalent, the per-invocation assertion rule, consent without
  `AskUserQuestion`, provisional timeout guidance), plus `README` install, `environments`,
  `host-detection` and a glossary entry for **asserted host**.

- **Choosing the artifact** — new SKILL.md guidance, prompted by a real misfire: asked to "review the
  README against the code and make a plan", a Cursor-hosted run sent the README alone. Two rules now
  stated explicitly: if the operator asked you to *do* something, do it first and review **your own
  output** (Impasse is not a way to hand the operator's task to another model); and a **relational**
  claim ("docs match code") needs **both sides** in the artifact, or the reviewer cannot check the
  correspondence and its confident-looking findings are unfounded.

**Partially dogfooded.** Cursor's Claude-compat discovery was **observed working once** — on
2026-08-21 Impasse loaded and ran in Cursor Desktop (macOS) from `~/.claude/skills/impasse` with no
Cursor-native install present. One observation on one build; it makes that path likely rather than
guaranteed. A **full review has still not been
completed from Cursor** — this was built
from Cursor's published skill docs plus a probe of the scripts in a Cursor shell. Every
Cursor-specific claim is marked provisional in the docs, `SKILL.md` still lists only Claude Code and
Codex as *tested* hosts, and the proposal's Phase A dogfood checklist remains open. A `grok` reviewer
backend (Phase C) is deferred by design.


### Second-round review of the issue-#11 fixes (review-of-the-fixes)
The commit that applied the first review's 11 findings was itself sent back for an independent
cross-provider review. It raised 7 findings; all 7 were verified against the code and fixed. The
pattern worth recording: **three of the seven were the same failure mode — a fix applied to the one
path the original finding named, while sibling paths on the same hazard were left untouched.**

- **`RecursionError` on untrusted reviewer stdout reached three more parsers.** The first round
  hardened `_claude_envelope` and `_codex_stream_meta` and stopped there; `_unwrap_error`, the codex
  JSONL error scan in `_extract_backend_error`, and `_parse_reviewer_json` still caught only
  `json.JSONDecodeError`, which `RecursionError` does not subclass. All three now classify it.
  `_parse_reviewer_json` normalizes it to a `JSONDecodeError` so every caller — not just today's
  one, which already caught it — treats it as `invalid_response`, which is never a false pass.
- **The timing store's no-artifact-content guarantee is now per FIELD, and stated exactly.** The
  value sanitizer bounded types and lengths but was shape-only, so a dict handed to a scalar field
  would have stored its KEYS verbatim — artifact text arriving as `{"<text>": 1}`. Values are now
  typed by destination field (`phases` takes a bounded number map; every other field takes only a
  finite bounded number, bool, ≤200-char string, or None). The docs no longer make the absolute
  claim: `model_resolved` and `backend_version` are read from the reviewer CLI's own output, so a
  misbehaving backend can place up to 200 characters in those two — bounded and attributable, not
  impossible.
- **`--wall` now covers the version probe.** The first round moved deadline creation ahead of the
  probe, but the probe still used a fixed 20s timeout and never received the budget, so a small wall
  could be overrun before the reviewer was spawned. `backend_version` now takes the remaining budget
  and is bounded by the smaller of it and 20s, skipping entirely when nothing is left. The `--wall`
  help discloses that bounded process teardown may still add a few seconds past the cap.
- **`performance` no longer pools incompatible histories.** The effort/speed match landed in
  `recommend_wall` but the report bypassed it by passing pre-grouped `rows=`, so the number the
  operator actually SEES still mixed low- and high-effort runs. The report groups on the same four
  keys the library fits on, and labels each group with the settings it was measured at.
- **Non-finite metric values no longer crash the report.** "Filtered to finite numbers" was
  implemented as an `isinstance` check; NaN and Infinity are floats and passed it, raising
  `ValueError`/`OverflowError` at the `int()` that formats them. `_nums` and `_percentile` now test
  `math.isfinite` and exclude bools.
- **`record_metrics` keeps its never-raises contract.** A huge int overflowed `math.isfinite`, and
  `OverflowError` is an `ArithmeticError` — not in the caught tuple — so it escaped from the one
  function documented TOTAL, on the failure paths whose diagnosis it exists to serve. Integers are
  bounded before conversion and the handler now catches `ArithmeticError`.
- **Two regression checks did not pin their fixes.** The round-1 floor test asserted
  `recommended_wall_s >= 250` on a case where the unfloored path already returned ~375, so it could
  not tell fixed from reverted. It is replaced with a case where the floor actually binds (long runs
  on large artifacts, queried for a small one: ~440s unfloored vs a 800s observed p90). Every new
  check in this round was verified to FAIL against the code with its fix removed.


### Timeout diagnosability, wall recommendations, and duration telemetry (issue #11)
A review that blew its `--wall` reported only `backend wall_timeout after 605s` — no phase, no
evidence the provider had ever responded, no resolved model, and no next step but a guess. Reported
against the Claude backend, where a ~10.8K-token code review timed out twice at 605s and a
~5.7K-token one completed in 594s against a 600s wall. The safety behavior was already correct (a
timeout was never reported as a pass); what was missing was predictability and diagnosis.

- **A timeout now says where the time went.** Results carry a `telemetry` block: the phase timeline
  (consent → spawn → first byte → exit → validated), `received_any_bytes`, time to first byte, bytes
  received, attempt/retry counts, and the resolved model. No bytes before the cap points at startup,
  auth or a provider queue rather than at the model reasoning — a distinction that previously took a
  re-run to establish. The supervisor records `first_byte_s`/`bytes_received` on every path,
  timeouts included.
- **A payload-aware `--wall` recommendation, before the send.** New `impasse_run.py estimate`
  (purely local — it sends nothing and needs no consent) and a `wall_advice` block on every result,
  also printed to stderr when the requested wall looks too short. `basis` states the provenance
  honestly: **heuristic** is a shipped estimate padded for margin, *not* a measurement of your
  account; **empirical** means fitted from ≥5 of this machine's own completed runs for that
  backend+model. An already-exceeded cap raises the floor rather than being averaged in.
- **A local timing store.** `config_dir()/metrics.jsonl` (`0600`, newest 1000 rows) records duration,
  payload size, outcome, time-to-first-byte and retry counts for **every** run that reached the
  backend, failures included — a timeout is the most informative sample there is. It holds **no
  artifact content**, and that is structural rather than a promise: writes are filtered to a field
  allowlist. The one content-derived field, the artifact digest, is withheld under `--no-record`/
  `--raw`. New `impasse_report.py performance` reports it (timeouts counted separately from
  completions); `performance --forget` deletes it, `IMPASSE_NO_METRICS=1` disables it.
- **Concrete recovery instead of prose.** A timeout returns ranked options with exact commands, each
  saying what it changes — the time budget only, the model, reasoning depth, scope, or the
  independence tier — plus `reusable_result: false`, since a timeout leaves nothing to resume.
- **The resolved model, not just the requested alias.** The claude backend now runs
  `--output-format json`, whose envelope reports `modelUsage`, `ttft_ms` and `session_id`; Impasse
  reads the review from the envelope's `result` and reports `model_resolved` + `model_source`.
  Non-envelope stdout still parses exactly as before, claiming no resolved model. Codex's event
  stream names no model (codex-cli 0.148), so codex runs report `requested`/`backend_default` and
  never overstate. Reviewer CLI versions are recorded so a comparison survives a CLI upgrade.
- **Tests:** silent-to-the-wall, bytes-then-stall, partial-JSON-then-stall, a live descendant at
  teardown, recovery-option shape, seed-vs-empirical recommendation, the timeout floor, the metrics
  allowlist (a planted artifact field is dropped), the `IMPASSE_NO_METRICS` opt-out, envelope model
  resolution and its fail-soft fallback, and both new CLI surfaces.
- **Dogfooded, and it paid.** A cross-provider Impasse review of this change (codex, `--effort high`,
  Fast mode; 24.9K-token diff, completed in 239s against a 1860s recommended wall) raised **11
  findings, all verified and fixed here** — among them a genuine failure-as-success path (a claude
  envelope marked `is_error` was only checked when the exit status was non-zero), an overclaiming
  timeout message (the byte signal was presented as evidence about model progress, which this run's
  own telemetry disproves — codex's first byte arrived at 0.053s), a metrics allowlist that bounded
  keys but not value types or lengths, empirical recommendations pooled across mismatched
  effort/speed, a `RecursionError` escape on deeply nested untrusted backend JSON, and the version
  probe sitting outside the wall budget. Run record: `issue-11-adversarial-review`.
- **Deferred:** the issue's opt-in *supervised chunking* for oversized artifacts changes the protocol
  rather than the runner and is outside its own acceptance criteria — designed, not built, in
  `docs/proposals/supervised-chunking.md`.
- **Unrelated fix:** the `resolve_codex_command` ChatGPT.app test asserted a path suffix that a
  higher-priority Homebrew `codex` install fails and a system-wide `ChatGPT.app` passed for the wrong
  reason; it now asserts the exact path and skips where an earlier candidate really exists.

### Multi-host support — Impasse now runs turnkey under Claude Code *and* OpenAI Codex
Both hosts implement the open [Agent Skills standard](https://agentskills.io); one installation serves
either, because the code is host-relative at runtime (the host is detected per run, never persisted)
and the shared per-user config dir holds no host-specific state — consent is keyed by *endpoint* (a
Claude host's OpenAI grant and a Codex host's Anthropic grant coexist), model/effort defaults are
keyed *per backend*, and run records are keyed by `review_id`.

- **Host-aware default backend.** `review --backend` now defaults to **`auto`**, which selects the
  most host-independent *available* backend via `review_mode()`: to a Claude host that's `codex`, to a
  **Codex host** it's `claude` (the ladder inverts). So a bare review on a Codex host gets a genuine
  cross-provider reviewer instead of a silent same-provider one; when the cross-provider backend is
  unavailable it degrades honestly to `same_provider` (never a false `cross_provider`), and with no
  backend it fails closed. Explicit `--backend codex|claude` still forces.
- **Reviewer hermeticity.** The reviewer subprocess now runs with its CWD set to the run's scratch
  dir, not the operator's project — so `claude -p` can't pick up the *reviewed* project's
  `CLAUDE.md`/hooks (an artifact-controlled injection / independence leak), newly load-bearing now
  that `claude` is the cross-provider reviewer for a Codex host. (Residual, documented: user-global
  `~/.claude` config — see `docs/backends/claude.md`.)
- **Backend discovery.** The Codex desktop app rebranded its bundle to `ChatGPT.app`;
  `resolve_codex_command()` gained that path (legacy `Codex.app` kept). Without it, Impasse couldn't
  find the backend after the app updated.
- **Turnkey install + docs.** New `scripts/install-codex.sh` — a **symlink-only** installer (safe by
  construction: it never deletes real data — it replaces only a verified symlink and refuses a
  physical destination), which detects the Codex skills root. `SKILL.md` generalized from a
  Claude-Code-only adapter to a host-neutral one (host-relative backend guidance, per-host consent
  endpoints, the Codex sandbox-escalation prompt distinguished from Impasse's endpoint consent).
- **Detection provenance + a closed composition fail-open.** `host_detection()` returns
  `{method, confidence}`; a positive `cross_provider` tier resting on a *heuristic* detection carries a
  soft notice. A holistic review of the assembled feature caught a fail-open no per-change review
  could: a presence-style Claude surface flag (any non-falsy value) had yielded *strong* confidence,
  so a stray one on a sandbox-bypassed Codex host produced a **silent** false `cross_provider`. Those
  flags now yield *heuristic* (notice-bearing); only strict `CLAUDECODE=1` / the `CLAUDE_SURFACE`
  allowlist stay *strong*.

#### Hardening (a full-source cross-provider review of the assembled feature)
Reviewing the whole thing surfaced latent issues, several pre-existing, now fixed:
- **Consent boundary:** `IMPASSE_CODEX_RESPECT_CONFIG` (which honors `~/.codex/config.toml`, able to
  reroute data) now **refuses** unless `OPENAI_BASE_URL` is pinned, so consent is never keyed to a
  destination the config could silently override. An explicitly-empty base URL is treated as the
  default (preflight and run now agree).
- **Audit records:** each run reserves a **unique** record directory (atomic `mkdir`, `-2/-3…` on
  collision) — a reused or untrusted `review_id`, or two hosts sharing one config dir, can no longer
  silently overwrite another run's record.
- **Settings writes** run under an interprocess lock, so concurrent `set-model`/`set-effort` from two
  hosts can't lose an update. `set-effort` is now codex-only (Claude has no effort knob — was dead
  config).
- **Supervisor:** the process-group id is captured before the leader is reaped, so descendant teardown
  works on a clean exit (previously the post-reap `getpgid` failed silently, leaking strays).
- **Self-review gate:** `detect_environment()` now matches presence-style Claude markers affirmatively
  (a stray `CLAUDE_COWORK=0` can't manufacture a sandbox surface that would permit self-review).
- **Robustness:** a bad `--wall`/`--idle` becomes a structured failure, not a traceback; every early
  failure path reports host provenance; `Backend.independence` (a vestigial duplicate) removed.
- Coverage: the bash installer is now driven by the suite (refuse-physical-dir, symlink, idempotent,
  dry-run); the presence-style/allowlist confidence branches, `review_mode(host="unknown")`, and the
  `other` host tier are all asserted. **One installation safely serves both a Claude Code and a Codex
  host** — verified: no host-specific persisted state; consent keyed by endpoint; settings per backend.

#### Host auto-detection (the detection core)
- `detect_host()` now **auto-detects four hosts** from genuine, strict-value env markers, not just
  Claude: `CLAUDECODE=1` → `claude`, `GEMINI_CLI=1` → `gemini` (new provider **Google**),
  `CURSOR_AGENT=1` → `cursor`, and `CODEX_SANDBOX=seatbelt` / `CODEX_SANDBOX_NETWORK_DISABLED=1` →
  `codex`. A non-Claude host no longer has to export `IMPASSE_HOST` to get an honest tier — though it
  still can, and that remains authoritative.
- **Fail-safe by construction.** Markers match by exact value (an inherited `GEMINI_CLI=0` doesn't
  count); ≥2 attributable markers, or one attributable marker plus Cursor, resolve to `unknown`
  (ambiguous inner-driver — an unordered env set has no nesting depth); and `IMPASSE_HOST` is now
  **validated and conflict-checked** — a nonempty unrecognized value, or one that disagrees with an
  observed marker, yields `unknown` instead of silently falling through. *Behavior change:* a nonempty
  unrecognized `IMPASSE_HOST` previously continued detection; it now returns `unknown`.
- **`codex` is a heuristic, not a contract.** OpenAI ships no branded host flag; the sandbox-state
  vars are absent under `--dangerously-bypass-approvals-and-sandbox` (a safe false-negative). New
  `host_detection: {method, confidence}` provenance rides on `review()`/`mode` results, and a positive
  `cross_provider` tier resting on the Codex heuristic carries a **soft `independence_notice`** so a
  guess can't read as a confirmed claim. Guaranteed labeling: `IMPASSE_HOST=codex`.
- **Trust floor, disclosed.** Env markers are unauthenticated inherited strings; the mitigations
  eliminate accidental collisions and ambiguity but cannot stop a deliberately/accidentally injected
  *exact* marker. New [`docs/host-detection.md`](docs/host-detection.md) carries the per-host
  compatibility matrix (marker, citation, verified version/OS) and the spoofing caveat. The unit
  suite proves the mapping logic (a 128-cell decision matrix vs an independent truth table); it cannot
  detect upstream marker drift — a periodic live smoke test is the documented follow-up.
- Plan of record: [`docs/proposals/multi-host-autodetection.md`](docs/proposals/multi-host-autodetection.md),
  hardened across three cross-provider Impasse reviews (6 findings → 3 → 0 fail-open paths).

### Added
- Schemas: `reviewer-response.v1.json` and `reconciliation-result.v1.json` — the
  reviewer emits observations with anchored evidence; reconciliation records the
  per-finding disposition and inline escalated deadlocks. Domain-general via an
  evidence *anchor* union (`file_range | text_quote | section | structured_path |
  generic`) plus an optional `external_source` citation. Invariants are enforced
  (evidence needs anchor+observation; approve ⇒ 0 findings; failed ⇒ failure object).
- Stdlib-only helpers under `scripts/`:
  - `impasse_lib.py` — config dir, cross-platform codex resolution, artifact hashing,
    endpoint normalization.
  - `impasse_consent.py` — block-by-default data-boundary consent, keyed to the
    normalized endpoint (a changed destination re-prompts), with a payload manifest
    and atomic consent-file writes.
  - `impasse_run.py` — supervised reviewer invocation: stdin-EOF, wall + idle
    timeouts, POSIX process-group termination with bounded reap, size-capped capture,
    and JSON/shape classification of reviewer output (never reports failure as success).
    Reliable process-group kill is POSIX-only; Windows is a roadmap.

### Pre-publish hardening (five-reviewer pass: Codex prose + security/bug/coverage agents)
- Docs: reframed the core promise so it matches read-only behavior (you get the verified
  findings *and* the escalated deadlocks, not "only" the deadlock); marked reconciliation as
  host-directed (not script-enforced); absolute skill-root paths; accurate OpenAI-plugin
  comparison; softened the independence claim; a concrete escalation example.
- Code: `review()` output-file creation moved inside the cleanup scope and uses
  `shutil.rmtree`; reliable reader-drain on clean completion (dropped a misleading
  `stdout_truncated` from the `review()` result); `_read_limited` reads `limit+1` (no TOCTOU);
  consent CLI normalizes destinations to match runtime keys; `_provider_label` uses an exact
  host suffix; `with open()` for the consent read.
- Tests: negative schema fixtures under `schemas/examples/invalid/` (the enforced invariants
  are now proven to *reject*); positive `approve`/`failed` fixtures; consent-integrity tests
  (malformed/wrong-version/symlink → block; notice-version drift); supervisor spawn-error +
  truncation; `review()` timeout + no-final classification; ruff lint in CI.

### Ship-decision improvements (four-lens rubric: CEO buyer, brand strategist, adversarial skeptic, Codex)
- Reframed as the open, pre-release **reimplementation of the essay's workflow**; made the
  top-line present-tense-accurate (verify/reconcile/escalate is *host-directed*, not a standalone
  engine) — closes the claim-vs-code seam.
- Added an advisory funnel: a "Who builds this" section linking the essay + AI Workshop for CEOs.
- Added `docs/walkthrough-decision.md` — a full **business-decision** review end to end (not
  code), reconciling the shipped decision fixtures; aligned `decision.reconciliation-result.json`
  with the reviewer fixture so the two tell one story.
- Qualified "backend-neutral" → "backend-neutral by design; one backend (Codex CLI) today".

### Run records + reports (the audit trail)
- Runs are now **persisted** under `config_dir()/runs/<id>/` (reviewer-response + reconciliation,
  `0600`/`0700`, gitignored). The runner auto-records the reviewer's findings (`--no-record` to
  skip); the host saves the reconciliation via `impasse_report.py save-reconciliation`.
- New `scripts/impasse_report.py`: `list` / `show <id>` / `save-reconciliation <file>` /
  `forget <id>`. `show` renders a scannable report — the **reviewer↔host back-and-forth** per
  finding, the **decision** made, a **tally** (raised/resolved/accepted/rejected/escalated), the
  verification, and the **escalated questions** — with emojis for context.
- `impasse_lib`: `runs_dir` / `save_run_doc` (atomic) / `list_runs` / `load_run` / `forget_run`.
- Closes the "governance tool with no audit trail" gap the ship-review flagged. Cumulative
  cross-run "what it caught" reporting remains a documented roadmap item (not built).
- Housekeeping: `impasse_report.py open` (runs with unresolved escalations + their questions),
  `prune --older-than N` (keeps runs with open escalations unless `--include-open`), and an
  open-count marker on `list`. SKILL guidance to proactively surface unanswered decisions and
  offer to clean up sensitive records.

### Configurable reasoning effort (codex backend)
- `--effort` now resolves like `--model`: per-run flag > `IMPASSE_CODEX_EFFORT` env > persisted
  default (`impasse_run.py set-effort <none|low|medium|high|xhigh>`, `--clear` to unset) > the
  codex CLI's own default (currently medium; Impasse omits the flag and reports `effort: null` —
  backend-controlled). The review result reports the resolved value in `effort`.
- Allowlisted at every entry point — argparse choices on the flag and `set-effort`, a read-path
  filter on the persisted value (a hand-edited `settings.json` can't smuggle a bad value), a
  structured `backend_error` naming `IMPASSE_CODEX_EFFORT` when the env var is invalid, and a
  defense-in-depth re-check in `build_codex_argv` itself before the value is interpolated into
  a codex `-c` config expression (Impasse's own cross-provider review of this change caught the
  last two gaps — see the dogfooding note in CLAUDE.md).
- Effort resolves **only for the codex backend**; claude has no effort knob, so nothing resolves
  there (an irrelevant `IMPASSE_CLAUDE_EFFORT` can neither fail a claude run nor masquerade in
  the result as applied configuration — its `effort` is always `null`).
- SKILL guidance: scale `--wall` with the resolved effort, and run long reviews in the
  background where the host's shell tool caps foreground commands (Claude Code: 10 min).

### Host-relative independence (phase 1 of multi-host support)
- Independence is now computed as a **relation between the host's provider and the reviewer's**
  (`lib.independence_tier`), not hardcoded per backend: to a Codex host, `--backend claude` is
  correctly labeled `cross_provider` (no downgrade notice), and the codex backend gets the
  `same_provider` notice — previously the labels assumed a Claude host and inverted the truth
  for anyone driving the protocol from Codex.
- Host identity: `IMPASSE_HOST` env (`claude|codex|cursor|other`, authoritative; unrecognized
  values ignored) with auto-detection of a Claude host from its **genuine** env markers only —
  `IMPASSE_ENV` (a surface-policy override) cannot manufacture a host identity.
- New `undetermined` tier (with its own `independence_notice`), and it is the **fail-safe
  floor**: an undeclared/unrecognized host, a mixed-model host (`cursor`/`other`, whose
  underlying model is operator-selected), and a backend routed through an endpoint whose
  provider can't be attributed (custom gateways) all get `undetermined` — a positive
  cross-provider claim requires both sides to be attributable. Non-Claude host adapters MUST
  export `IMPASSE_HOST`; a human at the CLI can too.
- `review_mode()` is host-aware and mirrors the actual run: it prefers the available backend
  most independent of the host (`cross_provider > undetermined > same_provider`; ties keep
  codex first for its hermetic OS sandbox), computes tiers against each backend's **configured
  endpoint**, never recommends a backend `get_backend()` would refuse (claude under
  Bedrock/Vertex), carries the downgrade `independence_notice` itself (shared formatter with
  `review()`), and gives host-aware recommendations (only a Claude host is pointed at Claude
  Code). `mode` CLI gains `--host`; review results carry `host`.
- Impasse's own cross-provider review of this change drove the hardening: the initial
  implementation fell back to the historical (Claude-host) labels for an undeclared host —
  fail-open at exactly the boundary the change exists to close — and let `IMPASSE_ENV` imply a
  host identity; both were caught, accepted, and fixed, along with the pre-flight/disclosure
  gaps above.

### Retry malformed reviewer output (issue #1)
- `invalid_response` from stochastically malformed reviewer output — invalid JSON, wrong-shape
  JSON, or an empty final message — is now **auto-retried once** (no backoff, inside the same
  wall-clock budget), the same way a transient outage is; a persistent failure surfaces with
  `retryable: true` so a host can distinguish "retry likely helps" from "structurally broken".
- The size-bound variants (stdout capture cap, final message over the 2 MB byte bound) fail
  immediately with **no auto-retry** but carry `retryable: true` — consistent with the
  `rate_limited` precedent, the hint means "recovery is plausible, offer it", not "the runner
  re-spent for you" — and their messages name the remedy (shrink/tighten/lower effort, or an
  unchanged re-run near the bound). This reversed the issue's original `retryable: false` spec
  after a cross-provider decision review showed the hint and the auto-retry policy are separate
  dimensions of the existing failure contract; operator-ratified 2026-07-16.
- The retry loop now tracks outage and output retries separately (`_MAX_TRANSIENT_RETRIES = 2`,
  `_MAX_OUTPUT_RETRIES = 1`); regression tests prove recovery, the exact retry count per
  failure class, budget independence (outage then malformed output still recovers), per-attempt
  truncation of the final-message file (a retry never re-reads stale output), and that oversize
  output is never retried.
- Fixed alongside (caught by the dogfood review of this fix): the 2 MB final-message bound was
  checked on decoded **characters**, not bytes — a multi-byte UTF-8 message over the byte bound
  slipped past it, and the tolerant parser could then accept a complete JSON object out of a
  silently truncated prefix. The bound is now enforced on bytes, before decoding, with a
  UTF-8 regression test.
- The issue's optional "self-repair round" (feed the reviewer its own broken output to re-emit)
  is deliberately not implemented: a full re-run rescues the common case at the same cost, one
  invocation, without a new echo-untrusted-output-back path.

### Escalation semantics: operator rulings count regardless of channel
- SKILL reconciliation guidance: an operator ruling that decides an item's disposition is
  recorded with an `escalation` object whether it arrived via a formal deadlock,
  `AskUserQuestion`, or prose in conversation — the metric is "questions that decided a
  disposition," and the channel is a UI detail. `operator_question` must be the question as
  actually posed (verbatim/excerpt, not a reconstruction); who initiated the decisive exchange
  is recorded in the positions; a low escalation count is explicitly not a goal.
- Amendments to past records must append amendment metadata (date, reason, what changed, prior
  state) to the item's `resolution` — never a silent rewrite. Applied once: the
  size-bound-retryability ruling (operator-ratified 2026-07-16, conversation path) now carries
  its escalation object with an amendment note.
- Cross-provider decision review of this rule change surfaced the audit-integrity requirements
  above (amendment provenance, verbatim questions, observable metric definition, anti-gaming
  language) — the initial proposal had none of them. Roadmap noted for a possible v1.1 schema:
  optional escalation-channel and amendment-provenance fields (`additionalProperties: false`
  currently confines provenance to structured resolution text).
- **Historical escalation counts are withheld from the public ledger** until 50 reconciled
  reviews accumulate under the corrected rule (from 2026-07-18): the operator attests that more
  judgment calls reached him than pre-rule records captured, and historical events whose exact
  wording is no longer recoverable cannot be amended in without violating the verbatim-question
  requirement. (The one amended record was eligible precisely because its exact wording — the
  operator's question and the delivered ruling — remained available in the retained
  conversation; events without recoverable wording stay uncounted, which is why the historical
  number is a known undercount.)

### Final pre-commit hardening (Fable + Impasse dual review of the full changeset)
- `review_mode` no longer offers a backend whose configured base URL fails to normalize
  (malformed / embedded credentials) — `get_backend()` would refuse it; the raw endpoint value
  is never echoed into labels or notices.
- `_read_limited` (CLI instruction/artifact input) now enforces its 4 MB limit on **bytes**,
  decoding only after the bound passes — the input-side mirror of the final-message byte fix.
- Size-bound failure messages are backend-aware: "lower `--effort`" is suggested only for
  codex (claude has no effort knob); host metadata now rides on backend-resolution failures too.
- Test hardening: the claude transport path (stdout, capture cap) now carries the same
  retry/size assertions as the codex file path; a review-CLI end-to-end test; a cross-feature
  matrix test (codex host + env effort + output retry → identical argv on both attempts); the
  suite clears ambient Impasse/backend env vars at startup so a user's own configuration can't
  break assertions. Docs sweep removed the remaining static-provider independence claims.
