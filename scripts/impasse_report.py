"""Human-readable run reports + the run-record audit trail for Impasse. stdlib only.

A run is persisted under the config dir (see impasse_lib.save_run_doc): the reviewer's
findings and the host's reconciliation, keyed by review_id. This renders a run as a
scannable report that shows the back-and-forth between the two models, the decision made
on each finding, a tally, and the questions escalated to the operator.

Run records contain artifact content — they are sensitive (0600, gitignored). `forget`
deletes one. The separate TIMING store (`performance`) holds no artifact content — only
durations, sizes and outcomes — and is deleted independently.

CLI:
  impasse_report.py list                          # past runs (newest first)
  impasse_report.py show <run_id>                 # the report for one run
  impasse_report.py findings <file>               # a reviewer-response's raw (UNVERIFIED) findings
  impasse_report.py escalations <file>            # the deadlocks awaiting the operator, in full
  impasse_report.py save-reconciliation <file>    # persist a reconciliation-result under its review_id
  impasse_report.py open                          # runs with decisions nobody has answered
  impasse_report.py prune --older-than N          # delete records older than N days
  impasse_report.py performance                   # how long reviews take here (timings only)
  impasse_report.py forget <run_id>               # delete a run record
"""
from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import re
import sys
import textwrap
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import impasse_lib as lib  # noqa: E402

OUTCOME = {"converged": "✅ converged", "deadlocked": "⚖️ deadlocked",
           "incomplete": "⏳ incomplete", "failed": "⚠️ failed"}
SEVERITY = {"critical": "🔴 critical", "high": "🟠 high", "medium": "🟡 medium", "low": "⚪ low"}
STATE = {"accepted": "🤝 accepted", "rejected": "❌ rejected", "resolved": "✅ resolved",
         "deadlocked": "⚖️ ESCALATED — needs your decision", "withdrawn": "↩️ withdrawn"}
VRESULT = {"supports": "✔ supports", "contradicts": "✗ contradicts", "inconclusive": "~ inconclusive"}


_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def _clean(text) -> str:
    """Strip terminal control/escape chars from UNTRUSTED reviewer text before rendering, so a
    malicious review can't inject ANSI/cursor sequences into the operator's terminal. Keeps
    tab and newline; textwrap handles layout."""
    return _CTRL_RE.sub("", str(text))


def _wrap(label: str, text: str, cont: str = "     ") -> str:
    # label on the first line only; continuation lines indented with `cont`.
    return textwrap.fill(_clean(text), width=96, initial_indent=label, subsequent_indent=cont)


def _safe_get(mapping: dict, key, default):
    """`mapping.get` that never raises on an unhashable (malformed, non-string) key from untrusted
    reviewer output — a JSON array/object where a string was expected returns `default`, not TypeError."""
    return mapping.get(key, default) if isinstance(key, str) else default


def _anchor_desc(anchor: dict) -> str:
    if not isinstance(anchor, dict):   # untrusted reviewer output — a non-dict anchor must not crash the render
        return "?"
    t = anchor.get("type")
    if t == "file_range":
        loc = anchor.get("path", "")
        if anchor.get("line_start"):
            loc += f":{anchor['line_start']}"
            if anchor.get("line_end"):
                loc += f"-{anchor['line_end']}"
        if anchor.get("symbol"):
            loc += f" ({anchor['symbol']})"
        return loc
    if t == "text_quote":
        return f'"{anchor["quote"]}"' if anchor.get("quote") else (anchor.get("section") or anchor.get("digest") or "text")
    if t == "section":
        return f'§ {anchor.get("heading", "")}'
    if t == "structured_path":
        return anchor.get("label") or anchor.get("pointer", "")
    if t == "generic":
        return anchor.get("locator", "")
    return t or "?"


def _item_position(item: dict, key: str):
    """A reconciliation item's position, from the item or (schema-permitted) its escalation object."""
    v = item.get(key)
    if isinstance(v, str) and v:
        return v
    esc = item.get("escalation")
    return esc.get(key) if isinstance(esc, dict) else None


def _renders_nonblank(v) -> bool:
    """True iff `v` is a string that still has content AFTER `_clean` strips control chars — so a
    required field that is only terminal-escape bytes (blank once cleaned) does not pass as present."""
    return isinstance(v, str) and _clean(v).strip() != ""


def _has_anchored_evidence(evidence) -> bool:
    """True iff at least one evidence entry RENDERS as real anchored evidence: a dict whose anchor is a
    dict that yields a genuine locator (not blank/'?') AND a non-blank observation. A dict-shaped but
    empty anchor (`{}`) or a blank observation is hollow context — it must not pass as full context."""
    if not isinstance(evidence, list):
        return False
    return any(isinstance(e, dict) and isinstance(e.get("anchor"), dict)
               and _anchor_desc(e["anchor"]).strip() not in ("", "?")
               and _renders_nonblank(e.get("observation"))
               for e in evidence)


def _render_finding(f: dict, item: dict | None) -> list[str]:
    lines = []
    sev = _safe_get(SEVERITY, f.get("severity"), _clean(f.get("severity", "?")))
    state = _safe_get(STATE, (item or {}).get("state"), "🔎 raised (not yet reconciled)")
    cat = _clean(f.get("category", ""))
    lines.append(f"{_clean(f.get('id', '?'))}  {sev}  {state}" + (f"  · {cat}" if cat else ""))
    lines.append(_wrap("  🔎 Reviewer: ", f.get("claim", "")))
    for ev in f.get("evidence", []):
        if not isinstance(ev, dict):   # untrusted reviewer output — a non-dict entry must not crash the render
            continue
        desc = _anchor_desc(ev.get("anchor", {}))
        obs = ev.get("observation", "")
        grounding = ev.get("grounding", "")
        lines.append(_wrap("  📌 Evidence: ", f"{desc} — {obs} [{grounding}]"))
        src = ev.get("external_source")
        if isinstance(src, dict):
            lines.append(_wrap("     ↗ source: ", src.get("uri") or src.get("title") or "external source"))
    if item:
        vers = [v for v in (item.get("verification") or []) if isinstance(v, dict)]
        if vers:
            checks = " · ".join(f"{_clean(v.get('method'))} {_safe_get(VRESULT, v.get('result'), _clean(v.get('result')))}" for v in vers)
            lines.append(f"  🧪 Verified: {checks}")
            for v in vers:
                if v.get("detail"):
                    lines.append(_wrap("     ", v["detail"]))
        # positions may sit on the item or (schema-permitted) inside the escalation — show either
        rp, hp = _item_position(item, "reviewer_position"), _item_position(item, "host_position")
        if rp or hp:
            lines.append("  🗣️ Back-and-forth:")
            if rp:
                lines.append(_wrap("     reviewer ▶ ", rp, "                "))
            if hp:
                lines.append(_wrap("     you      ◀ ", hp, "                "))
        if item.get("state") == "resolved" and item.get("resolution"):
            lines.append(_wrap("  ✅ Resolution: ", item["resolution"]))
        esc = item.get("escalation")
        if esc:
            lines.append(f"  ⚖️ Deadlock: {_clean(esc.get('dispute_kind'))} (stopped: {_clean(esc.get('stop_reason'))})")
            if esc.get("operator_question"):
                lines.append("  ❓ Question for you:")
                lines.append(_wrap("     ", esc["operator_question"]))
    return lines


def render(run: dict) -> str:
    rev_raw = run.get("reviewer_response")
    rec_raw = run.get("reconciliation_result")
    rev = rev_raw or {}
    rec = rec_raw or {}
    if not rev and not rec:
        return f"No records for run '{_clean(run.get('run_id'))}'."

    art = rev.get("artifact") or {}
    prod = rev.get("producer") or rec.get("producer") or {}
    review_id = _clean(rev.get("review_id") or rec.get("review_id") or run.get("run_id"))
    backend = f"{_clean(prod.get('backend', '?'))}/{_clean(prod.get('model', '?'))}" if prod else "?"

    findings = rev.get("findings") or []
    # lib.reconciliation_items(), not `rec.get("items") or []`: a malformed collection used to raise
    # AttributeError HERE, before the unverifiable banner below could ever be printed — so `show`
    # crashed on exactly the records it was being taught to report honestly.
    items = {it.get("finding_id"): it for it in lib.reconciliation_items(rec)}

    # Never invent a denominator (issue #16): when there IS a stored reconciliation, its pairing with
    # the reviewer-response must check out via the same storage-boundary validator `save-reconciliation`
    # enforces at write time — a mismatched/missing reviewer-response, a fabricated finding_id, or a
    # duplicate makes the raised count, the outcome, and the "converged" story all unverifiable. Skip
    # the check when there's no reconciliation yet — that's the normal "not yet recorded" case below,
    # not an integrity problem.
    problems = lib.reconciliation_problems(rec, rev_raw) if rec_raw else []
    # A reconciliation FILE that exists but cannot be read is not "not yet recorded" — the step
    # happened and its evidence is damaged. Saying otherwise is an affirmatively false statement
    # about the record, and `list` already calls the same run an orphan.
    if run.get("reconciliation_result_unreadable"):
        problems = problems + ["reconciliation-result.json is present but unreadable (corrupt, "
                               "not an object, or too large) — it was recorded and cannot be read"]
    if run.get("reviewer_response_unreadable"):
        problems = problems + ["reviewer-response.json is present but unreadable — coverage cannot "
                               "be checked against it"]
    unverifiable = bool(problems)

    out = ["⚖️  Impasse run report"]
    out.append(f"    review: {review_id}")
    if art:
        out.append(f"    artifact: {_clean(art.get('id', '(inline)'))} ({_clean(art.get('kind', '?'))}) · reviewed {_clean(rev.get('created_at', '?'))} · backend {backend}")
    if rec.get("outcome"):
        if unverifiable:
            out.append(f"    outcome: ⚠️ unverifiable (stored: {_clean(rec['outcome'])})")
        else:
            out.append(f"    outcome: {OUTCOME.get(rec['outcome'], _clean(rec['outcome']))}")
    if unverifiable:
        out.append("    ⚠️  UNVERIFIABLE — this reconciliation's pairing with its reviewer-response "
                   "could not be confirmed, so the tally and outcome above cannot be trusted:")
        for p in problems:
            out.append(f"        - {_clean(p)}")

    # tally
    n = "?" if unverifiable else str(len(findings))
    by = {"accepted": 0, "rejected": 0, "resolved": 0, "deadlocked": 0, "withdrawn": 0}
    for it in items.values():
        by[it.get("state")] = by.get(it.get("state"), 0) + 1
    # An operator ruling counts as an escalation regardless of channel (SKILL.md): a resolved item
    # carrying an escalation object was decided BY the operator, not settled between the two models.
    # Credit it separately so the tally can't under-report the operator's own involvement (issue #5) —
    # `pending` is the still-deadlocked work, `resolved_shown` the truly model-settled resolutions.
    decided_by_you = sum(1 for it in items.values()
                         if it.get("state") == "resolved" and isinstance(it.get("escalation"), dict))
    pending = by["deadlocked"]
    resolved_shown = by["resolved"] - decided_by_you
    out.append("")
    tally = (
        f"📊 Decisions: {n} finding(s) raised → ✅ {resolved_shown} resolved · 🤝 {by['accepted']} accepted · "
        f"❌ {by['rejected']} rejected · ⚖️ {pending} escalated to you"
    )
    if decided_by_you:
        tally += f" · 🧑‍⚖️ {decided_by_you} decided by you"
    out.append(tally)
    # `rec_raw` too: with no reconciliation yet, 0-of-N is the NORMAL mid-protocol state, not a
    # warning. Firing ⚠️ on every healthy un-reconciled run spends the signal this change relies on
    # everywhere else, and the honest footer below already says the reconciliation isn't recorded.
    if rec_raw and not unverifiable and findings and len(items) < len(findings):
        out.append(f"    ⚠️  partial: only {len(items)} of {len(findings)} findings dispositioned")
    if rec.get("failure"):
        out.append(f"⚠️ Failure: {rec['failure'].get('code')} — {rec['failure'].get('message')}")
    out.append("─" * 78)

    ordered = findings if findings else [{"id": k} for k in items]
    for f in ordered:
        out += _render_finding(f, items.get(f.get("id")))
        out.append("─" * 78)

    if unverifiable:
        # The denominator was only half the lie: an unverifiable record must not close with a
        # confident "nothing needed you" either — that footer is exactly what made the original bug
        # (issue #16) read as a passed gate instead of a broken one.
        out.append("⚠️  Cannot verify what was settled — the reconciliation above could not be checked "
                   "against its reviewer-response. Treat the tally and outcome as unconfirmed until "
                   "the pairing above is fixed and this is re-saved.")
    elif pending:
        if decided_by_you:
            out.append(f"⚖️  {pending} decision(s) need you; you decided {decided_by_you}; "
                       "the rest the models settled between themselves.")
        else:
            out.append(f"⚖️  {pending} decision(s) need you; the rest the models settled between themselves.")
    elif rec.get("outcome") in ("failed", "incomplete"):
        # The closing line used to branch only on unverifiable + pending deadlocks, so a record whose
        # own outcome says `failed` or `incomplete` — a review that never finished — still signed off
        # with the same "nothing needed you" as a converged one. The outcome is the record's own
        # statement about whether the protocol completed; a summary must not contradict it.
        out.append(f"⏳  Not a completed review — outcome is "
                   f"{OUTCOME.get(rec['outcome'], _clean(rec['outcome']))}. "
                   f"{len(items)} item(s) recorded; nothing here says the protocol finished.")
    elif decided_by_you:
        out.append(f"✅  Models settled {len(items) - decided_by_you}; you decided {decided_by_you}. "
                   "Nothing is waiting on you.")
    elif items:
        out.append(f"✅  Nothing needed you — the models settled all {len(items)} between themselves.")
    else:
        out.append("🔎  Reviewed; reconciliation not yet recorded (run `save-reconciliation`).")
    return "\n".join(out)


def render_findings(response: dict) -> str:
    """Compact, UNVERIFIED render of a reviewer-response's findings — for --raw mode (no
    verify/reconcile/escalate). Untrusted text is sanitized exactly like the full report."""
    response = response if isinstance(response, dict) else {}
    # Untrusted input: `findings` may be absent, null, or (malformed) a non-list. Coerce to a
    # list so a truthy non-list (e.g. a stray string) can't crash the render loop below.
    fs = response.get("findings")
    fs = fs if isinstance(fs, list) else []
    assessment = response.get("assessment")
    out = [f"🔎 Raw reviewer findings — {len(fs)} · UNVERIFIED (no verify/reconcile/escalate; "
           "the reviewer is sometimes confidently wrong — check before acting)"]
    if assessment:
        out.append(f"   assessment: {_clean(assessment)}")
    for f in fs:
        if not isinstance(f, dict):
            continue
        sev = SEVERITY.get(f.get("severity"), _clean(f.get("severity", "?")))
        cat = _clean(f.get("category", ""))
        out.append("─" * 78)
        out.append(f"{_clean(f.get('id', '?'))}  {sev}" + (f"  · {cat}" if cat else ""))
        out.append(_wrap("  ", f.get("claim", "")))
        for ev in (f.get("evidence") or [])[:2]:
            if not isinstance(ev, dict):
                continue
            out.append(_wrap("  📌 ", f"{_anchor_desc(ev.get('anchor', {}))} — {ev.get('observation', '')}"))
    if not fs:
        # Only claim approval when the assessment affirmatively says so. Empty findings paired
        # with a non-approving assessment is malformed reviewer output, not a pass — don't
        # mislabel it "approved".
        if assessment in (None, "", "approve"):
            out.append("  (no findings — the reviewer approved)")
        else:
            out.append(f"  (no individual findings, but assessment is '{_clean(assessment)}' — not an approval)")
    return "\n".join(out)


# Single source shared with lib.reconciliation_problems (the storage-boundary validator) — both
# functions must agree on which states are recognized, or a state one flags and the other doesn't
# would be a silent inconsistency between the write-time guard and the escalations-view guard.
_RECOGNIZED_STATES = lib.RECOGNIZED_ITEM_STATES


def _escalation_problems(rec: dict, rev: dict | None) -> list:
    """Return the reasons the escalations view CANNOT show full context for every pending decision —
    empty means safe to render. This is the guarantee behind the feature: the operator must never be
    prompted with a partial view (a bare question, or a deadlock whose claim/evidence isn't on disk),
    which is exactly the defect it fixes. So a deadlock missing its finding context (claim + anchored
    evidence), positions, or question — an item with an UNRECOGNIZED state (a typo that would silently
    hide an escalation), a duplicate finding_id, or a reviewer-response that isn't the one for this
    review — is a hard error, not something a hollow render papers over. TOTAL: never raises, even on
    malformed input; reviewer findings are UNTRUSTED. Required text is judged AFTER `_clean`, so a
    control-char-only value (blank once rendered) counts as missing."""
    problems = []
    if not isinstance(rec, dict):
        return ["reconciliation is not an object"]
    items = rec.get("items")
    if not isinstance(items, list):
        return ["reconciliation 'items' is not a list"]
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            problems.append(f"item[{i}] is not an object")
        elif not (isinstance(it.get("state"), str) and it["state"] in _RECOGNIZED_STATES):
            # isinstance guard first: a non-string (e.g. a JSON list) is unhashable and would raise
            # on the set membership test — this function must stay total.
            problems.append(f"item[{i}] (finding {it.get('finding_id')!r}) has an unrecognized state "
                            f"{it.get('state')!r} — a real escalation could be silently hidden")
    # finding_ids must be unique (JSON Schema can't enforce it — the runner/CI must): a duplicate
    # would render the same decision twice and inflate the count.
    seen = {}
    for it in items:
        fid = it.get("finding_id") if isinstance(it, dict) else None
        if isinstance(fid, str):
            seen[fid] = seen.get(fid, 0) + 1
    problems += [f"duplicate finding_id {k!r} across items" for k, n in sorted(seen.items()) if n > 1]
    opens = _open_escalations(rec)
    if not opens:
        return problems   # nothing to decide (or only the structural problems above)
    rid = rec.get("review_id")
    if not (isinstance(rid, str) and rid):
        problems.append("review_id is missing/not a string, so the reviewer-response (finding claims + "
                        "evidence) can't be located")
    findings = {}
    if isinstance(rev, dict):
        if rev.get("review_id") != rid:   # the loaded record must be THIS review's, not a crossed one
            problems.append(f"reviewer-response review_id {rev.get('review_id')!r} does not match the "
                            f"reconciliation's {rid!r} — refusing to show another review's findings")
        revf = rev.get("findings")
        if isinstance(revf, list):
            dup_fids = set()
            for f in revf:
                if isinstance(f, dict) and isinstance(f.get("id"), str):
                    if f["id"] in findings:   # duplicate reviewer finding id: can't tell which the deadlock means
                        dup_fids.add(f["id"])
                    findings[f["id"]] = f
            problems += [f"reviewer-response has a duplicate finding id {k!r} — ambiguous which the "
                         "deadlock refers to" for k in sorted(dup_fids)]
        else:
            problems.append("reviewer-response 'findings' is not a list")
    elif rev is None:
        problems.append(lib.MISSING_REVIEWER_RESPONSE_MSG.format(rid=rid))
    else:
        problems.append("reviewer-response is malformed (not an object)")
    for it in opens:
        fid = it.get("finding_id")
        label = f"deadlock {fid!r}"
        if not (isinstance(fid, str) and fid):
            problems.append(f"{label}: finding_id is missing or not a string")
        elif isinstance(rev, dict):
            f = findings.get(fid)
            if f is None:
                problems.append(f"{label}: no matching finding in the reviewer-response — its claim/"
                                "evidence can't be shown")
            else:
                if not _renders_nonblank(f.get("claim")):
                    problems.append(f"{label}: the matched finding has no claim text")
                if not _has_anchored_evidence(f.get("evidence")):
                    problems.append(f"{label}: the matched finding has no anchored evidence to show")
        if not _renders_nonblank(_item_position(it, "reviewer_position")):
            problems.append(f"{label}: missing reviewer_position")
        if not _renders_nonblank(_item_position(it, "host_position")):
            problems.append(f"{label}: missing host_position")
        esc = it.get("escalation")
        if not (isinstance(esc, dict) and _renders_nonblank(esc.get("operator_question"))):
            problems.append(f"{label}: missing escalation.operator_question (the footer promises one)")
    return problems


def render_escalations(rec: dict, rev: dict | None) -> str:
    """Render, IN FULL, only the items still awaiting the operator — the deadlocks — so they see each
    escalated issue's evidence and both positions BEFORE being asked to decide, symmetric with how
    resolved items appear in `show`. `rec` is the (draft or saved) reconciliation the deadlock items
    live in; `rev` is the reviewer-response holding the finding claims/evidence (its findings are
    UNTRUSTED — `_render_finding` cleans them). Presentation only, and the sanctioned path is the CLI
    subcommand, which runs `_escalation_problems` FIRST and refuses a partial view — so this ASSUMES
    validated input. It does not itself re-validate (a direct importer must run the check), but the
    shared render helpers are hardened so malformed sub-structures degrade rather than crash."""
    opens = _open_escalations(rec)
    review_id = _clean(rec.get("review_id") or (rev or {}).get("review_id") or "?")
    if not opens:
        return f"✅ No escalated decisions for '{review_id}' — nothing needs you."
    findings = {f["id"]: f for f in ((rev or {}).get("findings") or [])
                if isinstance(f, dict) and isinstance(f.get("id"), str)}
    out = [f"⚖️  {len(opens)} decision(s) need you — full context before you choose",
           f"    review: {review_id}",
           "─" * 78]
    for it in opens:
        fid = it.get("finding_id")
        out += _render_finding(findings.get(fid) or {"id": fid}, it)
        out.append("─" * 78)
    out.append("Answer each ❓ question; your ruling becomes that item's resolution.")
    return "\n".join(out)


def lifetime_recap() -> str:
    """A short, honest value recap across every reconciled run on disk — printed at the end of
    a `show` so the operator sees what independent review has surfaced for them. Facts only:
    counts come from real reconciliation records, and self-evident (no traction claims, no
    'issues you'd have shipped'). Returns "" when nothing has been reconciled yet.

    Eligibility comes from the validator, not from file presence (issue D5/#16/#17): a record whose
    reconciliation_problems() is non-empty — missing sibling, fabricated finding_id, duplicate, bad
    pairing — is QUARANTINED out of the totals rather than counted, because none of its numbers could
    be trusted if they were. Quarantined records are disclosed, not silently dropped: this recap has
    moved numbers the operator has already seen, in either direction, so a silent change here would
    be exactly the kind of unnoticed drift this whole feature exists to prevent."""
    reviewed = accepted = rejected = resolved = escalated = 0
    n = quarantined = 0
    for r in lib.list_runs():
        run = lib.load_run(r["run_id"])
        rec = run.get("reconciliation_result")
        if run.get("reconciliation_result_unreadable"):
            quarantined += 1          # present but unreadable IS a record, and a damaged one
            continue
        if not rec:
            continue
        if lib.reconciliation_problems(rec, run.get("reviewer_response")):
            quarantined += 1
            continue
        items = lib.reconciliation_items(rec)
        if not items:
            continue
        n += 1
        for it in items:
            reviewed += 1
            st = it.get("state")
            if st == "accepted":
                accepted += 1
            elif st == "rejected":
                rejected += 1
            elif st == "resolved":
                resolved += 1
            elif st == "deadlocked":
                escalated += 1
    if n == 0 and quarantined == 0:
        return ""
    lines = ["━" * 78]
    if n:
        rev_word = "review" if n == 1 else "reviews"
        # Keep the dispositions distinct: 'resolved' can be host-fixed OR operator-decided, so it must
        # NOT be rolled into the escalated count. 'escalated' counts only items still deadlocked — the
        # ones currently AWAITING the operator (a resolved escalation is no longer counted here).
        lines += [
            f"📈 Your Impasse record — {n} {rev_word} reconciled",
            f"   {reviewed} findings reviewed · {accepted} accepted · {rejected} refuted with evidence · "
            f"{resolved} resolved · {escalated} awaiting you",
            "   Each raised by an independent reviewer and ruled on by the host before it reached you.",
        ]
    else:
        lines.append("📈 Your Impasse record — no verified reconciliations yet")
    if quarantined:
        rec_word = "record" if quarantined == 1 else "records"
        lines.append(f"   ⚠️  {quarantined} {rec_word} quarantined (unverifiable — not counted above; "
                     "see `report list`).")
    return "\n".join(lines)


def _fmt_s(v) -> str:
    return "—" if not isinstance(v, (int, float)) else f"{v:.0f}s"


def render_performance(rows: list) -> str:
    """WHAT IT'S FOR: answering "how long does a review actually take on MY account, and what
    --wall should I give the next one" from recorded runs instead of a shipped guess.

    Groups by backend + model + effort + speed — the same four keys `recommend_wall` fits on. Model
    and backend move duration most, but effort and speed move it enough that pooling them makes the
    displayed recommendation meaningless: a history of low-effort runs would size a high-effort
    review far too small. The library learned this in issue #11; this report has to group the same
    way or it silently undoes the filter when it passes `rows=` (which bypasses the store's own
    filtering — see `recommend_wall`).

    Timeouts are reported SEPARATELY from completions — a timeout records when we stopped waiting,
    not how long the review needed, so folding it into a duration percentile would understate every
    future estimate.
    """
    if not rows:
        return ("⏱️  No run timings recorded yet.\n"
                "    Timings are recorded automatically as you run reviews; once ~5 runs exist for a "
                "backend+model,\n    `impasse_run.py estimate` switches from the shipped estimate to "
                "your own measurements.")
    groups = {}
    for r in rows:
        key = (r.get("backend") or "?",
               r.get("model_resolved") or r.get("model_requested") or "(backend default)",
               r.get("effort"), r.get("speed"))
        groups.setdefault(key, []).append(r)

    out = [f"⏱️  Impasse performance — {len(rows)} run(s) recorded",
           f"    store: {lib.metrics_path()} (0600) — timings and sizes; no artifact content "
           f"(model/version strings come from the backend, bounded to 200 chars).",
           "    `performance --forget` deletes it.",
           "─" * 78]
    for (backend, model, effort, speed), rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        done = [r for r in rs if r.get("outcome") == "completed"]
        timeouts = [r for r in rs if r.get("outcome") == "timeout"]
        errors = [r for r in rs if r.get("outcome") not in ("completed", "timeout")]
        # Filter to real numbers at the source. A row is a hand-editable file on disk that a crash
        # can also truncate, so a null or a string where a duration belongs must render as "—",
        # never raise out of a report the operator ran to diagnose something else.
        def _nums(rows_, key):
            # isfinite, not just isinstance: NaN/Infinity ARE floats, and they survive percentile
            # arithmetic only to raise ValueError/OverflowError at the int() that formats them.
            return [r.get(key) for r in rows_
                    if isinstance(r.get(key), (int, float)) and not isinstance(r.get(key), bool)
                    and math.isfinite(r.get(key))]
        durs = _nums(done, "duration_s")
        ttfbs = _nums(done, "ttfb_s")
        toks = _nums(done, "artifact_tokens_est")
        # `model_source` is the honesty flag: "requested" means an alias/flag we sent, which the
        # backend never confirmed — two such rows may be different models pooled under one name.
        srcs = {r.get("model_source") for r in rs}
        label = f"{_clean(backend)}/{_clean(model)}"
        # Name the settings this group is fitted at: the recommendation below is only valid for
        # them, and an unlabelled number invites reuse at settings it was never measured under.
        _knobs = []
        if effort:
            _knobs.append(f"effort {_clean(effort)}")
        if speed:
            _knobs.append(f"speed {_clean(speed)}")
        label += f"  [{' · '.join(_knobs)}]" if _knobs else "  [effort/speed unrecorded]"
        if srcs and srcs <= {"requested", "backend_default"}:
            label += "  (requested, not confirmed by the backend)"
        line = f"  {label}\n     {len(rs)} run(s) · {len(done)} completed"
        if timeouts:
            line += f" · ⏰ {len(timeouts)} timed out"
        if errors:
            line += f" · ⚠️ {len(errors)} error"
        out.append(line)
        if done:
            _ttfb50, _tok50 = lib._percentile(ttfbs, 0.5), lib._percentile(toks, 0.5)
            out.append(f"     duration p50 {_fmt_s(lib._percentile(durs, 0.5))} · "
                       f"p90 {_fmt_s(lib._percentile(durs, 0.9))}"
                       + (f" · first byte p50 {_ttfb50:.1f}s" if _ttfb50 is not None else "")
                       + (f" · artifact p50 ~{_tok50:.0f} tokens" if _tok50 is not None else ""))
            median_tokens = int(_tok50 or 0)
            # rs is already filtered to one backend/model/effort/speed by the group key above,
            # which is the filtering `recommend_wall` documents as the caller's job when rows= is
            # passed. Grouping and fitting must stay on the same four keys.
            rec = lib.recommend_wall(backend=backend, artifact_tokens=median_tokens,
                                     model=None if model == "(backend default)" else model,
                                     effort=effort, speed=speed, rows=rs)
            out.append(f"     → for a ~{median_tokens}-token review: --wall "
                       f"{rec['recommended_wall_s']:.0f}s ({rec['basis']})")
        _t_walls = [t.get("wall_s") for t in timeouts
                    if isinstance(t.get("wall_s"), (int, float))
                    and not isinstance(t.get("wall_s"), bool) and math.isfinite(t.get("wall_s"))]
        if _t_walls:
            out.append(f"     ⏰ longest cap already exceeded: {max(_t_walls):.0f}s — a retry needs "
                       "more than that, not the same again")
        out.append("─" * 78)
    return "\n".join(out)


def _open_escalations(rec: dict) -> list:
    """Items still deadlocked — an escalation the operator hasn't resolved yet. Once the
    operator decides, the host re-saves the reconciliation with that item moved to
    'resolved', so it stops showing as open."""
    return [it for it in (rec.get("items") or []) if isinstance(it, dict) and it.get("state") == "deadlocked"]


def open_runs() -> list:
    """Past runs that still have unresolved escalations, newest first — excluding any run whose
    reconciliation fails lib.reconciliation_problems(). About to ask the operator to rule on a
    deadlock, Impasse must not surface one it cannot confirm actually came from the reviewer-response
    it claims to (issues #16/#17's failure mode, applied to the one surface where an unverifiable
    record could still do active harm — prompting a decision built on it). Excluded runs are not
    silently dropped: see unverifiable_open_run_ids()."""
    result = []
    for r in lib.list_runs():
        run = lib.load_run(r["run_id"])
        rec = run.get("reconciliation_result") or {}
        opens = _open_escalations(rec)
        if opens and not lib.reconciliation_problems(rec, run.get("reviewer_response")):
            result.append({"run_id": r["run_id"], "open": opens})
    return result


def unverifiable_open_run_ids() -> list:
    """run_ids that appear to have unresolved (deadlocked) items but were excluded from open_runs()
    because their reconciliation cannot be verified against its reviewer-response — so `report open`
    can disclose them by name instead of just going quiet about work that may still be pending."""
    out = []
    for r in lib.list_runs():
        run = lib.load_run(r["run_id"])
        rec = run.get("reconciliation_result") or {}
        if _open_escalations(rec) and lib.reconciliation_problems(rec, run.get("reviewer_response")):
            out.append(r["run_id"])
    return out


def prune(older_than_days: int, include_open: bool = False) -> tuple:
    """Delete records older than N days. By default, runs with unresolved escalations are
    KEPT (a pending decision shouldn't be silently discarded) unless include_open=True.
    Returns (deleted_ids, kept_open_ids, invalid_ids) — invalid_ids names any inspected run (whether
    it ends up deleted or kept) whose stored reconciliation fails lib.reconciliation_problems(), so an
    orphan doesn't pass through prune without ever being flagged as one."""
    if older_than_days < 1:
        raise ValueError("prune requires --older-than >= 1 (a 0/negative age would delete everything)")
    cutoff = time.time() - older_than_days * 86400
    deleted, kept_open, invalid = [], [], []
    for r in lib.list_runs():
        if r["mtime"] >= cutoff:
            continue
        run = lib.load_run(r["run_id"])
        rec = run.get("reconciliation_result") or {}
        if rec and lib.reconciliation_problems(rec, run.get("reviewer_response")):
            invalid.append(r["run_id"])
        if not include_open and _open_escalations(rec):
            kept_open.append(r["run_id"])
            continue
        if lib.forget_run(r["run_id"]):
            deleted.append(r["run_id"])
    return deleted, kept_open, invalid


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="impasse_report")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    s = sub.add_parser("show")
    s.add_argument("run_id")
    fnd = sub.add_parser("findings", help="render a reviewer-response's raw findings (a review's --raw output)")
    fnd.add_argument("path", help="a reviewer-response JSON file, or a review result JSON (uses its .response)")
    esc = sub.add_parser("escalations", help="render, IN FULL, the deadlocks awaiting the operator in a (draft) reconciliation — show BEFORE prompting for decisions")
    esc.add_argument("path", help="a reconciliation-result JSON (draft or saved); its review_id locates the reviewer-response for finding context")
    sr = sub.add_parser("save-reconciliation")
    sr.add_argument("path")
    sr.add_argument("--partial", action="store_true",
                     help="allow saving before every raised finding is dispositioned (a deliberately "
                          "partial reconciliation mid-protocol); refuses outcome: converged")
    sr.add_argument("--force", action="store_true",
                     help="replace an existing reconciliation for this review_id; the previous one is "
                          "kept as reconciliation-result.<n>.json, never silently discarded")
    fg = sub.add_parser("forget")
    fg.add_argument("run_id")
    sub.add_parser("open")
    pr = sub.add_parser("prune")
    pr.add_argument("--older-than", type=int, required=True, metavar="DAYS", help="delete records older than N days")
    pr.add_argument("--include-open", action="store_true", help="also delete runs with unresolved escalations")
    pf = sub.add_parser("performance", help="how long reviews actually take on this machine, per "
                                            "backend+model — the basis for the --wall recommendation")
    pf.add_argument("--backend", default=None, choices=["codex", "claude"], help="only this backend")
    pf.add_argument("--model", default=None, help="only this model (matches requested or resolved)")
    pf.add_argument("--json", action="store_true", help="emit the raw metric rows instead of a report")
    pf.add_argument("--forget", action="store_true", help="delete the whole local timing store")
    args = ap.parse_args(argv)

    if args.cmd == "list":
        runs = lib.list_runs()
        if not runs:
            print("(no runs recorded)")
            return 0
        for r in runs:
            ts = datetime.datetime.fromtimestamp(r["mtime"]).strftime("%Y-%m-%d %H:%M")
            flags = ("R" if r["has_review"] else "-") + ("C" if r["has_reconciliation"] else "-")
            run = lib.load_run(r["run_id"])
            rec = run.get("reconciliation_result") or {}
            opens = len(_open_escalations(rec))
            mark = f"  ⚖️ {opens} open" if opens else ""
            # An explicit marker rather than making the reader decode the [-C]/[R-] flag combo: a
            # reconciliation that exists but fails the same validator save-reconciliation enforces at
            # write time (missing/mismatched reviewer-response, fabricated or duplicate finding_id) is
            # an orphan in the sense issues #16/#17 describe, regardless of which flag bit exposed it.
            if r["has_reconciliation"] and lib.reconciliation_problems(rec, run.get("reviewer_response")):
                mark += "  ⚠️ orphan (unverifiable)"
            print(f"  {ts}  [{flags}]  {r['run_id']}{mark}")
        return 0
    if args.cmd == "open":
        runs = open_runs()
        skipped = unverifiable_open_run_ids()
        if not runs:
            if skipped:
                print(f"✅ No verifiable unresolved escalations — but {len(skipped)} record(s) with "
                      "apparent deadlocks could not be verified against their reviewer-response "
                      "(see `report list`): " + ", ".join(skipped))
            else:
                print("✅ No unresolved escalations across recorded runs.")
            return 0
        total = sum(len(r["open"]) for r in runs)
        print(f"⚖️  {total} unresolved decision(s) across {len(runs)} run(s):")
        for r in runs:
            print(f"\n  {r['run_id']}")
            for it in r["open"]:
                esc = it.get("escalation") or {}
                q = esc.get("operator_question") or "(no question recorded)"
                print(_wrap(f"    • {it.get('finding_id')}: ", q, "      "))
        if skipped:
            print(f"\n⚠️  {len(skipped)} additional record(s) have apparent deadlocks that could not "
                  "be verified against their reviewer-response (see `report list`): " + ", ".join(skipped))
        return 0
    if args.cmd == "prune":
        try:
            deleted, kept, invalid = prune(args.older_than, include_open=args.include_open)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        for rid in deleted:
            print(f"  forgot {rid}")
        msg = f"pruned {len(deleted)} record(s) older than {args.older_than}d"
        if kept:
            msg += f"; kept {len(kept)} with open escalations (use --include-open to remove)"
        if invalid:
            msg += f"; {len(invalid)} inspected record(s) were unverifiable (orphaned or mismatched)"
        print(msg)
        return 0
    if args.cmd == "performance":
        if args.forget:
            # A separate opt-in from `forget`/`prune`: this store outlives individual run records
            # (they can be pruned while their timings remain), so it needs its own delete.
            print("timing store deleted" if lib.forget_metrics() else "no timing store on disk")
            return 0
        rows = lib.load_metrics(backend=args.backend, model=args.model)
        if args.json:
            print(json.dumps(rows, indent=2))
            return 0
        print(render_performance(rows))
        return 0
    if args.cmd == "show":
        print(render(lib.load_run(args.run_id)))
        recap = lifetime_recap()
        if recap:
            print(recap)
        return 0
    if args.cmd == "findings":
        try:
            with open(args.path, encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError) as e:
            print(f"cannot read findings file: {e}", file=sys.stderr)
            return 2
        resp = doc.get("response") if isinstance(doc, dict) and isinstance(doc.get("response"), dict) else doc
        if not (isinstance(resp, dict) and "findings" in resp):
            print("not a reviewer-response (no 'findings' field) — is this the right file? "
                  "Expected a reviewer-response JSON, or a review-result wrapping one under 'response'.",
                  file=sys.stderr)
            return 2
        print(render_findings(resp))
        return 0
    if args.cmd == "escalations":
        try:
            with open(args.path, encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, ValueError) as e:
            print(f"cannot read reconciliation file: {e}", file=sys.stderr)
            return 2
        if not (isinstance(rec, dict) and isinstance(rec.get("items"), list)):
            print("not a reconciliation-result (no 'items' list) — expected a reconciliation JSON "
                  "(draft or saved).", file=sys.stderr)
            return 2
        rid = rec.get("review_id")
        # One boundary around load + validate + render: untrusted/malformed data must yield a
        # controlled exit 2, never a traceback (the validator is total, but load_run/render can still
        # raise on storage or degenerate input).
        try:
            rev = lib.load_run(rid).get("reviewer_response") if isinstance(rid, str) and rid else None
            # _escalation_problems guarantees full context for PENDING deadlocks specifically, and (by
            # design) skips the reviewer-response pairing check entirely when there is nothing open to
            # render — so an orphaned or fabricated reconciliation with no CURRENT deadlock used to
            # pass here silently. lib.reconciliation_problems is the storage-boundary validator that
            # save-reconciliation also runs, and it checks pairing unconditionally — route through
            # both, so a structural or pairing problem refuses here even with zero open escalations.
            problems = list(dict.fromkeys(lib.reconciliation_problems(rec, rev) + _escalation_problems(rec, rev)))
            if problems:
                out = None
            else:
                out = render_escalations(rec, rev)
        except Exception as e:
            print(f"escalations: could not prepare the view: {e}", file=sys.stderr)
            return 2
        if out is None:   # refuse to prompt with a partial view — the whole point is full context
            print("escalations: cannot show full context for every pending decision — refusing to "
                  "present a partial view:", file=sys.stderr)
            for p in problems:
                print(f"  - {p}", file=sys.stderr)
            print("Populate each deadlock's positions + operator_question and ensure the reviewer-"
                  "response is recorded under this review_id, then retry.", file=sys.stderr)
            return 2
        print(out)
        return 0
    if args.cmd == "save-reconciliation":
        try:
            with open(args.path, encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"cannot read reconciliation file: {e}", file=sys.stderr)
            return 2
        res = lib.save_reconciliation_doc(doc, partial=args.partial, force=args.force)
        if not res.get("ok"):
            if res.get("conflict"):
                print("save-reconciliation: a reconciliation already exists for this review_id "
                      f"(reconciliation_id={res.get('reconciliation_id')!r}, "
                      f"{res.get('item_count')} item(s)) — pass --force to replace it; the previous "
                      "one is kept as a numbered backup, never silently discarded.", file=sys.stderr)
            else:
                print("save-reconciliation: refusing to save:", file=sys.stderr)
                for r in res.get("reasons", []):
                    print(f"  - {r}", file=sys.stderr)
            return 2
        verb = "replaced" if res["replaced"] else "saved"
        line = f"{verb}: {res['path']}"
        if res.get("backup_path"):
            line += f" (previous kept as {res['backup_path']})"
        if res.get("raised"):
            line += f" — {res['dispositioned']} of {res['raised']} findings dispositioned"
        print(line)
        return 0
    if args.cmd == "forget":
        print("forgotten" if lib.forget_run(args.run_id) else "no such run")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
