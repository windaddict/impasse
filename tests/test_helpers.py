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
import sys, os, json, time
argv = sys.argv[1:]
outp = None
for i, a in enumerate(argv):
    if a == "--output-last-message" and i + 1 < len(argv):
        outp = argv[i + 1]
mode = os.environ.get("FAKE_MODE", "valid")
time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))

cf_all = os.environ.get("FAKE_COUNT_ALL")   # counts EVERY invocation (proves retry behavior)
if cf_all:
    n_all = 0
    if os.path.exists(cf_all):
        try:
            n_all = int(open(cf_all).read() or "0")
        except Exception:
            n_all = 0
    open(cf_all, "w").write(str(n_all + 1))

def emit_error(status, message):
    inner = json.dumps({"type": "error", "status": status, "error": {"type": "x", "message": message}})
    print(json.dumps({"type": "turn.failed", "error": {"message": inner}}))
    sys.exit(1)

if mode == "unavailable_then_ok":   # fail on the first attempt, succeed after (proves retry recovery)
    cf = os.environ.get("FAKE_COUNTER")
    n = 0
    if cf and os.path.exists(cf):
        try:
            n = int(open(cf).read() or "0")
        except Exception:
            n = 0
    n += 1
    if cf:
        open(cf, "w").write(str(n))
    if n == 1:
        emit_error(503, "The service is temporarily unavailable, please try again.")
    mode = "valid"
def bump_counter():
    cf = os.environ.get("FAKE_COUNTER")
    n = 0
    if cf and os.path.exists(cf):
        try:
            n = int(open(cf).read() or "0")
        except Exception:
            n = 0
    n += 1
    if cf:
        open(cf, "w").write(str(n))
    return n

if mode == "badjson_then_ok":   # malformed final message once, valid on retry (issue #1)
    mode = "badjson" if bump_counter() == 1 else "valid"
if mode == "unavailable_then_badjson_then_ok":   # both retry budgets consumed independently
    n = bump_counter()
    if n == 1:
        emit_error(503, "The service is temporarily unavailable, please try again.")
    mode = "badjson" if n == 2 else "valid"
if mode == "badjson_then_nowrite":   # proves per-attempt truncation: retry must NOT see attempt 1's file
    mode = "badjson" if bump_counter() == 1 else "nowrite"
if mode == "noise_stderr_unavailable":   # exit nonzero with NO error event; "unavailable" only in stderr noise
    sys.stderr.write("warning: connection temporarily unavailable during an unrelated step\n")
    sys.exit(1)
if mode == "ratelimit":
    emit_error(429, "Rate limit reached for your account. Please try again later.")
if mode == "unavailable":
    emit_error(503, "Service is overloaded, temporarily unavailable.")
if mode == "authfail":
    emit_error(401, "You are not logged in. Please log in with codex login.")

content = {
    "valid": '{"schema_version":"1.0","review_id":"r","artifact":{"kind":"code","revision":{"algorithm":"sha256","value":"x"}},"assessment":"approve","summary":"ok","findings":[]}',
    "notjson": "this is not json at all",
    "noshape": '{"hello":"world"}',
    "badjson": '{"schema_version":"1.0","review_id":"r","findings":[{"id":"F001" "claim":"missing comma"}]}',
}.get(mode, "")
if mode == "oversize":   # a final message that cannot FIT — a retry can't fix this
    content = '{"schema_version":"1.0","findings":[]}' + " " * 2000001
if mode == "oversize_utf8":   # >2MB of BYTES but <2MB of CHARACTERS — a char-count check misses it
    content = '{"schema_version":"1.0","findings":[]}' + "é" * 1100000
if outp and mode != "nowrite":
    open(outp, "w", encoding="utf-8").write(content)
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

cf_all = os.environ.get("FAKE_COUNT_ALL")   # counts EVERY invocation (proves retry behavior)
if cf_all:
    n_all = 0
    if os.path.exists(cf_all):
        try:
            n_all = int(open(cf_all).read() or "0")
        except Exception:
            n_all = 0
    open(cf_all, "w").write(str(n_all + 1))

if mode == "notjson_then_ok":   # malformed stdout once, valid on retry (issue #1, claude path)
    cf = os.environ.get("FAKE_COUNTER")
    n = 0
    if cf and os.path.exists(cf):
        try:
            n = int(open(cf).read() or "0")
        except Exception:
            n = 0
    n += 1
    if cf:
        open(cf, "w").write(str(n))
    mode = "notjson" if n == 1 else "valid"

valid = ''' + repr(_VALID_REVIEW) + r'''
out = {
    "valid": valid,
    "fenced": "```json\n" + valid + "\n```",
    "preamble": "Here is my review:\n\n" + valid,   # chat backends sometimes prepend prose
    "notjson": "I could not produce JSON.",
}.get(mode, valid)
if mode == "oversize":     # within the capture cap but over the 2MB final-message bound
    out = valid + " " * 2000001
if mode == "hugestdout":   # breaches the supervisor's 8MB capture cap itself
    out = "x" * 8100000
sys.stdout.write(out)
sys.exit(int(os.environ.get("FAKE_CLAUDE_EXIT", "0")))
'''


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="impasse-test-")
    os.environ["IMPASSE_CONFIG_DIR"] = tmp
    os.environ.pop("IMPASSE_APPROVE_SEND", None)
    # Ambient Impasse/backend configuration must not leak into assertions — a user's own
    # IMPASSE_CODEX_MODEL or a custom base URL would otherwise break the suite. Standalone
    # process: clear, don't bother restoring.
    for _v in ("IMPASSE_HOST", "IMPASSE_ENV", "IMPASSE_CODEX_MODEL", "IMPASSE_CODEX_EFFORT",
               "IMPASSE_CLAUDE_MODEL", "IMPASSE_CLAUDE_EFFORT", "IMPASSE_CODEX_RESPECT_CONFIG",
               "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "OPENAI_BASE_URL",
               "ANTHROPIC_BASE_URL", "FAKE_COUNT_ALL", "FAKE_COUNTER"):
        os.environ.pop(_v, None)
    # Independence tiers are host-relative; every legacy assertion below reads them from a Claude
    # host's perspective. Pin it so the suite is deterministic in CI (no Claude markers there —
    # an unknown host is 'undetermined', not the historical labels). The host-relative block
    # below manages its own host identity.
    os.environ["IMPASSE_HOST"] = "claude"

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

    # --- backend error classification + transient-retry recovery (limits / outages) ---
    _orig_sleep = run.time.sleep
    run.time.sleep = lambda *a, **k: None   # don't actually wait during retry tests
    os.environ["FAKE_EXIT"] = "0"
    os.environ["FAKE_MODE"] = "ratelimit"
    res = run.review(kind="code", instruction="review", artifact_bytes=b"code", no_record=True)
    check(res["ok"] is False and res["failure"]["code"] == "rate_limited" and res["failure"].get("retryable") is True, "review: 429 -> rate_limited (retryable), surfaced not auto-retried")
    check("rate limit" in res["failure"]["message"].lower(), "review: failure carries the REAL error message, not stderr noise")
    os.environ["FAKE_MODE"] = "authfail"
    res = run.review(kind="code", instruction="review", artifact_bytes=b"code", no_record=True)
    check(res["failure"]["code"] == "auth_error" and res["failure"].get("retryable") is False, "review: 401 -> auth_error (not retryable)")
    os.environ["FAKE_MODE"] = "unavailable"
    res = run.review(kind="code", instruction="review", artifact_bytes=b"code", no_record=True)
    check(res["failure"]["code"] == "service_unavailable", "review: 503 -> service_unavailable")
    # trusted-gate: stderr NOISE containing "unavailable" (no error event) must NOT become retryable
    check(run._classify_backend_error(None, "temporarily unavailable", trusted=False) == ("backend_error", False), "classify: untrusted stderr text -> backend_error (no retry)")
    check(run._classify_backend_error(503, "overloaded", trusted=True) == ("service_unavailable", True), "classify: trusted 503 -> service_unavailable (retryable)")
    os.environ["FAKE_MODE"] = "noise_stderr_unavailable"
    res = run.review(kind="code", instruction="review", artifact_bytes=b"code", no_record=True)
    check(res["failure"]["code"] == "backend_error" and not res["failure"].get("retryable"), "review: stderr noise with 'unavailable' stays backend_error (no wasted retries)")
    counter = os.path.join(tmp, "fake-counter")
    if os.path.exists(counter):
        os.remove(counter)
    os.environ["FAKE_MODE"] = "unavailable_then_ok"
    os.environ["FAKE_COUNTER"] = counter
    res = run.review(kind="code", instruction="review", artifact_bytes=b"code", no_record=True)
    check(res["ok"] is True and res["response"]["schema_version"] == "1.0", "review: transient outage auto-recovers on retry")
    os.environ.pop("FAKE_COUNTER", None)

    # --- issue #1: stochastically malformed reviewer output is retried once + retryable hint ---
    cnt = os.path.join(tmp, "fake-count-all")
    if os.path.exists(counter):
        os.remove(counter)
    os.environ["FAKE_MODE"] = "badjson_then_ok"
    os.environ["FAKE_COUNTER"] = counter
    res = run.review(kind="code", instruction="review", artifact_bytes=b"code", no_record=True)
    check(res["ok"] is True and res["response"]["schema_version"] == "1.0",
          "review: malformed JSON on attempt 1 auto-retries and recovers (issue #1)")
    os.environ.pop("FAKE_COUNTER", None)
    os.environ["FAKE_COUNT_ALL"] = cnt
    os.environ["FAKE_MODE"] = "badjson"
    res = run.review(kind="code", instruction="review", artifact_bytes=b"code", no_record=True)
    check(res["ok"] is False and res["failure"]["code"] == "invalid_response"
          and res["failure"].get("retryable") is True,
          "review: persistently malformed JSON -> invalid_response with retryable: true")
    check(int(open(cnt).read()) == 2, "review: malformed output retried exactly once (2 invocations)")
    os.remove(cnt)
    os.environ["FAKE_MODE"] = "noshape"
    res = run.review(kind="code", instruction="review", artifact_bytes=b"code", no_record=True)
    check(res["failure"]["code"] == "invalid_response" and res["failure"].get("retryable") is True
          and int(open(cnt).read()) == 2,
          "review: wrong-shape JSON also retried once, then retryable: true")
    os.remove(cnt)
    os.environ["FAKE_MODE"] = "nowrite"
    res = run.review(kind="code", instruction="review", artifact_bytes=b"code", no_record=True)
    check(res["failure"]["code"] == "invalid_response" and res["failure"].get("retryable") is True
          and int(open(cnt).read()) == 2,
          "review: empty final message also retried once, then retryable: true")
    os.remove(cnt)
    # size-bound failures: retryable: TRUE (an offer, like rate_limited) but never AUTO-retried,
    # and the message carries the remedy — operator ruling on finding F002, 2026-07-16
    os.environ["FAKE_MODE"] = "oversize"
    res = run.review(kind="code", instruction="review", artifact_bytes=b"code", no_record=True)
    check(res["failure"]["code"] == "invalid_response" and res["failure"].get("retryable") is True
          and int(open(cnt).read()) == 1 and "shrinking the artifact" in res["failure"]["message"],
          "review: oversize -> retryable hint (offer) + remedy in message, but NO auto-retry spend")
    os.remove(cnt)
    # the 2MB bound is enforced on BYTES: >2MB of multi-byte UTF-8 is <2MB of characters, and a
    # char-count check would silently parse (and accept!) a truncated prefix
    os.environ["FAKE_MODE"] = "oversize_utf8"
    res = run.review(kind="code", instruction="review", artifact_bytes=b"code", no_record=True)
    check(res["failure"]["code"] == "invalid_response" and "exceeds" in res["failure"]["message"]
          and int(open(cnt).read()) == 1,
          "review: multi-byte UTF-8 oversize caught by the BYTE bound (not fooled by char count)")
    os.remove(cnt)
    # the outage retry ceiling is pinned, not just eventual recovery
    os.environ["FAKE_MODE"] = "unavailable"
    res = run.review(kind="code", instruction="review", artifact_bytes=b"code", no_record=True)
    check(res["failure"]["code"] == "service_unavailable" and int(open(cnt).read()) == 3,
          "review: persistent outage stops after exactly 2 retries (3 invocations)")
    os.remove(cnt)
    # the two retry budgets are independent: an outage retry doesn't consume the output retry
    if os.path.exists(counter):
        os.remove(counter)
    os.environ["FAKE_MODE"] = "unavailable_then_badjson_then_ok"
    os.environ["FAKE_COUNTER"] = counter
    res = run.review(kind="code", instruction="review", artifact_bytes=b"code", no_record=True)
    check(res["ok"] is True and int(open(cnt).read()) == 3,
          "review: outage then malformed output recovers — the budgets are independent")
    os.remove(cnt)
    os.remove(counter)
    # per-attempt truncation: the retry must never re-read attempt 1's stale final message
    os.environ["FAKE_MODE"] = "badjson_then_nowrite"
    res = run.review(kind="code", instruction="review", artifact_bytes=b"code", no_record=True)
    check(res["failure"]["code"] == "invalid_response" and "no final message" in res["failure"]["message"],
          "review: retry never reads a prior attempt's stale output (out_last truncated per attempt)")
    os.environ.pop("FAKE_COUNTER", None)
    os.environ.pop("FAKE_COUNT_ALL", None)
    run.time.sleep = _orig_sleep
    os.environ["FAKE_MODE"] = "valid"
    os.environ["FAKE_EXIT"] = "0"

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
    check(argv_x[argv_x.index("-c") + 1] == 'model_reasoning_effort="low"', "build_codex_argv: effort -> -c model_reasoning_effort")
    check("-c" not in run.build_codex_argv(["/x/codex"], instruction="I", output_last_message="/tmp/o"), "build_codex_argv: no effort -> flag omitted (backend default)")
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

    # --- the claude transport path (stdout, capture cap) carries the SAME retry/size contract ---
    cnt_c = os.path.join(tmp, "fake-count-claude")
    ctr_c = os.path.join(tmp, "fake-counter-claude")
    os.environ["FAKE_COUNT_ALL"] = cnt_c
    os.environ["FAKE_CLAUDE_MODE"] = "notjson_then_ok"
    os.environ["FAKE_COUNTER"] = ctr_c
    res = run.review(kind="decision", instruction="review", artifact_bytes=b"memo", backend="claude", no_record=True)
    check(res["ok"] is True and int(open(cnt_c).read()) == 2,
          "review(claude): malformed stdout recovers on the single output retry")
    os.environ.pop("FAKE_COUNTER", None)
    os.remove(cnt_c)
    os.environ["FAKE_CLAUDE_MODE"] = "notjson"
    res = run.review(kind="decision", instruction="review", artifact_bytes=b"memo", backend="claude", no_record=True)
    check(res["failure"]["code"] == "invalid_response" and res["failure"].get("retryable") is True
          and int(open(cnt_c).read()) == 2,
          "review(claude): persistent malformed stdout -> retryable true after exactly one retry")
    os.remove(cnt_c)
    os.environ["FAKE_CLAUDE_MODE"] = "oversize"
    res = run.review(kind="decision", instruction="review", artifact_bytes=b"memo", backend="claude", no_record=True)
    check(res["failure"]["code"] == "invalid_response" and res["failure"].get("retryable") is True
          and int(open(cnt_c).read()) == 1 and "--effort" not in res["failure"]["message"],
          "review(claude): oversize stdout -> retryable hint, no auto-retry, no effort remedy (claude has none)")
    os.remove(cnt_c)
    os.environ["FAKE_CLAUDE_MODE"] = "hugestdout"
    res = run.review(kind="decision", instruction="review", artifact_bytes=b"memo", backend="claude", no_record=True)
    check(res["failure"]["code"] == "invalid_response" and res["failure"].get("retryable") is True
          and "capture cap" in res["failure"]["message"] and int(open(cnt_c).read()) == 1,
          "review(claude): capture-cap breach -> retryable hint, no auto-retry")
    os.remove(cnt_c)
    os.environ.pop("FAKE_COUNT_ALL", None)
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
    resr = run.review(kind="code", instruction="review", artifact_bytes=b"code", raw=True)
    check(resr["ok"] and resr.get("raw") is True and resr.get("recorded") is False, "raw: --raw returns findings, marks raw, does NOT record")
    check(resr.get("record_notice") == "Not recorded (raw mode).", "raw: notice says raw mode")
    check("UNVERIFIED" in report.render_findings(resr["response"]), "raw: render_findings labels output UNVERIFIED")
    check("\x1b" not in report.render_findings({"findings": [{"id": "F\x1b[31m", "severity": "high", "claim": "c"}]}), "raw: render_findings sanitizes untrusted text")
    # F001: a truthy non-list `findings` (malformed reviewer output) must not crash the render.
    for bad in ("looks fine", 5, {"a": 1}, ["x", "y"]):
        try:
            report.render_findings({"findings": bad})
            _ok = True
        except Exception:
            _ok = False
        check(_ok, f"raw: render_findings tolerates non-list/non-dict findings ({type(bad).__name__})")
    # F002: empty findings + a non-approving assessment must NOT be labeled "approved".
    _r = report.render_findings({"assessment": "needs_attention", "findings": []})
    check("approved" not in _r and "not an approval" in _r, "raw: empty findings + needs_attention is not called approved")
    check("approved" in report.render_findings({"assessment": "approve", "findings": []}), "raw: genuine approve still reads as approved")
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
    # malformed settings must not crash the hot path (F001), and set-model repairs it
    with open(lib._settings_path(), "w") as _sf:
        _sf.write('{"default_model": "not-a-mapping"}')
    check(lib.get_default_model("codex") is None, "settings: non-mapping default_model -> None, no crash")
    lib.set_default_model("codex", "repaired-model")
    check(lib.get_default_model("codex") == "repaired-model", "settings: set-model repairs a malformed default_model")
    lib.set_default_model("codex", None)
    check(run._main(["set-model", "--backend", "codex", "x", "--clear"]) == 2, "set-model: a model + --clear together is rejected")
    # effort precedence mirrors model: per-run --effort > IMPASSE_CODEX_EFFORT env > persisted
    # set-effort default > the backend's own default (flag omitted)
    check(lib.get_default_effort("codex") is None, "settings: no persisted effort by default")
    rm = run.review(kind="code", instruction="review", artifact_bytes=b"code", no_record=True)
    check(rm["ok"] and rm.get("effort") is None, "review: no effort configured -> backend default (flag omitted)")
    lib.set_default_effort("codex", "low")
    check(lib.get_default_effort("codex") == "low", "settings: set/get persisted default effort")
    rm = run.review(kind="code", instruction="review", artifact_bytes=b"code", no_record=True)
    check(rm.get("effort") == "low", "review: persisted default effort resolves when no flag/env")
    os.environ["IMPASSE_CODEX_EFFORT"] = "high"
    rm = run.review(kind="code", instruction="review", artifact_bytes=b"code", no_record=True)
    check(rm.get("effort") == "high", "review: IMPASSE_CODEX_EFFORT beats the persisted default")
    rm = run.review(kind="code", instruction="review", artifact_bytes=b"code", effort="medium", no_record=True)
    check(rm.get("effort") == "medium", "review: per-run --effort beats env and persisted")
    os.environ["IMPASSE_CODEX_EFFORT"] = "minimal"
    rm = run.review(kind="code", instruction="review", artifact_bytes=b"code", no_record=True)
    check(rm["ok"] is False and rm["failure"]["code"] == "backend_error"
          and "IMPASSE_CODEX_EFFORT" in rm["failure"]["message"],
          "review: invalid env effort -> structured failure naming the env var, not a traceback")
    os.environ.pop("IMPASSE_CODEX_EFFORT", None)
    lib.set_default_effort("codex", None)
    check(lib.get_default_effort("codex") is None, "settings: clear persisted default effort")
    try:
        lib.set_default_effort("codex", "minimal")
        bad_persist = False
    except ValueError:
        bad_persist = True
    check(bad_persist, "settings: set_default_effort refuses a disallowed value ('minimal')")
    with open(lib._settings_path(), "w") as _sf:
        _sf.write('{"default_effort": {"codex": "minimal"}}')
    check(lib.get_default_effort("codex") is None, "settings: hand-edited invalid effort dropped on read (fail safe)")
    check(run._main(["set-effort", "--backend", "codex", "high", "--clear"]) == 2, "set-effort: an effort + --clear together is rejected")
    check(run._main(["set-effort", "high"]) == 0 and lib.get_default_effort("codex") == "high", "set-effort: persists via CLI (and repairs a malformed store)")
    check(run._main(["set-effort", "--clear"]) == 0 and lib.get_default_effort("codex") is None, "set-effort: --clear via CLI")
    # the resolved effort must actually reach the codex argv, not just the result metadata
    _orig_sup, _cap = run.supervise, {}

    def _spy(argv, **kw):
        _cap["argv"] = argv
        return _orig_sup(argv, **kw)
    run.supervise = _spy
    os.environ["IMPASSE_CODEX_EFFORT"] = "high"
    rm = run.review(kind="code", instruction="review", artifact_bytes=b"code", no_record=True)
    run.supervise = _orig_sup
    os.environ.pop("IMPASSE_CODEX_EFFORT", None)
    check(rm.get("effort") == "high" and 'model_reasoning_effort="high"' in _cap.get("argv", []),
          "review: env-resolved effort reaches the codex argv (not just metadata)")
    # defense in depth: the argv builder itself refuses a non-allowlisted effort (config-syntax payload)
    inj = False
    try:
        run.build_codex_argv(["/x/codex"], instruction="I", output_last_message="/tmp/o",
                             effort='high" injected="1')
    except ValueError:
        inj = True
    check(inj, "build_codex_argv: rejects a non-allowlisted effort itself (no config injection)")
    # claude has no effort knob: an irrelevant IMPASSE_CLAUDE_EFFORT (even an invalid one) must
    # neither fail the run nor be reported as configuration that was applied
    os.environ["IMPASSE_CLAUDE_EFFORT"] = "minimal"
    rc = run.review(kind="decision", instruction="review", artifact_bytes=b"memo", backend="claude", no_record=True)
    os.environ.pop("IMPASSE_CLAUDE_EFFORT", None)
    check(rc["ok"] is True and rc.get("effort") is None,
          "review(claude): irrelevant IMPASSE_CLAUDE_EFFORT neither fails the run nor reports as applied")
    # the generic settings writer preserves sibling keys and the 0600 discipline
    lib.set_default_model("codex", "keep-model")
    lib.set_default_effort("codex", "low")
    check(lib.get_default_model("codex") == "keep-model" and lib.get_default_effort("codex") == "low",
          "settings: effort write preserves the model default")
    lib.set_default_model("codex", "keep-model-2")
    check(lib.get_default_effort("codex") == "low", "settings: model write preserves the effort default")
    if os.name == "posix":
        check(stat.S_IMODE(os.stat(lib._settings_path()).st_mode) == 0o600, "settings: settings.json stays 0600 after generic writes")
    lib.set_default_model("codex", None)
    lib.set_default_effort("codex", None)

    # --- host-relative independence (IMPASSE_HOST): the tier is a relation, not a backend property ---
    _host_env = {k: os.environ.pop(k, None) for k in (
        "IMPASSE_HOST", "IMPASSE_ENV", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_COWORK", "CLAUDE_SURFACE", "CLAUDE_CHAT_SANDBOX")}
    try:
        check(lib.detect_host() == "unknown", "detect_host: no markers -> unknown")
        # a surface-policy override must not be able to manufacture a host identity (F002)
        os.environ["IMPASSE_ENV"] = "claude_code"
        check(lib.detect_environment() == "claude_code" and lib.detect_host() == "unknown",
              "detect_host: IMPASSE_ENV alone cannot manufacture a claude host identity")
        os.environ.pop("IMPASSE_ENV", None)
        os.environ["CLAUDECODE"] = "1"
        check(lib.detect_host() == "claude", "detect_host: genuine Claude Code markers -> claude host")
        os.environ.pop("CLAUDECODE", None)   # clear the marker before exercising the override alone
        os.environ["IMPASSE_HOST"] = "codex"
        check(lib.detect_host() == "codex", "detect_host: IMPASSE_HOST overrides auto-detection")
        os.environ["IMPASSE_HOST"] = "skynet"
        check(lib.detect_host() == "unknown",
              "detect_host: nonempty-invalid IMPASSE_HOST -> unknown (refuse, not fallthrough)")
        os.environ.pop("IMPASSE_HOST", None)   # undeclared host for the e2e below
        check(lib.independence_tier("claude", "OpenAI") == "cross_provider", "tier: claude host + OpenAI reviewer -> cross_provider")
        check(lib.independence_tier("claude", "Anthropic") == "same_provider", "tier: claude host + Anthropic reviewer -> same_provider")
        check(lib.independence_tier("codex", "Anthropic") == "cross_provider", "tier: codex host + Anthropic reviewer -> cross_provider (ladder inverts)")
        check(lib.independence_tier("codex", "OpenAI") == "same_provider", "tier: codex host + OpenAI reviewer -> same_provider (no false independence)")
        check(lib.independence_tier("cursor", "OpenAI") == "undetermined", "tier: mixed-model host (cursor) -> undetermined")
        check(lib.independence_tier("claude", "https://gw.corp.example") == "undetermined", "tier: unattributable backend endpoint -> undetermined, never overstated")
        # fail-safe boundary (F001): an unattributed host NEVER receives a positive independence claim
        check(lib.independence_tier("unknown", "OpenAI") == "undetermined", "tier: unknown host -> undetermined, never cross_provider")
        check(lib.independence_tier("unknown", "Anthropic") == "undetermined", "tier: unknown host -> undetermined (claude too)")
        # e2e: an UNDECLARED host gets the undetermined disclosure, not a cross-provider claim
        ru = run.review(kind="code", instruction="review", artifact_bytes=b"code", no_record=True)
        check(ru.get("host") == "unknown" and ru.get("independence") == "undetermined"
              and "IMPASSE_HOST" in (ru.get("independence_notice") or ""),
              "review(codex, undeclared host): undetermined + notice telling the driver to declare itself")
        # e2e: a codex host inverts the ladder — claude becomes the cross-provider reviewer
        os.environ["IMPASSE_HOST"] = "codex"
        rc = run.review(kind="decision", instruction="review", artifact_bytes=b"memo", backend="claude", no_record=True)
        check(rc["ok"] and rc.get("independence") == "cross_provider" and rc.get("independence_notice") is None,
              "review(claude, codex host): cross-provider, no downgrade notice")
        check(rc.get("host") == "codex", "review: result reports the host")
        rx = run.review(kind="code", instruction="review", artifact_bytes=b"code", no_record=True)
        check(rx.get("independence") == "same_provider" and "Same-provider" in (rx.get("independence_notice") or ""),
              "review(codex, codex host): same-provider notice fires (was mislabeled cross before)")
        os.environ["IMPASSE_HOST"] = "cursor"
        ru = run.review(kind="code", instruction="review", artifact_bytes=b"code", no_record=True)
        check(ru.get("independence") == "undetermined" and "undetermined" in (ru.get("independence_notice") or "").lower(),
              "review(codex, cursor host): undetermined tier + notice (host model is operator-chosen)")
        os.environ.pop("IMPASSE_HOST", None)
        # review_mode prefers the backend most independent of the host
        m = lib.review_mode("code", environment="claude_code", codex_available=True, claude_available=True, host="codex")
        check(m["mode"] == "claude" and m["tier"] == "cross_provider" and m["host"] == "codex",
              "review_mode: codex host + both available -> claude (cross-provider) preferred")
        check(m["notice"] is None, "review_mode: cross_provider selection owes no notice")
        m = lib.review_mode("code", environment="claude_code", codex_available=True, claude_available=True, host="claude")
        check(m["mode"] == "codex" and m["tier"] == "cross_provider", "review_mode: claude host + both available -> codex (unchanged)")
        m = lib.review_mode("code", environment="claude_code", codex_available=True, claude_available=True, host="cursor")
        check(m["mode"] == "codex" and m["tier"] == "undetermined", "review_mode: undetermined tie -> codex first (hermetic sandbox)")
        m = lib.review_mode("code", environment="claude_code", codex_available=False, claude_available=True, host="codex")
        check(m["mode"] == "claude" and m["tier"] == "cross_provider", "review_mode: codex host, claude only -> cross_provider, honest label")
        # a downgraded tier carries its own disclosure from the pre-flight too (F004)
        m = lib.review_mode("code", environment="claude_code", codex_available=True, claude_available=False, host="codex")
        check(m["tier"] == "same_provider" and "Same-provider" in (m["notice"] or ""),
              "review_mode: same_provider selection carries the independence notice")
        # a pre-flight must not recommend a backend get_backend() is guaranteed to refuse (F003)
        os.environ["CLAUDE_CODE_USE_BEDROCK"] = "1"
        m = lib.review_mode("code", environment="claude_code", codex_available=True, claude_available=True, host="codex")
        check(m["mode"] == "codex" and m["tier"] == "same_provider",
              "review_mode: claude excluded under Bedrock routing (never recommend a refused backend)")
        os.environ.pop("CLAUDE_CODE_USE_BEDROCK", None)
        # tiers are computed against the CONFIGURED endpoint, mirroring the actual run (F003)
        os.environ["OPENAI_BASE_URL"] = "https://gw.corp.example"
        m = lib.review_mode("code", environment="claude_code", codex_available=True, claude_available=False, host="claude")
        check(m["tier"] == "undetermined" and m["notice"] is not None,
              "review_mode: custom gateway endpoint -> undetermined pre-flight tier + notice")
        os.environ.pop("OPENAI_BASE_URL", None)
        # the Claude Code pitch is only apt for a Claude host; other hosts get the capability framing (F005)
        m = lib.review_mode("code", environment="unknown", codex_available=True, claude_available=True, host="codex")
        check(m["recommendation"] == lib.SUBPROCESS_RECOMMENDATION,
              "review_mode: non-claude host is not steered to Claude Code")
        m = lib.review_mode("decision", environment="unknown", codex_available=False, claude_available=False, host="claude")
        check(m["mode"] == "refuse" and m["recommendation"] == lib.CLAUDE_CODE_RECOMMENDATION,
              "review_mode: claude host on a weak surface still gets the Claude Code recommendation")
        # a malformed base URL (embedded creds) means get_backend() would refuse — the pre-flight
        # must EXCLUDE that backend, and must never echo the raw value (it's where creds live)
        os.environ["OPENAI_BASE_URL"] = "https://user:secret@gw.example"
        check(lib._configured_provider("OPENAI_BASE_URL", "https://api.openai.com") is None,
              "pre-flight: malformed endpoint -> provider None (backend unofferable), raw value never echoed")
        m = lib.review_mode("code", environment="claude_code", codex_available=True, claude_available=False, host="claude")
        check(m["mode"] == "refuse" and "secret" not in str(m),
              "review_mode: never recommends a backend get_backend() would refuse (malformed endpoint), no credential echo")
        m = lib.review_mode("code", environment="claude_code", codex_available=True, claude_available=True, host="claude")
        check(m["mode"] == "claude", "review_mode: malformed codex endpoint -> falls to the usable claude backend")
        os.environ.pop("OPENAI_BASE_URL", None)
        # the mode CLI end-to-end honors --host (host-relative pre-flight through _main)
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc_mode = run._main(["mode", "--kind", "code", "--host", "codex"])
        mode_out = buf.getvalue()
        check(rc_mode == 0 and '"host": "codex"' in mode_out,
              "mode CLI: --host flows through to the decision")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            check(run._main(["set-effort"]) == 0 and "backend default" in buf.getvalue(),
                  "set-effort: bare show path works (no persisted value)")

        # === phase 2: strict-value auto-detection of Codex / Gemini / Cursor hosts, with provenance ===
        _p2markers = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_COWORK", "CLAUDE_SURFACE",
                      "CLAUDE_CHAT_SANDBOX", "GEMINI_CLI", "CURSOR_AGENT", "CODEX_SANDBOX",
                      "CODEX_SANDBOX_NETWORK_DISABLED", "IMPASSE_HOST")
        _p2saved = {k: os.environ.pop(k, None) for k in _p2markers}

        def _p2set(**kw):
            for k in _p2markers:
                os.environ.pop(k, None)
            os.environ.update(kw)
        try:
            # single-marker strict-value detection + provenance confidence
            _p2set(GEMINI_CLI="1")
            check(lib.host_detection() == {"host": "gemini", "method": "auto", "confidence": "strong"},
                  "detect: GEMINI_CLI=1 -> gemini/strong")
            _p2set(CODEX_SANDBOX="seatbelt")
            check(lib.host_detection() == {"host": "codex", "method": "auto", "confidence": "heuristic"},
                  "detect: CODEX_SANDBOX=seatbelt -> codex/heuristic (sandbox-state, not a branded flag)")
            _p2set(CODEX_SANDBOX_NETWORK_DISABLED="1")
            check(lib.detect_host() == "codex", "detect: CODEX_SANDBOX_NETWORK_DISABLED=1 -> codex")
            _p2set(CURSOR_AGENT="1")
            check(lib.host_detection() == {"host": "cursor", "method": "auto", "confidence": "none"},
                  "detect: CURSOR_AGENT=1 -> cursor/none (not provider-attributable)")

            # strict-value negatives: benign/false values must NOT count as a host. Includes the
            # Claude surface flags, which are affirmatively-matched, not truthy (F001 in the diff review).
            for var, val, why in (("GEMINI_CLI", "0", "GEMINI_CLI=0"), ("GEMINI_CLI", "", "GEMINI_CLI empty"),
                                  ("CODEX_SANDBOX", "1", "CODEX_SANDBOX=1 (value is 'seatbelt')"),
                                  ("CODEX_SANDBOX", "off", "CODEX_SANDBOX=off"), ("CURSOR_AGENT", "0", "CURSOR_AGENT=0"),
                                  ("CLAUDE_COWORK", "0", "CLAUDE_COWORK=0"), ("CLAUDE_CHAT_SANDBOX", "off", "CLAUDE_CHAT_SANDBOX=off"),
                                  ("CLAUDE_CODE_ENTRYPOINT", "0", "CLAUDE_CODE_ENTRYPOINT=0")):
                _p2set(**{var: val})
                check(lib.detect_host() == "unknown", f"detect strict-value: {why} -> unknown")
            # ...but an AFFIRMATIVE Claude surface marker (no CLAUDECODE) still resolves to claude
            _p2set(CLAUDE_CODE_ENTRYPOINT="cli")
            check(lib.detect_host() == "claude", "detect: CLAUDE_CODE_ENTRYPOINT=cli (affirmative) -> claude")
            _p2set(CLAUDE_COWORK="1")
            check(lib.detect_host() == "claude", "detect: CLAUDE_COWORK=1 -> claude")

            # ambiguity / conflict fail-safe (F001): never guess a driver from an unordered marker set
            _p2set(CLAUDECODE="1", CURSOR_AGENT="1")
            check(lib.detect_host() == "unknown", "detect: claude + cursor -> unknown (can't tell inner driver)")
            _p2set(GEMINI_CLI="1", CODEX_SANDBOX="seatbelt")
            check(lib.detect_host() == "unknown", "detect: gemini + codex -> unknown (2 attributable)")
            _p2set(CLAUDECODE="1", GEMINI_CLI="1", CODEX_SANDBOX="seatbelt")
            check(lib.detect_host() == "unknown", "detect: all three attributable -> unknown")

            # override validation + conflict-check (F002/F003)
            _p2set(IMPASSE_HOST="gemini")
            check(lib.host_detection() == {"host": "gemini", "method": "override", "confidence": "asserted"},
                  "override: IMPASSE_HOST=gemini alone -> gemini/asserted")
            _p2set(IMPASSE_HOST="gemini", CODEX_SANDBOX="seatbelt")
            check(lib.detect_host() == "unknown", "override: disagrees with observed marker -> unknown (fail-safe)")
            _p2set(IMPASSE_HOST="zzinvalid", CLAUDECODE="1")
            check(lib.detect_host() == "unknown",
                  "override: nonempty-invalid does NOT fall through to a weaker marker (F002)")
            _p2set(IMPASSE_HOST="", CLAUDECODE="1")
            check(lib.detect_host() == "claude", "override: empty string == absent -> markers resolve")

            # Explicit decision-pinning for override + Cursor (operator ruling: Cursor is
            # non-attributable, so it does NOT contradict an attributable override — the escape hatch
            # resolves the very claude+cursor ambiguity auto-mode refuses). Hand-written, NOT derived
            # from the matrix oracle, so the intended behavior is asserted independently (F004).
            _p2set(IMPASSE_HOST="claude", CURSOR_AGENT="1")
            check(lib.detect_host() == "claude", "override+cursor: IMPASSE_HOST=claude + CURSOR_AGENT=1 -> claude (honored)")
            _p2set(IMPASSE_HOST="gemini", CURSOR_AGENT="1")
            check(lib.detect_host() == "gemini", "override+cursor: IMPASSE_HOST=gemini + CURSOR_AGENT=1 -> gemini (honored)")
            _p2set(IMPASSE_HOST="cursor", CURSOR_AGENT="1")
            check(lib.detect_host() == "cursor", "override+cursor: IMPASSE_HOST=cursor + CURSOR_AGENT=1 -> cursor (agrees)")
            # but an ATTRIBUTABLE marker that disagrees with the override still conflicts, cursor or not
            _p2set(IMPASSE_HOST="claude", CODEX_SANDBOX="seatbelt", CURSOR_AGENT="1")
            check(lib.detect_host() == "unknown", "override+cursor: attributable marker disagreeing with override -> unknown")

            # exhaustive matrix: every {A-subset} x {cursor} x {override} cell vs an INDEPENDENT truth fn,
            # asserting both detect_host output AND that unknown/cursor never yield a positive tier (F003 rev2)
            def _expected(A, cursor, override):
                if override:                                    # nonempty
                    if override not in lib.KNOWN_HOSTS:
                        return "unknown"
                    if A and A != {override}:
                        return "unknown"
                    return override
                if len(A) >= 2:
                    return "unknown"
                if len(A) == 1:
                    return "unknown" if cursor else next(iter(A))
                return "cursor" if cursor else "unknown"

            import itertools
            _marker_for = {"claude": ("CLAUDECODE", "1"), "gemini": ("GEMINI_CLI", "1"),
                           "codex": ("CODEX_SANDBOX", "seatbelt")}
            _attr = ("claude", "gemini", "codex")
            _subsets = [set(c) for r in range(len(_attr) + 1) for c in itertools.combinations(_attr, r)]
            _matrix_ok, _cells = True, 0
            for A in _subsets:
                for cur in (False, True):
                    for ov in (None, "", "claude", "codex", "gemini", "cursor", "other", "zzinvalid"):
                        kw = {}
                        for h in A:
                            k, v = _marker_for[h]
                            kw[k] = v
                        if cur:
                            kw["CURSOR_AGENT"] = "1"
                        if ov is not None:
                            kw["IMPASSE_HOST"] = ov
                        _p2set(**kw)
                        _cells += 1
                        got = lib.detect_host()
                        exp = _expected(A, cur, ov or None)
                        if got != exp:
                            _matrix_ok = False
                            print(f"   matrix MISMATCH A={sorted(A)} cursor={cur} override={ov!r}: got {got!r} exp {exp!r}")
                        if exp in ("unknown", "cursor") and (
                                lib.independence_tier(got, "OpenAI") == "cross_provider"
                                or lib.independence_tier(got, "Anthropic") == "cross_provider"):
                            _matrix_ok = False
                            print(f"   matrix TIER LEAK A={sorted(A)} cursor={cur} override={ov!r}: host {got!r}")
            check(_matrix_ok, f"detect_host matrix ({_cells} cells): all match the independent truth table, no positive-tier leak")

            # tier reachability for the new gemini host
            check(lib.independence_tier("gemini", "OpenAI") == "cross_provider", "tier: gemini host + OpenAI -> cross_provider")
            check(lib.independence_tier("gemini", "Anthropic") == "cross_provider", "tier: gemini host + Anthropic -> cross_provider")

            # e2e review(): gemini host (strong) + codex backend -> cross_provider, NULL notice, strong provenance
            _p2set(GEMINI_CLI="1")
            rg = run.review(kind="decision", instruction="review", artifact_bytes=b"memo", no_record=True)
            check(rg.get("host") == "gemini" and rg.get("independence") == "cross_provider"
                  and rg.get("independence_notice") is None
                  and rg.get("host_detection") == {"method": "auto", "confidence": "strong"},
                  "review(codex, gemini host): cross_provider, null notice, strong provenance")
            # e2e review(): codex host (heuristic) + claude backend -> cross_provider WITH the soft notice
            _p2set(CODEX_SANDBOX="seatbelt")
            rch = run.review(kind="decision", instruction="review", artifact_bytes=b"memo", backend="claude", no_record=True)
            check(rch.get("host") == "codex" and rch.get("independence") == "cross_provider"
                  and rch.get("host_detection") == {"method": "auto", "confidence": "heuristic"}
                  and "INFERRED" in (rch.get("independence_notice") or ""),
                  "review(claude, codex heuristic host): cross_provider carries the soft heuristic notice")

            # review_mode() must ALSO carry the heuristic notice on its own path (F003 rev3)
            _p2set(CODEX_SANDBOX="seatbelt")
            m = lib.review_mode("decision", environment="claude_code", codex_available=True, claude_available=True)
            check(m["host"] == "codex" and m["tier"] == "cross_provider"
                  and m["host_detection"] == {"method": "auto", "confidence": "heuristic"}
                  and "INFERRED" in (m["notice"] or ""),
                  "review_mode: codex heuristic host -> claude cross_provider WITH soft notice")
            _p2set(CURSOR_AGENT="1")
            m = lib.review_mode("decision", environment="claude_code", codex_available=True, claude_available=True)
            check(m["host"] == "cursor" and m["tier"] == "undetermined"
                  and m["host_detection"]["confidence"] == "none",
                  "review_mode: cursor host -> undetermined, confidence none")
        finally:
            for k in _p2markers:
                os.environ.pop(k, None)
            for k, v in _p2saved.items():
                if v is not None:
                    os.environ[k] = v
    finally:
        os.environ.pop("IMPASSE_HOST", None)
        os.environ.pop("CLAUDECODE", None)
        os.environ.pop("IMPASSE_ENV", None)
        os.environ.pop("CLAUDE_CODE_USE_BEDROCK", None)
        os.environ.pop("OPENAI_BASE_URL", None)
        for k, v in _host_env.items():
            if v is not None:
                os.environ[k] = v

    # --- review CLI end-to-end + the cross-feature matrix (host x effort x output retry) ---
    # Runs AFTER the host-block restore: IMPASSE_HOST is back to the suite baseline ('claude').
    import contextlib
    import io
    _ins = os.path.join(tmp, "cli-instr.txt")
    _art = os.path.join(tmp, "cli-art.txt")
    open(_ins, "w").write("review this artifact")
    open(_art, "w").write("artifact body")
    os.environ["FAKE_MODE"] = "valid"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc_rev = run._main(["review", "--kind", "code", "--instruction-file", _ins,
                            "--artifact-file", _art, "--no-record"])
    cli_out = _json.loads(buf.getvalue())
    check(rc_rev == 0 and cli_out["ok"] is True and cli_out["backend"] == "codex"
          and cli_out.get("host") == "claude",
          "review CLI: end-to-end through _main returns the full result JSON (host included)")
    # instruction-file bound is BYTES, not characters (mirror of the final-message fix)
    _fat = os.path.join(tmp, "fat-instr.txt")
    with open(_fat, "w", encoding="utf-8") as f:
        f.write("é" * 6)   # 6 characters, 12 bytes
    fat_ok = False
    try:
        run._read_limited(_fat, 10, binary=False)
    except ValueError:
        fat_ok = True
    check(fat_ok, "_read_limited: multi-byte text over the BYTE limit rejected (char count would pass)")
    check(run._read_limited(_fat, 12, binary=False) == "é" * 6, "_read_limited: within the byte limit decodes cleanly")
    # matrix: codex host + env effort + malformed-then-ok output — identical argv on retry
    argvs = []
    _orig_sup_m = run.supervise

    def _spy_m(argv, **kw):
        argvs.append(list(argv))
        return _orig_sup_m(argv, **kw)
    # A real Codex host does not carry Claude's markers; clear the ambient one so IMPASSE_HOST=codex
    # is not (correctly) rejected as an override↔marker conflict under phase-2 detection.
    _claude_ambient = {k: os.environ.pop(k, None) for k in (
        "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_COWORK", "CLAUDE_SURFACE", "CLAUDE_CHAT_SANDBOX")}
    try:
        run.supervise = _spy_m
        os.environ["IMPASSE_HOST"] = "codex"
        os.environ["IMPASSE_CODEX_EFFORT"] = "high"
        if os.path.exists(counter):
            os.remove(counter)
        os.environ["FAKE_MODE"] = "badjson_then_ok"
        os.environ["FAKE_COUNTER"] = counter
        res_m = run.review(kind="code", instruction="review", artifact_bytes=b"code", no_record=True)
    finally:
        run.supervise = _orig_sup_m
        os.environ.pop("FAKE_COUNTER", None)
        os.environ.pop("IMPASSE_CODEX_EFFORT", None)
        os.environ["IMPASSE_HOST"] = "claude"   # back to the suite baseline
        os.environ["FAKE_MODE"] = "valid"
        for _k, _v in _claude_ambient.items():
            if _v is not None:
                os.environ[_k] = _v
    check(res_m["ok"] is True and res_m.get("host") == "codex" and res_m.get("effort") == "high"
          and "Same-provider" in (res_m.get("independence_notice") or ""),
          "matrix: codex host + env effort + output retry -> all metadata correct after recovery")
    check(len(argvs) == 2 and argvs[0] == argvs[1] and 'model_reasoning_effort="high"' in argvs[0],
          "matrix: the retry re-runs the IDENTICAL argv (effort/model resolved once, not per attempt)")

    # F003: the reviewer subprocess must run in the run's scratch dir, NOT the operator's project CWD
    # (else `claude -p` could load the reviewed project's CLAUDE.md/hooks — artifact-controlled bleed).
    _cwds = []
    _orig_sup_c = run.supervise

    def _spy_cwd(argv, **kw):
        _cwds.append(kw.get("cwd"))
        return _orig_sup_c(argv, **kw)
    _proc_cwd = os.getcwd()
    _cfg_dir = lib.ensure_config_dir()
    try:
        run.supervise = _spy_cwd
        run.review(kind="code", instruction="review", artifact_bytes=b"code", backend="claude", no_record=True)
    finally:
        run.supervise = _orig_sup_c
        os.environ["FAKE_MODE"] = "valid"
    check(len(_cwds) == 1 and _cwds[0] is not None and _cwds[0] != _proc_cwd
          and os.path.realpath(_cwds[0]).startswith(os.path.realpath(_cfg_dir)),
          "F003: reviewer runs in a scratch dir under the config dir, not the operator's project CWD")

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
    check("1 refuted with evidence" in recap and "1 resolved" in recap and "1 awaiting you" in recap, "recap: resolved and escalated counted separately (not conflated)")
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
    check("/" not in lib._safe_id("a/b/../../etc"), "safe_id: path separators collapsed")
    check(lib._safe_id("a/b").startswith("a_b-") and lib._safe_id("a/b") != lib._safe_id("a?b"), "safe_id: lossy ids get a disambiguating hash (injective, no collision)")
    check(lib._safe_id(12345) == "12345" and lib._safe_id(None) == "unknown", "safe_id: a non-string id is coerced, not crashed")
    lib.save_run_doc("../evil", "reviewer-response", {"schema_version": "1.0", "review_id": "../evil", "findings": []})
    escaped = os.path.join(os.path.dirname(lib.runs_dir()), "evil")
    check(os.path.isdir(os.path.join(lib.runs_dir(), lib._safe_id("../evil"))) and not os.path.exists(escaped), "save_run_doc: a traversal review_id stays inside runs_dir")
    lib.forget_run("../evil")
    check(report._clean("a\x1b[31mX\x1b[0m\x07b") == "a[31mX[0mb", "report: strips ANSI/control escapes from untrusted reviewer text")
    check(lib.review_mode("CODE", environment="chat_sandbox")["mode"] == "refuse", "review_mode: 'CODE' normalized -> still refused in the sandbox")

    # --- full-codebase-review fixes ---
    prune_guarded = False
    try:
        report.prune(0)
    except ValueError:
        prune_guarded = True
    check(prune_guarded, "prune: rejects --older-than < 1 (won't silently delete everything)")
    with open(consent.consent_path(), "w") as _cf:
        _cf.write('{"version":1,"grants":["not-a-dict",{"destination_id":"' + D1 + '","notice_version":"1"}]}')
    check(consent.check(be1)[0] is True, "consent: a non-dict grant entry is ignored, valid grant still honored (no crash)")
    consent.revoke(D1)
    lib.save_run_doc("hdrtest", "reviewer-response", {"schema_version": "1.0", "review_id": "r\x1b[31mX",
                     "artifact": {"kind": "code", "revision": {"algorithm": "sha256", "value": "x"}},
                     "assessment": "approve", "summary": "s", "findings": []})
    check("\x1b" not in report.render(lib.load_run("hdrtest")), "report: terminal escapes in an untrusted review_id are stripped from the header")
    lib.forget_run("hdrtest")

    print()
    if _fails:
        print(f"{len(_fails)} FAILURES: " + "; ".join(_fails))
        return 1
    print("all helper tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
