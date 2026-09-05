#!/usr/bin/env python3
"""Standalone tests for the stdlib helpers (no pytest needed): run with python3.

Covers config/hashing, endpoint-keyed consent precedence, the process supervisor
(timeouts, tree kill, stdin-can't-block-the-supervisor), and end-to-end review()
classification via a fake codex backend. Uses a temp IMPASSE_CONFIG_DIR so it never
touches real user config. POSIX assumptions (killpg) — skips the tree tests off POSIX.
"""
from __future__ import annotations

import os
import json
import shutil
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

echo = os.environ.get("FAKE_ECHO_INSTR")   # capture the instruction (argv[-1]) so a test can assert what was embedded
if echo and argv:
    open(echo, "w", encoding="utf-8").write(argv[-1])

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
if mode == "silent_hang":       # never emits a byte, outlives any test wall -> "backend never spoke"
    time.sleep(600)
if mode == "speak_then_hang":   # a byte lands, THEN it stalls -> "backend spoke, then went quiet"
    sys.stdout.write(json.dumps({"type": "thread.started", "thread_id": "th_fake_1"}) + "\n")
    sys.stdout.flush()
    time.sleep(600)
if mode == "partial_then_stall":   # partial JSON in the out-file, then stall (never completes)
    if outp:
        open(outp, "w", encoding="utf-8").write('{"schema_version":"1.0","findi')
    sys.stdout.write(json.dumps({"type": "thread.started", "thread_id": "th_partial"}) + "\n")
    sys.stdout.flush()
    time.sleep(600)
if mode == "orphan_then_hang":  # leaves a CHILD alive, then stalls: proves group teardown reaps both
    import subprocess as _sp
    _sp.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    sys.stdout.write(json.dumps({"type": "thread.started", "thread_id": "th_orphan"}) + "\n")
    sys.stdout.flush()
    time.sleep(600)
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
    # passes the runner's shape-check (schema_version + findings) but OMITS the schema-required
    # `artifact` — the case where "helpfully" filling the field in would mask a malformed response
    "noartifact": '{"schema_version":"1.0","review_id":"r","assessment":"approve","summary":"ok","findings":[]}',
    # artifact present but NOT an object, and artifact whose `kind` contradicts the caller's --kind
    "artifactstr": '{"schema_version":"1.0","review_id":"r","artifact":"not-an-object","assessment":"approve","summary":"ok","findings":[]}',
    "wrongkind": '{"schema_version":"1.0","review_id":"r","artifact":{"kind":"research","revision":{"algorithm":"other","value":"made-up"}},"assessment":"approve","summary":"ok","findings":[]}',
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
import sys, os, json, time
try:
    sys.stdin.buffer.read()   # drain the piped artifact (reaches EOF)
except Exception:
    pass
mode = os.environ.get("FAKE_CLAUDE_MODE", "valid")
time.sleep(float(os.environ.get("FAKE_CLAUDE_SLEEP", "0")))

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
if mode in ("envelope", "envelope_error"):
    # The `claude -p --output-format json` result envelope. Two models appear in modelUsage (a
    # headless run can bill a small helper model for side work); the REVIEWER is the one that read
    # the artifact, i.e. the larger total input including cache.
    env = {
        "type": "result", "subtype": "success", "is_error": mode == "envelope_error",
        "result": "Rate limit reached for your account." if mode == "envelope_error" else valid,
        "session_id": "sess_fake_9", "ttft_ms": 1234, "duration_api_ms": 4321,
        "usage": {"input_tokens": 2, "output_tokens": 4},
        "modelUsage": {
            "claude-haiku-4-5-20251001": {"inputTokens": 525, "outputTokens": 11,
                                          "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0},
            "claude-sonnet-5": {"inputTokens": 2, "outputTokens": 4,
                                "cacheReadInputTokens": 15565, "cacheCreationInputTokens": 21157},
        },
    }
    if mode == "envelope_error":
        env["api_error_status"] = 429
    sys.stdout.write(json.dumps(env))
    sys.exit(int(os.environ.get("FAKE_CLAUDE_EXIT", "0")))
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
               "IMPASSE_CLAUDE_MODEL", "IMPASSE_CLAUDE_EFFORT", "IMPASSE_CODEX_SPEED",
               "IMPASSE_CLAUDE_SPEED", "IMPASSE_CODEX_RESPECT_CONFIG",
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

    # --- resolver finds the codex binary in the rebranded ChatGPT.app bundle ---
    # (The Codex desktop app moved its binary to ~/Applications/ChatGPT.app/.../codex.) Fake the
    # HOME-relative bundle, strip PATH + overrides so only the known-locations branch can match.
    _rz_home, _rz_path = os.environ.get("HOME"), os.environ.get("PATH")
    _rz_ov = {k: os.environ.pop(k, None) for k in ("IMPASSE_CODEX_BIN", "CODEX_BIN")}
    _rz_dir = os.path.join(tmp, "resolver-home")
    try:
        _rz_bin = os.path.join(_rz_dir, "Applications/ChatGPT.app/Contents/Resources/codex")
        os.makedirs(os.path.dirname(_rz_bin), exist_ok=True)
        with open(_rz_bin, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(_rz_bin, 0o755)
        os.environ["HOME"] = _rz_dir
        os.environ["PATH"] = os.path.join(tmp, "no-such-bin")   # ensure `which codex` misses
        # The candidate list is ORDERED, and the entries above the HOME-relative bundle are absolute
        # paths we can't neutralize with a temp HOME. On a developer machine that really has one
        # (a Homebrew codex, or a system-wide ChatGPT.app), resolution correctly stops there and this
        # case is unreachable — so skip rather than assert a suffix that a Homebrew hit fails and a
        # system ChatGPT.app passes for the WRONG reason. CI runs on a clean image and covers it.
        _rz_earlier = [p for p in ("/opt/homebrew/bin/codex", "/usr/local/bin/codex",
                                   "/Applications/ChatGPT.app/Contents/Resources/codex")
                       if os.path.isfile(p) and os.access(p, os.X_OK)]
        if _rz_earlier:
            check(True, "resolve_codex_command: ChatGPT.app bundle case skipped — a "
                        f"higher-priority install exists here ({_rz_earlier[0]})")
        else:
            _rz = lib.resolve_codex_command()
            check(_rz is not None and _rz[0] == _rz_bin,
                  "resolve_codex_command: finds the binary in the rebranded ChatGPT.app bundle")
    finally:
        for _k, _v in (("HOME", _rz_home), ("PATH", _rz_path)):
            if _v is None:
                os.environ.pop(_k, None)   # was absent -> remove the temp value, don't leak it
            else:
                os.environ[_k] = _v
        for _k, _v in _rz_ov.items():
            if _v is not None:
                os.environ[_k] = _v

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

    # artifact.revision is the RUNNER's to set, not the reviewer's. The fake emits a fabricated
    # value ("x"), exactly as a real run was observed doing — it stored
    # {"algorithm":"other","value":"caller-provided-bundle-..."}. That field is what stops findings
    # being reconciled against changed content, so a fabricated value defeats it silently.
    _rev_bytes = b"revision-stamp-artifact"
    _rev_res = run.review(kind="code", instruction="review", artifact_bytes=_rev_bytes)
    _rev_true = lib.artifact_revision(_rev_bytes)
    check(_rev_res["ok"] and _rev_res.get("artifact_revision") == _rev_true,
          "revision: the result carries the real digest, so a host copies it instead of deriving it")
    check(_rev_res["response"]["artifact"]["revision"] == _rev_true,
          "revision: the runner OVERWRITES the reviewer's invented revision with the real digest")
    _rev_stored = json.loads(open(_rev_res["record_path"]).read())
    check(_rev_stored["artifact"]["revision"] == _rev_true
          and _rev_stored["artifact"]["revision"]["value"] != "x",
          "revision: the PERSISTED record holds the real digest, not the reviewer's fiction")
    # The manifest is where a host can see the same identity; it must agree, and the documented
    # accessor must be the way across (hosts were observed guessing key names, then recomputing).
    check(lib.revision_from_digest(_rev_res["manifest"]["digest"]) == _rev_true,
          "revision: revision_from_digest(manifest.digest) yields the same identity")
    lib.forget_run(_rev_res["run_id"])

    # Correcting a field the reviewer cannot know is legitimate; INVENTING one it never sent is not.
    # `artifact` is schema-required while the runner's shape-check does not demand it, so
    # synthesizing it would turn a response that must fail validation into one that passes — inside
    # data whose whole premise is that it is untrusted.
    os.environ["FAKE_MODE"] = "noartifact"
    _na_res = run.review(kind="code", instruction="review", artifact_bytes=b"no-artifact-case")
    check(_na_res["ok"] and "artifact" not in _na_res["response"],
          "revision: a response MISSING artifact is left missing, not repaired into schema-validity")
    check(_na_res.get("artifact_revision") == lib.artifact_revision(b"no-artifact-case"),
          "revision: the identity is still on the result even when the response omits artifact")
    _na_stored = json.loads(open(_na_res["record_path"]).read())
    check("artifact" not in _na_stored,
          "revision: the stored record preserves the reviewer's omission for validation to catch")
    lib.forget_run(_na_res["run_id"])

    # A non-object artifact must NOT be replaced either — same rule, different shape.
    os.environ["FAKE_MODE"] = "artifactstr"
    _as_res = run.review(kind="code", instruction="review", artifact_bytes=b"artifact-not-object")
    check(_as_res["ok"] and _as_res["response"]["artifact"] == "not-an-object",
          "revision: a non-object artifact is left alone, not replaced with a synthesized one")
    lib.forget_run(_as_res["run_id"])

    # `kind` is caller-owned: the reviewer echoes it, so a disagreement means the reviewer is wrong.
    # Both runner-owned fields are corrected, not one corrected and the other trusted.
    os.environ["FAKE_MODE"] = "wrongkind"
    _wk_bytes = b"wrong-kind-case"
    _wk_res = run.review(kind="code", instruction="review", artifact_bytes=_wk_bytes)
    check(_wk_res["ok"] and _wk_res["response"]["artifact"]["kind"] == "code",
          "revision: a reviewer `kind` contradicting the caller is corrected, not preserved")
    check(_wk_res["response"]["artifact"]["revision"] == lib.artifact_revision(_wk_bytes),
          "revision: the invented 'other'/'made-up' revision is replaced by the real digest")
    lib.forget_run(_wk_res["run_id"])
    os.environ["FAKE_MODE"] = "valid"

    os.environ["FAKE_MODE"] = "notjson"
    res = run.review(kind="code", instruction="review", artifact_bytes=b"code")
    check(res["ok"] is False and res["failure"]["code"] == "invalid_response", "review: non-JSON output -> invalid_response")
    check(res.get("artifact_revision") == lib.artifact_revision(b"code"),
          "revision: a FAILED review still identifies the bytes it tried to review")

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

    # --- schema resolution: bundled self-location + loud failure on a bad/missing schema ---
    # SKILL.md promises `--schema` is optional because the runner self-locates its bundled schema.
    # With NONE embedded a compliant reviewer returns prose and the run fails invalid_response, so the
    # fallback must (a) actually embed the bundled schema and (b) fail LOUD on a broken/invalid copy
    # rather than degrade to a silent no-schema run.
    os.environ["FAKE_MODE"] = "valid"
    os.environ["FAKE_EXIT"] = "0"
    # the self-located path is ABSOLUTE (module-derived, not cwd-relative) and points at the shipped schema
    check(os.path.isabs(run._BUNDLED_SCHEMA_PATH) and os.path.isfile(run._BUNDLED_SCHEMA_PATH)
          and run._BUNDLED_SCHEMA_PATH.endswith(os.path.join("schemas", "reviewer-response.v1.json")),
          "schema: bundled path self-locates (absolute, module-derived) to the shipped schema")
    # the instruction-capture hook reads argv[-1]; prove that IS the instruction the runner builds (else
    # a flag-order change would make the embedding assertions below silently check the wrong string).
    _argv = run.build_codex_argv(["codex"], instruction="INSTR_SENTINEL", output_last_message="x")
    check(_argv[-1] == "INSTR_SENTINEL", "schema: build_codex_argv puts the instruction last (argv[-1] capture is valid)")

    echo_path = os.path.join(tmp, "echo-instr.txt")
    _prev_echo = os.environ.get("FAKE_ECHO_INSTR")
    try:
        os.environ["FAKE_ECHO_INSTR"] = echo_path
        bundled_text = open(run._BUNDLED_SCHEMA_PATH, encoding="utf-8").read()
        # (a) omitted --schema embeds the ENTIRE bundled schema text (not just an incidental $id token),
        # plus the JSON directive and the caller's lens. Full-text match: a regressed embedding fails this.
        res = run.review(kind="code", instruction="MY_LENS", artifact_bytes=b"code", schema_path=None, no_record=True)
        sent = open(echo_path, encoding="utf-8").read()
        check(res["ok"] is True and bundled_text in sent
              and "Return ONLY a JSON object" in sent and "MY_LENS" in sent,
              "schema: omitted --schema embeds the FULL bundled schema text into the instruction")
        # (b) an explicit --schema is actually READ AND EMBEDDED, not ignored in favour of bundled: a
        # unique sentinel in the explicit file must appear in what was sent (would pass vacuously otherwise).
        good_schema = os.path.join(tmp, "good.schema.json")
        with open(good_schema, "w") as f:
            f.write('{"type": "object", "title": "EXPLICIT_SENTINEL_SCHEMA"}')
        res = run.review(kind="code", instruction="review", artifact_bytes=b"code", schema_path=good_schema, no_record=True)
        sent = open(echo_path, encoding="utf-8").read()
        check(res["ok"] is True and "EXPLICIT_SENTINEL_SCHEMA" in sent,
              "schema: an explicit --schema is read AND embedded (not silently ignored for bundled)")
    finally:
        if _prev_echo is None:
            os.environ.pop("FAKE_ECHO_INSTR", None)
        else:
            os.environ["FAKE_ECHO_INSTR"] = _prev_echo

    # bad EXPLICIT schema -> structured backend_error labelled "schema file", no reinstall hint (operator's path)
    def _schema_case(fname, content, needle, *, binary=False):
        p = os.path.join(tmp, fname)
        with open(p, "wb" if binary else "w") as f:
            f.write(content)
        r = run.review(kind="code", instruction="review", artifact_bytes=b"code", schema_path=p, no_record=True)
        ok = (r["ok"] is False and r["failure"]["code"] == "backend_error"
              and "schema file" in r["failure"]["message"] and needle in r["failure"]["message"]
              and "reinstall the skill" not in r["failure"]["message"])
        check(ok, f"schema: explicit {fname} -> structured backend_error ({needle!r})")

    _schema_case("empty.json", "", "not valid JSON")               # empty file -> would embed nothing
    _schema_case("notjson.json", "not json", "not valid JSON")
    _schema_case("badutf8.json", b"\xff\xfe", "not valid UTF-8", binary=True)   # non-UTF-8 (not an OSError)
    _schema_case("emptyobj.json", "{}", "not a non-empty JSON object")          # empty object -> the emptiness clause
    _schema_case("emptyarr.json", "[]", "not a non-empty JSON object")          # falsy non-object
    _schema_case("arr.json", "[1]", "not a non-empty JSON object")              # TRUTHY non-object -> proves the isinstance clause
    _schema_case("scalar.json", "42", "not a non-empty JSON object")            # truthy non-object scalar

    # size bound: a schema EXACTLY at _MAX_SCHEMA bytes is accepted; one byte over is rejected. Pinning both
    # sides of the boundary proves the bound's value — a plain unbounded read would pass the over-bound case.
    _orig_max_schema = run._MAX_SCHEMA
    try:
        at_payload = '{"k":"' + ("a" * 10) + '"}'
        run._MAX_SCHEMA = len(at_payload.encode("utf-8"))
        at_bound = os.path.join(tmp, "at-bound.json")
        with open(at_bound, "w") as f:
            f.write(at_payload)
        res = run.review(kind="code", instruction="review", artifact_bytes=b"code", schema_path=at_bound, no_record=True)
        check(res["ok"] is True, "schema: a schema exactly at the _MAX_SCHEMA byte bound is accepted")
        over_bound = os.path.join(tmp, "over-bound.json")
        with open(over_bound, "w") as f:
            f.write(at_payload + " ")   # one byte over
        res = run.review(kind="code", instruction="review", artifact_bytes=b"code", schema_path=over_bound, no_record=True)
        check(res["ok"] is False and res["failure"]["code"] == "backend_error"
              and "exceeds the" in res["failure"]["message"],
              "schema: one byte over _MAX_SCHEMA is rejected (bounded read)")
    finally:
        run._MAX_SCHEMA = _orig_max_schema

    # explicit empty-string path must fail against the OPERATOR's path, NOT silently fall back to bundled
    res = run.review(kind="code", instruction="review", artifact_bytes=b"code", schema_path="", no_record=True)
    check(res["ok"] is False and res["failure"]["code"] == "backend_error"
          and "schema file" in res["failure"]["message"] and "reinstall the skill" not in res["failure"]["message"],
          "schema: explicit --schema '' fails against the operator path (not treated as omission)")

    # a BROKEN bundled schema (broken install) fails loud, labelled "bundled reviewer schema" + reinstall hint
    # + the specific defect — for BOTH a missing file and a corrupt one (the bundled branch, not the explicit one).
    _orig_bundled = run._BUNDLED_SCHEMA_PATH
    try:
        run._BUNDLED_SCHEMA_PATH = os.path.join(tmp, "does-not-exist.json")
        res = run.review(kind="code", instruction="review", artifact_bytes=b"code", schema_path=None, no_record=True)
        check(res["ok"] is False and res["failure"]["code"] == "backend_error"
              and "bundled reviewer schema" in res["failure"]["message"]
              and "reinstall the skill" in res["failure"]["message"],
              "schema: MISSING bundled schema -> loud backend_error, bundled-labelled + reinstall hint")
        corrupt_bundled = os.path.join(tmp, "corrupt-bundled.json")
        with open(corrupt_bundled, "w") as f:
            f.write("not json")
        run._BUNDLED_SCHEMA_PATH = corrupt_bundled
        res = run.review(kind="code", instruction="review", artifact_bytes=b"code", schema_path=None, no_record=True)
        check(res["ok"] is False and res["failure"]["code"] == "backend_error"
              and "bundled reviewer schema" in res["failure"]["message"]
              and "reinstall the skill" in res["failure"]["message"]
              and "not valid JSON" in res["failure"]["message"],
              "schema: CORRUPT bundled schema -> bundled-labelled + reinstall hint + specific defect")
    finally:
        run._BUNDLED_SCHEMA_PATH = _orig_bundled

    # integration: the REAL CLI with --schema OMITTED, run from a FOREIGN cwd, must self-locate + succeed
    # (proves the user-facing path — the skill omits --schema — resolves the schema regardless of cwd).
    import subprocess as _subprocess
    _instr_f = os.path.join(tmp, "cli-instr.txt")
    _art_f = os.path.join(tmp, "cli-art.txt")
    with open(_instr_f, "w") as f:
        f.write("review")
    with open(_art_f, "w") as f:
        f.write("code")
    _run_py = os.path.join(HERE, "..", "scripts", "impasse_run.py")
    _cli = _subprocess.run(
        [sys.executable, _run_py, "review", "--kind", "code", "--backend", "codex", "--no-record",
         "--approve-send", "https://api.openai.com",
         "--instruction-file", _instr_f, "--artifact-file", _art_f],
        cwd=os.path.dirname(tmp), capture_output=True, text=True,
        env={**os.environ, "FAKE_MODE": "valid", "FAKE_EXIT": "0"})
    try:
        _cli_out = run.json.loads(_cli.stdout)
    except Exception:
        _cli_out = {"ok": None}
    check(_cli_out.get("ok") is True,
          "schema: real CLI with --schema omitted, from a foreign cwd, self-locates the bundled schema and succeeds")

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
    # the tier is computed per-run from the host (not cached on Backend — F011); baseline host is claude
    check(lib.independence_tier("claude", be_c.provider) == "same_provider"
          and be_c.destination_id == "https://api.anthropic.com",
          "get_backend('claude'): same-provider tier (claude host) + Anthropic destination")
    for ev in ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"):
        os.environ[ev] = "1"
        routed_ok = False
        try:
            lib.get_backend("claude")
        except ValueError:
            routed_ok = True
        os.environ.pop(ev, None)
        check(routed_ok, f"get_backend('claude'): refuses under {ev} (would mis-key consent to Anthropic)")
    # F001: the codex backend ALWAYS passes --ignore-user-config (config.toml can't reroute data away
    # from the consented endpoint); IMPASSE_CODEX_RESPECT_CONFIG only opts into --ignore-rules.
    try:
        _cbe = lib.get_backend("codex")
        os.environ.pop("IMPASSE_CODEX_RESPECT_CONFIG", None)
        av = run.build_codex_argv(_cbe.command, instruction="i", output_last_message="o")
        check("--ignore-user-config" in av and "--ignore-rules" in av,
              "F001: hermetic by default -> --ignore-user-config AND --ignore-rules")
        os.environ["IMPASSE_CODEX_RESPECT_CONFIG"] = "1"
        av = run.build_codex_argv(_cbe.command, instruction="i", output_last_message="o")
        check("--ignore-user-config" in av and "--ignore-rules" not in av,
              "F001: RESPECT_CONFIG drops ONLY --ignore-rules; --ignore-user-config stays (consent honest)")
        os.environ.pop("IMPASSE_CODEX_RESPECT_CONFIG", None)
        # F008: an explicitly-EMPTY base URL is treated as the default (consistent preflight vs run)
        os.environ["OPENAI_BASE_URL"] = ""
        check(lib.get_backend("codex").destination_id == "https://api.openai.com",
              "F008: empty OPENAI_BASE_URL -> default endpoint (not an empty-string destination)")
    finally:
        os.environ.pop("IMPASSE_CODEX_RESPECT_CONFIG", None)
        os.environ.pop("OPENAI_BASE_URL", None)
    argv_c = run.build_claude_argv(be_c.command, instruction="LENS")
    # json (not text): the envelope carries the RESOLVED model, time-to-first-token and session id
    # that a plain text answer discards — see build_claude_argv and issue #11.
    check("-p" in argv_c and "LENS" in argv_c and argv_c[argv_c.index("--output-format") + 1] == "json", "build_claude_argv: -p + json envelope output")
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
    _rc_argv = run.build_codex_argv(["/x/codex"], instruction="I", output_last_message="/tmp/o")
    check("--ignore-user-config" in _rc_argv and "--ignore-rules" not in _rc_argv,
          "build_codex_argv: RESPECT_CONFIG drops only --ignore-rules; --ignore-user-config stays (F001)")
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
    # IMPASSE_HOST is set suite-wide (see setup), so this cross_provider tier is ASSERTED and owes
    # a soft provenance notice — but never a DOWNGRADE notice. Both halves matter: the tier stays
    # positive, and the operator can still see the claim was taken on their word.
    _cp_notice = codex_only.get("independence_notice") or ""
    check(codex_only.get("independence") == "cross_provider" and "ASSERTION" in _cp_notice
          and "Same-provider" not in _cp_notice and "undetermined" not in _cp_notice.lower(),
          "review(codex): cross-provider keeps its tier, carries an asserted-provenance notice")
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
    # F012(core-rev): the LIBRARY setter (not just the CLI) refuses a non-null claude effort — dead
    # config the runner can't consume — but still allows CLEARING one (legacy migration path).
    _claude_effort_refused = False
    try:
        lib.set_default_effort("claude", "high")
    except ValueError:
        _claude_effort_refused = True
    check(_claude_effort_refused, "F012: set_default_effort refuses a non-null claude write (library level)")
    lib.set_default_effort("claude", None)   # clearing must NOT raise (migration path)
    check(True, "F012: set_default_effort(claude, None) clears without error")
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

    # --- execution speed (Codex Fast mode): a codex-only service-tier knob, mirroring effort ---
    _sp_fast = run.build_codex_argv(["/x/codex"], instruction="I", output_last_message="/tmp/o", speed="fast")
    check('service_tier="fast"' in _sp_fast and "features.fast_mode=true" in _sp_fast,
          "build_codex_argv: speed=fast adds both -c service_tier and -c features.fast_mode")
    _sp_std = run.build_codex_argv(["/x/codex"], instruction="I", output_last_message="/tmp/o", speed="standard")
    _sp_none = run.build_codex_argv(["/x/codex"], instruction="I", output_last_message="/tmp/o")
    check('service_tier="fast"' not in _sp_std and "features.fast_mode=true" not in _sp_std
          and 'service_tier="fast"' not in _sp_none and "features.fast_mode=true" not in _sp_none,
          "build_codex_argv: speed=standard/None adds neither fast flag")
    check(lib.get_default_speed("codex") is None, "settings: no persisted speed by default")
    # no speed configured -> "standard" (Fast OFF) reported, and no fast flags reach the argv
    _spd_orig, _spd_cap = run.supervise, {}

    def _spd_spy(argv, **kw):
        _spd_cap["argv"] = argv
        return _spd_orig(argv, **kw)
    run.supervise = _spd_spy
    rm = run.review(kind="code", instruction="review", artifact_bytes=b"code", no_record=True)
    run.supervise = _spd_orig
    check(rm["ok"] and rm.get("speed") == "standard"
          and 'service_tier="fast"' not in _spd_cap.get("argv", []),
          "review: no speed configured -> standard (Fast OFF) reported, no fast flags in argv")
    lib.set_default_speed("codex", "fast")
    check(lib.get_default_speed("codex") == "fast", "settings: set/get persisted default speed")
    rm = run.review(kind="code", instruction="review", artifact_bytes=b"code", no_record=True)
    check(rm.get("speed") == "fast", "review: persisted default speed resolves when no flag/env")
    os.environ["IMPASSE_CODEX_SPEED"] = "standard"
    rm = run.review(kind="code", instruction="review", artifact_bytes=b"code", no_record=True)
    check(rm.get("speed") == "standard", "review: IMPASSE_CODEX_SPEED beats the persisted default")
    rm = run.review(kind="code", instruction="review", artifact_bytes=b"code", speed="fast", no_record=True)
    check(rm.get("speed") == "fast", "review: per-run --speed beats env and persisted")
    os.environ["IMPASSE_CODEX_SPEED"] = "turbo"
    rm = run.review(kind="code", instruction="review", artifact_bytes=b"code", no_record=True)
    check(rm["ok"] is False and rm["failure"]["code"] == "backend_error"
          and "IMPASSE_CODEX_SPEED" in rm["failure"]["message"],
          "review: invalid env speed -> structured failure naming the env var")
    os.environ.pop("IMPASSE_CODEX_SPEED", None)
    lib.set_default_speed("codex", None)
    try:
        lib.set_default_speed("codex", "turbo")
        _sp_bad = False
    except ValueError:
        _sp_bad = True
    check(_sp_bad, "settings: set_default_speed refuses a disallowed value ('turbo')")
    # only codex has a service-tier knob: the LIBRARY setter refuses a non-null claude write (dead
    # config the runner can't consume) but still allows CLEARING one (legacy migration path).
    _sp_claude_refused = False
    try:
        lib.set_default_speed("claude", "fast")
    except ValueError:
        _sp_claude_refused = True
    check(_sp_claude_refused, "F008: set_default_speed refuses a non-null claude write (library level)")
    lib.set_default_speed("claude", None)   # clearing must NOT raise (migration path)
    check(True, "F008: set_default_speed(claude, None) clears without error")
    with open(lib._settings_path(), "w") as _sf:
        _sf.write('{"default_speed": {"codex": "turbo"}}')
    check(lib.get_default_speed("codex") is None, "settings: hand-edited invalid speed dropped on read (fail safe)")
    check(run._main(["set-speed", "--backend", "codex", "fast", "--clear"]) == 2, "set-speed: a speed + --clear together is rejected")
    check(run._main(["set-speed", "fast"]) == 0 and lib.get_default_speed("codex") == "fast", "set-speed: persists via CLI (and repairs a malformed store)")
    check(run._main(["set-speed", "--clear"]) == 0 and lib.get_default_speed("codex") is None, "set-speed: --clear via CLI")
    # a non-null claude speed write is refused by the library — the CLI must surface it as a clean
    # exit 2, never an uncaught ValueError traceback
    check(run._main(["set-speed", "--backend", "claude", "fast"]) == 2,
          "set-speed: a non-null claude write exits 2 cleanly (no traceback)")
    # the resolved speed must actually reach the codex argv, not just the result metadata
    _spd_orig2, _spd_cap2 = run.supervise, {}

    def _spd_spy2(argv, **kw):
        _spd_cap2["argv"] = argv
        return _spd_orig2(argv, **kw)
    run.supervise = _spd_spy2
    os.environ["IMPASSE_CODEX_SPEED"] = "fast"
    rm = run.review(kind="code", instruction="review", artifact_bytes=b"code", no_record=True)
    run.supervise = _spd_orig2
    os.environ.pop("IMPASSE_CODEX_SPEED", None)
    check(rm.get("speed") == "fast" and 'service_tier="fast"' in _spd_cap2.get("argv", [])
          and "features.fast_mode=true" in _spd_cap2.get("argv", []),
          "review: env-resolved speed reaches the codex argv (not just metadata)")
    # defense in depth: the argv builder itself refuses a non-allowlisted speed (config-syntax payload)
    _sp_inj = False
    try:
        run.build_codex_argv(["/x/codex"], instruction="I", output_last_message="/tmp/o",
                             speed='fast" injected="1')
    except ValueError:
        _sp_inj = True
    check(_sp_inj, "build_codex_argv: rejects a non-allowlisted speed itself (no config injection)")
    # claude has no speed knob: an irrelevant IMPASSE_CLAUDE_SPEED (even an invalid one) must
    # neither fail the run nor be reported as configuration that was applied
    os.environ["IMPASSE_CLAUDE_SPEED"] = "turbo"
    rc = run.review(kind="decision", instruction="review", artifact_bytes=b"memo", backend="claude", no_record=True)
    os.environ.pop("IMPASSE_CLAUDE_SPEED", None)
    check(rc["ok"] is True and rc.get("speed") is None,
          "review(claude): irrelevant IMPASSE_CLAUDE_SPEED neither fails the run nor reports as applied")
    # the generic settings writer preserves sibling keys (model + effort) when speed is written
    lib.set_default_model("codex", "keep-model-3")
    lib.set_default_effort("codex", "low")
    lib.set_default_speed("codex", "fast")
    check(lib.get_default_model("codex") == "keep-model-3" and lib.get_default_effort("codex") == "low"
          and lib.get_default_speed("codex") == "fast",
          "settings: speed write preserves the model and effort defaults")
    lib.set_default_model("codex", None)
    lib.set_default_effort("codex", None)
    lib.set_default_speed("codex", None)
    # speed rides the success-path result metadata alongside model + effort
    rm = run.review(kind="code", instruction="review", artifact_bytes=b"code", no_record=True)
    check(rm["ok"] and "speed" in rm and "model" in rm and "effort" in rm,
          "review: speed appears in a successful codex run's result metadata alongside model/effort")
    # HOST-FACING doc consistency: the operator drives Impasse THROUGH the host AI, so the speed
    # surface must be documented where the host reads (stdlib file reads, no deps).
    def _sp_doc(fn):
        with open(os.path.join(HERE, "..", fn), encoding="utf-8") as _df:
            return _df.read()
    _sp_skill = _sp_doc("SKILL.md")
    _sp_readme = _sp_doc("README.md")
    _sp_codex = _sp_doc("docs/backends/codex.md")
    check(all("--speed" in d and "IMPASSE_CODEX_SPEED" in d and "set-speed" in d
              for d in (_sp_skill, _sp_readme, _sp_codex))
          and "AskUserQuestion" in _sp_skill and "standard" in _sp_skill and "fast" in _sp_skill
          and "codex-only" in _sp_skill,
          "docs: SKILL/README/codex document the --speed / set-speed / IMPASSE_CODEX_SPEED surface")

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
        _rc_notice = rc.get("independence_notice") or ""
        check(rc["ok"] and rc.get("independence") == "cross_provider" and "ASSERTION" in _rc_notice
              and "Same-provider" not in _rc_notice,
              "review(claude, codex host): cross-provider tier kept, asserted provenance disclosed")
        check(rc.get("host") == "codex", "review: result reports the host")
        # force --backend codex: the same-provider path we're asserting is now the NON-default
        # under a codex host (auto would pick claude — see the F002 bare-review tests below)
        rx = run.review(kind="code", instruction="review", artifact_bytes=b"code", backend="codex", no_record=True)
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
        # Passing host= IS an assertion (review_mode records method=override/confidence=asserted for
        # it), so this cross_provider selection owes the soft provenance notice — not a downgrade.
        check(m["tier"] == "cross_provider" and "ASSERTION" in (m["notice"] or "")
              and "Same-provider" not in (m["notice"] or ""),
              "review_mode: an ASSERTED cross_provider keeps its tier and discloses the assertion")
        # A DETECTED identity is the one cross_provider case that owes nothing at all: nothing was
        # guessed and nothing was taken on the operator's word.
        # `claude`, not `codex`: host_detection can only ever emit codex as heuristic or asserted,
        # so a "strong codex" detection is a state the system cannot produce, and asserting on it
        # would pin an impossible combination instead of a real invariant.
        _m_strong = lib.review_mode("code", environment="claude_code", codex_available=True,
                                    claude_available=True, host="claude",
                                    detection={"host": "claude", "method": "auto",
                                               "confidence": "strong"})
        check(_m_strong["tier"] == "cross_provider" and _m_strong["notice"] is None,
              "review_mode: a DETECTED cross_provider owes no notice (nothing inferred or asserted)")
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
            # ...but an AFFIRMATIVE Claude surface marker (no CLAUDECODE) still resolves to claude —
            # yet only at HEURISTIC confidence, NOT strong (integration-review F001): a presence-style
            # flag accepts any value, so a stray one must not mint a SILENT strong cross_provider.
            _p2set(CLAUDECODE="1")
            check(lib.host_detection() == {"host": "claude", "method": "auto", "confidence": "strong"},
                  "detect: CLAUDECODE=1 -> claude/STRONG (strict primary)")
            _p2set(CLAUDE_CODE_ENTRYPOINT="cli")
            check(lib.host_detection() == {"host": "claude", "method": "auto", "confidence": "heuristic"},
                  "detect: stray presence-style CLAUDE_CODE_ENTRYPOINT -> claude/HEURISTIC (not strong)")
            _p2set(CLAUDE_CODE_ENTRYPOINT="garbage")
            check(lib.host_detection()["confidence"] == "heuristic",
                  "detect: arbitrary presence-style value -> heuristic (F001 fail-open closed)")
            # T2: the OTHER two presence-style Claude flags also resolve claude/heuristic (positive path)
            for _var in ("CLAUDE_COWORK", "CLAUDE_CHAT_SANDBOX"):
                _p2set(**{_var: "1"})
                check(lib.host_detection() == {"host": "claude", "method": "auto", "confidence": "heuristic"},
                      f"T2: {_var}=1 -> claude/heuristic")
            # T3: every CLAUDE_SURFACE allowlist value yields strong (not just 'cowork')
            for _sv in ("cowork", "chat", "sandbox"):
                _p2set(CLAUDE_SURFACE=_sv)
                check(lib.host_detection()["confidence"] == "strong", f"T3: CLAUDE_SURFACE={_sv} -> strong")
            # F007: detect_environment matches presence-style markers AFFIRMATIVELY (not raw truthiness),
            # so a stray falsy value can't manufacture a cowork/sandbox surface (which would permit self-review)
            _p2set(CLAUDE_COWORK="0")
            check(lib.detect_environment() == "unknown", "F007: CLAUDE_COWORK=0 -> unknown env (no false cowork)")
            _p2set(CLAUDE_CHAT_SANDBOX="off")
            check(lib.detect_environment() == "unknown", "F007: CLAUDE_CHAT_SANDBOX=off -> unknown env")
            _p2set(CLAUDE_COWORK="1")
            check(lib.detect_environment() == "cowork", "F007: CLAUDE_COWORK=1 -> cowork (affirmative still works)")
            # F003(core-rev): the self-review-gating markers use a STRICT boolean-true allowlist, so an
            # arbitrary non-falsy value can't manufacture a self-review-eligible surface.
            _p2set(CLAUDE_COWORK="garbage")
            check(lib.detect_environment() == "unknown", "F003: CLAUDE_COWORK=garbage -> unknown (not cowork)")
            _p2set(CLAUDE_CHAT_SANDBOX="true")
            check(lib.detect_environment() == "chat_sandbox", "F003: CLAUDE_CHAT_SANDBOX=true -> chat_sandbox (allowlist value)")
            # T4: review_mode honors an explicit host='unknown' verbatim (no silent re-detect to a real host)
            _p2set()
            m = lib.review_mode("decision", codex_available=True, claude_available=True, host="unknown")
            check(m["host"] == "unknown" and m["tier"] == "undetermined",
                  "T4: review_mode(host='unknown') -> undetermined, not re-detected")
            # T5: the valid 'other' override host is non-attributable -> undetermined tier
            check(lib.independence_tier("other", "OpenAI") == "undetermined"
                  and lib.independence_tier("other", "Anthropic") == "undetermined",
                  "T5: independence_tier('other', ...) -> undetermined (mixed-model host)")
            _p2set(CLAUDE_SURFACE="cowork")
            # e2e via AUTO: a heuristic Claude host -> auto selects codex -> cross_provider that CARRIES
            # the soft notice (composition of F001 detection + auto selection + notice; not silent).
            _p2set(CLAUDE_CODE_ENTRYPOINT="cli")
            rh = run.review(kind="decision", instruction="review", artifact_bytes=b"m", no_record=True)
            check(rh.get("host") == "claude" and rh.get("backend") == "codex"
                  and rh.get("independence") == "cross_provider"
                  and "INFERRED" in (rh.get("independence_notice") or ""),
                  "F001-integration: heuristic Claude host -> auto codex cross_provider WITH soft notice")
            # F003: review_mode uses a passed detection VERBATIM (heuristic survives, not laundered to asserted)
            _p2set()  # clear markers; pass detection explicitly
            m = lib.review_mode("decision", codex_available=True, claude_available=True,
                                detection={"host": "claude", "method": "auto", "confidence": "heuristic"})
            check(m["host"] == "claude" and m["tier"] == "cross_provider"
                  and m["host_detection"] == {"method": "auto", "confidence": "heuristic"}
                  and "INFERRED" in (m["notice"] or ""),
                  "F003: review_mode preserves a passed heuristic detection (soft notice, not laundered)")

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
                    # Literal, NOT lib.KNOWN_HOSTS: an oracle that reads the production table can
                    # never contradict it, which is the whole point of an independent truth table.
                    # Update this list deliberately when a host is added.
                    if override not in ("claude", "codex", "gemini", "grok", "composer",
                                        "cursor", "other"):
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
                    # Overrides are DERIVED from KNOWN_HOSTS, not hardcoded: this matrix is the
                    # exhaustiveness claim, and a hardcoded list silently stops covering the new
                    # host the moment one is added (adding `grok` did exactly that).
                    for ov in (None, "", *lib.KNOWN_HOSTS, "zzinvalid"):
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

            # F002: the DEFAULT backend is host-aware ('auto'). A BARE review (no --backend) on a
            # Codex host must auto-select the cross-provider claude backend, not the same-provider codex.
            _p2set(CODEX_SANDBOX="seatbelt")
            ra = run.review(kind="decision", instruction="review", artifact_bytes=b"memo", no_record=True)
            check(ra.get("ok") and ra.get("host") == "codex" and ra.get("backend") == "claude"
                  and ra.get("independence") == "cross_provider",
                  "F002: bare review on a codex host auto-selects the claude (cross-provider) backend")
            # ...and on a Claude host the default still resolves to codex (unchanged behavior)
            _p2set(CLAUDECODE="1")
            rcl = run.review(kind="decision", instruction="review", artifact_bytes=b"memo", no_record=True)
            check(rcl.get("ok") and rcl.get("host") == "claude" and rcl.get("backend") == "codex"
                  and rcl.get("independence") == "cross_provider",
                  "F002: bare review on a claude host auto-selects codex (unchanged)")
            # an explicit --backend still overrides auto (force same-provider if the operator insists)
            _p2set(CODEX_SANDBOX="seatbelt")
            rfx = run.review(kind="decision", instruction="review", artifact_bytes=b"memo", backend="codex", no_record=True)
            check(rfx.get("backend") == "codex" and rfx.get("independence") == "same_provider",
                  "F002: explicit --backend codex overrides auto (same-provider, honestly labeled)")

            # F002 availability (the fail-open guard): when the cross-provider backend is UNavailable,
            # auto must DEGRADE honestly to the same-provider one — never fake cross_provider — and with
            # NO backend it must fail closed, carrying the host + provenance (exercises the new branch).
            _orig_rc, _orig_rcl = lib.resolve_codex_command, lib.resolve_claude_command
            try:
                _p2set(CODEX_SANDBOX="seatbelt")   # codex host; IMPASSE_CODEX_BIN fake still runnable
                lib.resolve_claude_command = lambda: None   # cross-provider (claude) backend unavailable
                rdeg = run.review(kind="decision", instruction="review", artifact_bytes=b"memo", no_record=True)
                check(rdeg.get("ok") and rdeg.get("backend") == "codex"
                      and rdeg.get("independence") == "same_provider"
                      and "Same-provider" in (rdeg.get("independence_notice") or ""),
                      "F002: codex host, only codex available -> auto degrades to same_provider (no false cross)")
                lib.resolve_codex_command = lambda: None     # now NEITHER backend available
                rnone = run.review(kind="decision", instruction="review", artifact_bytes=b"memo", no_record=True)
                check(rnone.get("ok") is False and rnone.get("failure", {}).get("code") == "backend_error"
                      and rnone.get("host") == "codex"
                      and rnone.get("host_detection") == {"method": "auto", "confidence": "heuristic"},
                      "F002: no backend available -> auto fails closed, carrying host + provenance")
            finally:
                lib.resolve_codex_command, lib.resolve_claude_command = _orig_rc, _orig_rcl

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
        # force --backend codex: this matrix asserts the codex argv/effort/retry path, which under a
        # codex host is no longer the auto default (auto picks claude — the F002 change)
        res_m = run.review(kind="code", instruction="review", artifact_bytes=b"code", backend="codex", no_record=True)
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

    # F002: a recorded run whose reviewer review_id ('r') collides lands in a UNIQUE suffixed dir whose
    # STORED review_id matches the dir — so a later reconciliation links to THIS record, not another.
    res2 = run.review(kind="code", instruction="review", artifact_bytes=b"code2")
    _rid2 = res2.get("run_id")
    check(_rid2 and _rid2 != "r" and _rid2.startswith("r-")
          and lib.load_run(_rid2)["reviewer_response"]["review_id"] == _rid2,
          "F002: reserved run_id is propagated into the stored record (reconciliation links correctly)")
    lib.forget_run(_rid2)

    # F004: reserve_run_id gives a UNIQUE dir per run so a reused/untrusted review_id can't clobber
    id1 = lib.reserve_run_id("dup-review-id")
    id2 = lib.reserve_run_id("dup-review-id")
    check(id1 == "dup-review-id" and id2 == "dup-review-id-2" and id1 != id2
          and os.path.isdir(lib._run_dir(id1)) and os.path.isdir(lib._run_dir(id2)),
          "F004: reserve_run_id disambiguates a colliding review_id (no silent overwrite)")
    lib.forget_run(id1)
    lib.forget_run(id2)
    # F009: a non-positive/non-finite timeout becomes a STRUCTURED failure, not an uncaught traceback
    rbad = run.review(kind="code", instruction="review", artifact_bytes=b"x", wall_timeout=-5, no_record=True)
    check(rbad.get("ok") is False and rbad.get("failure", {}).get("code") == "backend_error"
          and "--wall" in rbad.get("failure", {}).get("message", "")
          and rbad.get("host_detection") is not None,
          "F009: bad --wall -> structured backend_error (with provenance), not a traceback")
    # F010: a get_backend failure path still reports host_detection provenance
    _saved_cbin = os.environ.pop("IMPASSE_CODEX_BIN", None)
    _orig_rc2 = lib.resolve_codex_command
    try:
        lib.resolve_codex_command = lambda: None
        rnb = run.review(kind="code", instruction="review", artifact_bytes=b"x", backend="codex", no_record=True)
        check(rnb.get("ok") is False and rnb.get("host_detection") is not None,
              "F010: get_backend failure carries host_detection provenance")
    finally:
        lib.resolve_codex_command = _orig_rc2
        if _saved_cbin is not None:
            os.environ["IMPASSE_CODEX_BIN"] = _saved_cbin
    res = run.review(kind="code", instruction="review", artifact_bytes=b"code", no_record=True)
    check(res.get("recorded") is False, "run record: --no-record skips persistence")
    check(res.get("record_notice") == "Not recorded (--no-record).", "run record: --no-record notice surfaced")

    drid = _json.load(open("schemas/examples/decision.reviewer-response.json"))["review_id"]
    lib.save_run_doc(drid, "reviewer-response", _json.load(open("schemas/examples/decision.reviewer-response.json")))
    lib.save_run_doc(drid, "reconciliation-result",
                     _json.load(open("schemas/examples/decision.reconciliation-result.json")),
                     _sanctioned=lib._RECONCILIATION_TOKEN)
    out = report.render(lib.load_run(drid))
    check("Decisions:" in out and "escalated to you" in out, "report: renders the decisions tally")
    check("reviewer ▶" in out and "you      ◀" in out, "report: shows the reviewer/host back-and-forth")
    check("Question for you" in out and "decision(s) need you" in out, "report: shows the escalated question")
    check(any(r["run_id"] == drid for r in lib.list_runs()), "run record: listed by list_runs")

    # --- report credits operator-decided items (issue #5): a resolved item carrying an escalation
    # object was decided BY the operator, not settled between the models — the tally + footer must say so ---
    # A matching reviewer_response (review_id + the referenced finding ids) is now required for
    # render() to treat the record as verified at all (issue #16/D4) — the original fixtures used
    # `{}` as a stand-in for "don't care", which is now indistinguishable from a genuine pairing
    # mismatch, so each fixture below is a minimal but properly PAIRED record instead.
    def _b_rev(review_id, ids):
        return {"review_id": review_id, "findings": [{"id": fid} for fid in ids]}

    _b_run1 = {"reviewer_response": _b_rev("b-1", ["F1", "F2"]),
               "reconciliation_result": {"schema_version": "1.0", "reconciliation_id": "b1",
               "review_id": "b-1", "outcome": "converged", "items": [
        {"finding_id": "F1", "state": "resolved", "resolution": "Operator chose repair A over B.",
         "escalation": {"dispute_kind": "value_or_priority_tradeoff",
                        "stop_reason": "operator_authority_required", "operator_question": "A or B?"}},
        {"finding_id": "F2", "state": "accepted"}]}}
    _b_out1 = report.render(_b_run1)
    check("UNVERIFIABLE" not in _b_out1, "report: a properly paired record is not flagged unverifiable")
    check("decided by you" in _b_out1 and "you decided" in _b_out1
          and "Nothing is waiting on you" in _b_out1 and "Nothing needed you" not in _b_out1,
          "report: operator-decided (resolved + escalation) item is credited, not counted as autonomous")
    _b_run2 = {"reviewer_response": _b_rev("b-2", ["F1", "F2"]),
               "reconciliation_result": {"schema_version": "1.0", "reconciliation_id": "b2",
               "review_id": "b-2", "outcome": "converged", "items": [
        {"finding_id": "F1", "state": "resolved", "resolution": "host fix"},
        {"finding_id": "F2", "state": "accepted"}]}}
    _b_out2 = report.render(_b_run2)
    check("Nothing needed you — the models settled all 2 between themselves." in _b_out2
          and "decided by you" not in _b_out2,
          "report: genuinely-autonomous run still says 'Nothing needed you'")
    _b_run3 = {"reviewer_response": _b_rev("b-3", ["F1", "F2"]),
               "reconciliation_result": {"schema_version": "1.0", "reconciliation_id": "b3",
               "review_id": "b-3", "outcome": "deadlocked", "items": [
        {"finding_id": "F1", "state": "deadlocked",
         "escalation": {"dispute_kind": "x", "stop_reason": "y", "operator_question": "q1?"}},
        {"finding_id": "F2", "state": "resolved", "resolution": "op ruling",
         "escalation": {"dispute_kind": "z", "stop_reason": "w", "operator_question": "q2?"}}]}}
    _b_out3 = report.render(_b_run3)
    # Mixed case: pending deadlock AND an operator-decided item. The footer stays on the pending
    # branch, but must CREDIT the operator ("you decided 1") — not attribute their ruling to the
    # models. The footer is the last rendered line; "you decided" appears only there (not the tally).
    _b_foot3 = _b_out3.splitlines()[-1]
    check("decision(s) need you" in _b_out3 and "escalated to you" in _b_out3
          and "decided by you" in _b_out3
          and "you decided 1" in _b_foot3 and "decision(s) need you" in _b_foot3,
          "report: a deadlock still takes footer precedence over an operator-decided item")

    # --- lifetime recap: aggregate value across reconciled runs (isolated config dir) ---
    recap_dir = tempfile.mkdtemp(prefix="impasse-recap-")
    _prev_cfg = os.environ["IMPASSE_CONFIG_DIR"]
    os.environ["IMPASSE_CONFIG_DIR"] = recap_dir
    check(report.lifetime_recap() == "", "recap: empty when nothing reconciled")
    # Each reconciliation now needs a matching, PAIRED reviewer-response to count as verified at all
    # (D5) — a rejected item also needs contradicting verification (D6), which the original fixture
    # left implicit; both are made explicit here rather than narrowing what the test checks.
    lib.save_run_doc("recap-a", "reviewer-response",
                     {"schema_version": "1.0", "review_id": "recap-a",
                      "artifact": {"kind": "code", "revision": {"algorithm": "sha256", "value": "x"}},
                      "assessment": "needs_attention", "summary": "s", "findings": [
                          {"id": "F1", "severity": "low", "category": "x", "claim": "c", "evidence": []},
                          {"id": "F2", "severity": "low", "category": "x", "claim": "c", "evidence": []},
                          {"id": "F3", "severity": "low", "category": "x", "claim": "c", "evidence": []}]})
    lib.save_run_doc("recap-b", "reviewer-response",
                     {"schema_version": "1.0", "review_id": "recap-b",
                      "artifact": {"kind": "code", "revision": {"algorithm": "sha256", "value": "x"}},
                      "assessment": "needs_attention", "summary": "s", "findings": [
                          {"id": "F1", "severity": "low", "category": "x", "claim": "c", "evidence": []},
                          {"id": "F2", "severity": "low", "category": "x", "claim": "c", "evidence": []}]})
    rec_a = {"schema_version": "1.0", "reconciliation_id": "a", "review_id": "recap-a",
             "outcome": "deadlocked", "items": [
                 {"finding_id": "F1", "state": "accepted"},
                 {"finding_id": "F2", "state": "rejected",
                  "verification": [{"method": "artifact_inspection", "result": "contradicts", "detail": "checked"}]},
                 {"finding_id": "F3", "state": "deadlocked",
                  "escalation": {"dispute_kind": "value_or_priority_tradeoff",
                                 "stop_reason": "operator_authority_required", "operator_question": "q?"}}]}
    rec_b = {"schema_version": "1.0", "reconciliation_id": "b", "review_id": "recap-b",
             "outcome": "converged", "items": [
                 {"finding_id": "F1", "state": "accepted"},
                 {"finding_id": "F2", "state": "resolved", "resolution": "done"}]}
    lib.save_run_doc("recap-a", "reconciliation-result", rec_a, _sanctioned=lib._RECONCILIATION_TOKEN)
    lib.save_run_doc("recap-b", "reconciliation-result", rec_b, _sanctioned=lib._RECONCILIATION_TOKEN)
    recap = report.lifetime_recap()
    check("2 reviews reconciled" in recap, "recap: counts reconciled runs")
    check("5 findings reviewed" in recap and "2 accepted" in recap, "recap: sums findings + accepted")
    check("1 refuted with evidence" in recap and "1 resolved" in recap and "1 awaiting you" in recap, "recap: resolved and escalated counted separately (not conflated)")

    # --- D5 [R]: eligibility comes from the VALIDATOR, not from "does a sibling file exist" — and
    # the exclusion is DISCLOSED, never silent (a number that quietly stopped counting something is
    # exactly the failure mode issue #16 is about). ---
    orphan_rec = {"schema_version": "1.0", "reconciliation_id": "o", "review_id": "recap-orphan",
                  "outcome": "converged", "items": [{"finding_id": "F1", "state": "accepted"},
                                                     {"finding_id": "F2", "state": "accepted"}]}
    lib.save_run_doc("recap-orphan", "reconciliation-result", orphan_rec, _sanctioned=lib._RECONCILIATION_TOKEN)   # no reviewer-response saved: an orphan
    _recap_o = report.lifetime_recap()
    check("2 reviews reconciled" in _recap_o,
          "recap [R/D5]: an orphan's items don't inflate the reconciled-review count (still 2, not 3)")
    check("5 findings reviewed" in _recap_o,
          "recap [R/D5]: an orphan's 2 findings don't inflate the findings-reviewed total (still 5)")
    check("1 record" in _recap_o and "quarantined" in _recap_o,
          "recap [R/D5]: the excluded orphan is disclosed by name/count, not dropped silently")

    # --- F005 [R]: quarantine is not ONLY "missing sibling" — a fabricated finding_id must trigger
    # it too, via the SAME validator (a record can be paired to the right review and still be lying
    # about which findings it disposed of). ---
    lib.save_run_doc("recap-fake-fid", "reviewer-response",
                     {"schema_version": "1.0", "review_id": "recap-fake-fid",
                      "artifact": {"kind": "code", "revision": {"algorithm": "sha256", "value": "x"}},
                      "assessment": "needs_attention", "summary": "s",
                      "findings": [{"id": "F1", "severity": "low", "category": "x", "claim": "c", "evidence": []}]})
    fake_fid_rec = {"schema_version": "1.0", "reconciliation_id": "f", "review_id": "recap-fake-fid",
                    "outcome": "converged", "items": [{"finding_id": "F999", "state": "accepted"}]}
    lib.save_run_doc("recap-fake-fid", "reconciliation-result", fake_fid_rec, _sanctioned=lib._RECONCILIATION_TOKEN)
    _recap_f = report.lifetime_recap()
    check("2 reviews reconciled" in _recap_f,
          "recap [R/F005]: a fabricated finding_id quarantines the record too, not only a missing sibling")
    check("2 record" in _recap_f and "quarantined" in _recap_f,
          "recap [R/F005]: both quarantined records (orphan + fabricated finding_id) are disclosed")

    lib.save_run_doc("recap-review-only", "reviewer-response",
                     {"schema_version": "1.0", "review_id": "recap-review-only",
                      "artifact": {"kind": "code", "revision": {"algorithm": "sha256", "value": "x"}},
                      "assessment": "approve", "summary": "s", "findings": []})
    check("2 reviews reconciled" in report.lifetime_recap(), "recap: review-only runs don't inflate the count")
    os.environ["IMPASSE_CONFIG_DIR"] = _prev_cfg

    check(lib.forget_run(drid) is True and lib.load_run(drid)["reviewer_response"] is None, "run record: forget deletes it")

    # --- housekeeping: open-escalation detection + prune ---
    # A matching reviewer-response is required so this run is a VALID (verifiable) record under
    # reconciliation_problems() — without one it would now be an orphan and excluded from open_runs().
    lib.save_run_doc("open-run", "reviewer-response",
                     {"schema_version": "1.0", "review_id": "open-run",
                      "artifact": {"kind": "code", "revision": {"algorithm": "sha256", "value": "x"}},
                      "assessment": "needs_attention", "summary": "s",
                      "findings": [{"id": "F001", "severity": "medium", "category": "x", "claim": "c",
                                    "evidence": [{"anchor": {"type": "file_range", "path": "p", "line_start": 1},
                                                  "observation": "o", "grounding": "artifact_observed"}]}]})
    open_rec = {"schema_version": "1.0", "reconciliation_id": "x", "review_id": "open-run",
                "outcome": "deadlocked", "items": [{"finding_id": "F001", "state": "deadlocked",
                "escalation": {"dispute_kind": "value_or_priority_tradeoff",
                               "stop_reason": "operator_authority_required", "operator_question": "pick one?"}}]}
    lib.save_run_doc("open-run", "reconciliation-result", open_rec, _sanctioned=lib._RECONCILIATION_TOKEN)
    check(any(r["run_id"] == "open-run" for r in report.open_runs()), "housekeeping: open_runs detects an unresolved escalation")
    resolved_rec = {"schema_version": "1.0", "reconciliation_id": "x", "review_id": "open-run",
                    "outcome": "converged", "items": [{"finding_id": "F001", "state": "resolved", "resolution": "decided"}]}
    lib.save_run_doc("open-run", "reconciliation-result", resolved_rec, _sanctioned=lib._RECONCILIATION_TOKEN)
    check(not any(r["run_id"] == "open-run" for r in report.open_runs()), "housekeeping: resolving clears the open flag")
    old = time.time() - 3 * 86400
    os.utime(os.path.join(lib.runs_dir(), "open-run"), (old, old))
    deleted, _kept, _invalid = report.prune(1)
    check("open-run" in deleted, "housekeeping: prune deletes an old resolved record")
    lib.save_run_doc("old-open", "reconciliation-result", open_rec, _sanctioned=lib._RECONCILIATION_TOKEN)
    os.utime(os.path.join(lib.runs_dir(), "old-open"), (old, old))
    deleted2, kept2, _invalid2 = report.prune(1)
    check("old-open" in kept2 and "old-open" not in deleted2, "housekeeping: prune KEEPS old runs with open escalations")
    deleted3, _k, _invalid3 = report.prune(1, include_open=True)
    check("old-open" in deleted3, "housekeeping: prune --include-open removes even open runs")
    check("old-open" in _invalid3, "housekeeping [R]: prune discloses an invalid record among what it deletes "
          "('old-open' reuses open_rec's review_id 'open-run', so it has no reviewer-response of its own)")

    # --- escalations view: GUARANTEE full deadlock context BEFORE the operator decides (symmetric with `show`) ---
    esc_rev = {"schema_version": "1.0", "review_id": "esc-run", "findings": [
        {"id": "F001", "severity": "high", "category": "correctness", "claim": "CLAIM_TEXT_UNIQUE",
         "evidence": [{"anchor": {"type": "file_range", "path": "anchor_a.py", "line_start": 1, "line_end": 2},
                       "observation": "EVIDENCE_OBS_UNIQUE", "grounding": "artifact_observed"}]},
        {"id": "F002", "severity": "low", "category": "style", "claim": "RESOLVED_CLAIM", "evidence": []},
        {"id": "F003", "severity": "medium", "category": "x", "claim": "HISTORIC_ESC_CLAIM", "evidence": []}]}

    def _esc_item(fid, state, **kw):
        it = {"finding_id": fid, "state": state}
        it.update(kw)
        return it

    def _deadlock(fid="F001", q="OPERATOR_Q_UNIQUE?"):
        esc = {"dispute_kind": "value_or_priority_tradeoff", "stop_reason": "operator_authority_required"}
        if q is not None:
            esc["operator_question"] = q
        return _esc_item(fid, "deadlocked", reviewer_position="REVIEWER_POS_UNIQUE",
                         host_position="HOST_POS_UNIQUE", escalation=esc)

    esc_rec = {"schema_version": "1.0", "reconciliation_id": "y", "review_id": "esc-run",
               "outcome": "deadlocked", "items": [
                   _deadlock(),
                   _esc_item("F002", "resolved", resolution="already decided"),
                   # a RESOLVED item that still carries historical escalation data must NOT re-appear:
                   _esc_item("F003", "resolved", resolution="ruled",
                             escalation={"dispute_kind": "x", "stop_reason": "y", "operator_question": "OLD_Q?"})]}
    _e = report.render_escalations(esc_rec, esc_rev)
    check(all(t in _e for t in ("CLAIM_TEXT_UNIQUE", "anchor_a.py:1-2", "EVIDENCE_OBS_UNIQUE",
              "REVIEWER_POS_UNIQUE", "HOST_POS_UNIQUE", "OPERATOR_Q_UNIQUE?", "ESCALATED")),
          "escalations: renders full deadlock context (claim, evidence anchor, both positions, question)")
    check(not any(t in _e for t in ("already decided", "RESOLVED_CLAIM", "HISTORIC_ESC_CLAIM", "OLD_Q?")),
          "escalations: shows ONLY pending deadlocks — resolved items (even ones with old escalation data) are excluded")
    check("nothing needs you" in report.render_escalations(
        {"review_id": "r", "items": [{"finding_id": "F001", "state": "resolved"}]}, esc_rev),
        "escalations: no deadlocks -> 'nothing needs you'")
    # sanitize UNTRUSTED text across every field a finding/item contributes (claim, position, question)
    _e3 = report.render_escalations(
        {"review_id": "r", "items": [_deadlock(q="q\x1b[0m?") | {"reviewer_position": "p\x1b[31mos"}]},
        {"findings": [{"id": "F001", "claim": "c\x1b[1mlaim", "severity": "high"}]})
    check("\x1b" not in _e3, "escalations: sanitizes terminal escapes in untrusted finding/item text")

    # _escalation_problems is the guarantee: it must flag EVERY way full context could be missing.
    check(report._escalation_problems(esc_rec, esc_rev) == [], "escalations: a fully-populated deadlock has no problems")

    def _one(rec, rev):   # at least one problem flagged
        return len(report._escalation_problems(rec, rev)) >= 1

    def _mk(items):
        return {"review_id": "esc-run", "items": items}
    check(_one(_mk([_deadlock()]), None), "escalations problem: missing reviewer-response (e.g. --no-record run)")
    check(_one(_mk([_deadlock(fid="NOPE")]), esc_rev), "escalations problem: deadlock finding_id has no matching finding")
    check(_one(_mk([_deadlock(fid="F002")]), esc_rev), "escalations problem: matched finding has no anchored evidence")
    check(_one(_mk([_deadlock(q=None)]), esc_rev), "escalations problem: deadlock missing operator_question")
    check(_one(_mk([_deadlock() | {"reviewer_position": ""}]), esc_rev), "escalations problem: deadlock missing a position")
    check(_one(_mk([_deadlock() | {"reviewer_position": "\x1b"}]), esc_rev),
          "escalations problem: a control-char-only position is blank once rendered")
    check(_one({"review_id": None, "items": [_deadlock()]}, esc_rev), "escalations problem: missing/non-string review_id")
    check(_one(_mk([_deadlock()]), {"review_id": "OTHER", "findings": esc_rev["findings"]}),
          "escalations problem: the loaded reviewer-response is for a different review_id")
    check(_one(_mk([_deadlock(), _deadlock()]), esc_rev), "escalations problem: duplicate finding_id across items")
    check(_one(_mk([{"finding_id": "F001", "state": "deadlock"}]), esc_rev),  # typo'd state
          "escalations problem: an unrecognized state (could silently hide an escalation)")
    check(_one(_mk([_deadlock()]), {"review_id": "esc-run", "findings": 5}),
          "escalations problem: reviewer-response findings is not a list (total, no crash)")
    check(report._escalation_problems(_mk([_esc_item("F002", "resolved", resolution="x")]), esc_rev) == [],
          "escalations: an all-resolved reconciliation is problem-free (nothing to decide)")
    # evidence must render to REAL anchored content, not merely be dict-shaped (an empty/blank anchor is hollow)
    check(_one(_mk([_deadlock(fid="F9")]),
               {"review_id": "esc-run", "findings": [{"id": "F9", "claim": "c", "evidence": [{"anchor": {}, "observation": "obs"}]}]}),
          "escalations problem: evidence anchor is empty/unrenderable")
    check(_one(_mk([_deadlock(fid="F9")]),
               {"review_id": "esc-run", "findings": [{"id": "F9", "claim": "c", "evidence": [{"anchor": {"type": "file_range", "path": "p"}, "observation": ""}]}]}),
          "escalations problem: evidence observation is blank")
    # totality: malformed shapes yield problems, never a raise
    check(_one(_mk([{"finding_id": "F1", "state": ["not", "a", "string"]}]), esc_rev),
          "escalations problem: an unhashable item state is flagged, not crashed (validator stays total)")
    check(report._escalation_problems("not a dict", esc_rev) == ["reconciliation is not an object"],
          "escalations: a non-dict reconciliation is total (no crash)")
    # duplicate finding id in the reviewer-response itself -> ambiguous which the deadlock means
    _rev_dup = {"review_id": "esc-run", "findings": [
        {"id": "F1", "claim": "a", "evidence": [{"anchor": {"type": "file_range", "path": "p", "line_start": 1}, "observation": "o"}]},
        {"id": "F1", "claim": "b", "evidence": [{"anchor": {"type": "file_range", "path": "q", "line_start": 2}, "observation": "o2"}]}]}
    check(_one(_mk([_deadlock(fid="F1")]), _rev_dup), "escalations problem: duplicate finding id in the reviewer-response")

    def _good_rev_for(rid):   # a complete, single, matching finding — everything valid except review_id
        return {"review_id": rid, "findings": [{"id": "F1", "claim": "c",
                "evidence": [{"anchor": {"type": "file_range", "path": "p", "line_start": 1}, "observation": "o"}]}]}
    # review_id association is DECISIVE on its own: identical rec/finding, only the response's own review_id differs
    check(report._escalation_problems(_mk([_deadlock(fid="F1")]), _good_rev_for("esc-run")) == [],
          "escalations: matching review_id + complete finding -> no problems (de-confounds the mismatch test)")
    check(_one(_mk([_deadlock(fid="F1")]), _good_rev_for("OTHER")),
          "escalations problem: same complete finding but the response's own review_id differs -> rejected")
    # a non-string (unhashable) severity must not crash the renderer
    check("F1" in report.render_escalations(_mk([_deadlock(fid="F1")]),
          {"review_id": "esc-run", "findings": [{"id": "F1", "claim": "c", "severity": ["oops"],
           "evidence": [{"anchor": {"type": "file_range", "path": "p", "line_start": 1}, "observation": "o"}]}]}),
          "escalations: a non-string severity renders without crashing")
    # positions stored INSIDE the escalation object (schema-permitted) are accepted AND rendered
    _pos_esc = {"finding_id": "F001", "state": "deadlocked",
                "escalation": {"dispute_kind": "x", "stop_reason": "y", "operator_question": "q?",
                               "reviewer_position": "RP_IN_ESC", "host_position": "HP_IN_ESC"}}
    check(report._escalation_problems(_mk([_pos_esc]), esc_rev) == [],
          "escalations: positions inside the escalation object are accepted (schema-valid placement)")
    check(all(t in report.render_escalations(_mk([_pos_esc]), esc_rev) for t in ("RP_IN_ESC", "HP_IN_ESC")),
          "escalations: renders positions that live in the escalation object")

    # CLI dispatch: a fully-populated draft renders + exit 0; every incomplete/bad input -> exit 2 (never a partial view)
    lib.save_run_doc("esc-run", "reviewer-response", esc_rev)
    def _esc_cli(rec_obj):
        p = os.path.join(tmp, "esc-cli.json")
        with open(p, "w") as f:
            _json.dump(rec_obj, f)
        ob, eb = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(ob), contextlib.redirect_stderr(eb):
            rc = report._main(["escalations", p])
        return rc, ob.getvalue(), eb.getvalue()
    _rc, _outp, _errp = _esc_cli(esc_rec)
    check(_rc == 0 and "CLAIM_TEXT_UNIQUE" in _outp and "OPERATOR_Q_UNIQUE?" in _outp,
          "escalations CLI: fully-populated draft -> exit 0 + full context (reviewer-response loaded by review_id)")
    # a refusal writes the diagnostic to STDERR and prints NOTHING to stdout (no partial view can leak)
    _rc2, _out2, _err2 = _esc_cli({"review_id": "esc-run", "items": [_deadlock(q=None)]})
    check(_rc2 == 2 and _out2 == "" and "refusing to present a partial view" in _err2,
          "escalations CLI: a deadlock missing its question -> exit 2, diagnostic on stderr, empty stdout")
    check(_esc_cli({"review_id": "no-such-run", "items": [_deadlock()]})[0] == 2,
          "escalations CLI: no recorded reviewer-response for the review_id -> exit 2")
    check(_esc_cli({"review_id": "esc-run", "items": [123, "nope"]})[0] == 2,
          "escalations CLI: non-dict items -> exit 2, not a traceback")
    # untrusted malformed reviewer data (unhashable finding id) must fail controlled, not traceback
    lib.save_run_doc("bad-rev", "reviewer-response", {"schema_version": "1.0", "review_id": "bad-rev",
                     "findings": [{"id": ["not", "hashable"], "claim": "x"}]})
    check(_esc_cli({"review_id": "bad-rev", "items": [_deadlock()]})[0] == 2,
          "escalations CLI: malformed reviewer-response (unhashable id) -> exit 2, not a crash")
    _badf = os.path.join(tmp, "not-rec.json")
    with open(_badf, "w") as f:
        f.write('{"hello": "world"}')
    check(report._main(["escalations", _badf]) == 2, "escalations CLI: a non-reconciliation file -> exit 2")

    # --- issues #16/#17/#18: reconciliation write/read integrity -----------------------------------
    # All three are one defect seen from three sides: a reconciliation can become separated from the
    # reviewer-response it claims to reconcile, and nothing used to notice. lib.reconciliation_problems
    # is the one shared validator; lib.save_reconciliation_doc is the one sanctioned way to write.

    def _rt_rev(review_id, ids=("F001", "F002", "F003")):
        return {"schema_version": "1.0", "review_id": review_id,
                "artifact": {"kind": "code", "revision": {"algorithm": "sha256", "value": "x"}},
                "assessment": "needs_attention", "summary": "s",
                "findings": [{"id": fid, "severity": "medium", "category": "x", "claim": f"claim {fid}",
                              "evidence": [{"anchor": {"type": "file_range", "path": "p", "line_start": 1},
                                            "observation": "o", "grounding": "artifact_observed"}]}
                             for fid in ids]}

    def _rt_item(fid, state, **kw):
        it = {"finding_id": fid, "state": state}
        it.update(kw)
        return it

    def _save_cli(doc, *extra_args):
        p = os.path.join(tmp, "save-rec-input.json")
        with open(p, "w") as f:
            _json.dump(doc, f)
        ob, eb = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(ob), contextlib.redirect_stderr(eb):
            rc = report._main(["save-reconciliation", p, *extra_args])
        return rc, ob.getvalue(), eb.getvalue()

    def _list_cli():
        ob, eb = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(ob), contextlib.redirect_stderr(eb):
            rc = report._main(["list"])
        return rc, ob.getvalue()

    def _open_cli():
        ob, eb = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(ob), contextlib.redirect_stderr(eb):
            rc = report._main(["open"])
        return rc, ob.getvalue()

    # --- lib.reconciliation_problems(): the shared validator, checked in isolation first (D6) ---
    check(lib.reconciliation_problems("not a dict", None) == ["reconciliation is not an object"],
          "reconciliation_problems: non-dict input is total, returns exactly the one reason")
    check(lib.reconciliation_problems({"schema_version": "1.0", "reconciliation_id": "x", "review_id": "y",
                                       "outcome": "converged", "items": "not a list"}, None) != [],
          "reconciliation_problems [hardening]: non-list 'items' is flagged, never raises")
    check(lib.reconciliation_problems({"schema_version": "1.0", "reconciliation_id": "x", "review_id": "y",
                                       "outcome": "converged",
                                       "items": [{"finding_id": None, "state": "accepted"}]}, None) != [],
          "reconciliation_problems [hardening]: a null finding_id is flagged, never raises")
    check(lib.reconciliation_problems({"review_id": "y",
                                       "items": [{"finding_id": ["not", "hashable"], "state": "accepted"}]}, None) != [],
          "reconciliation_problems [hardening]: an unhashable finding_id is flagged, not crashed")
    check(lib.reconciliation_problems({}, None) != [],
          "reconciliation_problems: missing required top-level fields is flagged")
    check(any("no escalation" in p for p in lib.reconciliation_problems(
        {"schema_version": "1.0", "reconciliation_id": "x", "review_id": "y", "outcome": "deadlocked",
         "items": [{"finding_id": "F1", "state": "deadlocked"}]}, None)),
          "reconciliation_problems [D6]: a deadlocked item with no escalation object is flagged")
    check(any("converged" in p and "deadlocked" in p for p in lib.reconciliation_problems(
        {"schema_version": "1.0", "reconciliation_id": "x", "review_id": "y", "outcome": "converged",
         "items": [{"finding_id": "F1", "state": "deadlocked",
                    "escalation": {"dispute_kind": "x", "stop_reason": "y", "operator_question": "q?"}}]}, None)),
          "reconciliation_problems [D6]: outcome 'converged' alongside a deadlocked item is inconsistent")
    check(any("no item is in state deadlocked" in p for p in lib.reconciliation_problems(
        {"schema_version": "1.0", "reconciliation_id": "x", "review_id": "y", "outcome": "deadlocked",
         "items": [{"finding_id": "F1", "state": "accepted"}]}, None)),
          "reconciliation_problems [D6]: outcome 'deadlocked' with no deadlocked item is inconsistent")

    # --- #17.1: an unknown review_id is refused, and refusing must NOT create an orphan run dir ---
    os.makedirs(lib.runs_dir(), exist_ok=True)
    _before_dirs = set(os.listdir(lib.runs_dir()))
    _rc, _out, _err = _save_cli({"schema_version": "1.0", "reconciliation_id": "z1", "review_id": "rt-unknown",
                                 "outcome": "converged", "items": [{"finding_id": "F001", "state": "accepted"}]})
    check(_rc != 0, "save-reconciliation [#17.1]: an unknown review_id is refused (exit != 0)")
    check(not os.path.isdir(lib._run_dir("rt-unknown")),
          "save-reconciliation [#17.1]: refusing does NOT create the orphan run directory")
    check(set(os.listdir(lib.runs_dir())) == _before_dirs,
          "save-reconciliation [#17.1]: no run directory of ANY name appeared as a side effect")

    lib.save_run_doc("rt-run", "reviewer-response", _rt_rev("rt-run"))

    # --- #17.2: a fabricated finding_id is refused, naming it ---
    _rc, _out, _err = _save_cli({"schema_version": "1.0", "reconciliation_id": "z2", "review_id": "rt-run",
                                 "outcome": "converged",
                                 "items": [_rt_item("F001", "accepted"), _rt_item("F002", "accepted"),
                                           _rt_item("F003", "accepted"), _rt_item("F999", "accepted")]})
    check(_rc != 0 and "F999" in _err, "save-reconciliation [#17.2]: a fabricated finding_id is refused, naming it")
    check(not os.path.isfile(os.path.join(lib._run_dir("rt-run"), "reconciliation-result.json")),
          "save-reconciliation [#17.2]: the refused document was not written")

    # --- #17.3: duplicate finding_ids are refused ---
    _rc, _out, _err = _save_cli({"schema_version": "1.0", "reconciliation_id": "z3", "review_id": "rt-run",
                                 "outcome": "converged",
                                 "items": [_rt_item("F001", "accepted"), _rt_item("F001", "accepted"),
                                           _rt_item("F002", "accepted"), _rt_item("F003", "accepted")]})
    check(_rc != 0 and "duplicate" in _err and "F001" in _err,
          "save-reconciliation [#17.3]: a duplicate finding_id is refused, naming it")

    # --- #17.4: partial coverage is refused; --partial allows it and reports N of M ---
    _partial_doc = {"schema_version": "1.0", "reconciliation_id": "z4", "review_id": "rt-run",
                    "outcome": "incomplete", "items": [_rt_item("F001", "accepted"), _rt_item("F002", "accepted")]}
    _rc, _out, _err = _save_cli(_partial_doc)
    check(_rc != 0 and "partial" in _err,
          "save-reconciliation [#17.4]: partial coverage (2 of 3) is refused without --partial")
    check(not os.path.isfile(os.path.join(lib._run_dir("rt-run"), "reconciliation-result.json")),
          "save-reconciliation [#17.4]: the refused partial document was not written")
    _rc2, _out2, _err2 = _save_cli(_partial_doc, "--partial")
    check(_rc2 == 0 and "2 of 3" in _out2,
          "save-reconciliation [#17.4]: --partial allows it and the success line reports 'N of M'")

    # --- [R] F002: --partial may not write outcome:'converged' — the exact bug class behind a flag ---
    _conv_partial = {"schema_version": "1.0", "reconciliation_id": "z5", "review_id": "rt-run",
                     "outcome": "converged", "items": [_rt_item("F001", "accepted"), _rt_item("F002", "accepted")]}
    _rc, _out, _err = _save_cli(_conv_partial, "--partial")
    check(_rc != 0 and "converged" in _err,
          "save-reconciliation [R/F002]: --partial refuses outcome:'converged' with incomplete coverage")

    # --- [R] D6: a 'rejected' item with no contradicting verification is refused (the protocol invariant) ---
    _rej_doc = {"schema_version": "1.0", "reconciliation_id": "z6", "review_id": "rt-run",
               "outcome": "converged",
               "items": [_rt_item("F001", "rejected"), _rt_item("F002", "accepted"), _rt_item("F003", "accepted")]}
    _rc, _out, _err = _save_cli(_rej_doc)
    check(_rc != 0 and "contradicts" in _err,
          "save-reconciliation [R/D6]: a rejected item with no contradicting verification is refused")

    # --- [R] F008: the sibling reviewer-response's OWN review_id disagreeing is refused (not just absence) ---
    lib.save_run_doc("rt-mismatch", "reviewer-response", _rt_rev("OTHER-REVIEW-ID"))
    _mm_doc = {"schema_version": "1.0", "reconciliation_id": "z7", "review_id": "rt-mismatch",
              "outcome": "converged",
              "items": [_rt_item("F001", "accepted"), _rt_item("F002", "accepted"), _rt_item("F003", "accepted")]}
    _rc, _out, _err = _save_cli(_mm_doc)
    check(_rc != 0 and "does not" in _err and "match" in _err,
          "save-reconciliation [R/F008]: a reviewer-response whose own review_id disagrees is refused")

    # --- [R] F001: the guard is at the STORAGE BOUNDARY — lib.save_reconciliation_doc refuses directly,
    # not only when called via the CLI ---
    _res_f001 = lib.save_reconciliation_doc({"schema_version": "1.0", "reconciliation_id": "z8",
                                             "review_id": "rt-no-such-review", "outcome": "converged",
                                             "items": [{"finding_id": "F001", "state": "accepted"}]})
    check(_res_f001.get("ok") is False and bool(_res_f001.get("reasons")),
          "lib.save_reconciliation_doc [R/F001]: refuses a bad pair directly at the library boundary")

    # --- #18: overwrite is refused without --force; --force writes AND backs up the old content ---
    lib.save_run_doc("rt-force", "reviewer-response", _rt_rev("rt-force"))
    _first_doc = {"schema_version": "1.0", "reconciliation_id": "first", "review_id": "rt-force",
                 "outcome": "converged",
                 "items": [_rt_item("F001", "accepted"), _rt_item("F002", "accepted"), _rt_item("F003", "accepted")]}
    _rc1, _out1, _err1 = _save_cli(_first_doc)
    check(_rc1 == 0 and _out1.startswith("saved:"),
          "save-reconciliation [#18]: the first save succeeds and says 'saved' (not 'replaced')")
    _second_doc = {"schema_version": "1.0", "reconciliation_id": "second", "review_id": "rt-force",
                  "outcome": "converged",
                  "items": [_rt_item("F001", "rejected", verification=[
                                {"method": "artifact_inspection", "result": "contradicts", "detail": "x"}]),
                            _rt_item("F002", "accepted"), _rt_item("F003", "accepted")]}
    _rc2, _out2, _err2 = _save_cli(_second_doc)
    check(_rc2 != 0 and "reconciliation_id='first'" in _err2,
          "save-reconciliation [#18]: overwrite refused WITHOUT --force, naming the existing reconciliation_id")
    check(_json.load(open(os.path.join(lib._run_dir("rt-force"), "reconciliation-result.json")))["reconciliation_id"] == "first",
          "save-reconciliation [#18]: the ORIGINAL content is untouched after a refused overwrite")
    _rc3, _out3, _err3 = _save_cli(_second_doc, "--force")
    check(_rc3 == 0 and "replaced:" in _out3,
          "save-reconciliation [#18]: --force writes and the success line says 'replaced' (not 'saved')")
    _backup_path = os.path.join(lib._run_dir("rt-force"), "reconciliation-result.1.json")
    check(os.path.isfile(_backup_path),
          "save-reconciliation [#18]: --force leaves reconciliation-result.1.json behind")
    check(_json.load(open(_backup_path))["reconciliation_id"] == "first",
          "save-reconciliation [#18]: the backup holds the OLD content, not the new")
    check(_json.load(open(os.path.join(lib._run_dir("rt-force"), "reconciliation-result.json")))["reconciliation_id"] == "second",
          "save-reconciliation [#18]: the primary now holds the NEW content")
    if os.name == "posix":
        check(stat.S_IMODE(os.stat(_backup_path).st_mode) == 0o600,
              "save-reconciliation [R/F007]: the backup file is 0600 like every other record")
    _lr_force = lib.load_run("rt-force")
    check(_lr_force["reconciliation_result"]["reconciliation_id"] == "second",
          "[R/F007]: load_run reads the PRIMARY reconciliation-result.json, ignoring the numbered backup")
    check(sum(1 for r in lib.list_runs() if r["run_id"] == "rt-force") == 1,
          "[R/F007]: list_runs counts the run directory once, not once per backup file inside it")
    check(lib.forget_run("rt-force") is True and not os.path.isdir(lib._run_dir("rt-force")),
          "forget [Group B, no regression]: still deletes a run directory that contains backup files")

    # --- #16: show() on an orphan prints the banner, renders '?', and never a number == len(items) ---
    _orphan_show_rec = {"schema_version": "1.0", "reconciliation_id": "o1", "review_id": "rt-orphan-show",
                        "outcome": "converged",
                        "items": [_rt_item("F001", "accepted"), _rt_item("F002", "accepted"), _rt_item("F999", "accepted")]}
    lib.save_run_doc("rt-orphan-show", "reconciliation-result", _orphan_show_rec, _sanctioned=lib._RECONCILIATION_TOKEN)   # no reviewer-response: an orphan
    _orphan_out = report.render(lib.load_run("rt-orphan-show"))
    check("?" in _orphan_out and "UNVERIFIABLE" in _orphan_out,
          "show [#16]: an orphan prints the banner and renders '?' for the raised count")
    check(f"{len(_orphan_show_rec['items'])} finding(s) raised" not in _orphan_out,
          "show [#16]: never prints a number equal to len(items) as the raised count")
    check("⚠️ unverifiable (stored: converged)" in _orphan_out,
          "show [R/F002]: the stored outcome line is suppressed to 'unverifiable', not shown as converged")
    check("Cannot verify what was settled" in _orphan_out and "Nothing needed you" not in _orphan_out,
          "show [R/F002]: the all-settled footer is replaced, not left reading as a passed gate")

    # --- Group B (regression guard, no behaviour change): show on a well-formed record still prints
    # the TRUE raised count — before this change `n = len(findings)` already for a non-empty findings
    # list, so a valid record's tally is unaffected. ---
    lib.save_run_doc("rt-wellformed", "reviewer-response", _rt_rev("rt-wellformed"))
    _wf_rec = {"schema_version": "1.0", "reconciliation_id": "wf", "review_id": "rt-wellformed",
              "outcome": "converged",
              "items": [_rt_item("F001", "accepted"), _rt_item("F002", "accepted"),
                        _rt_item("F003", "resolved", resolution="done")]}
    lib.save_run_doc("rt-wellformed", "reconciliation-result", _wf_rec, _sanctioned=lib._RECONCILIATION_TOKEN)
    _wf_out = report.render(lib.load_run("rt-wellformed"))
    check("3 finding(s) raised" in _wf_out and "UNVERIFIABLE" not in _wf_out,
          "show [Group B, no regression]: a well-formed record still prints the TRUE raised count")

    # --- [R] F006: 'list' marks an orphan explicitly, and leaves a well-formed run unmarked ---
    # Check for the full marker phrase, not a bare "orphan" substring — the fixture's own run_id
    # ("rt-orphan-show") contains that substring, which would make the check pass even with the
    # marker code removed (a false positive the revert-check caught).
    _lrc, _lout = _list_cli()
    _orphan_line = next((ln for ln in _lout.splitlines() if "rt-orphan-show" in ln), "")
    _wf_line = next((ln for ln in _lout.splitlines() if "rt-wellformed" in ln), "")
    check(bool(_orphan_line) and "orphan (unverifiable)" in _orphan_line,
          "list [R/F006]: marks the orphan record's own line explicitly")
    check(bool(_wf_line) and "orphan (unverifiable)" not in _wf_line,
          "list [R/F006]: does not mark a well-formed run as orphan")

    # --- [R] F006: 'open' refuses to surface a deadlock from an unverifiable (orphan) record, but
    # discloses that it skipped one rather than going silent ---
    _orphan_deadlock_rec = {"schema_version": "1.0", "reconciliation_id": "od", "review_id": "rt-orphan-deadlock",
                            "outcome": "deadlocked",
                            "items": [{"finding_id": "F001", "state": "deadlocked",
                                       "escalation": {"dispute_kind": "value_or_priority_tradeoff",
                                                      "stop_reason": "operator_authority_required",
                                                      "operator_question": "pick?"}}]}
    lib.save_run_doc("rt-orphan-deadlock", "reconciliation-result", _orphan_deadlock_rec, _sanctioned=lib._RECONCILIATION_TOKEN)   # orphan: no reviewer-response
    check(not any(r["run_id"] == "rt-orphan-deadlock" for r in report.open_runs()),
          "open [R/F006]: refuses to surface a deadlock from an unverifiable (orphan) record")
    check("rt-orphan-deadlock" in report.unverifiable_open_run_ids(),
          "open [R/F006]: the skipped record is disclosed by id via unverifiable_open_run_ids(), not silently dropped")
    _orc, _oout = _open_cli()
    check("rt-orphan-deadlock" in _oout,
          "open CLI [R/F006]: discloses the unverifiable run's id in its own output")

    # --- issue #11: timeout diagnosability, wall recommendation, duration telemetry ---
    # The defect: a Claude review that blew its wall returned "backend wall_timeout after 605s" and
    # nothing else — no phase, no evidence the provider ever responded, no resolved model, and no
    # next step but a guess. Each block below pins one half of the fix: the run must say WHERE the
    # time went, and the tooling must say what wall to use next.
    if os.name == "posix":
        # (a) The supervisor's liveness evidence: silence and speech must be distinguishable.
        _i11_silent = run.supervise(["bash", "-c", "sleep 30"], wall_timeout=2, idle_timeout=100)
        check(_i11_silent.termination == "wall_timeout" and _i11_silent.first_byte_s is None
              and _i11_silent.bytes_received == 0,
              "issue #11: a backend that never speaks -> first_byte_s None, bytes_received 0")
        _i11_loud = run.supervise(["bash", "-c", "echo hello; sleep 30"], wall_timeout=2, idle_timeout=100)
        check(_i11_loud.termination == "wall_timeout" and _i11_loud.first_byte_s is not None
              and _i11_loud.bytes_received >= 5,
              "issue #11: a backend that speaks then stalls -> first_byte_s recorded")

        # (b) A SILENT timeout must say the time went to startup/auth/queue, not to reasoning.
        os.environ["FAKE_MODE"] = "silent_hang"
        _i11_a = run.review(kind="code", instruction="review", artifact_bytes=b"code",
                            wall_timeout=2, idle_timeout=100, no_record=True)
        _i11_at = _i11_a.get("telemetry") or {}
        check(_i11_a["ok"] is False and _i11_a["failure"]["code"] == "timeout"
              and _i11_at.get("received_any_bytes") is False
              and "wrote nothing to stdout or stderr" in _i11_a["failure"]["message"],
              "issue #11: silent-to-the-wall timeout reports that the backend produced no output")
        check(_i11_at.get("last_phase", "").startswith("attempt_1")
              and isinstance(_i11_at.get("phases"), dict)
              and "consent_granted" in _i11_at["phases"] and "attempt_1_spawn" in _i11_at["phases"],
              "issue #11: timeout carries a phase timeline (consent -> spawn -> ...)")
        check(_i11_a.get("reusable_result") is False,
              "issue #11: timeout states plainly that nothing is reusable from the attempt")

        # (c) Bytes-then-stall must read differently — the backend WAS responding.
        os.environ["FAKE_MODE"] = "speak_then_hang"
        _i11_b = run.review(kind="code", instruction="review", artifact_bytes=b"code",
                            wall_timeout=3, idle_timeout=100, no_record=True)
        _i11_bt = _i11_b.get("telemetry") or {}
        check(_i11_b["ok"] is False and _i11_bt.get("received_any_bytes") is True
              and _i11_bt.get("ttfb_s") is not None
              and "first output" in _i11_b["failure"]["message"]
              and "byte(s) total" in _i11_b["failure"]["message"],
              "issue #11: bytes-then-stall timeout reports time-to-first-byte, not just the cap")
        check(_i11_bt.get("request_id") == "th_fake_1",
              "issue #11: the codex thread id is recovered as the request id")

        # (d) Partial JSON that never completes is a TIMEOUT, not a half-parsed review.
        os.environ["FAKE_MODE"] = "partial_then_stall"
        _i11_c = run.review(kind="code", instruction="review", artifact_bytes=b"code",
                            wall_timeout=3, idle_timeout=100, no_record=True)
        check(_i11_c["ok"] is False and _i11_c["failure"]["code"] == "timeout"
              and "response" not in _i11_c,
              "issue #11: partial JSON then stall -> timeout, never a partial response")

        # (e) A descendant alive at the timeout must be torn down with the group, not leaked.
        os.environ["FAKE_MODE"] = "orphan_then_hang"
        _i11_t0 = time.monotonic()
        _i11_d = run.review(kind="code", instruction="review", artifact_bytes=b"code",
                            wall_timeout=2, idle_timeout=100, no_record=True)
        check(_i11_d["failure"]["code"] == "timeout" and time.monotonic() - _i11_t0 < 25,
              "issue #11: a backend leaving a live child still tears down and returns promptly")

        # (f) Recovery: ranked, concrete, and honest about what each option changes.
        _i11_rec = _i11_a.get("recovery") or []
        _i11_first = _i11_rec[0] if _i11_rec else {}
        check(len(_i11_rec) >= 3 and _i11_first.get("action") == "retry_longer_wall"
              and all(o.get("new_invocation") is True for o in _i11_rec),
              "issue #11: timeout returns ranked recovery options, each a full new invocation")
        check(any(o.get("action") == "switch_backend" and "INDEPENDENCE" in (o.get("changes") or "")
                  for o in _i11_rec)
              and any(o.get("action") == "split_artifact" for o in _i11_rec),
              "issue #11: recovery names the independence cost of switching backends, and a split target")
        os.environ["FAKE_MODE"] = "valid"

    # (g) A copy-pasteable command is built only when the CLI supplies its file paths.
    _i11_ctx = {"prog": "impasse_run.py", "kind": "code", "instruction_file": "I.txt",
                "artifact_file": "A md.txt"}
    _i11_opts = run._recovery_options(backend_name="claude", model=None, effort=None, speed=None,
                                      wall_timeout=600.0, recommended_wall=900.0,
                                      artifact_tokens=5693, host="codex", ctx=_i11_ctx)
    _i11_cmd = _i11_opts[0]["command"]
    check("--wall 900" in _i11_cmd and "'A md.txt'" in _i11_cmd,
          "issue #11: recovery command uses the recommended wall and quotes awkward paths")
    check(run._recovery_options(backend_name="claude", model=None, effort=None, speed=None,
                                wall_timeout=600.0, recommended_wall=900.0, artifact_tokens=10,
                                host="codex", ctx=None)[0]["command"] is None,
          "issue #11: without CLI context the options still describe the change, minus the command")

    # (h) The wall recommendation: shipped seed until there is enough local history, then measured.
    _i11_seed = lib.recommend_wall(backend="claude", artifact_tokens=5693, rows=[])
    check(_i11_seed["basis"] == "heuristic" and _i11_seed["recommended_wall_s"] > 600,
          "issue #11: with no history, the seed recommends MORE than the 600s that timed out")
    _i11_rows = [{"outcome": "completed", "duration_s": 540 + i * 18, "artifact_tokens_est": 5693}
                 for i in range(6)]
    _i11_emp = lib.recommend_wall(backend="claude", artifact_tokens=5693, rows=_i11_rows)
    check(_i11_emp["basis"] == "empirical" and _i11_emp["sample_count"] == 6
          and _i11_emp["p90_s"] is not None,
          "issue #11: >=5 completed samples switch the basis from shipped seed to measured")
    # A timeout is NOT a duration — it must never pull an estimate down, and must raise the floor.
    _i11_mixed = _i11_rows + [{"outcome": "timeout", "wall_s": 1800.0, "artifact_tokens_est": 5693,
                               "duration_s": 1805.0}]
    _i11_floor = lib.recommend_wall(backend="claude", artifact_tokens=5693, rows=_i11_mixed)
    check(_i11_floor["recommended_wall_s"] > 1800 and _i11_floor["floor_reason"],
          "issue #11: an already-exceeded cap raises the floor instead of being averaged in")
    check(lib.recommend_wall(backend="claude", artifact_tokens=99_000_000,
                             rows=[])["recommended_wall_s"] <= 5400,
          "issue #11: the recommendation is capped — past it, split rather than wait")
    # A history of SMALL fast reviews fits a near-zero rate. Extrapolating it to a large artifact
    # would confidently recommend a short wall for a payload nothing local resembles — the very
    # failure this feature exists to prevent. The shipped estimate must win there.
    _i11_small = [{"outcome": "completed", "duration_s": 4.0, "artifact_tokens_est": 40}
                  for _ in range(8)]
    _i11_far = lib.recommend_wall(backend="claude", artifact_tokens=25000, rows=_i11_small)
    _i11_near = lib.recommend_wall(backend="claude", artifact_tokens=40, rows=_i11_small)
    check(_i11_far["recommended_wall_s"] >= 1800 and "no local evidence at this size" in _i11_far["rationale"],
          "issue #11: a large artifact isn't sized from a history of tiny ones (extrapolation floor)")
    check(_i11_near["recommended_wall_s"] < _i11_far["recommended_wall_s"]
          and "no local evidence" not in _i11_near["rationale"],
          "issue #11: within observed sizes the measured fit is still used")

    # The store is a ROLLING log: reading its head would trim away the newest rows and report the
    # oldest. Force an oversized file and prove the newest row survives and is what gets read.
    _i11_tail_dir = tempfile.mkdtemp(prefix="impasse-i11-tail-")
    _i11_cfg_keep = os.environ["IMPASSE_CONFIG_DIR"]
    os.environ["IMPASSE_CONFIG_DIR"] = _i11_tail_dir
    try:
        _i11_pad = "P" * 900
        for _n in range(40):
            lib.record_metrics({"backend": "codex", "outcome": "completed", "duration_s": float(_n),
                                "model_requested": _i11_pad})
        _i11_prev_cap = lib._MAX_METRICS_BYTES
        lib._MAX_METRICS_BYTES = 8000     # smaller than the file we just wrote
        try:
            _i11_tail = lib.load_metrics()
            check(_i11_tail and _i11_tail[-1]["duration_s"] == 39.0
                  and all(isinstance(r, dict) for r in _i11_tail),
                  "issue #11: an oversized timing store reads its NEWEST rows, not its oldest")
        finally:
            lib._MAX_METRICS_BYTES = _i11_prev_cap
    finally:
        os.environ["IMPASSE_CONFIG_DIR"] = _i11_cfg_keep

    # (i) The metrics store: an allowlist makes 'no artifact content' structural, not a promise.
    _i11_before = len(lib.load_metrics())
    lib.record_metrics({"backend": "codex", "outcome": "completed", "duration_s": 12.0,
                        "artifact_text": "SECRET ARTIFACT BODY", "claim": "leak me"})
    _i11_stored = lib.load_metrics()[-1]
    check(len(lib.load_metrics()) == _i11_before + 1 and "artifact_text" not in _i11_stored
          and "claim" not in _i11_stored and _i11_stored["duration_s"] == 12.0,
          "issue #11: metrics writes drop every non-allowlisted field (no artifact content)")
    check(lib.record_metrics({"nothing": "allowlisted"}) is False,
          "issue #11: a row with no allowlisted field is not written at all")

    # (j) Runs are recorded whatever their outcome — a timeout is the most useful sample there is.
    _i11_m_before = len(lib.load_metrics())
    os.environ["FAKE_MODE"] = "valid"
    _i11_ok = run.review(kind="code", instruction="review", artifact_bytes=b"code", no_record=True)
    _i11_rows_after = lib.load_metrics()
    _i11_last = _i11_rows_after[-1]
    check(len(_i11_rows_after) == _i11_m_before + 1 and _i11_last["outcome"] == "completed"
          and _i11_last["backend"] == "codex" and _i11_last.get("artifact_tokens_est") == 1,
          "issue #11: a completed run records one metrics row with its sizes")
    check(_i11_last.get("artifact_digest") is None,
          "issue #11: --no-record withholds the content-derived digest from the metrics store")
    check(isinstance(_i11_ok.get("wall_advice"), dict)
          and _i11_ok["wall_advice"].get("recommended_wall_s")
          and _i11_ok["wall_advice"].get("basis") in ("heuristic", "empirical"),
          "issue #11: every result carries a wall recommendation and says which basis it used")
    check(_i11_ok.get("backend_version") and _i11_ok.get("model_source") == "backend_default",
          "issue #11: a backend-default model is labelled as such, never as a resolved model")

    # (k) IMPASSE_NO_METRICS is a real opt-out, not a documented intention.
    _i11_off_before = len(lib.load_metrics())
    os.environ["IMPASSE_NO_METRICS"] = "1"
    try:
        run.review(kind="code", instruction="review", artifact_bytes=b"code", no_record=True)
    finally:
        os.environ.pop("IMPASSE_NO_METRICS", None)
    check(len(lib.load_metrics()) == _i11_off_before,
          "issue #11: IMPASSE_NO_METRICS=1 records nothing")

    # (l) The pre-send advice reaches the operator BEFORE the send, where it can still matter.
    _i11_buf = io.StringIO()
    run.review(kind="code", instruction="review", artifact_bytes=b"x" * 40000, no_record=True,
               advise_stream=_i11_buf, wall_timeout=60)
    check("recommended --wall" in _i11_buf.getvalue() and "⚠" in _i11_buf.getvalue(),
          "issue #11: an underprovisioned --wall is warned about on the advice stream pre-send")

    # (m) The claude envelope: the resolved model, not the alias the operator typed.
    _i11_env_prev = os.environ.get("FAKE_CLAUDE_MODE")
    try:
        os.environ["FAKE_CLAUDE_MODE"] = "envelope"
        _i11_cl = run.review(kind="decision", instruction="review", artifact_bytes=b"doc",
                             backend="claude", model="sonnet", no_record=True)
        check(_i11_cl["ok"] is True and _i11_cl["model_resolved"] == "claude-sonnet-5"
              and _i11_cl["model"] == "sonnet" and _i11_cl["model_source"] == "resolved",
              "issue #11: the claude envelope resolves the alias to the model that actually ran")
        check((_i11_cl.get("telemetry") or {}).get("ttfb_s") == 1.234
              and _i11_cl["telemetry"]["request_id"] == "sess_fake_9",
              "issue #11: envelope time-to-first-token and session id are recorded")
        check(_i11_cl["response"]["review_id"] == "cr",
              "issue #11: the review itself is read out of the envelope's result field")
        # A backend that does NOT emit an envelope must still work — stdout is the answer.
        os.environ["FAKE_CLAUDE_MODE"] = "valid"
        _i11_bare = run.review(kind="decision", instruction="review", artifact_bytes=b"doc",
                               backend="claude", no_record=True)
        check(_i11_bare["ok"] is True and _i11_bare["model_resolved"] is None,
              "issue #11: bare (non-envelope) stdout still parses, with no resolved model claimed")
        # An envelope carrying an API error names it instead of leaving a bare exit code.
        os.environ["FAKE_CLAUDE_MODE"] = "envelope_error"
        os.environ["FAKE_CLAUDE_EXIT"] = "1"
        _i11_er = run.review(kind="decision", instruction="review", artifact_bytes=b"doc",
                             backend="claude", no_record=True)
        check(_i11_er["ok"] is False and _i11_er["failure"]["code"] == "rate_limited",
              "issue #11: an envelope api_error_status classifies the failure (not backend_error)")
    finally:
        os.environ.pop("FAKE_CLAUDE_EXIT", None)
        if _i11_env_prev is None:
            os.environ.pop("FAKE_CLAUDE_MODE", None)
        else:
            os.environ["FAKE_CLAUDE_MODE"] = _i11_env_prev

    # (n) `estimate` is a LOCAL pre-flight — it must size the artifact without sending it.
    _i11_art = os.path.join(tmp, "i11-artifact.txt")
    with open(_i11_art, "w") as _fh:
        _fh.write("x" * 23000)
    # Isolate the config dir: by now the suite's own fake-backend runs have filled the metrics store
    # with sub-second "reviews", which would (correctly) fit a tiny empirical rate and defeat the
    # point of this case. `estimate` needs no consent, so redirecting the dir here is safe.
    _i11_eb = io.StringIO()
    _i11_cfg_prev = os.environ["IMPASSE_CONFIG_DIR"]
    os.environ["IMPASSE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="impasse-i11-estimate-")
    try:
        with contextlib.redirect_stdout(_i11_eb):
            _i11_rc = run._main(["estimate", "--artifact-file", _i11_art, "--backend", "claude",
                                 "--wall", "600"])
    finally:
        os.environ["IMPASSE_CONFIG_DIR"] = _i11_cfg_prev
    _i11_ej = _json.loads(_i11_eb.getvalue())
    check(_i11_rc == 0 and _i11_ej["underprovisioned"] is True
          and _i11_ej["recommended_wall_s"] > 600 and _i11_ej["artifact_tokens_est"] == 5750,
          "issue #11: estimate flags a 600s wall for a ~5.7k-token claude review (the reported case)")

    # (o) The performance report, and its own delete.
    _i11_pb = io.StringIO()
    with contextlib.redirect_stdout(_i11_pb):
        report._main(["performance"])
    check("Impasse performance" in _i11_pb.getvalue() and "no artifact content" in _i11_pb.getvalue(),
          "issue #11: `performance` reports recorded timings and states what the store holds")
    _i11_fb = io.StringIO()
    with contextlib.redirect_stdout(_i11_fb):
        report._main(["performance", "--forget"])
    check("deleted" in _i11_fb.getvalue() and lib.load_metrics() == [],
          "issue #11: `performance --forget` deletes the timing store")
    _i11_eb2 = io.StringIO()
    with contextlib.redirect_stdout(_i11_eb2):
        report._main(["performance"])
    check("No run timings recorded yet" in _i11_eb2.getvalue(),
          "issue #11: an empty timing store reports honestly instead of rendering a hollow table")

    # --- issue #11, round 2: defects the cross-provider review of this change found ---
    # An Impasse review of the issue-#11 diff (codex, high effort, Fast mode) raised 11 findings;
    # these pin the ones verified as real.

    # R-F001 (critical): an envelope declaring an error must FAIL even on a zero exit status.
    # Exit code and envelope are independent signals; "a failure is never reported as success"
    # means trusting neither alone. Previously the envelope check sat inside `if exit_code != 0`.
    _r11_prev_mode = os.environ.get("FAKE_CLAUDE_MODE")
    try:
        os.environ["FAKE_CLAUDE_MODE"] = "envelope_error"
        os.environ["FAKE_CLAUDE_EXIT"] = "0"           # error envelope, SUCCESSFUL exit status
        _r11_a = run.review(kind="decision", instruction="review", artifact_bytes=b"doc",
                            backend="claude", no_record=True)
        check(_r11_a["ok"] is False and _r11_a["failure"]["code"] == "rate_limited",
              "review F001: an is_error envelope fails even when the CLI exits 0")
    finally:
        os.environ.pop("FAKE_CLAUDE_EXIT", None)
        if _r11_prev_mode is None:
            os.environ.pop("FAKE_CLAUDE_MODE", None)
        else:
            os.environ["FAKE_CLAUDE_MODE"] = _r11_prev_mode

    # R-F002 (high): the byte signal must be reported for what it is. A CLI that writes a startup
    # event immediately (codex does, within ~50ms) makes "bytes arrived" say nothing about model
    # progress, so the message must not claim the cap was the problem.
    if os.name == "posix":
        os.environ["FAKE_MODE"] = "speak_then_hang"
        _r11_b = run.review(kind="code", instruction="review", artifact_bytes=b"code",
                            wall_timeout=3, idle_timeout=100, no_record=True)
        _r11_msg = _r11_b["failure"]["message"]
        check("not something this signal shows" in _r11_msg
              and "cap was most likely too short" not in _r11_msg,
              "review F002: bytes-then-stall states what was observed, not a diagnosis")
        os.environ["FAKE_MODE"] = "silent_hang"
        _r11_c = run.review(kind="code", instruction="review", artifact_bytes=b"code",
                            wall_timeout=2, idle_timeout=100, no_record=True)
        check("does not on its own separate" in _r11_c["failure"]["message"],
              "review F002: silence rules out a stall mid-stream, and claims nothing more")
        os.environ["FAKE_MODE"] = "valid"

    # R-F003 (high): the allowlist must bound VALUES, not just keys — otherwise an unbounded
    # backend-supplied string (a model name read out of the CLI's own output) lands in the store.
    _r11_long = "M" * 5000
    lib.record_metrics({"backend": "codex", "outcome": "completed", "duration_s": 1.0,
                        "model_resolved": _r11_long, "phases": {"a": 1.0, "bad": "not-a-number"},
                        "ttfb_s": float("inf"), "findings_count": [1, 2, 3]})
    _r11_row = lib.load_metrics()[-1]
    check(len(_r11_row["model_resolved"]) <= 200 and _r11_row["phases"] == {"a": 1.0}
          and _r11_row.get("ttfb_s") is None and _r11_row.get("findings_count") is None,
          "review F003: metric values are bounded and type-checked, not just key-filtered")

    # R-F005 (high): history must be matched on effort/speed, which move duration substantially —
    # a history of --effort low must not size the wall for --effort high.
    _r11_cfg_prev = os.environ["IMPASSE_CONFIG_DIR"]
    os.environ["IMPASSE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="impasse-r11-knobs-")
    try:
        for _n in range(6):
            lib.record_metrics({"backend": "codex", "outcome": "completed", "duration_s": 30.0,
                                "artifact_tokens_est": 5000, "effort": "low", "speed": "standard"})
        _r11_low = lib.recommend_wall(backend="codex", artifact_tokens=5000, effort="low",
                                      speed="standard")
        _r11_high = lib.recommend_wall(backend="codex", artifact_tokens=5000, effort="high",
                                       speed="standard")
        check(_r11_low["basis"] == "empirical" and _r11_high["basis"] == "heuristic"
              and _r11_high["recommended_wall_s"] > _r11_low["recommended_wall_s"],
              "review F005: a low-effort history does not size a high-effort review")
    finally:
        os.environ["IMPASSE_CONFIG_DIR"] = _r11_cfg_prev

    # R-F006 (high): the fit subtracts a fixed base, so runs shorter than it produce a rate of zero.
    # The recommendation must never fall below a duration actually observed at these settings.
    _r11_slow = [{"outcome": "completed", "duration_s": 250.0, "artifact_tokens_est": 4000}
                 for _ in range(6)]
    _r11_p90 = lib.recommend_wall(backend="claude", artifact_tokens=4000, rows=_r11_slow)
    check(_r11_p90["recommended_wall_s"] >= 250,
          "review F006: a zero-rate fit still can't recommend below the observed p90")

    # R-F007 (medium): "comparable" must be bounded on BOTH sides — a timeout on something ten
    # times larger says nothing about this review and would inflate every estimate.
    _r11_huge_to = [{"outcome": "timeout", "wall_s": 5000.0, "artifact_tokens_est": 500000,
                     "duration_s": 5005.0}]
    check(lib.recommend_wall(backend="claude", artifact_tokens=1000,
                             rows=_r11_huge_to)["floor_reason"] is None,
          "review F007: a timeout on a far larger artifact is not treated as comparable")
    # And when the ceiling clamps below a known-insufficient cap, it must say so rather than imply
    # a floor it no longer keeps.
    _r11_clamped = lib.recommend_wall(
        backend="claude", artifact_tokens=200000,
        rows=[{"outcome": "timeout", "wall_s": 5400.0, "artifact_tokens_est": 200000,
               "duration_s": 5405.0}])
    check(_r11_clamped["recommended_wall_s"] <= 5400
          and "not expected to be enough" in (_r11_clamped["floor_reason"] or ""),
          "review F007: a clamped recommendation admits it may not be enough")

    # R-F008 (high): untrusted backend JSON that is deeply nested raises RecursionError, not
    # ValueError. Every parser on that path must classify it, never let it escape.
    _r11_deep = ("[" * 200000).encode()
    check(run._claude_envelope(b'{"a":' + _r11_deep + b"}") is None,
          "review F008: pathologically nested envelope JSON degrades to 'no envelope'")
    check(run._codex_stream_meta(b'{"x":' + _r11_deep + b"}\n") == {},
          "review F008: pathologically nested codex events are skipped, not fatal")

    # R-F009 (medium): a hand-edited or crash-truncated row must not crash the report.
    _r11_bad_rows = [{"backend": "codex", "model_resolved": "m", "outcome": "completed",
                      "duration_s": None, "artifact_tokens_est": None, "ttfb_s": "soon"},
                     {"backend": "codex", "model_resolved": "m", "outcome": "timeout",
                      "wall_s": "six hundred"}]
    _r11_rendered = report.render_performance(_r11_bad_rows)
    check("Impasse performance" in _r11_rendered and "—" in _r11_rendered,
          "review F009: malformed metric rows render as unknown instead of raising")

    # R-F010 (medium): --wall is documented as the cap for the WHOLE review, so the version probe
    # must sit inside the budget. With a wall already spent, the run must time out, not overrun it.
    _r11_vprev = dict(run._VERSION_CACHE)
    run._VERSION_CACHE.clear()
    try:
        _r11_t0 = time.monotonic()
        _r11_d = run.review(kind="code", instruction="review", artifact_bytes=b"code",
                            wall_timeout=0.001, idle_timeout=100, no_record=True)
        check(_r11_d["ok"] is False and _r11_d["failure"]["code"] == "timeout"
              and time.monotonic() - _r11_t0 < 30,
              "review F010: the version probe runs inside the wall budget, not before it")
    finally:
        run._VERSION_CACHE.clear()
        run._VERSION_CACHE.update(_r11_vprev)

    # --- R2-F001..F007: fixes for the SECOND cross-provider review, of the fix commit itself ---
    # (a26b736 applied 11 findings; this block pins the defects that review-of-the-fixes found.)

    # R2-F002 (high): RecursionError escaped THREE more parsers of untrusted reviewer stdout.
    # The first round fixed _claude_envelope/_codex_stream_meta and stopped there.
    _r2_deep = "{\"a\":" + "[" * 200000 + "}"
    _r2_unwrap_ok = False
    try:
        run._unwrap_error(_r2_deep)
        _r2_unwrap_ok = True
    except RecursionError:
        pass
    check(_r2_unwrap_ok, "review2 F002: _unwrap_error classifies deeply nested JSON, never raises")

    _r2_ebe_ok = False
    try:
        run._extract_backend_error((_r2_deep + "\n").encode(), b"", True, None)
        _r2_ebe_ok = True
    except RecursionError:
        pass
    check(_r2_ebe_ok, "review2 F002: the codex JSONL error scan survives deeply nested lines")

    # The main SUCCESS path: the reviewer's own final message. A raise here would escape as a
    # traceback instead of classifying as invalid_response (which is never a false pass).
    _r2_prj = None
    try:
        run._parse_reviewer_json(_r2_deep)
    except RecursionError:
        _r2_prj = "recursion"
    except ValueError:      # json.JSONDecodeError is a ValueError; the module isn't imported here
        _r2_prj = "classified"
    check(_r2_prj == "classified",
          "review2 F002: _parse_reviewer_json classifies nested JSON as invalid, not RecursionError")

    # R2-F001 (high): the metrics store's "no artifact content" claim must be structural per FIELD,
    # not merely truncated. A scalar field must never accept a dict whose KEYS carry text.
    check(lib._sanitize_metric_value({"ARTIFACT TEXT": 1}, field="kind") is None,
          "review2 F001: a dict on a scalar metric field is dropped, not stored as keys")
    check(lib._sanitize_metric_value("x" * 500, field="phases") is None,
          "review2 F001: a string on the phases field is dropped (per-field typing)")
    _r2_ph = lib._sanitize_metric_value({"spawn": 1.5}, field="phases")
    check(_r2_ph == {"spawn": 1.5}, "review2 F001: the phases map still stores its numbers")
    lib.record_metrics({"kind": {"ARTIFACT TEXT": 1}, "backend": "codex"})
    _r2_rows_now = lib.load_metrics(backend="codex")
    check(all("ARTIFACT TEXT" not in str(r) for r in _r2_rows_now),
          "review2 F001: artifact text on a scalar field never reaches metrics.jsonl")

    # R2-F006 (low): record_metrics documents itself TOTAL (never raises). A huge int overflowed
    # math.isfinite with OverflowError, which is an ArithmeticError -- not in the caught tuple.
    _r2_overflow_ok = False
    try:
        lib.record_metrics({"phases": {"x": 10 ** 10000}})
        _r2_overflow_ok = True
    except OverflowError:
        pass
    check(_r2_overflow_ok, "review2 F006: an unbounded int can't break record_metrics's never-raises contract")

    # R2-F005 (medium): "filtered to finite numbers" must mean isfinite, not merely isinstance.
    for _r2_bad, _r2_name in ((float("nan"), "NaN"), (float("inf"), "Infinity")):
        _r2_fin_ok = False
        try:
            report.render_performance([{"backend": "codex", "model_resolved": "m",
                                        "model_source": "resolved", "outcome": "completed",
                                        "duration_s": 100.0, "artifact_tokens_est": _r2_bad}])
            _r2_fin_ok = True
        except (ValueError, OverflowError):
            pass
        check(_r2_fin_ok, f"review2 F005: {_r2_name} in a metric series renders instead of crashing")

    # R2-F004 (medium): the effort/speed match landed in recommend_wall but the performance report
    # bypassed it by passing pre-grouped rows, so the number the operator SEES pooled both.
    _r2_mixed = [{"backend": "codex", "model_resolved": "m", "model_source": "resolved",
                  "outcome": "completed", "duration_s": 100.0, "artifact_tokens_est": 4000,
                  "effort": "low", "speed": "standard"} for _ in range(5)]
    _r2_mixed += [{"backend": "codex", "model_resolved": "m", "model_source": "resolved",
                   "outcome": "completed", "duration_s": 900.0, "artifact_tokens_est": 4000,
                   "effort": "high", "speed": "standard"} for _ in range(5)]
    _r2_perf = report.render_performance(_r2_mixed)
    check("effort" in _r2_perf.lower(),
          "review2 F004: the performance report separates histories by effort/speed")

    # R2-F007 (low): a regression test must FAIL with its fix reverted, or it pins nothing. The
    # round-1 F006 check asserted >= 250 on a case where the UNFLOORED path already returned ~375,
    # so it could not tell fixed from reverted. This case is chosen so the floor actually BINDS:
    # a history of long runs on large artifacts, queried for a SMALL one. The fitted rate
    # ((800-300)/10k = 50/1k) puts the raw estimate at 300 + 50*1 = 350s -> ~440s after margin,
    # which is BELOW the 800s p90 actually observed. Only the floor lifts it above.
    _r2_slow = [{"outcome": "completed", "duration_s": 800.0, "artifact_tokens_est": 10000}
                for _ in range(6)]
    _r2_floor = lib.recommend_wall(backend="claude", artifact_tokens=1000, rows=_r2_slow)
    check(_r2_floor["basis"] == "empirical" and _r2_floor["p90_s"] == 800.0
          and _r2_floor["recommended_wall_s"] >= _r2_floor["p90_s"],
          "review2 F007: the recommendation never falls below the OBSERVED p90 (floor binds here)")
    _r2_seed_only = lib.recommend_wall(backend="claude", artifact_tokens=1000, rows=[])
    check(_r2_seed_only["basis"] == "heuristic",
          "review2 F007: an empty history is heuristic, distinguishing the two bases")

    # R2-F003 (high): --wall is documented as the cap for the WHOLE review, so the version probe
    # must be bounded by the REMAINING budget, not its own fixed 20s.
    check(run.backend_version(["/nonexistent/codex"], remaining=0.0) is None,
          "review2 F003: the version probe with no budget left returns None immediately")
    _r2_vprev = dict(run._VERSION_CACHE)
    run._VERSION_CACHE.clear()
    try:
        _r2_t0 = time.monotonic()
        run.backend_version([sys.executable, "-c", "import time; time.sleep(30)"], remaining=0.5)
        _r2_probe_elapsed = time.monotonic() - _r2_t0
        check(_r2_probe_elapsed < 5,
              "review2 F003: a hanging version probe is bounded by the remaining wall, not 20s")
    finally:
        run._VERSION_CACHE.clear()
        run._VERSION_CACHE.update(_r2_vprev)

    # --- Cursor host adapter (Phase A) + Grok as an attributable host (Phase B) ---
    _cur_prev = {k: os.environ.get(k) for k in
                 ("IMPASSE_HOST", "CURSOR_AGENT", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT",
                  "GEMINI_CLI", "CODEX_SANDBOX")}
    try:
        for _k in _cur_prev:
            os.environ.pop(_k, None)

        # PHASE B: grok is nameable as a HOST. It has no marker to detect and no backend to review
        # with — asserting it is the ONLY way in, and that is the whole point: it lets a Grok-driven
        # session (typically Cursor) label a Codex/Claude reviewer honestly instead of undetermined.
        check("grok" in lib.KNOWN_HOSTS and lib._HOST_PROVIDERS.get("grok") == "xAI",
              "grok: nameable as a host attributed to xAI")
        check(lib.independence_tier("grok", "OpenAI") == "cross_provider"
              and lib.independence_tier("grok", "Anthropic") == "cross_provider",
              "grok host: both shipped backends are cross-provider")
        # The asymmetry is deliberate: xAI is a known HOST provider but not a known BACKEND
        # provider, because no Grok backend ships. Pin it so it reads as a decision, not an omission.
        check("xAI" not in lib._KNOWN_PROVIDERS,
              "grok: xAI is a host provider but NOT a backend provider (no grok backend ships)")
        check(lib.independence_tier("grok", "xAI") == "same_provider",
              "grok: equality is checked first, so an xAI backend on a grok host is same_provider")
        # Pin the LIMITATION that absence creates, so it reads as a known, fail-safe cost rather
        # than a property someone later mistakes for correct: an xAI backend would score
        # `undetermined` against non-xAI hosts until xAI is added to _KNOWN_PROVIDERS. Understating
        # independence is the safe direction, but a future backend must update that tuple.
        check(all(lib.independence_tier(_h, "xAI") == "undetermined"
                  for _h in ("claude", "codex", "gemini")),
              "grok: an unshipped provider understates (never overstates) independence — fail-safe")
        os.environ["IMPASSE_HOST"] = "grok"
        _hd_grok = lib.host_detection()
        check(_hd_grok["host"] == "grok" and _hd_grok["method"] == "override"
              and _hd_grok["confidence"] == "asserted",
              "grok: IMPASSE_HOST=grok is accepted as an ASSERTED host")
        os.environ.pop("IMPASSE_HOST", None)

        # COMPOSER vs AUTO — the distinction the whole Cursor adapter turns on. `composer` is a
        # real model from a real organization (Anysphere), so it is attributable. `cursor` is the
        # AUTO ROUTER, which is not a lab at all and picks per request.
        check("composer" in lib.KNOWN_HOSTS and lib._HOST_PROVIDERS.get("composer") == "Anysphere",
              "composer: Cursor's own model is attributable to Anysphere")
        check(lib.independence_tier("composer", "OpenAI") == "cross_provider"
              and lib.independence_tier("composer", "Anthropic") == "cross_provider",
              "composer: a Codex/Claude reviewer is cross-provider vs Anysphere")
        check(lib.independence_tier("composer", "Anysphere") == "same_provider",
              "composer: an Anysphere reviewer would be same_provider")
        # THE SAFETY PROPERTY: Auto must NEVER inherit Composer's attributability. They arrive from
        # the same IDE and are easy to conflate, but Auto's pool contains BOTH reviewer providers —
        # so a positive tier there could label a Codex reviewer cross-provider on a Codex-routed turn.
        check(lib.independence_tier("cursor", "OpenAI") == "undetermined"
              and lib.independence_tier("cursor", "Anthropic") == "undetermined",
              "AUTO: the cursor host stays undetermined even though composer is attributable")
        check(lib._HOST_PROVIDERS.get("cursor") is None,
              "AUTO: the router is deliberately given no provider — it is not a lab")
        # F001: the provenance uncertainty must ride on the EXECUTABLE claim, not live only in a
        # source comment and a doc page. A tier is asserted in code; qualifying it elsewhere leaves
        # the result an operator actually reads unqualified.
        _comp_notice = lib.independence_notice("cross_provider", "composer", "codex", "OpenAI", "asserted") or ""
        check("Provenance caveat" in _comp_notice and "not fully public" in _comp_notice,
              "composer: the positive tier CARRIES its provenance caveat in the notice")
        check("Provenance caveat" not in (lib.independence_notice(
                  "cross_provider", "claude", "codex", "OpenAI", "asserted") or ""),
              "composer: the caveat is host-specific — claude/codex pairings don't inherit it")
        # Even on a basis that would otherwise owe NO notice, a caveated host must still disclose.
        check("Provenance caveat" in (lib.independence_notice(
                  "cross_provider", "composer", "codex", "OpenAI", "strong") or ""),
              "composer: a caveated host owes a notice even when the basis alone wouldn't")

        # F002: pin the Composer/Auto boundary END TO END through host_detection, not just at
        # independence_tier with literal strings. Auto and Composer arrive from the same IDE.
        os.environ["CURSOR_AGENT"] = "1"
        os.environ["IMPASSE_HOST"] = "composer"
        _hd_comp = lib.host_detection()
        check(_hd_comp["host"] == "composer" and _hd_comp["confidence"] == "asserted"
              and lib.independence_tier(_hd_comp["host"], "OpenAI") == "cross_provider",
              "composer e2e: asserting composer under CURSOR_AGENT reaches a cross-provider tier")
        os.environ.pop("IMPASSE_HOST", None)
        _hd_auto = lib.host_detection()
        check(_hd_auto["host"] == "cursor"
              and lib.independence_tier(_hd_auto["host"], "OpenAI") == "undetermined"
              and lib.independence_tier(_hd_auto["host"], "Anthropic") == "undetermined",
              "AUTO e2e: with no assertion, a Cursor session cannot reach the composer mapping")

        # CURSOR: the marker alone must never buy a positive tier. This is anti-pattern #1 in the
        # proposal and the single most important property of the whole adapter.
        os.environ["CURSOR_AGENT"] = "1"
        _hd_cursor = lib.host_detection()
        check(_hd_cursor["host"] == "cursor" and _hd_cursor["confidence"] == "none",
              "cursor: CURSOR_AGENT=1 detects the host but with NO confidence")
        check(lib.independence_tier("cursor", "OpenAI") == "undetermined"
              and lib.independence_tier("cursor", "Anthropic") == "undetermined",
              "cursor: the marker alone NEVER yields a positive tier (anti-pattern 1)")
        _m_cursor = lib.review_mode("code", environment="claude_code", codex_available=True,
                                    claude_available=True, detection=_hd_cursor)
        check(_m_cursor["tier"] == "undetermined"
              and "undetermined" in (_m_cursor["notice"] or "").lower(),
              "cursor: an unasserted Cursor session still reviews, but the notice is surfaced")

        # ...and the asserted upgrade path, which is what the adapter actually adds.
        os.environ["IMPASSE_HOST"] = "claude"
        _hd_asserted = lib.host_detection()
        check(_hd_asserted["host"] == "claude" and _hd_asserted["confidence"] == "asserted",
              "cursor: the operator's assertion overrides the cursor marker")
        _m_asserted = lib.review_mode("code", environment="claude_code", codex_available=True,
                                      claude_available=True, detection=_hd_asserted)
        check(_m_asserted["mode"] == "codex" and _m_asserted["tier"] == "cross_provider",
              "cursor: asserting the host model earns a real cross-provider reviewer")
        # THE POINT OF PHASE A: the tier goes positive AND the notice says it was asserted. A host
        # that prints only independence_notice previously showed nothing here.
        check("ASSERTION" in (_m_asserted["notice"] or "")
              and "IMPASSE_HOST" in (_m_asserted["notice"] or ""),
              "cursor: an asserted tier discloses its provenance in the NOTICE, not only in host_detection")
        # An assertion goes stale silently when the operator switches model mid-session; the notice
        # has to say so, because nothing in the system can detect that.
        check("switch" in (_m_asserted["notice"] or "").lower(),
              "cursor: the asserted notice warns that a mid-session model switch invalidates it")
        os.environ.pop("IMPASSE_HOST", None)

        # The heuristic notice must not promise something the asserted branch doesn't deliver.
        # It used to say "set IMPASSE_HOST=<host> to confirm it"; once an asserted positive tier
        # started carrying its own notice, that advice pointed at a different warning, not silence.
        _heur = lib.independence_notice("cross_provider", "codex", "claude", "Anthropic", "heuristic") or ""
        check("IMPASSE_HOST" in _heur and "confirm it" not in _heur,
              "notice: heuristic advice no longer promises that asserting SILENCES the disclosure")
        # The two weak-basis branches must stay distinguishable — an operator has to be able to tell
        # "we guessed" from "you told us", since only one of them is theirs to re-check.
        _asrt = lib.independence_notice("cross_provider", "codex", "claude", "Anthropic", "asserted") or ""
        check("INFERRED" in _heur and "ASSERTION" in _asrt and _heur != _asrt,
              "notice: inferred and asserted provenance read differently")

        # A conflicting assertion must not be silently honored over a strong detection.
        os.environ["CLAUDECODE"] = "1"
        os.environ["CLAUDE_CODE_ENTRYPOINT"] = "cli"
        os.environ["IMPASSE_HOST"] = "codex"
        _hd_conflict = lib.host_detection()
        # EXACTLY unknown. Accepting "unknown or codex" would have let the unsafe outcome
        # {host: codex, confidence: asserted} pass — which is the very thing that would label a
        # Claude reviewer cross-provider on a machine whose strong marker says Claude.
        check(_hd_conflict["host"] == "unknown" and _hd_conflict["confidence"] == "none",
              "cursor: an override conflicting with a strong marker resolves to unknown, exactly")
    finally:
        for _k, _v in _cur_prev.items():
            if _v is None:
                os.environ.pop(_k, None)
            else:
                os.environ[_k] = _v

    # --- skill version: ONE source of truth, and the surfaces must agree ---
    # Surfacing a version in several files is how an operator learns it without running anything —
    # and is also a new way to be wrong. This gate is the whole reason the redundancy is safe: a
    # release that bumps VERSION but forgets SKILL.md fails here instead of shipping a stale claim.
    import re as _re
    _ver_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "VERSION")
    _ver_declared = open(_ver_file, encoding="utf-8").read().strip()
    check(_re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", _ver_declared) is not None,
          f"version: VERSION holds a bare semver ({_ver_declared!r})")
    check(lib.version(with_revision=False) == _ver_declared,
          "version: lib.version(with_revision=False) matches the VERSION file exactly")

    _skill_text = open(os.path.join(os.path.dirname(_ver_file), "SKILL.md"), encoding="utf-8").read()
    _fm = _re.search(r"\A---\n(.*?)\n---", _skill_text, _re.S)
    _fm_body = _fm.group(1) if _fm else ""
    # SCOPED to the metadata block, not a loose search of the whole frontmatter. A bare
    # `^version:` match would accept a TOP-LEVEL version key and pass while metadata.version
    # disagreed — a gate that can be satisfied by the wrong line is not a gate.
    _md = _re.search(r"^metadata:\s*\n((?:[ \t]+.*\n?)+)", _fm_body, _re.M)
    _md_ver = _re.search(r"^[ \t]+version:\s*(\S+)\s*$", _md.group(1), _re.M) if _md else None
    check(_md_ver is not None and _md_ver.group(1) == _ver_declared,
          "version: SKILL.md frontmatter metadata.version matches VERSION (scoped to metadata)")
    # And there must be exactly ONE version key in the frontmatter, so a stray top-level one can't
    # sit alongside the real one saying something different.
    check(len(_re.findall(r"^\s*version:", _fm_body, _re.M)) == 1,
          "version: the frontmatter declares version exactly once (no competing keys)")
    _hdr_ver = _re.search(r"\*\*Version ([0-9]+\.[0-9]+\.[0-9]+)\*\*", _skill_text)
    check(_hdr_ver is not None and _hdr_ver.group(1) == _ver_declared,
          "version: the SKILL.md header line matches VERSION (the surface a host reads for free)")

    # The git suffix is environment-specific: it must decorate the runtime value and NEVER leak
    # into the documented one, or the docs would differ per checkout.
    check(lib.version().startswith(_ver_declared),
          "version: the runtime value extends the released version rather than replacing it")
    check("+" not in lib.version(with_revision=False),
          "version: the base value carries no git suffix (docs stay checkout-independent)")

    # Degradation: an install that cannot identify itself must SAY so, not guess or raise.
    _ver_prev = dict(lib._VERSION_CACHED)
    _ver_path_prev = lib._VERSION_FILE
    try:
        lib._VERSION_CACHED.clear()
        lib._VERSION_FILE = os.path.join(tempfile.gettempdir(), "impasse-no-such-VERSION")
        check(lib.version() == "unknown", "version: a missing VERSION file yields 'unknown', not a crash")
        lib._VERSION_CACHED.clear()
        _bad = tempfile.mkstemp(prefix="impasse-badver-")[1]
        open(_bad, "w").write("v1; rm -rf /\n")
        lib._VERSION_FILE = _bad
        check(lib.version() == "unknown",
              "version: a malformed VERSION string is rejected, not interpolated into reports")
        os.unlink(_bad)
    finally:
        lib._VERSION_FILE = _ver_path_prev
        lib._VERSION_CACHED.clear()
        lib._VERSION_CACHED.update(_ver_prev)

    # It must reach the surfaces a host and a stored record actually read.
    # revision_from_digest must refuse to mint an identity for reviewed content from junk.
    check(lib.revision_from_digest("sha256:" + "a" * 64)["algorithm"] == "sha256",
          "revision_from_digest: a well-formed sha256 digest parses")
    check(all(lib.revision_from_digest(_b) is None for _b in
              (None, "", "nonsense", "md5:" + "a" * 32, "sha256:", "sha256:zzzz", 12345, b"sha256:aa")),
          "revision_from_digest: junk yields None, never a fabricated identity")
    # Length is per ALGORITHM. A shared range would accept an ABBREVIATION as an immutable identity
    # — the one thing this field must never be, since abbreviations stop distinguishing revisions.
    check(lib.revision_from_digest("sha256:" + "a" * 7) is None
          and lib.revision_from_digest("sha256:" + "a" * 63) is None
          and lib.revision_from_digest("sha256:" + "a" * 65) is None
          and lib.revision_from_digest("sha256:" + "a" * 128) is None,
          "revision_from_digest: sha256 must be EXACTLY 64 hex — no abbreviations, no overlong")
    check(lib.revision_from_digest("git:" + "a" * 40) is not None
          and lib.revision_from_digest("git:" + "a" * 64) is not None
          and lib.revision_from_digest("git:" + "a" * 7) is None,
          "revision_from_digest: git accepts a full SHA-1 or SHA-256 object id, not an abbreviation")

    check("impasse_version" in lib._METRIC_FIELDS,
          "version: the timing store is allowed to record which version produced each row")
    # ...and prove it actually ARRIVES on the surfaces, by driving the real code paths. Membership
    # in an allowlist proves only that the field is permitted, not that anything writes it; each of
    # these fails if its production line is reverted.
    import subprocess as _vsp
    _cli = os.path.join(os.path.dirname(_ver_file), "scripts", "impasse_run.py")
    _mode_out = _vsp.run([sys.executable, _cli, "mode", "--kind", "code"],
                         capture_output=True, text=True)
    check(json.loads(_mode_out.stdout).get("impasse_version") == lib.version(),
          "version: `mode` reports it (the command a host runs FIRST, before any review)")
    _est_art = tempfile.mkstemp(prefix="impasse-estver-")[1]
    open(_est_art, "w").write("x" * 400)
    try:
        _est_out = _vsp.run([sys.executable, _cli, "estimate", "--artifact-file", _est_art,
                             "--backend", "codex"], capture_output=True, text=True)
        check(json.loads(_est_out.stdout).get("impasse_version") == lib.version(),
              "version: `estimate` reports it")
    finally:
        os.unlink(_est_art)
    # The FAILURE envelope matters most: "which version?" is asked precisely when a run went wrong.
    _vfail = run.review(kind="code", instruction="x", artifact_bytes=b"y", backend="codex",
                        no_record=True)
    check(_vfail.get("impasse_version") == lib.version(),
          "version: a FAILED review still identifies the version that produced it")
    _vm_prev = os.environ.get("IMPASSE_CONFIG_DIR")
    _vm_dir = tempfile.mkdtemp(prefix="impasse-vermeta-")
    try:
        os.environ["IMPASSE_CONFIG_DIR"] = _vm_dir
        _vm_path = lib.save_run_meta("ver-meta-test")
        check(_vm_path and os.path.isfile(_vm_path)
              and json.loads(open(_vm_path).read())["impasse_version"] == lib.version(),
              "version: a run record is stamped with the Impasse that produced it")
        check(stat.S_IMODE(os.stat(_vm_path).st_mode) == 0o600,
              "version: the run-meta stamp is 0600 like every other record file")
    finally:
        if _vm_prev is None:
            os.environ.pop("IMPASSE_CONFIG_DIR", None)
        else:
            os.environ["IMPASSE_CONFIG_DIR"] = _vm_prev
        shutil.rmtree(_vm_dir, ignore_errors=True)

    # A record that PARSES but is not an object crashed `show` AND `list` with AttributeError —
    # so the command you would run to FIND the bad record was the one that died on it. Found while
    # verifying the #16/#17/#18 work; not part of that plan.
    _nd_prev = os.environ.get("IMPASSE_CONFIG_DIR")
    _nd_dir = tempfile.mkdtemp(prefix="impasse-nondict-")
    try:
        os.environ["IMPASSE_CONFIG_DIR"] = _nd_dir
        _nd_run = os.path.join(_nd_dir, "runs", "corrupt")
        os.makedirs(_nd_run, exist_ok=True)
        with open(os.path.join(_nd_run, "reconciliation-result.json"), "w") as _f:
            _f.write('["not", "an", "object"]')
        _nd_loaded = lib.load_run("corrupt")
        check(_nd_loaded["reconciliation_result"] is None,
              "load_run: a record that parses but isn't an object reads as unreadable, not raw")
        _nd_ok = True
        try:
            report.render(lib.load_run("corrupt"))
            [r["run_id"] for r in lib.list_runs()]
        except AttributeError:
            _nd_ok = False
        check(_nd_ok, "load_run: a non-object record no longer crashes show/list with AttributeError")
    finally:
        if _nd_prev is None:
            os.environ.pop("IMPASSE_CONFIG_DIR", None)
        else:
            os.environ["IMPASSE_CONFIG_DIR"] = _nd_prev
        shutil.rmtree(_nd_dir, ignore_errors=True)

    # --- fixes from the cross-provider review OF the #16/#17/#18 implementation ---
    # R-F001 (critical): the plan guarded the CLI, leaving save_run_doc — a PUBLIC writer — able to
    # re-create the exact orphan of #17, outside the per-run lock. Advice is not an invariant.
    _bp_prev = os.environ.get("IMPASSE_CONFIG_DIR")
    _bp_dir = tempfile.mkdtemp(prefix="impasse-bypass-")
    try:
        os.environ["IMPASSE_CONFIG_DIR"] = _bp_dir
        _bp_refused = False
        try:
            lib.save_run_doc("bypass-run", "reconciliation-result",
                             {"schema_version": "1.0", "review_id": "bypass-run",
                              "outcome": "converged", "items": []})
        except ValueError as _e:
            _bp_refused = "save_reconciliation_doc" in str(_e)
        check(_bp_refused,
              "R-F001: save_run_doc REFUSES a reconciliation-result — the guard is at the writer")
        check(not os.path.isdir(os.path.join(_bp_dir, "runs", "bypass-run")),
              "R-F001: the refused bypass creates no orphan directory")
        check(bool(lib.save_run_doc("rev-ok", "reviewer-response",
                                    {"schema_version": "1.0", "review_id": "rev-ok"})),
              "R-F001: reviewer-responses still write through the same primitive")
    finally:
        if _bp_prev is None:
            os.environ.pop("IMPASSE_CONFIG_DIR", None)
        else:
            os.environ["IMPASSE_CONFIG_DIR"] = _bp_prev
        shutil.rmtree(_bp_dir, ignore_errors=True)

    # R-F002 (high): every reader did `rec.get("items") or []` then `it.get(...)`, so a malformed
    # collection raised AttributeError BEFORE the unverifiable banner could print — `show` and
    # `list` crashed on precisely the records they were being taught to report honestly.
    for _mal_name, _mal in (("string", "nope"), ("non-dict entry", [42]),
                            ("dict", {"a": 1}), ("int", 7), ("None", None)):
        check(lib.reconciliation_items({"items": _mal}) == [],
              f"R-F002: reconciliation_items({_mal_name}) degrades to [] rather than raising")
    _mal_ok = True
    try:
        for _mal in ("nope", [42], {"a": 1}, 7):
            report.render({"run_id": "x", "reviewer_response": None,
                           "reconciliation_result": {"schema_version": "1.0", "review_id": "x",
                                                     "outcome": "converged", "items": _mal}})
    except AttributeError:
        _mal_ok = False
    check(_mal_ok, "R-F002: show renders a malformed items collection instead of crashing")

    # R-F003 (high): a sibling FILE is not a usable reviewer-response. Without this, a structurally
    # broken response certified a reconciliation as a verified pair.
    check(any("findings" in p for p in lib.reconciliation_problems(
              {"schema_version": "1.0", "review_id": "r", "outcome": "converged", "items": []},
              {"review_id": "r"})),
          "R-F003: a reviewer-response with no findings list cannot certify a pair")
    check(any("string 'id'" in p for p in lib.reconciliation_problems(
              {"schema_version": "1.0", "review_id": "r", "outcome": "converged", "items": []},
              {"review_id": "r", "findings": [{"no": "id"}]})),
          "R-F003: findings without string ids cannot be used for coverage")

    # R-F004 (medium): the closing line branched only on unverifiable + pending deadlocks, so a
    # record whose OWN outcome says the protocol never finished still signed off "nothing needed you".
    _f4 = report.render({"run_id": "f4", "reviewer_response":
                         {"schema_version": "1.0", "review_id": "f4", "assessment": "approve",
                          "findings": [{"id": "F001", "claim": "c",
                                        "evidence": [{"location": "l", "observation": "o"}]}]},
                         "reconciliation_result":
                         {"schema_version": "1.0", "reconciliation_id": "rc-f4",
                          "review_id": "f4", "outcome": "failed",
                          "items": [{"finding_id": "F001", "state": "resolved"}]}})
    check("Nothing needed you" not in _f4 and "Not a completed review" in _f4,
          "R-F004: a failed/incomplete outcome never renders as an all-settled review")

    # R-F005/R-F006 (medium): both live inside functions documented TOTAL, and both were reachable
    # from a hand-edited file — a truthy non-sized `items`, and a repr that blows the stack.
    check(len(lib.reconciliation_items({"items": 1})) == 0,
          "R-F005: a truthy non-sized items value counts as 0 rather than raising TypeError")
    _deep = []
    _cur = _deep
    for _ in range(200000):
        _nxt = []
        _cur.append(_nxt)
        _cur = _nxt
    _deep_ok = True
    try:
        lib.reconciliation_problems({"items": [{"finding_id": _deep, "state": "bogus"}]},
                                    {"findings": []})
    except RecursionError:
        _deep_ok = False
    check(_deep_ok, "R-F006: a deeply nested value in a diagnostic can't RecursionError out")

    # --- fixes from the same-provider (Fable) depth review of PR #19 ---
    _fb_prev = os.environ.get("IMPASSE_CONFIG_DIR")
    _fb_dir = tempfile.mkdtemp(prefix="impasse-fable-")
    try:
        os.environ["IMPASSE_CONFIG_DIR"] = _fb_dir
        _fb_rev = {"schema_version": "1.0", "review_id": "fb", "assessment": "approve", "summary": "s",
                   "artifact": {"kind": "code", "revision": {"algorithm": "sha256", "value": "a" * 64}},
                   "findings": [{"id": "F001", "severity": "low", "claim": "c", "confidence": "high",
                                 "evidence": [{"location": "l", "observation": "o"}]}]}
        lib.save_run_doc("fb", "reviewer-response", _fb_rev)
        lib.save_reconciliation_doc({"schema_version": "1.0", "reconciliation_id": "rc-fb",
                                     "review_id": "fb", "outcome": "converged",
                                     "items": [{"finding_id": "F001", "state": "resolved"}]})

        # FB-F1: "absent" and "present but unreadable" are OPPOSITE facts about a run. Collapsing
        # them made `show` state that a recorded, converged reconciliation was "not yet recorded" —
        # a false claim of exactly the class this whole change exists to stop — while `list` called
        # the same run an orphan.
        with open(os.path.join(_fb_dir, "runs", "fb", "reconciliation-result.json"), "w") as _f:
            _f.write("[1,2,3]")
        _fb_run = lib.load_run("fb")
        check(_fb_run["reconciliation_result"] is None
              and _fb_run["reconciliation_result_unreadable"] is True,
              "FB-F1: load_run distinguishes an unreadable record from an absent one")
        _fb_show = report.render(lib.load_run("fb"))
        check("UNVERIFIABLE" in _fb_show and "not yet recorded" not in _fb_show,
              "FB-F1: a corrupt reconciliation renders unverifiable, never 'not yet recorded'")
        check("quarantined" in report.lifetime_recap(),
              "FB-F1: a corrupt record is disclosed as quarantined, not silently dropped")

        # FB-F6: 0-of-N is the NORMAL state before a reconciliation exists; warning on it spends the
        # ⚠️ signal the rest of this change depends on.
        _fb_fresh = report.render({"run_id": "fresh", "reviewer_response": _fb_rev,
                                   "reconciliation_result": None})
        check("partial:" not in _fb_fresh and "not yet recorded" in _fb_fresh,
              "FB-F6: a healthy un-reconciled run carries no spurious partial warning")

        # FB-F3: a forget landing between validation and write let makedirs recreate the directory
        # holding a reconciliation ALONE — issue #17's orphan, from two commands each behaving as
        # documented. Deterministic in one process, contrary to the CHANGELOG's original claim that
        # this needed multi-process orchestration.
        lib.save_run_doc("fb2", "reviewer-response", dict(_fb_rev, review_id="fb2"))
        _fb_real = lib.save_run_doc

        def _fb_racing(run_id, name, doc, **kw):
            if name == "reconciliation-result":
                lib.forget_run(run_id)          # the interleaving
            return _fb_real(run_id, name, doc, **kw)

        lib.save_run_doc = _fb_racing
        try:
            _fb_res = lib.save_reconciliation_doc(
                {"schema_version": "1.0", "reconciliation_id": "rc-fb2", "review_id": "fb2",
                 "outcome": "converged", "items": [{"finding_id": "F001", "state": "resolved"}]})
        finally:
            lib.save_run_doc = _fb_real
        _fb_d = os.path.join(_fb_dir, "runs", "fb2")
        _fb_files = sorted(os.listdir(_fb_d)) if os.path.isdir(_fb_d) else []
        check(_fb_res.get("ok") is False and _fb_files != ["reconciliation-result.json"],
              "FB-F3: a forget racing a save cannot recreate the #17 orphan")
        check("disappeared while writing" in " ".join(_fb_res.get("reasons") or []),
              "FB-F3: the racing save refuses with a reason, leaving nothing behind")

        # The lock must be reentrant WITHIN a process: forget_run now takes the same per-run lock a
        # caller may already hold, and a self-deadlock in a records tool is worse than the race.
        with lib._interprocess_lock("run-reentry-test.lock"):
            with lib._interprocess_lock("run-reentry-test.lock"):
                check(True, "FB-F3: the per-run lock is reentrant in-process (no self-deadlock)")
    finally:
        if _fb_prev is None:
            os.environ.pop("IMPASSE_CONFIG_DIR", None)
        else:
            os.environ["IMPASSE_CONFIG_DIR"] = _fb_prev
        shutil.rmtree(_fb_dir, ignore_errors=True)

    # FB-F4: the validator's enums are a SECOND copy of the schema's. They gate every write and
    # quarantine every read, so silent drift would refuse every new-format record and brand it
    # unverifiable on six surfaces. Turn drift into a red gate instead.
    _fb_schema = json.load(open("schemas/reconciliation-result.v1.json"))
    check(set(lib.RECOGNIZED_OUTCOMES) == set(_fb_schema["properties"]["outcome"]["enum"]),
          "FB-F4: RECOGNIZED_OUTCOMES matches the schema's outcome enum (drift is a failure)")
    check(set(lib.RECOGNIZED_ITEM_STATES)
          == set(_fb_schema["$defs"]["item"]["properties"]["state"]["enum"]),
          "FB-F4: RECOGNIZED_ITEM_STATES matches the schema's state enum (drift is a failure)")

    # --- FB-F2: completing a --partial reconciliation must not require the destructive flag ---
    # The finished record conflicts with the operator's own interim one, so the normal workflow's
    # last step became --force. A guard everyone types by default guards nothing.
    _sp_prev = os.environ.get("IMPASSE_CONFIG_DIR")
    _sp_dir = tempfile.mkdtemp(prefix="impasse-supersede-")
    try:
        os.environ["IMPASSE_CONFIG_DIR"] = _sp_dir
        _sp_rev = {"schema_version": "1.0", "review_id": "sp", "assessment": "approve", "summary": "s",
                   "artifact": {"kind": "code", "revision": {"algorithm": "sha256", "value": "a" * 64}},
                   "findings": [{"id": f"F00{i}", "severity": "low", "claim": "c",
                                 "confidence": "high",
                                 "evidence": [{"location": "l", "observation": "o"}]}
                                for i in (1, 2, 3)]}
        lib.save_run_doc("sp", "reviewer-response", _sp_rev)
        lib.save_reconciliation_doc({"schema_version": "1.0", "reconciliation_id": "rc-i",
                                     "review_id": "sp", "outcome": "incomplete",
                                     "items": [{"finding_id": "F001", "state": "resolved"}]},
                                    partial=True)
        _sp_done = lib.save_reconciliation_doc(
            {"schema_version": "1.0", "reconciliation_id": "rc-done", "review_id": "sp",
             "outcome": "converged",
             "items": [{"finding_id": f, "state": "resolved"} for f in ("F001", "F002", "F003")]})
        check(_sp_done.get("ok") and _sp_done.get("superseded") is True,
              "FB-F2: completing an interim reconciliation needs no --force")
        check(bool(_sp_done.get("backup_path")),
              "FB-F2: superseding still keeps the interim record as a backup")

        # The two rails that must NOT open. A converged record claims to be finished, and a save
        # that would DROP dispositions is a clobber whatever the existing outcome says.
        _sp_clob = lib.save_reconciliation_doc(
            {"schema_version": "1.0", "reconciliation_id": "rc-other", "review_id": "sp",
             "outcome": "converged",
             "items": [{"finding_id": f, "state": "resolved"} for f in ("F001", "F002", "F003")]})
        check(_sp_clob.get("conflict") is True and _sp_clob.get("existing_outcome") == "converged",
              "FB-F2: replacing a CONVERGED record still requires --force")

        lib.save_run_doc("sp2", "reviewer-response", dict(_sp_rev, review_id="sp2"))
        lib.save_reconciliation_doc({"schema_version": "1.0", "reconciliation_id": "rc-i2",
                                     "review_id": "sp2", "outcome": "incomplete",
                                     "items": [{"finding_id": "F001", "state": "resolved"},
                                               {"finding_id": "F002", "state": "resolved"}]},
                                    partial=True)
        _sp_drop = lib.save_reconciliation_doc(
            {"schema_version": "1.0", "reconciliation_id": "rc-drop", "review_id": "sp2",
             "outcome": "incomplete", "items": [{"finding_id": "F003", "state": "resolved"}]},
            partial=True)
        check(_sp_drop.get("conflict") is True
              and sorted(_sp_drop.get("would_drop") or []) == ["F001", "F002"],
              "FB-F2: a save that would DROP dispositions still requires --force, and names them")

        # IDENTITY BY ID IS NOT IDENTITY OF WORK. A bare item is an id-superset of one carrying an
        # operator's ruling and a paragraph of verification notes — exactly the content --force
        # exists to protect, since findings can be re-derived from the reviewer-response and a
        # human's reasoning cannot. Found while probing my own supersede predicate.
        lib.save_run_doc("sp3", "reviewer-response", dict(_sp_rev, review_id="sp3"))
        _sp_rich = {"schema_version": "1.0", "reconciliation_id": "rc-rich", "review_id": "sp3",
                    "outcome": "deadlocked",
                    "items": [{"finding_id": "F001", "state": "deadlocked",
                               "host_position": "hours of verification reasoning",
                               "escalation": {"dispute_kind": "value_or_priority_tradeoff",
                                              "stop_reason": "operator_authority_required",
                                              "operator_question": "Runway or speed?"}}]}
        lib.save_reconciliation_doc(_sp_rich, partial=True)
        _sp_thin = lib.save_reconciliation_doc(
            {"schema_version": "1.0", "reconciliation_id": "rc-thin", "review_id": "sp3",
             "outcome": "converged",
             "items": [{"finding_id": f, "state": "resolved"} for f in ("F001", "F002", "F003")]})
        check(_sp_thin.get("conflict") is True
              and _sp_thin.get("would_impoverish") == ["F001"],
              "FB-F2: an id-superset that STRIPS an operator ruling is not a supersede")
        # ...but answering the deadlock while keeping the content IS the normal forward step.
        _sp_answered = lib.save_reconciliation_doc(
            {"schema_version": "1.0", "reconciliation_id": "rc-answered", "review_id": "sp3",
             "outcome": "converged",
             "items": [{"finding_id": "F001", "state": "resolved",
                        "host_position": "hours of verification reasoning",
                        "resolution": "Operator ruled: protect runway.",
                        "escalation": {"dispute_kind": "value_or_priority_tradeoff",
                                       "stop_reason": "operator_authority_required",
                                       "operator_question": "Runway or speed?"}}]
             + [{"finding_id": f, "state": "resolved"} for f in ("F002", "F003")]})
        check(_sp_answered.get("ok") and _sp_answered.get("superseded") is True,
              "FB-F2: answering a deadlock and keeping its content supersedes without --force")
        # An existing record we cannot READ is never supersedable: reconciliation_items degrades a
        # corrupt collection to [], which would make the superset test hold VACUOUSLY — the more
        # damaged the old record, the easier it would have been to overwrite unflagged.
        lib.save_run_doc("sp4", "reviewer-response", dict(_sp_rev, review_id="sp4"))
        with open(os.path.join(_sp_dir, "runs", "sp4", "reconciliation-result.json"), "w") as _f:
            _f.write(json.dumps({"review_id": "sp4", "items": "corrupt-but-truthy"}))
        _sp_corrupt = lib.save_reconciliation_doc(
            {"schema_version": "1.0", "reconciliation_id": "rc-x", "review_id": "sp4",
             "outcome": "converged",
             "items": [{"finding_id": f, "state": "resolved"} for f in ("F001", "F002", "F003")]})
        check(_sp_corrupt.get("conflict") is True
              and _sp_corrupt.get("existing_unreadable") is True,
              "FB-F2: an UNREADABLE existing record is never superseded vacuously")

        check(lib._item_loses_substance({"escalation": {"a": 1}}, {"state": "resolved"}) is True
              and lib._item_loses_substance({"state": "resolved"}, {"escalation": {"a": 1}}) is False
              and lib._item_loses_substance("nope", 7) is False,
              "FB-F2: _item_loses_substance detects loss, permits gain, and is total")
    finally:
        if _sp_prev is None:
            os.environ.pop("IMPASSE_CONFIG_DIR", None)
        else:
            os.environ["IMPASSE_CONFIG_DIR"] = _sp_prev
        shutil.rmtree(_sp_dir, ignore_errors=True)

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

    # --- T1: install-codex.sh safety (drive the bash installer against a throwaway --root) ---
    import shutil as _sh
    import subprocess as _sp
    _installer = os.path.join(os.getcwd(), "scripts", "install-codex.sh")
    if _sh.which("bash") and os.path.isfile(_installer):
        _iroot = tempfile.mkdtemp(prefix="impasse-installer-test-")

        def _inst(*args):
            return _sp.run(["bash", _installer, "--root", _iroot, *args],
                           capture_output=True, text=True)
        try:
            # fresh symlink install + idempotent re-run
            r1 = _inst()
            _dest = os.path.join(_iroot, "impasse")
            check(r1.returncode == 0 and os.path.islink(_dest)
                  and os.path.realpath(_dest) == os.getcwd(),
                  "T1 installer: fresh install creates a symlink to the repo")
            check(_inst().returncode == 0 and "Already installed" in _inst().stdout,
                  "T1 installer: re-run is idempotent (Already installed)")
            # REFUSE a physical dir at the destination, leaving it untouched (the core safety guarantee)
            os.remove(_dest)
            os.makedirs(_dest)
            _keep = os.path.join(_dest, "keep.txt")
            open(_keep, "w").write("precious")
            r2 = _inst()
            check(r2.returncode != 0 and "not a symlink" in (r2.stderr + r2.stdout)
                  and os.path.isfile(_keep),
                  "T1 installer: refuses a physical dir and leaves it INTACT (never deletes real data)")
            # --dry-run changes nothing
            _sh.rmtree(_dest)
            before = os.path.exists(_dest)
            check(_inst("--dry-run").returncode == 0 and os.path.exists(_dest) == before,
                  "T1 installer: --dry-run makes no filesystem change")
        finally:
            _sh.rmtree(_iroot, ignore_errors=True)
    else:
        check(True, "T1 installer: skipped (bash or installer unavailable)")

    # --- install-cursor.sh: same safety contract, driven against a throwaway --root ---
    # The guarantee that matters is identical to the Codex installer's: it acts only on a symlink or
    # an empty slot, and REFUSES a physical path rather than deleting real data. Re-tested rather
    # than assumed, because the two scripts are separate files that can drift apart.
    _cur_installer = os.path.join(os.getcwd(), "scripts", "install-cursor.sh")
    if _sh.which("bash") and os.path.isfile(_cur_installer):
        _curoot = tempfile.mkdtemp(prefix="impasse-cursor-installer-test-")

        def _cinst(*args):
            return _sp.run(["bash", _cur_installer, "--root", _curoot, *args],
                           capture_output=True, text=True)
        try:
            _cr1 = _cinst()
            _cdest = os.path.join(_curoot, "impasse")
            check(_cr1.returncode == 0 and os.path.islink(_cdest)
                  and os.path.realpath(_cdest) == os.getcwd(),
                  "cursor installer: fresh install creates a symlink to the repo")
            check(_cinst().returncode == 0 and "Already installed" in _cinst().stdout,
                  "cursor installer: re-run is idempotent (Already installed)")
            # The core safety guarantee: a real directory is refused and left byte-for-byte intact.
            os.remove(_cdest)
            os.makedirs(_cdest)
            _ckeep = os.path.join(_cdest, "keep.txt")
            open(_ckeep, "w").write("precious")
            _cr2 = _cinst()
            check(_cr2.returncode != 0 and "not a symlink" in (_cr2.stderr + _cr2.stdout)
                  and os.path.isfile(_ckeep) and open(_ckeep).read() == "precious",
                  "cursor installer: refuses a physical dir and leaves it INTACT")
            _sh.rmtree(_cdest)
            _cbefore = os.path.exists(_cdest)
            check(_cinst("--dry-run").returncode == 0 and os.path.exists(_cdest) == _cbefore,
                  "cursor installer: --dry-run makes no filesystem change")
            # It must NOT quietly imply Cursor gets independence for free: the operator-assertion
            # requirement is the one thing a Cursor user most needs to be told at install time.
            check("IMPASSE_HOST" in _cr1.stdout,
                  "cursor installer: tells the operator they must assert the host model")
            # F002: `ln -s SRC DEST` does NOT fail when DEST is a directory — it silently creates
            # DEST/<name> inside it and exits 0. Simulate the post-rm race by pre-creating the
            # directory and confirming the installer FAILS loudly rather than reporting success
            # with a link buried one level down.
            _sh.rmtree(_cdest, ignore_errors=True)
            os.makedirs(_cdest)
            _cr3 = _cinst()
            check(_cr3.returncode != 0
                  and not os.path.islink(os.path.join(_cdest, "impasse")),
                  "cursor installer: a directory at the destination never becomes a link INSIDE it")
            _sh.rmtree(_cdest, ignore_errors=True)
        finally:
            _sh.rmtree(_curoot, ignore_errors=True)
    else:
        # NOT an unconditional pass: deleting install-cursor.sh is exactly "reverting the production
        # change", and a green skip would hide it. Only a missing bash may excuse the suite.
        check(_sh.which("bash") is None,
              "cursor installer: present (a missing installer is a FAILURE, not a skip)")

    print()
    if _fails:
        print(f"{len(_fails)} FAILURES: " + "; ".join(_fails))
        return 1
    print("all helper tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
