#!/usr/bin/env python3
"""Standalone tests for the stdlib helpers (no pytest needed): run with python3.

Covers config/hashing, endpoint-keyed consent precedence, the process supervisor
(timeouts, tree kill, stdin-can't-block-the-supervisor), and end-to-end review()
classification via a fake codex backend. Uses a temp IMPASSE_CONFIG_DIR so it never
touches real user config. POSIX assumptions (killpg) — skips the tree tests off POSIX.
"""
from __future__ import annotations

import os
import stat
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

_fails = []


def check(cond, label):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        _fails.append(label)


FAKE_CODEX = r"""#!/usr/bin/env python3
import sys, os
argv = sys.argv[1:]
outp = None
for i, a in enumerate(argv):
    if a == "--output-last-message" and i + 1 < len(argv):
        outp = argv[i + 1]
mode = os.environ.get("FAKE_MODE", "valid")
content = {
    "valid": '{"schema_version":"1.0","review_id":"r","artifact":{"kind":"code","revision":{"algorithm":"sha256","value":"x"}},"assessment":"approve","summary":"ok","findings":[]}',
    "notjson": "this is not json at all",
    "noshape": '{"hello":"world"}',
}.get(mode, "")
import time
time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
if outp and mode != "nowrite":
    open(outp, "w").write(content)
print('{"type":"turn.completed"}')
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
"""


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="impasse-test-")
    os.environ["IMPASSE_CONFIG_DIR"] = tmp
    os.environ.pop("IMPASSE_APPROVE_SEND", None)

    import impasse_lib as lib
    import impasse_consent as consent
    import impasse_run as run

    # --- lib ---
    check(lib.config_dir() == tmp, "config_dir honors IMPASSE_CONFIG_DIR (absolute)")
    check(lib.artifact_revision(b"hello")["value"] and len(lib.artifact_revision(b"hello")["value"]) == 64, "artifact_revision")
    check(lib.normalize_destination("https://api.openai.com/v1") == "https://api.openai.com", "normalize_destination strips path")
    try:
        lib.normalize_destination("https://user:pass@host/x")
        creds_ok = False
    except ValueError:
        creds_ok = True
    check(creds_ok, "normalize_destination rejects embedded credentials")
    try:
        lib.normalize_destination("ftp://h")
        scheme_ok = False
    except ValueError:
        scheme_ok = True
    check(scheme_ok, "normalize_destination rejects non-http(s) scheme")
    check(lib.normalize_destination("https://H:8443/x") == "https://h:8443", "normalize preserves port, lowercases host")
    check(lib._provider_label("https://evil-openai.com.attacker.net") != "OpenAI", "provider_label not fooled by substring host")
    try:
        lib.get_backend("gemini")
        unknown_ok = False
    except ValueError:
        unknown_ok = True
    check(unknown_ok, "get_backend rejects an unknown backend")

    D1 = "https://api.openai.com"
    D2 = "https://azure.example.com"
    be1 = lib.Backend("codex", "codex-cli", "OpenAI", D1, D1, ["/x/codex"])
    be2 = lib.Backend("codex", "codex-cli", "Azure", D2, D2, ["/x/codex"])

    # --- consent: block by default, each approval path, endpoint-keyed ---
    check(consent.check(be1)[0] is False, "consent blocks by default")
    check(consent.check(be1, approve_send=D1)[0] is True, "consent: per-run --approve-send")
    os.environ["IMPASSE_APPROVE_SEND"] = D1
    check(consent.check(be1)[0] is True, "consent: IMPASSE_APPROVE_SEND env")
    os.environ.pop("IMPASSE_APPROVE_SEND")
    consent.grant(D1, "codex-cli", D1, "OpenAI")
    check(consent.check(be1)[0] is True, "consent: persistent grant approves its destination")
    check(consent.check(be2)[0] is False, "consent: a grant for D1 does NOT approve a different endpoint D2")
    st = os.stat(consent.consent_path())
    check(bool(st.st_mode & stat.S_IRUSR) and not (st.st_mode & stat.S_IRWXO), "consent file is user-only")
    check(consent.revoke(D1) is True and consent.check(be1)[0] is False, "consent: revoke")
    m = consent.manifest_for_bytes(b"x" * 4000)
    check(m["total_bytes"] == 4000 and m["digest"].startswith("sha256:"), "manifest_for_bytes has size + digest")

    # consent integrity: a corrupt/wrong-version/symlinked consent file must fall back to BLOCK.
    consent.grant(D1, "codex-cli", D1, "OpenAI")
    with open(consent.consent_path(), "w") as fh:
        fh.write('{"version":2,"grants":[{"destination_id":"' + D1 + '","notice_version":"1"}]}')
    check(consent.check(be1)[0] is False, "consent: wrong-version file falls back to block")
    with open(consent.consent_path(), "w") as fh:
        fh.write("not json at all")
    check(consent.check(be1)[0] is False, "consent: malformed file falls back to block")
    with open(consent.consent_path(), "w") as fh:
        fh.write('{"version":1,"grants":[{"destination_id":"' + D1 + '","notice_version":"0"}]}')
    check(consent.has_grant(D1) is False, "consent: stale notice_version grant does not approve")
    os.remove(consent.consent_path())
    os.symlink(os.path.join(tmp, "nonexistent-target"), consent.consent_path())
    try:
        consent.grant(D1, "codex-cli", D1, "OpenAI")
        symlink_ok = False
    except OSError:
        symlink_ok = True
    check(symlink_ok, "consent: _save refuses to write through a symlink")
    os.remove(consent.consent_path())

    # --- supervisor ---
    r = run.supervise(["bash", "-c", "printf hi"], wall_timeout=10, idle_timeout=5)
    check(r.termination == "completed" and r.exit_code == 0 and r.stdout == b"hi", "supervise: completed + stdout")
    r = run.supervise(["cat"], input_bytes=b"piped-eof", wall_timeout=10, idle_timeout=5)
    check(r.termination == "completed" and r.stdout == b"piped-eof", "supervise: stdin delivered + EOF")

    r = run.supervise(["/definitely/not/a/real/binary/xyz"], wall_timeout=5, idle_timeout=5)
    check(r.termination == "spawn_error" and r.exit_code is None, "supervise: spawn_error on a bad binary")

    try:
        run.supervise(["true"], wall_timeout=0)
        bad_to = False
    except ValueError:
        bad_to = True
    check(bad_to, "supervise: rejects non-positive timeout")

    if os.name == "posix":
        t0 = time.monotonic()
        r = run.supervise(["bash", "-c", "sleep 60"], wall_timeout=100, idle_timeout=2)
        check(r.termination == "idle_timeout" and time.monotonic() - t0 < 15, "supervise: idle_timeout fires")
        t0 = time.monotonic()
        r = run.supervise(["bash", "-c", "while true; do echo tick; sleep 0.3; done"], wall_timeout=2, idle_timeout=100)
        check(r.termination == "wall_timeout" and time.monotonic() - t0 < 15, "supervise: wall_timeout fires")
        t0 = time.monotonic()
        r = run.supervise(["bash", "-c", "sleep 60 & sleep 60"], wall_timeout=2, idle_timeout=100)
        check(r.termination == "wall_timeout" and time.monotonic() - t0 < 20, "supervise: tree-kill returns fast")
        # BLOCKER fix: a big stdin to a process that never reads it must NOT block the supervisor.
        t0 = time.monotonic()
        r = run.supervise(["bash", "-c", "sleep 60"], input_bytes=b"x" * 300000, wall_timeout=100, idle_timeout=2)
        check(r.termination == "idle_timeout" and time.monotonic() - t0 < 15, "supervise: large stdin to a non-reader still times out")
        r = run.supervise(["bash", "-c", "yes | head -c 5000"], max_output_bytes=1000, wall_timeout=10, idle_timeout=5)
        check(r.stdout_truncated is True and len(r.stdout) == 1000, "supervise: output truncated at max_output_bytes")

    # --- review() end-to-end via a fake codex backend ---
    fake = os.path.join(tmp, "fake-codex")
    with open(fake, "w") as f:
        f.write(FAKE_CODEX)
    os.chmod(fake, 0o755)
    os.environ["IMPASSE_CODEX_BIN"] = fake
    os.environ.pop("OPENAI_BASE_URL", None)  # default destination https://api.openai.com

    # consent still enforced first:
    os.environ["FAKE_MODE"] = "valid"
    os.environ["FAKE_EXIT"] = "0"
    res = run.review(kind="code", instruction="review", artifact_bytes=b"code", approve_send=None)
    check(res["ok"] is False and res["failure"]["code"] == "consent_denied", "review: blocked without consent")

    consent.grant("https://api.openai.com", "codex-cli", "https://api.openai.com", "OpenAI")
    res = run.review(kind="code", instruction="review", artifact_bytes=b"code")
    check(res["ok"] is True and res["response"]["schema_version"] == "1.0", "review: valid backend output -> ok, parsed")

    os.environ["FAKE_MODE"] = "notjson"
    res = run.review(kind="code", instruction="review", artifact_bytes=b"code")
    check(res["ok"] is False and res["failure"]["code"] == "invalid_response", "review: non-JSON output -> invalid_response")

    os.environ["FAKE_MODE"] = "noshape"
    res = run.review(kind="code", instruction="review", artifact_bytes=b"code")
    check(res["ok"] is False and res["failure"]["code"] == "invalid_response", "review: wrong-shape JSON -> invalid_response")

    os.environ["FAKE_MODE"] = "valid"
    os.environ["FAKE_EXIT"] = "3"
    res = run.review(kind="code", instruction="review", artifact_bytes=b"code")
    check(res["ok"] is False and res["failure"]["code"] == "backend_error", "review: nonzero exit -> backend_error")

    os.environ["FAKE_MODE"] = "nowrite"
    os.environ["FAKE_EXIT"] = "0"
    res = run.review(kind="code", instruction="review", artifact_bytes=b"code")
    check(res["ok"] is False and res["failure"]["code"] == "invalid_response", "review: no final message -> invalid_response")

    if os.name == "posix":
        os.environ["FAKE_MODE"] = "valid"
        os.environ["FAKE_SLEEP"] = "5"
        res = run.review(kind="code", instruction="review", artifact_bytes=b"code", wall_timeout=1, idle_timeout=100)
        check(res["ok"] is False and res["failure"]["code"] == "timeout", "review: backend exceeding wall -> timeout")
        os.environ.pop("FAKE_SLEEP", None)

    try:
        run.review(kind="code", instruction="x", artifact_bytes=b"a", effort="minimal")
        bad_eff = False
    except ValueError:
        bad_eff = True
    check(bad_eff, "review: rejects disallowed effort ('minimal')")

    # --- run records (audit trail) + report ---
    import json as _json
    import impasse_report as report
    os.environ["FAKE_MODE"] = "valid"
    os.environ["FAKE_EXIT"] = "0"
    res = run.review(kind="code", instruction="review", artifact_bytes=b"code")
    check(res["ok"] and res.get("recorded") is True, "run record: review persists a record by default")
    check(lib.load_run("r")["reviewer_response"] is not None, "run record: reviewer-response is loadable")
    res = run.review(kind="code", instruction="review", artifact_bytes=b"code", no_record=True)
    check(res.get("recorded") is False, "run record: --no-record skips persistence")

    drid = _json.load(open("schemas/examples/decision.reviewer-response.json"))["review_id"]
    lib.save_run_doc(drid, "reviewer-response", _json.load(open("schemas/examples/decision.reviewer-response.json")))
    lib.save_run_doc(drid, "reconciliation-result", _json.load(open("schemas/examples/decision.reconciliation-result.json")))
    out = report.render(lib.load_run(drid))
    check("Decisions:" in out and "escalated to you" in out, "report: renders the decisions tally")
    check("reviewer ▶" in out and "you      ◀" in out, "report: shows the reviewer/host back-and-forth")
    check("Question for you" in out and "decision(s) need you" in out, "report: shows the escalated question")
    check(any(r["run_id"] == drid for r in lib.list_runs()), "run record: listed by list_runs")
    check(lib.forget_run(drid) is True and lib.load_run(drid)["reviewer_response"] is None, "run record: forget deletes it")

    # --- housekeeping: open-escalation detection + prune ---
    open_rec = {"schema_version": "1.0", "reconciliation_id": "x", "review_id": "open-run",
                "outcome": "deadlocked", "items": [{"finding_id": "F001", "state": "deadlocked",
                "escalation": {"dispute_kind": "value_or_priority_tradeoff",
                               "stop_reason": "operator_authority_required", "operator_question": "pick one?"}}]}
    lib.save_run_doc("open-run", "reconciliation-result", open_rec)
    check(any(r["run_id"] == "open-run" for r in report.open_runs()), "housekeeping: open_runs detects an unresolved escalation")
    resolved_rec = {"schema_version": "1.0", "reconciliation_id": "x", "review_id": "open-run",
                    "outcome": "converged", "items": [{"finding_id": "F001", "state": "resolved", "resolution": "decided"}]}
    lib.save_run_doc("open-run", "reconciliation-result", resolved_rec)
    check(not any(r["run_id"] == "open-run" for r in report.open_runs()), "housekeeping: resolving clears the open flag")
    old = time.time() - 3 * 86400
    os.utime(os.path.join(lib.runs_dir(), "open-run"), (old, old))
    deleted, _kept = report.prune(1)
    check("open-run" in deleted, "housekeeping: prune deletes an old resolved record")
    lib.save_run_doc("old-open", "reconciliation-result", open_rec)
    os.utime(os.path.join(lib.runs_dir(), "old-open"), (old, old))
    deleted2, kept2 = report.prune(1)
    check("old-open" in kept2 and "old-open" not in deleted2, "housekeeping: prune KEEPS old runs with open escalations")
    deleted3, _k = report.prune(1, include_open=True)
    check("old-open" in deleted3, "housekeeping: prune --include-open removes even open runs")

    print()
    if _fails:
        print(f"{len(_fails)} FAILURES: " + "; ".join(_fails))
        return 1
    print("all helper tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
