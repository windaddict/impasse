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
        lib.normalize_destination("https://user:pass@host/x"); creds_ok = False
    except ValueError:
        creds_ok = True
    check(creds_ok, "normalize_destination rejects embedded credentials")

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

    # --- supervisor ---
    r = run.supervise(["bash", "-c", "printf hi"], wall_timeout=10, idle_timeout=5)
    check(r.termination == "completed" and r.exit_code == 0 and r.stdout == b"hi", "supervise: completed + stdout")
    r = run.supervise(["cat"], input_bytes=b"piped-eof", wall_timeout=10, idle_timeout=5)
    check(r.termination == "completed" and r.stdout == b"piped-eof", "supervise: stdin delivered + EOF")

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

    # --- review() end-to-end via a fake codex backend ---
    fake = os.path.join(tmp, "fake-codex")
    with open(fake, "w") as f:
        f.write(FAKE_CODEX)
    os.chmod(fake, 0o755)
    os.environ["IMPASSE_CODEX_BIN"] = fake
    os.environ.pop("OPENAI_BASE_URL", None)  # default destination https://api.openai.com

    # consent still enforced first:
    os.environ["FAKE_MODE"] = "valid"; os.environ["FAKE_EXIT"] = "0"
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

    os.environ["FAKE_MODE"] = "valid"; os.environ["FAKE_EXIT"] = "3"
    res = run.review(kind="code", instruction="review", artifact_bytes=b"code")
    check(res["ok"] is False and res["failure"]["code"] == "backend_error", "review: nonzero exit -> backend_error")

    try:
        run.review(kind="code", instruction="x", artifact_bytes=b"a", effort="minimal")
        bad_eff = False
    except ValueError:
        bad_eff = True
    check(bad_eff, "review: rejects disallowed effort ('minimal')")

    print()
    if _fails:
        print(f"{len(_fails)} FAILURES: " + "; ".join(_fails))
        return 1
    print("all helper tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
