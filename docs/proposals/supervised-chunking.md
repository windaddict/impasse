# Proposal: opt-in supervised chunking for large code artifacts

**Status: design proposal — NOT built, and not scheduled.** Nothing described here exists in
`scripts/`; every mechanism below is written in the conditional because none of it runs. This file
exists so the decision to defer is recorded with its reasoning, rather than the item quietly
disappearing from issue #11.

**Scope:** item 5 of issue #11 ("Improve Claude review reliability, timeout diagnostics, and ETA
prediction"). For a code artifact above some size threshold, split it into coherent file/module
groups, review each group independently, then run one bounded synthesis pass over the structured
findings — with every chunk and its digest shown in the [consent](../glossary.md) manifest before
anything is sent, the full verify → reconcile → escalate protocol preserved, and chunk agreement
never presented as proof.

**Non-goals:** this is not a "review a whole repository" mode (the
[data-boundary](../security-model.md) guidance is still *allowlist the exact inputs*); it does not
change the [independence tier](../glossary.md) math (the reviewer's provider is the same in every
chunk, so the tier is whatever a single review of the same artifact would earn); and it is
**code-only** by design (see "Why not documents or decisions").

## The problem, and the evidence for it

Reviews scale with artifact size, and the large case is the one that fails. From issue #11, three
supervised runs against the same change on the `claude` backend, `--wall 600 --idle 600`, full
protocol:

| Attempt | Payload | Model | Result |
|---|---|---|---|
| 1 | 43,397 bytes (~10,849 tokens) | backend default (`null`) | `timeout` at 605s, **no findings** |
| 2 | 22,772 bytes (~5,693 tokens) | backend default (`null`) | `timeout` at 605s, **no findings** |
| 3 | same bytes as attempt 2 | `--model sonnet` | completed in **594.21s**, 7 findings |

Two things follow. First, a **timeout discards the entire review** — there is no partial result to
resume from, so the operator has paid for a full model invocation and received nothing. Second,
halving the artifact was necessary but not sufficient: attempt 3 completed only after also pinning a
faster model, and then with ~5.8 seconds of margin inside a 600s wall. The size axis is real, and the
artifacts that are worth an independent review — a whole feature, not one file — sit on the wrong
side of it.

There is a second constraint the wall alone cannot solve: **the host's own command cap is often
shorter than `--wall`.** Claude Code kills a foreground command at 10 minutes, which is why
`SKILL.md` tells the host to run any `--wall` ≥ ~550s in the background. Chunking is the only one of
the seven proposed improvements that shortens the *individual command*, rather than asking the host
to hold a longer one open.

## Why it is deferred

Honestly, and in order of weight:

1. **It changes the protocol, not just the runner.** The other six items are observability and
   advice: they measure a run, recommend a wall, record metrics, or print a better failure. This one
   changes what "a review" *is* — one reviewer over one byte-string becomes N reviewers over N
   disjoint byte-strings plus a synthesis step, with knock-on effects on `artifact.revision`, the
   `review_id` that keys [reconciliation](../glossary.md), and the [run record](../glossary.md)
   layout. That belongs in its own change with its own review, not appended to a diagnostics issue.
2. **The issue's own acceptance criteria don't require it.** Every criterion listed in #11 is about
   where time went, a payload-aware wall recommendation, concrete recovery commands, resolved-model
   capture, summarizable failed-run metrics, and timeout tests. Chunking satisfies none of them, and
   all six shipped without it.
3. **The shipped work addresses the immediate pain.** Attempts 1 and 2 failed against a wall that
   was simply too short for the payload, and nothing at the time said so before the send. `review()`
   now returns a `wall_advice` block computed **before** anything leaves the machine — a recommended
   wall from the backend, model, artifact tokens, and this machine's own recorded percentiles, plus
   an explicit `underprovisioned` flag — and a `timeout` now returns ranked `recovery` options with
   copy-pasteable commands. One of those options (`split_artifact`, rank 3) already tells the
   operator to split by hand, with a concrete token target and the honest caveat that "each piece is
   reviewed WITHOUT sight of the others." Supervised chunking would *automate and record* that
   manual split; it is not what makes the split possible.

None of this argues chunking is a bad idea. It argues that the cheap, honest fixes came first, and
that the expensive one should be justified by evidence the metrics store can now actually produce.

## The design, as it would work

Five stages. The first four are runner work; the fifth is the existing protocol, unchanged.

**1. Split by coherent file/module groups — never mid-file.** A chunk is a whole number of whole
files. This is not a stylistic preference: [anchored evidence](../glossary.md) pairs a locator
(typically `file:line`) with an observation, and the host verifies a finding by reading that
location in the *real* artifact. A chunk that ends halfway through a file gives the reviewer
truncated context while its anchors still point into a file it only partly saw — a class of
unverifiable or misleading anchor that the current path cannot produce. Grouping heuristics are an
open question below; the invariant is the file boundary.

**2. One consent manifest covering every chunk, shown before the first send.** The
[consent gate](../glossary.md) is keyed to the normalized endpoint, so N sends to one already-granted
destination need no extra grant — but consent is also *what* is sent, not only *where*:
`manifest_for_bytes()` today reports total bytes, an approximate token count, and a `digest` of the
exact payload, and `render_manifest()` already knows how to print a per-file `files` list that the
single-artifact path leaves empty. A chunked run would populate exactly that: one manifest listing
every chunk, its byte count, and its digest, plus the digest of the whole artifact the chunks were
cut from — rendered **before any chunk is sent**, so the operator approves the whole plan, not the
first slice of it. Anything less turns one reviewed approval into N unreviewed ones.

**3. Independent per-chunk review.** Each chunk is a normal supervised review: same reviewer stance,
same schema, same [supervisor](../glossary.md) with its own wall and idle caps. Each returns its own
reviewer-response with `artifact.revision` set from *that chunk's* digest and an `artifact.id`
naming the chunk. Any chunk that fails, fails visibly — see "Partial completion" below.

**4. One bounded synthesis pass over structured findings only.** A single pass that reads the N
reviewer-responses — **not** the artifact — and produces a merged, ordered view: which findings
appear to describe the same underlying problem, which conflict, and which chunk limitations left
something uncovered. Bounded means one pass, no rebuttal round, and a hard cap on its output.

**5. The unchanged protocol.** The host then verifies each surviving finding against the real
artifact, dispositions it, and escalates only the [deadlocks](../glossary.md). Chunking must not
touch this. In particular a rejection still needs contradicting evidence, and an evidence-less
refutation is still an `unverified_refutation` deadlock.

## The honesty problem this design lives or dies on

Chunking **buys completion at the cost of cross-cutting findings.** That sentence is the design, and
the output has to carry it.

**A finding that spans two chunks can be invisible to every chunk reviewer.** A caller in chunk A and
its callee in chunk B; an invariant established in one module and violated in another; a validation
step deleted in one file that a second file still assumes ran. No single chunk contains the anchored
evidence for that finding, and a reviewer behaving *correctly* — refusing to raise a claim it cannot
anchor — produces silence. The gap is therefore silent by construction: it does not look like a
failure, it looks like a clean chunk.

*Mitigation, which converts the silence into a statement but does not close the gap:* require every
chunk review to declare what it could not see — unresolved references out of the chunk, recorded in
the response's `limitations` and its `scope.excluded`. The synthesis pass collects those into an
explicit cross-chunk coverage statement, and the report prints it beside the findings. The operator
then knows which seams no reviewer examined. They still have to examine them, or run an unchunked
review, or accept the risk. Nothing here recovers a finding that was never made.

**Agreement across chunks is not corroboration.** Two chunk reviews are the *same model on different
inputs*, not two independent reviewers of the same input. That is not repeated measurement of one
claim — it is one measurement each of two different things. Even where chunks overlap, the second
look is the same provider with the same blind spots, which is the `same_provider` rung of the
ladder: breadth, not independence. So "8 of 9 chunks found nothing" is not evidence the artifact is
sound; it is nine narrow reviews, each of which saw less than the whole.

*Mitigation:* the report must never present N chunk reviews as a stronger review than one. Concretely,
a chunked run's output should state the exact guarantee — **each chunk was reviewed in isolation; the
artifact was not reviewed as a whole** — carry a `chunked: true` marker and the chunk map (id, files,
digest, outcome) wherever the run is rendered or recorded, and keep the tally per-chunk-attributed so
a reader cannot mistake N clean chunks for one clean artifact. The "no findings" case is the dangerous
one and needs the loudest disclosure: an unchunked `approve` means "no material findings under this
scope," and a chunked one means considerably less than that.

**Partial completion is `incomplete`, never `converged`.** N chunks are N chances to time out. A run
where any chunk failed must report the run-level outcome `incomplete`, name the unreviewed chunks,
and — following the existing rule that a failure is never reported as success — must not let the
chunks that did complete stand in for the ones that did not.

## Where it collides with what exists today

Not blockers, but they are the real cost, and none of them is decided here.

- **`artifact.revision` assumes one byte-string.** It is the immutable identity of the exact bytes
  reviewed, set by the host from the manifest digest, and it is what stops findings being reconciled
  against changed content. With N chunks there are N digests the reviewers actually saw plus one
  whole-artifact digest that no reviewer saw. Per-chunk responses can carry their own revisions
  honestly; what the reconciliation's single `artifact_revision` should hold is unresolved.
- **Reconciliation keys off one `review_id`.** `reconciliation-result.v1.json` requires exactly one
  `review_id`, and `impasse_report.py escalations` refuses unless it can load the reviewer-response
  recorded under that id. Chunking needs either a parent id whose record directory holds N chunk
  responses, or a set of ids — the latter would invalidate existing records and so requires a `v2`,
  not an in-place edit. Both schemas do have an additive `extensions` object, but routing a
  first-class concept through `extensions` to dodge a version bump would hide it from validation.
- **Record layout is one response per run.** `runs/<review_id>/reviewer-response.json` would become
  a parent directory plus per-chunk children, with the chunk map stored alongside. Records hold
  artifact content, so the `0600`/`0700` handling and `forget`/`prune` would have to cover the new
  shape.

## Open questions (resolve before building any of it)

1. **How to split.** Directory grouping is cheap, language-agnostic, and blind to coupling.
   Import-graph grouping keeps a caller with its callee more often but needs per-language parsing —
   and `scripts/` is **stdlib-only**, so Python's `ast` is free and every other language is a new
   dependency or a heuristic. Whatever the strategy, a cut still lands somewhere; the question is
   whether a smarter cut reduces cross-chunk blindness enough to be worth the machinery, which is
   itself unmeasured.
2. **How the synthesis pass avoids becoming a second unreviewed model opinion.** It reads findings
   the host has not yet verified and emits something the operator may read as authoritative. The
   restriction that seems right: synthesis may **merge, order, and flag conflicts among existing
   findings, and may not create new ones** — it cannot anchor a claim, because it never sees the
   artifact, and an unanchored claim is not evidence. Anything it wants to add belongs in
   `limitations` or as a candidate the host must verify like any other finding. Whether that
   restriction is enforceable in the output shape, or only in the instruction, is open.
3. **What identity a chunked run carries.** See the `review_id` and `artifact.revision` collisions
   above. This is a schema decision, and it gates everything else.
4. **Whether deduplicating findings across chunks is safe.** Probably not by default. The same
   unsafe pattern in two files is two real instances with two different anchors; merging them loses
   one anchor, so the host can no longer verify each instance and the tally under-counts. Merging by
   `(claim, anchor)` is safe only when the anchors match, which across disjoint chunks they never do.
   The conservative lean is: group for display, keep every finding separately verifiable, and let the
   host merge only after verifying both.
5. **Cost.** N chunks are N paid invocations plus the synthesis — and each invocation re-sends the
   fixed overhead the runner prepends and appends (the reviewer stance and the ~9.9 KB schema,
   roughly 2.5K tokens before the artifact). A chunked run therefore costs *more* total tokens than
   the single review it replaces, and buys completion with that money. Whether the threshold should
   be operator-set, derived from the recorded percentiles, or both, is open.

## Why not documents or decisions

Code has genuine module boundaries; an argument does not. A memo's flaw is usually in the
*relationship* between its parts — an assumption stated in section 2 that section 5 contradicts —
so chunking a document or a decision removes exactly the structure the reviewer is there to judge.
The cross-chunk blindness described above is a manageable cost for code and close to a total loss
for prose. If chunking is built, `kind=code` should be the only accepted kind, and the refusal for
other kinds should say why.

## What would have to be true to revisit this

Trigger conditions, not a schedule:

1. **The metrics store shows the failure is real after the wall fix.** `impasse_report.py
   performance` should show a repeated population of code artifacts timing out *at or above* the
   recommended wall — not merely under-provisioned runs, which the pre-send advice now catches. If
   large reviews now complete when given the wall they were told to give, the case evaporates.
2. **Or the recommended wall routinely exceeds what the host will hold.** If the recommendation for
   ordinary feature-sized artifacts lands past what background execution can carry, shortening each
   command is the only remedy left, and chunking is the only proposal that does it.
3. **Operators are already splitting by hand.** The rank-3 `split_artifact` recovery option makes
   manual splitting the documented path. If that option is being taken and the pain is the *audit
   trail* — N ad-hoc runs, N unrelated records, no chunk map, no coverage statement — then the value
   of building it is in the record layer, which is exactly where a supervised mode helps.
4. **A decided answer on run identity.** Question 3 above, settled, with a schema plan (parent
   `review_id` versus a `v2` reconciliation) before any code.
5. **The disclosure text exists first.** The report's honest framing — the exact guarantee, the chunk
   map, the coverage statement — should be written and reviewed *before* the splitter, because it is
   the part that keeps the feature from quietly overstating what it did.

Run this proposal through Impasse (`kind=decision`) before building any of it.

## Limitations (stated, not hidden)

- **Chunking reduces what a review can see.** Cross-chunk findings can be missed silently. Mitigated
  by mandatory per-chunk `limitations`/`scope.excluded` and a printed coverage statement — which
  makes the gap visible, not smaller.
- **Chunk agreement is not corroboration**, and N clean chunks are not one clean artifact. Mitigated
  only by disclosure: the chunk map, the `chunked` marker, and the exact-guarantee sentence in every
  rendering of the run.
- **It costs more, not less.** More total tokens than the single review, in exchange for finishing.
- **It adds failure surface.** N invocations, N timeout opportunities; mitigated by reporting any
  partial run as `incomplete` with the unreviewed chunks named, never as `converged`.
- **The independence tier is unchanged.** Chunking is orthogonal to independence — it neither earns
  nor spends any rung of the ladder, and must not be described as strengthening a review.
