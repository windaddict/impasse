# Evidence bundle: the host-independence review

The records of one Impasse review — the one narrated in
[`docs/case-study-host-independence.md`](../../case-study-host-independence.md), where the
reviewer's top finding overturned the author's fail-open design decision.

- [`reviewer-response.json`](reviewer-response.json) — the reviewer's findings, published by the
  maintainer as the runner-recorded output (6 findings; F001 is the fail-open fallback).
- [`reconciliation-result.json`](reconciliation-result.json) — the host's verification and
  disposition of each finding, including the author's concession on F001 and the verification
  and correction of the rest.

**Why this is published when run records normally aren't.** Impasse's policy is that run records
stay local — they can contain reviewed artifact content, which is the operator's sensitive data.
This one is a deliberate, operator-approved exception: the reviewed artifact was this public
repository's own code, and both files were inspected line-by-line before publication. The fix
these records describe is commit `1a66e6c` ("effort config + host-relative independence + retry
malformed output"); the CHANGELOG's "Host-relative independence" section summarizes it.

**What these records can and can't show.** They let you inspect the detailed claims instead of
relying only on the maintainer's summary — but they are maintainer-selected, self-published
files with no independent attestation, so they remain a published process record, not proof the
process occurred exactly as represented. Known limitations, disclosed rather than smoothed over:

- **The two records don't share a verifiable artifact revision.** The reviewer identified the
  input only descriptively (`algorithm: "other"` — the run predates the practice of stamping the
  runner's digest into the response), while the reconciliation carries the runner's SHA-256 of
  the exact bytes sent. The maintainer asserts they refer to the same run; the files alone
  cannot prove it.
- **Provenance beyond `backend: codex-cli` is maintainer-reported.** The records don't encode
  which model authored the reviewed change or what context the reviewer had — the
  "cross-provider reviewer, reading cold" characterization comes from the maintainer's account.
- For whether the host's verification statements are *true*, read the code and tests in the
  commit — the records only show what was claimed and decided.
