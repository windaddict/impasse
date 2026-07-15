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


_VALID_REVIEW = ('{"schema_version":"1.0","review_id":"cr","artifact":{"kind":"decision",'
                 '"revision":{"algorithm":"sha256","value":"x"}},"assessment":"needs_attention",'
                 '"summary":"s","findings":[]}')

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import sys, os
try:
    sys.stdin.buffer.read()   # drain the piped artifact (reaches EOF)
except Exception:
    pass
mode = os.environ.get("FAKE_CLAUDE_MODE", "valid")
valid = ''' + repr(_VALID_REVIEW) + r'''
out = {
    "valid": valid,
    "fenced": "```json\n" + valid + "\n```",
    "preamble": "Here is my review:\n\n" + valid,   # chat backends sometimes prepend prose
    "notjson": "I could not produce JSON.",
}.get(mode, valid)
sys.stdout.write(out)
sys.exit(int(os.environ.get("FAKE_CLAUDE_EXIT", "0")))
'''


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

    # --- reviewer stance is runner-enforced (anti-self-preference), not left to the host ---
    fi = run.compose_full_instruction("EVALUATE THE MEMO", schema_text='{"type":"object"}')
    check("no stake" in fi and "prompt injection" in fi, "compose: prepends the invariant no-stake / prompt-injection stance")
    check("believe you produced it" in fi, "compose: hardens against self-preference even if the reviewer thinks it authored the artifact")
    check(fi.index("no stake") < fi.index("EVALUATE THE MEMO"), "compose: stance precedes the host's task lens")
    check("JSON Schema" in fi and fi.index("EVALUATE THE MEMO") < fi.index("JSON Schema"), "compose: schema appended after the host instruction")
    check(run.compose_full_instruction("X").endswith("X"), "compose: no schema block when schema omitted")

    # --- Claude fallback backend: resolution, backend metadata, argv, tolerant parsing, e2e ---
    check(lib._provider_label("https://api.anthropic.com") == "Anthropic", "provider_label: Anthropic host")
    check(lib._provider_label("https://api.anthropic.com.evil.net") != "Anthropic", "provider_label: not fooled by Anthropic substring host")
    fake_claude = os.path.join(tmp, "fake-claude")
    with open(fake_claude, "w") as f:
        f.write(FAKE_CLAUDE)
    os.chmod(fake_claude, 0o755)
    os.environ["IMPASSE_CLAUDE_BIN"] = fake_claude
    os.environ.pop("ANTHROPIC_BASE_URL", None)
    be_c = lib.get_backend("claude")
    check(be_c.type == "claude-cli" and be_c.provider == "Anthropic", "get_backend('claude'): type + provider")
    check(be_c.independence == "same_provider" and be_c.destination_id == "https://api.anthropic.com", "get_backend('claude'): same-provider tier + Anthropic destination")
    for ev in ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"):
        os.environ[ev] = "1"
        routed_ok = False
        try:
            lib.get_backend("claude")
        except ValueError:
            routed_ok = True
        os.environ.pop(ev, None)
        check(routed_ok, f"get_backend('claude'): refuses under {ev} (would mis-key consent to Anthropic)")
    argv_c = run.build_claude_argv(be_c.command, instruction="LENS")
    check("-p" in argv_c and "LENS" in argv_c and argv_c[argv_c.index("--output-format") + 1] == "text", "build_claude_argv: -p + text output")
    check("--output-last-message" not in argv_c, "build_claude_argv: no output-file (reads stdout)")
    check(argv_c[argv_c.index("--allowed-tools") + 1] == "", "build_claude_argv: empty allowlist (fails closed)")
    check("--strict-mcp-config" in argv_c and argv_c[argv_c.index("--permission-mode") + 1] == "default", "build_claude_argv: strict MCP + pinned default permission mode")
    check(all(t in argv_c for t in ("Bash", "WebFetch", "WebSearch", "Task")), "build_claude_argv: denylist covers exec + exfil + spawn (defense in depth)")
    check(argv_c.index("--disallowed-tools") == len(argv_c) - 1 - len(run._CLAUDE_DENIED_TOOLS), "build_claude_argv: variadic --disallowed-tools comes last")
    argv_x = run.build_codex_argv(["/x/codex"], instruction="INSTR", output_last_message="/tmp/o", effort="low", model="gpt-x")
    check("--ignore-user-config" in argv_x and "--ignore-rules" in argv_x, "build_codex_argv: hermetic (ignores user config + repo rules) by default")
    check(argv_x[argv_x.index("-m") + 1] == "gpt-x" and argv_x[-1] == "INSTR", "build_codex_argv: -m model set, instruction stays the final positional")
    os.environ["IMPASSE_CODEX_RESPECT_CONFIG"] = "1"
    check("--ignore-user-config" not in run.build_codex_argv(["/x/codex"], instruction="I", output_last_message="/tmp/o"), "build_codex_argv: IMPASSE_CODEX_RESPECT_CONFIG opts out of hermetic mode")
    os.environ.pop("IMPASSE_CODEX_RESPECT_CONFIG", None)
    argv_cm = run.build_claude_argv(["/x/claude"], instruction="I", model="claude-x")
    check(argv_cm[argv_cm.index("--model") + 1] == "claude-x" and argv_cm.index("--model") < argv_cm.index("--disallowed-tools"), "build_claude_argv: --model set, before the trailing --disallowed-tools")
    check(run._parse_reviewer_json('```json\n{"a":1}\n```')["a"] == 1, "parse: strips a ```json fence")
    check(run._parse_reviewer_json('here you go:\n{"a":2} thanks')["a"] == 2, "parse: extracts JSON from surrounding prose")
    check(run._parse_reviewer_json('{"note":"a } brace inside","a":3}')["a"] == 3, "parse: string-aware — a brace inside a string value doesn't end the object")
    check(run._parse_reviewer_json('prefix {"a":4} tail }')["a"] == 4, "parse: balanced scan ignores a stray trailing brace")
    parse_bad = False
    try:
        run._parse_reviewer_json("no json here at all")
    except ValueError:   # JSONDecodeError is a ValueError subclass
        parse_bad = True
    check(parse_bad, "parse: non-JSON raises (classified invalid_response, never a false pass)")

    consent.grant("https://api.anthropic.com", "claude-cli", "https://api.anthropic.com", "Anthropic")
    os.environ["FAKE_CLAUDE_MODE"] = "valid"
    res = run.review(kind="decision", instruction="review", artifact_bytes=b"memo", backend="claude")
    check(res["ok"] and res["response"]["schema_version"] == "1.0", "review(claude): valid stdout JSON -> ok, parsed")
    check(res.get("independence") == "same_provider" and "Same-provider" in (res.get("independence_notice") or ""), "review(claude): surfaces the same-provider independence notice")
    check(res.get("backend") == "claude" and res.get("provider") == "Anthropic", "review(claude): result names the backend + provider")
    os.environ["FAKE_CLAUDE_MODE"] = "preamble"
    res = run.review(kind="decision", instruction="review", artifact_bytes=b"memo", backend="claude", no_record=True)
    check(res["ok"], "review(claude): tolerant parse survives a chat preamble before the JSON")
    os.environ["FAKE_CLAUDE_MODE"] = "notjson"
    res = run.review(kind="decision", instruction="review", artifact_bytes=b"memo", backend="claude", no_record=True)
    check(res["ok"] is False and res["failure"]["code"] == "invalid_response", "review(claude): non-JSON stdout -> invalid_response")
    # a fresh Anthropic-only backend (no per-run approve_send) is approved by the persistent grant,
    # while the separate OpenAI grant is untouched
    check(consent.check(lib.get_backend("claude"))[0] is True, "review(claude): persistent Anthropic grant approves; codex grant is separate")
    os.environ["FAKE_MODE"] = "valid"
    os.environ["FAKE_EXIT"] = "0"
    codex_only = run.review(kind="code", instruction="review", artifact_bytes=b"code")
    check(codex_only.get("independence") == "cross_provider" and codex_only.get("independence_notice") is None, "review(codex): cross-provider, no downgrade notice")
    os.environ.pop("FAKE_CLAUDE_MODE", None)

    # --- environment-aware review-mode policy + self-review tier ---
    os.environ["IMPASSE_ENV"] = "cowork"
    check(lib.detect_environment() == "cowork", "detect_environment: IMPASSE_ENV overrides")
    os.environ.pop("IMPASSE_ENV", None)
    check(lib.self_review_allowed("chat_sandbox") and lib.self_review_allowed("cowork"), "self_review_allowed: sandbox + cowork")
    check(not lib.self_review_allowed("claude_code") and not lib.self_review_allowed("unknown"), "self_review_allowed: NOT in Claude Code or unknown")
    note = lib.self_review_notice("chat_sandbox")
    check("NOT an independent" in note and "Claude Code is the best" in note, "self_review_notice: discloses non-independence + recommends Claude Code")
    m = lib.review_mode("decision", environment="claude_code", codex_available=True)
    check(m["mode"] == "codex" and m["tier"] == "cross_provider" and m["recommendation"] is None, "review_mode: Codex in Claude Code -> cross_provider, no nag")
    m = lib.review_mode("decision", environment="claude_code", claude_available=True)
    check(m["mode"] == "claude" and m["tier"] == "same_provider", "review_mode: only Claude available -> same_provider")
    m = lib.review_mode("decision", environment="chat_sandbox")
    check(m["mode"] == "self_review" and m["notice"] and m["recommendation"], "review_mode: no backend in sandbox -> self_review + disclosure")
    m = lib.review_mode("code", environment="chat_sandbox")
    check(m["mode"] == "refuse" and not m["allowed"], "review_mode: code refused in the sandbox (verification impossible)")
    m = lib.review_mode("decision", environment="claude_code")
    check(m["mode"] == "refuse", "review_mode: no backend in Claude Code -> refuse (never self-review here)")
    m = lib.review_mode("document", environment="cowork")
    check(m["mode"] == "self_review", "review_mode: Cowork with no backend -> self_review")
    m = lib.review_mode("decision", environment="unknown")
    check(m["mode"] == "refuse", "review_mode: unknown surface -> refuse (fail safe)")
    m = lib.review_mode("decision", environment="cowork", codex_available=True)
    check(m["mode"] == "codex", "review_mode: capability-first — a backend in Cowork beats self-review")

    # --- run records (audit trail) + report ---
    import json as _json
    import impasse_report as report
    os.environ["FAKE_MODE"] = "valid"
    os.environ["FAKE_EXIT"] = "0"
    res = run.review(kind="code", instruction="review", artifact_bytes=b"code")
    check(res["ok"] and res.get("recorded") is True, "run record: review persists a record by default")
    os.environ["IMPASSE_CODEX_MODEL"] = "persisted-x"
    rm = run.review(kind="code", instruction="review", artifact_bytes=b"code", no_record=True)
    check(rm.get("model") == "persisted-x", "review: persisted IMPASSE_CODEX_MODEL resolved into the run")
    rm = run.review(kind="code", instruction="review", artifact_bytes=b"code", model="perrun-x", no_record=True)
    check(rm.get("model") == "perrun-x", "review: per-run --model overrides the env default")
    os.environ.pop("IMPASSE_CODEX_MODEL", None)
    # persisted default model (settings.json) + full resolution order
    check(lib.get_default_model("codex") is None, "settings: no persisted model by default")
    lib.set_default_model("codex", "persist-model-y")
    check(lib.get_default_model("codex") == "persist-model-y", "settings: set/get persisted default model")
    rm = run.review(kind="code", instruction="review", artifact_bytes=b"code", no_record=True)
    check(rm.get("model") == "persist-model-y", "review: persisted default resolves when no flag/env")
    os.environ["IMPASSE_CODEX_MODEL"] = "env-model-z"
    rm = run.review(kind="code", instruction="review", artifact_bytes=b"code", no_record=True)
    check(rm.get("model") == "env-model-z", "review: env var beats the persisted default")
    rm = run.review(kind="code", instruction="review", artifact_bytes=b"code", model="flag-x", no_record=True)
    check(rm.get("model") == "flag-x", "review: per-run --model beats env and persisted")
    os.environ.pop("IMPASSE_CODEX_MODEL", None)
    lib.set_default_model("codex", None)
    check(lib.get_default_model("codex") is None, "settings: clear persisted default model")
    check(lib.load_run("r")["reviewer_response"] is not None, "run record: reviewer-response is loadable")
    check(res.get("record_path") and "Recorded locally" in (res.get("record_notice") or ""), "run record: result surfaces where it was saved")
    res = run.review(kind="code", instruction="review", artifact_bytes=b"code", no_record=True)
    check(res.get("recorded") is False, "run record: --no-record skips persistence")
    check(res.get("record_notice") == "Not recorded (--no-record).", "run record: --no-record notice surfaced")

    drid = _json.load(open("schemas/examples/decision.reviewer-response.json"))["review_id"]
    lib.save_run_doc(drid, "reviewer-response", _json.load(open("schemas/examples/decision.reviewer-response.json")))
    lib.save_run_doc(drid, "reconciliation-result", _json.load(open("schemas/examples/decision.reconciliation-result.json")))
    out = report.render(lib.load_run(drid))
    check("Decisions:" in out and "escalated to you" in out, "report: renders the decisions tally")
    check("reviewer ▶" in out and "you      ◀" in out, "report: shows the reviewer/host back-and-forth")
    check("Question for you" in out and "decision(s) need you" in out, "report: shows the escalated question")
    check(any(r["run_id"] == drid for r in lib.list_runs()), "run record: listed by list_runs")

    # --- lifetime recap: aggregate value across reconciled runs (isolated config dir) ---
    recap_dir = tempfile.mkdtemp(prefix="impasse-recap-")
    _prev_cfg = os.environ["IMPASSE_CONFIG_DIR"]
    os.environ["IMPASSE_CONFIG_DIR"] = recap_dir
    check(report.lifetime_recap() == "", "recap: empty when nothing reconciled")
    rec_a = {"schema_version": "1.0", "reconciliation_id": "a", "review_id": "recap-a",
             "outcome": "deadlocked", "items": [
                 {"finding_id": "F1", "state": "accepted"},
                 {"finding_id": "F2", "state": "rejected"},
                 {"finding_id": "F3", "state": "deadlocked",
                  "escalation": {"dispute_kind": "value_or_priority_tradeoff",
                                 "stop_reason": "operator_authority_required", "operator_question": "q?"}}]}
    rec_b = {"schema_version": "1.0", "reconciliation_id": "b", "review_id": "recap-b",
             "outcome": "converged", "items": [
                 {"finding_id": "F1", "state": "accepted"},
                 {"finding_id": "F2", "state": "resolved", "resolution": "done"}]}
    lib.save_run_doc("recap-a", "reconciliation-result", rec_a)
    lib.save_run_doc("recap-b", "reconciliation-result", rec_b)
    recap = report.lifetime_recap()
    check("2 reviews reconciled" in recap, "recap: counts reconciled runs")
    check("5 findings reviewed" in recap and "2 accepted" in recap, "recap: sums findings + accepted")
    check("1 refuted with evidence" in recap and "1 resolved" in recap and "1 escalated to you" in recap, "recap: resolved and escalated counted separately (not conflated)")
    lib.save_run_doc("recap-review-only", "reviewer-response",
                     {"schema_version": "1.0", "review_id": "recap-review-only",
                      "artifact": {"kind": "code", "revision": {"algorithm": "sha256", "value": "x"}},
                      "assessment": "approve", "summary": "s", "findings": []})
    check("2 reviews reconciled" in report.lifetime_recap(), "recap: review-only runs don't inflate the count")
    os.environ["IMPASSE_CONFIG_DIR"] = _prev_cfg

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

    # --- hardening fixes surfaced by the cross-provider code audit ---
    check(lib._safe_id("..") == "unknown" and lib._safe_id(".") == "unknown", "safe_id: '.'/'..' collapse to 'unknown' (no traversal)")
    check("/" not in lib._safe_id("a/b/../../etc") and lib._safe_id("a/b") == "a_b", "safe_id: path separators collapsed")
    lib.save_run_doc("../evil", "reviewer-response", {"schema_version": "1.0", "review_id": "../evil", "findings": []})
    escaped = os.path.join(os.path.dirname(lib.runs_dir()), "evil")
    check(os.path.isdir(os.path.join(lib.runs_dir(), lib._safe_id("../evil"))) and not os.path.exists(escaped), "save_run_doc: a traversal review_id stays inside runs_dir")
    lib.forget_run("../evil")
    check(report._clean("a\x1b[31mX\x1b[0m\x07b") == "a[31mX[0mb", "report: strips ANSI/control escapes from untrusted reviewer text")
    check(lib.review_mode("CODE", environment="chat_sandbox")["mode"] == "refuse", "review_mode: 'CODE' normalized -> still refused in the sandbox")

    print()
    if _fails:
        print(f"{len(_fails)} FAILURES: " + "; ".join(_fails))
        return 1
    print("all helper tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
