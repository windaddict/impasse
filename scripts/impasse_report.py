"""Human-readable run reports + the run-record audit trail for Impasse. stdlib only.

A run is persisted under the config dir (see impasse_lib.save_run_doc): the reviewer's
findings and the host's reconciliation, keyed by review_id. This renders a run as a
scannable report that shows the back-and-forth between the two models, the decision made
on each finding, a tally, and the questions escalated to the operator.

Run records contain artifact content — they are sensitive (0600, gitignored). `forget`
deletes one.

CLI:
  impasse_report.py list                          # past runs (newest first)
  impasse_report.py show <run_id>                 # the report for one run
  impasse_report.py save-reconciliation <file>    # persist a reconciliation-result under its review_id
  impasse_report.py forget <run_id>               # delete a run record
"""
from __future__ import annotations

import argparse
import datetime
import json
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
    rev = run.get("reviewer_response") or {}
    rec = run.get("reconciliation_result") or {}
    if not rev and not rec:
        return f"No records for run '{_clean(run.get('run_id'))}'."

    art = rev.get("artifact") or {}
    prod = rev.get("producer") or rec.get("producer") or {}
    review_id = _clean(rev.get("review_id") or rec.get("review_id") or run.get("run_id"))
    backend = f"{_clean(prod.get('backend', '?'))}/{_clean(prod.get('model', '?'))}" if prod else "?"

    findings = rev.get("findings") or []
    items = {it.get("finding_id"): it for it in (rec.get("items") or [])}

    out = ["⚖️  Impasse run report"]
    out.append(f"    review: {review_id}")
    if art:
        out.append(f"    artifact: {_clean(art.get('id', '(inline)'))} ({_clean(art.get('kind', '?'))}) · reviewed {_clean(rev.get('created_at', '?'))} · backend {backend}")
    if rec.get("outcome"):
        out.append(f"    outcome: {OUTCOME.get(rec['outcome'], _clean(rec['outcome']))}")

    # tally
    n = len(findings) if findings else len(items)
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
    if rec.get("failure"):
        out.append(f"⚠️ Failure: {rec['failure'].get('code')} — {rec['failure'].get('message')}")
    out.append("─" * 78)

    ordered = findings if findings else [{"id": k} for k in items]
    for f in ordered:
        out += _render_finding(f, items.get(f.get("id")))
        out.append("─" * 78)

    if pending:
        if decided_by_you:
            out.append(f"⚖️  {pending} decision(s) need you; you decided {decided_by_you}; "
                       "the rest the models settled between themselves.")
        else:
            out.append(f"⚖️  {pending} decision(s) need you; the rest the models settled between themselves.")
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


_RECOGNIZED_STATES = frozenset({"accepted", "rejected", "resolved", "deadlocked", "withdrawn"})


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
        problems.append(f"reviewer-response not found for review_id {rid!r} — run the FULL protocol so "
                        "the findings are recorded, or point at the correct review_id")
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
    'issues you'd have shipped'). Returns "" when nothing has been reconciled yet."""
    reviewed = accepted = rejected = resolved = escalated = 0
    n = 0
    for r in lib.list_runs():
        items = (lib.load_run(r["run_id"]).get("reconciliation_result") or {}).get("items") or []
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
    if n == 0:
        return ""
    rev_word = "review" if n == 1 else "reviews"
    # Keep the dispositions distinct: 'resolved' can be host-fixed OR operator-decided, so it must
    # NOT be rolled into the escalated count. 'escalated' counts only items still deadlocked — the
    # ones currently AWAITING the operator (a resolved escalation is no longer counted here).
    lines = [
        "━" * 78,
        f"📈 Your Impasse record — {n} {rev_word} reconciled",
        f"   {reviewed} findings reviewed · {accepted} accepted · {rejected} refuted with evidence · "
        f"{resolved} resolved · {escalated} awaiting you",
        "   Each raised by an independent reviewer and ruled on by the host before it reached you.",
    ]
    return "\n".join(lines)


def _open_escalations(rec: dict) -> list:
    """Items still deadlocked — an escalation the operator hasn't resolved yet. Once the
    operator decides, the host re-saves the reconciliation with that item moved to
    'resolved', so it stops showing as open."""
    return [it for it in (rec.get("items") or []) if isinstance(it, dict) and it.get("state") == "deadlocked"]


def open_runs() -> list:
    """Past runs that still have unresolved escalations, newest first."""
    result = []
    for r in lib.list_runs():
        rec = lib.load_run(r["run_id"]).get("reconciliation_result") or {}
        opens = _open_escalations(rec)
        if opens:
            result.append({"run_id": r["run_id"], "open": opens})
    return result


def prune(older_than_days: int, include_open: bool = False) -> tuple:
    """Delete records older than N days. By default, runs with unresolved escalations are
    KEPT (a pending decision shouldn't be silently discarded) unless include_open=True.
    Returns (deleted_ids, kept_open_ids)."""
    if older_than_days < 1:
        raise ValueError("prune requires --older-than >= 1 (a 0/negative age would delete everything)")
    cutoff = time.time() - older_than_days * 86400
    deleted, kept_open = [], []
    for r in lib.list_runs():
        if r["mtime"] >= cutoff:
            continue
        if not include_open:
            rec = lib.load_run(r["run_id"]).get("reconciliation_result") or {}
            if _open_escalations(rec):
                kept_open.append(r["run_id"])
                continue
        if lib.forget_run(r["run_id"]):
            deleted.append(r["run_id"])
    return deleted, kept_open


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
    fg = sub.add_parser("forget")
    fg.add_argument("run_id")
    sub.add_parser("open")
    pr = sub.add_parser("prune")
    pr.add_argument("--older-than", type=int, required=True, metavar="DAYS", help="delete records older than N days")
    pr.add_argument("--include-open", action="store_true", help="also delete runs with unresolved escalations")
    args = ap.parse_args(argv)

    if args.cmd == "list":
        runs = lib.list_runs()
        if not runs:
            print("(no runs recorded)")
            return 0
        for r in runs:
            ts = datetime.datetime.fromtimestamp(r["mtime"]).strftime("%Y-%m-%d %H:%M")
            flags = ("R" if r["has_review"] else "-") + ("C" if r["has_reconciliation"] else "-")
            rec = lib.load_run(r["run_id"]).get("reconciliation_result") or {}
            opens = len(_open_escalations(rec))
            mark = f"  ⚖️ {opens} open" if opens else ""
            print(f"  {ts}  [{flags}]  {r['run_id']}{mark}")
        return 0
    if args.cmd == "open":
        runs = open_runs()
        if not runs:
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
        return 0
    if args.cmd == "prune":
        try:
            deleted, kept = prune(args.older_than, include_open=args.include_open)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        for rid in deleted:
            print(f"  forgot {rid}")
        msg = f"pruned {len(deleted)} record(s) older than {args.older_than}d"
        if kept:
            msg += f"; kept {len(kept)} with open escalations (use --include-open to remove)"
        print(msg)
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
            problems = _escalation_problems(rec, rev)
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
        if not isinstance(doc, dict) or not doc.get("review_id"):
            print("reconciliation must be a JSON object with a review_id", file=sys.stderr)
            return 2
        rid = doc["review_id"]
        path = lib.save_run_doc(rid, "reconciliation-result", doc)
        print(f"saved: {path}")
        return 0
    if args.cmd == "forget":
        print("forgotten" if lib.forget_run(args.run_id) else "no such run")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
