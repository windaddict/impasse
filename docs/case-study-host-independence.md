# Case study: the reviewer overturns its author

*One review, end to end — written for an engineering leader deciding whether independent AI
review is worth process weight. This recounts a real pre-release review of Impasse's own code;
the resulting commit and CHANGELOG entry are public, and the limitations of this kind of
evidence are stated at the end.*

## The risk, in plain terms

When an AI system tells you "this work was independently reviewed," the value of that sentence
depends entirely on whether the claimed independence is real. Impasse's job is to attach an
honest independence label to every review. The failure that matters is quiet: a system that
*overstates* independence hands you correlated opinions dressed as a second set of eyes — and
you make the decision with more confidence than the evidence supports.

## What happened

In July 2026 the maintainer reworked how Impasse computes that label, so it would hold up no
matter which AI agent was driving the process. The pre-release implementation carried a
deliberate compatibility choice: **when the driving agent couldn't be identified, keep the old,
more favorable labels.** The author had reasons — don't change behavior for existing setups, and
software can't identify a driver that refuses to identify itself. Tests were written that locked
the choice in.

Before the change shipped, it went through Impasse's own protocol: a reviewer from a rival
provider (OpenAI's Codex, reviewing work produced with Anthropic's Claude — the authorship and
fresh-context details are the maintainer's account; the published records identify only the
reviewer backend) read the change cold. Its top finding, paraphrased:

> The fallback grants a positive independence claim in exactly the case this change exists to
> fix. An agent that fails to identify itself — by omission, misspelling, or misconfiguration —
> gets "independent cross-provider review" as a label, with no warning. The new tests don't
> protect that boundary; they enshrine the hole. This is fail-open at the most important gate.

The author had framed the fallback as backward compatibility. The reviewer reframed it as a
security default — and under that frame the design was indefensible. Same code, same facts; a
different reading.

## What the protocol contributed

Impasse's record format has one unusual rule: a saved reconciliation **cannot record a rejected
finding without at least one piece of contradicting evidence** — "I meant to do that" doesn't
validate. That rule constrains the *record*, not the person: the workflow is directed by the
host following the protocol, and a host that ignores it can. What the rule did here was make the
honest paths explicit: produce evidence against the finding, or concede it, or escalate it as a
dispute. The author checked the finding against the code (accurate), against the project's own
precedent (an unidentified *environment* already failed safe — the inconsistency was real), and
conceded. The fallback was reversed the same day: an unidentified driver now gets
"undetermined," never a positive claim, and the tests assert the fail-safe instead of the hole.
The CHANGELOG entry and the commit are public, and — as a deliberate exception to Impasse's
records-stay-local policy, safe here because the reviewed artifact was this public repository's
own code — the reviewer's full response and the reconciliation for this case are published
verbatim in [`evidence/host-independence-review/`](evidence/host-independence-review/). Two
further author decisions fell to the same process that week (summarized in the CHANGELOG).

## What this does and doesn't show

- **The class of error is the interesting part.** This wasn't a bug the author lacked skill to
  see — it was a decision the author had *reasons* for. Review by the same assistant that helped
  make a decision tends to inherit the reasoning behind it; a fresh reviewer, plus a rule that
  dismissals need evidence, gave the objection somewhere to land.
- **One case is an anecdote, not a controlled result.** Cross-provider diversity is Impasse's
  design hypothesis — different training plausibly means partially different blind spots — but
  this record can't isolate that from fresh context or plain variation between runs. It is
  offered as a compatible example, not causal proof. Agreement between two models, likewise, is
  evidence and never proof.
- **The human stays the decider.** Genuine judgment calls escalate to the operator rather than
  being settled between models; in this period one operator ruling reversed the operator's own
  earlier spec — on the reviewer's evidence.
- **The costs are real.** The full cross-provider tier needs access to two AI providers (a
  same-provider fallback exists, with a disclosed weaker guarantee); reviews take minutes and
  cost tokens; and this account is the author reporting on his own tool.

*Impasse is the open (MIT) implementation of the workflow described in
[AI's Second Opinion: When Rival Models Disagree](https://www.movingavg.com/essays/ai-second-opinion-rival-model.html).
Moving Average Inc. advises engineering organizations on exactly this class of AI-process
design.*
