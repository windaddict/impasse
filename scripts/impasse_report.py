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


def _wrap(label: str, text: str, cont: str = "     ") -> str:
    # label on the first line only; continuation lines indented with `cont`.
    return textwrap.fill(str(text), width=96, initial_indent=label, subsequent_indent=cont)


def _anchor_desc(anchor: dict) -> str:
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


def _render_finding(f: dict, item: dict | None) -> list[str]:
    lines = []
    sev = SEVERITY.get(f.get("severity"), f.get("severity", "?"))
    state = STATE.get((item or {}).get("state"), "🔎 raised (not yet reconciled)")
    cat = f.get("category", "")
    lines.append(f"{f.get('id', '?')}  {sev}  {state}" + (f"  · {cat}" if cat else ""))
    lines.append(_wrap("  🔎 Reviewer: ", f.get("claim", "")))
    for ev in f.get("evidence", []):
        desc = _anchor_desc(ev.get("anchor", {}))
        obs = ev.get("observation", "")
        grounding = ev.get("grounding", "")
        lines.append(_wrap("  📌 Evidence: ", f"{desc} — {obs} [{grounding}]"))
        if ev.get("external_source"):
            src = ev["external_source"]
            lines.append(_wrap("     ↗ source: ", src.get("uri") or src.get("title") or "external source"))
    if item:
        vers = item.get("verification") or []
        if vers:
            checks = " · ".join(f"{v.get('method')} {VRESULT.get(v.get('result'), v.get('result'))}" for v in vers)
            lines.append(f"  🧪 Verified: {checks}")
            for v in vers:
                if v.get("detail"):
                    lines.append(_wrap("     ", v["detail"]))
        rp, hp = item.get("reviewer_position"), item.get("host_position")
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
            lines.append(f"  ⚖️ Deadlock: {esc.get('dispute_kind')} (stopped: {esc.get('stop_reason')})")
            if esc.get("operator_question"):
                lines.append("  ❓ Question for you:")
                lines.append(_wrap("     ", esc["operator_question"]))
    return lines


def render(run: dict) -> str:
    rev = run.get("reviewer_response") or {}
    rec = run.get("reconciliation_result") or {}
    if not rev and not rec:
        return f"No records for run '{run.get('run_id')}'."

    art = rev.get("artifact") or {}
    prod = rev.get("producer") or rec.get("producer") or {}
    review_id = rev.get("review_id") or rec.get("review_id") or run.get("run_id")
    backend = f"{prod.get('backend', '?')}/{prod.get('model', '?')}" if prod else "?"

    findings = rev.get("findings") or []
    items = {it.get("finding_id"): it for it in (rec.get("items") or [])}

    out = ["⚖️  Impasse run report"]
    out.append(f"    review: {review_id}")
    if art:
        out.append(f"    artifact: {art.get('id', '(inline)')} ({art.get('kind', '?')}) · reviewed {rev.get('created_at', '?')} · backend {backend}")
    if rec.get("outcome"):
        out.append(f"    outcome: {OUTCOME.get(rec['outcome'], rec['outcome'])}")

    # tally
    n = len(findings) if findings else len(items)
    by = {"accepted": 0, "rejected": 0, "resolved": 0, "deadlocked": 0, "withdrawn": 0}
    for it in items.values():
        by[it.get("state")] = by.get(it.get("state"), 0) + 1
    out.append("")
    out.append(
        f"📊 Decisions: {n} finding(s) raised → ✅ {by['resolved']} resolved · 🤝 {by['accepted']} accepted · "
        f"❌ {by['rejected']} rejected · ⚖️ {by['deadlocked']} escalated to you"
    )
    if rec.get("failure"):
        out.append(f"⚠️ Failure: {rec['failure'].get('code')} — {rec['failure'].get('message')}")
    out.append("─" * 78)

    ordered = findings if findings else [{"id": k} for k in items]
    for f in ordered:
        out += _render_finding(f, items.get(f.get("id")))
        out.append("─" * 78)

    esc = by["deadlocked"]
    if esc:
        out.append(f"⚖️  {esc} decision(s) need you; the rest the models settled between themselves.")
    elif items:
        out.append(f"✅  Nothing needed you — the models settled all {len(items)} between themselves.")
    else:
        out.append("🔎  Reviewed; reconciliation not yet recorded (run `save-reconciliation`).")
    return "\n".join(out)


def _open_escalations(rec: dict) -> list:
    """Items still deadlocked — an escalation the operator hasn't resolved yet. Once the
    operator decides, the host re-saves the reconciliation with that item moved to
    'resolved', so it stops showing as open."""
    return [it for it in (rec.get("items") or []) if it.get("state") == "deadlocked"]


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
        deleted, kept = prune(args.older_than, include_open=args.include_open)
        for rid in deleted:
            print(f"  forgot {rid}")
        msg = f"pruned {len(deleted)} record(s) older than {args.older_than}d"
        if kept:
            msg += f"; kept {len(kept)} with open escalations (use --include-open to remove)"
        print(msg)
        return 0
    if args.cmd == "show":
        print(render(lib.load_run(args.run_id)))
        return 0
    if args.cmd == "save-reconciliation":
        try:
            with open(args.path, encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"cannot read reconciliation file: {e}", file=sys.stderr)
            return 2
        rid = doc.get("review_id")
        if not isinstance(doc, dict) or not rid:
            print("reconciliation must be a JSON object with a review_id", file=sys.stderr)
            return 2
        path = lib.save_run_doc(rid, "reconciliation-result", doc)
        print(f"saved: {path}")
        return 0
    if args.cmd == "forget":
        print("forgotten" if lib.forget_run(args.run_id) else "no such run")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
