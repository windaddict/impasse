"""Supervised reviewer-backend invocation for Impasse. stdlib only.

Runs the reviewer as a subprocess with:
  - argument-array execution (never a shell string);
  - stdin written on a SEPARATE thread then closed (EOF) — an open, unwritten stdin
    is what makes `codex exec` hang; and writing on a thread means a backend that
    stops reading stdin can't dodge the timeouts below;
  - a hard WALL timeout AND an IDLE (no-output) timeout;
  - reliable process-TREE termination (own process group -> SIGTERM -> grace ->
    SIGKILL, polling the GROUP, not just the leader), then a BOUNDED reap;
  - size-capped stdout/stderr capture (avoids pipe-buffer backpressure deadlock);
  - a machine-readable termination reason.

The `review` entry ENFORCES data-boundary consent before anything is sent, and
classifies the reviewer's output: non-JSON / wrong-shape output is `invalid_response`,
never success. The reviewer's output is UNTRUSTED data — consumers must validate it
against the schema and must not render/execute it as trusted content.

Reliable process-group termination is POSIX-only (macOS/Linux). On non-POSIX the
supervisor degrades to process-level kill; Windows is a documented roadmap.

CLI:
  impasse_run.py review --kind code --instruction-file I.txt --artifact-file A.md \\
      [--schema schemas/reviewer-response.v1.json] [--approve-send DEST] \\
      [--effort low] [--wall 180] [--idle 60]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import impasse_lib as lib          # noqa: E402
import impasse_consent as consent  # noqa: E402

_POSIX = os.name == "posix"
_ALLOWED_EFFORT = {"none", "low", "medium", "high", "xhigh"}  # 'minimal' is rejected by codex
_MAX_FINAL = 2_000_000
_MAX_INPUT = 4_000_000


@dataclass
class RunResult:
    termination: str          # completed | wall_timeout | idle_timeout | termination_failed | spawn_error
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool
    reader_error: bool
    duration_s: float


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True  # EPERM etc. -> assume still there


def _kill_tree(proc: subprocess.Popen, grace: float = 5.0) -> None:
    """SIGTERM the process GROUP, poll the group, SIGKILL at grace. POSIX only;
    otherwise best-effort process-level terminate/kill."""
    if _POSIX:
        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            pgid = None
        if pgid is not None:
            try:
                os.killpg(pgid, signal.SIGTERM)
            except OSError:
                pass
            end = time.monotonic() + grace
            while time.monotonic() < end:
                if not _group_alive(pgid):
                    return
                time.sleep(0.1)
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass
            return
    try:
        proc.terminate()
    except OSError:
        pass
    end = time.monotonic() + grace
    while time.monotonic() < end:
        if proc.poll() is not None:
            return
        time.sleep(0.1)
    try:
        proc.kill()
    except OSError:
        pass


def supervise(argv, input_bytes: bytes | None = None, *, wall_timeout: float = 180.0,
              idle_timeout: float = 60.0, max_output_bytes: int = 8_000_000,
              cwd: str | None = None, env: dict | None = None) -> RunResult:
    for label, val in (("wall_timeout", wall_timeout), ("idle_timeout", idle_timeout)):
        if not (isinstance(val, (int, float)) and math.isfinite(val) and val > 0):
            raise ValueError(f"{label} must be a positive finite number")
    if not (isinstance(max_output_bytes, int) and max_output_bytes > 0):
        raise ValueError("max_output_bytes must be a positive integer")

    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,  # own process group -> killable as a tree (POSIX)
            cwd=cwd, env=env,
        )
    except (OSError, ValueError) as e:
        return RunResult("spawn_error", None, b"", str(e).encode(), False, False, False, 0.0)

    out = bytearray()
    err = bytearray()
    out_trunc = {"v": False}
    err_trunc = {"v": False}
    reader_err = {"v": False}
    last = [time.monotonic()]
    lock = threading.Lock()

    def reader(stream, buf, trunc):
        try:
            read = stream.read1 if hasattr(stream, "read1") else stream.read
            while True:
                chunk = read(65536)
                if not chunk:
                    break
                with lock:
                    last[0] = time.monotonic()
                    room = max_output_bytes - len(buf)
                    if room > 0:
                        buf += chunk[:room]
                        if len(chunk) > room:
                            trunc["v"] = True
                    else:
                        trunc["v"] = True
        except (OSError, ValueError):
            reader_err["v"] = True
        finally:
            try:
                stream.close()
            except OSError:
                pass

    t_out = threading.Thread(target=reader, args=(proc.stdout, out, out_trunc), daemon=True)
    t_err = threading.Thread(target=reader, args=(proc.stderr, err, err_trunc), daemon=True)
    t_out.start()
    t_err.start()

    # Write stdin on a thread so a backend that stops reading can't block the supervisor.
    def stdin_writer():
        try:
            proc.stdin.write(input_bytes)
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    if input_bytes is not None:
        threading.Thread(target=stdin_writer, daemon=True).start()

    termination = "completed"
    while True:
        if proc.poll() is not None:
            break
        now = time.monotonic()
        if now - start >= wall_timeout:
            termination = "wall_timeout"
            break
        with lock:
            idle = now - last[0]
        if idle >= idle_timeout:
            termination = "idle_timeout"
            break
        time.sleep(0.2)

    if termination != "completed":
        _kill_tree(proc)

    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            termination = "termination_failed"

    # On a clean completion the process is reaped, so the pipes are closed and the readers
    # hit EOF promptly — join them reliably so stdout_truncated isn't under-reported. On a
    # kill/termination path the output is already suspect, so bound the join.
    join_timeout = None if termination == "completed" else 5
    t_out.join(timeout=join_timeout)
    t_err.join(timeout=join_timeout)
    if t_out.is_alive() or t_err.is_alive():
        reader_err["v"] = True
    return RunResult(termination, proc.returncode, bytes(out), bytes(err),
                     out_trunc["v"], err_trunc["v"], reader_err["v"], time.monotonic() - start)


def build_codex_argv(backend_command, *, instruction: str, output_last_message: str,
                     effort: str | None = None) -> list[str]:
    """Assemble a read-only `codex exec` review command. The artifact is fed on stdin
    (as context), not as an argv element, so large artifacts don't hit ARG_MAX and
    stdin still reaches EOF.

    NOTE: we do NOT use `--output-schema`. OpenAI's structured-output mode requires a
    restricted schema (every property in `required`, no oneOf/allOf/if-then/minLength/
    pattern) — the rich reviewer-response schema doesn't qualify. Instead the schema is
    embedded in the instruction (see review()) and the output is validated afterward.
    """
    argv = list(backend_command) + [
        "exec", "--json", "--output-last-message", output_last_message,
        "--sandbox", "read-only", "--color", "never",
        "--skip-git-repo-check", "--ephemeral",
    ]
    if effort:
        argv += ["-c", f'model_reasoning_effort="{effort}"']
    argv += [instruction]
    return argv


def _fail(code, message, kind, notice, manifest, termination=None) -> dict:
    r = {"ok": False, "outcome": "failed", "kind": kind,
         "failure": {"code": code, "message": message}, "notice": notice, "manifest": manifest}
    if termination:
        r["termination"] = termination
    return r


def review(*, kind: str, instruction: str, artifact_bytes: bytes,
           schema_path: str | None = None, approve_send: str | None = None,
           effort: str | None = None, wall_timeout: float = 180.0,
           idle_timeout: float = 60.0, no_record: bool = False) -> dict:
    """Enforce consent, run a supervised read-only review, and classify the result.
    The returned 'response' is UNTRUSTED reviewer output — validate against the schema."""
    if effort is not None and effort not in _ALLOWED_EFFORT:
        raise ValueError(f"effort must be one of {sorted(_ALLOWED_EFFORT)}")

    backend = lib.get_backend("codex")
    manifest = consent.manifest_for_bytes(artifact_bytes)
    approved, notice = consent.check(backend, manifest=manifest, approve_send=approve_send)
    if not approved:
        return _fail("consent_denied", notice, kind, notice, manifest)

    scratch = tempfile.mkdtemp(prefix="impasse-run-", dir=lib.ensure_config_dir())
    try:
        out_fd, out_last = tempfile.mkstemp(prefix="last-", suffix=".txt", dir=scratch)
        os.close(out_fd)
        full_instruction = instruction
        if schema_path:
            try:
                with open(schema_path, encoding="utf-8") as f:
                    schema_text = f.read()
                full_instruction = (
                    instruction
                    + "\n\nReturn ONLY a JSON object — no prose, no markdown fence — that "
                    "validates against this JSON Schema:\n" + schema_text
                )
            except OSError:
                pass
        argv = build_codex_argv(backend.command, instruction=full_instruction,
                                output_last_message=out_last, effort=effort)
        result = supervise(argv, input_bytes=artifact_bytes,
                           wall_timeout=wall_timeout, idle_timeout=idle_timeout)

        if result.termination == "spawn_error":
            return _fail("backend_error", result.stderr.decode("utf-8", "replace")[-800:], kind, notice, manifest)
        if result.termination in ("wall_timeout", "idle_timeout", "termination_failed"):
            return _fail("timeout", f"backend {result.termination} after {result.duration_s:.0f}s",
                         kind, notice, manifest, termination=result.termination)
        if result.exit_code != 0:
            return _fail("backend_error",
                         f"exit={result.exit_code}; stderr: {result.stderr.decode('utf-8', 'replace')[-800:]}",
                         kind, notice, manifest)

        final = None
        try:
            with open(out_last, "rb") as f:
                final = f.read().decode("utf-8", "replace")
        except OSError:
            pass
        if not final or not final.strip():
            return _fail("invalid_response", "reviewer produced no final message", kind, notice, manifest)
        if len(final) > _MAX_FINAL:
            return _fail("invalid_response", f"final message exceeds {_MAX_FINAL} bytes", kind, notice, manifest)
        try:
            parsed = json.loads(final)
        except json.JSONDecodeError as e:
            return _fail("invalid_response", f"final message is not valid JSON: {e}", kind, notice, manifest)
        if not isinstance(parsed, dict) or "schema_version" not in parsed or not (("findings" in parsed) or ("items" in parsed)):
            return _fail("invalid_response", "final message JSON is missing expected top-level fields", kind, notice, manifest)

        run_id = parsed.get("review_id")
        recorded = False
        record_path = None
        # Persistence is a data boundary too: surface where the reviewed content lands locally.
        record_notice = "Not recorded (--no-record)." if no_record else None
        if run_id and not no_record:
            try:
                p = lib.save_run_doc(run_id, "reviewer-response", parsed)
                recorded = True
                record_path = os.path.dirname(p)
                record_notice = (
                    f"Recorded locally at {record_path} (0600) — this holds the reviewed content. "
                    f"Re-run with --no-record to skip; `impasse_report.py forget {run_id}` to delete; "
                    f"`impasse_report.py prune --older-than N` to clean up old records."
                )
            except OSError:
                pass
        return {
            "ok": True, "kind": kind, "termination": result.termination,
            "duration_s": round(result.duration_s, 2),
            "response": parsed,   # UNTRUSTED — validate against the schema; don't render as trusted content
            "run_id": run_id, "recorded": recorded, "record_path": record_path,
            "record_notice": record_notice,
            "notice": notice, "manifest": manifest,
        }
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _read_limited(path: str, limit: int, *, binary: bool) -> bytes | str:
    # Read limit+1 and reject if longer — no getsize()/read() TOCTOU.
    mode = "rb" if binary else "r"
    kwargs = {} if binary else {"encoding": "utf-8"}
    with open(path, mode, **kwargs) as f:
        data = f.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{path} exceeds {limit} bytes")
    return data


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="impasse_run")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rv = sub.add_parser("review")
    rv.add_argument("--kind", required=True, choices=["code", "document", "decision", "research", "data", "other"])
    rv.add_argument("--instruction-file", required=True)
    rv.add_argument("--artifact-file", required=True)
    rv.add_argument("--schema", default=None)
    rv.add_argument("--approve-send", default=None)
    rv.add_argument("--effort", default=None)
    rv.add_argument("--wall", type=float, default=180.0)
    rv.add_argument("--idle", type=float, default=60.0)
    rv.add_argument("--no-record", action="store_true", help="don't persist the run record")
    args = ap.parse_args(argv)

    if args.cmd == "review":
        try:
            instruction = _read_limited(args.instruction_file, _MAX_INPUT, binary=False)
            artifact_bytes = _read_limited(args.artifact_file, _MAX_INPUT, binary=True)
        except (OSError, ValueError) as e:
            print(json.dumps({"ok": False, "outcome": "failed",
                              "failure": {"code": "artifact_unavailable", "message": str(e)}}, indent=2))
            return 1
        result = review(kind=args.kind, instruction=instruction, artifact_bytes=artifact_bytes,
                        schema_path=args.schema, approve_send=args.approve_send, effort=args.effort,
                        wall_timeout=args.wall, idle_timeout=args.idle, no_record=args.no_record)
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
