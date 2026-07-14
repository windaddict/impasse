"""Block-by-default data-boundary consent for Impasse.

Sending an artifact to a reviewer backend means it LEAVES this machine for a
third-party provider, under that provider's terms and retention. This gate records
the operator's approval per DESTINATION (a normalized endpoint, not just a provider
label) and shows a payload manifest so the operator approves *what* is sent, not
merely *where*. A changed endpoint or notice version invalidates an old grant.

Enforcement lives here (in code) so every host adapter behaves the same — the
markdown skill must not silently edit consent. A warning is not consent: default = BLOCK.

Precedence: explicit per-run approval  >  IMPASSE_APPROVE_SEND (per-process)  >
persistent grant (matching destination + notice version)  >  block. IMPASSE_APPROVE_SEND
is per-process only; it is not a permanent global bypass.

CLI:
  impasse_consent.py grant <destination> [--endpoint URL] [--backend-type T] [--provider P]
  impasse_consent.py revoke <destination>
  impasse_consent.py list
  impasse_consent.py check <destination> [--endpoint URL] [--approve-send D]
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import impasse_lib as lib  # noqa: E402

CONSENT_VERSION = 1
NOTICE_VERSION = "1"


def consent_path() -> str:
    return os.path.join(lib.config_dir(), "consent.json")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load() -> dict:
    p = consent_path()
    if os.path.isfile(p) and not os.path.islink(p):
        try:
            with open(p, encoding="utf-8") as fh:
                data = json.loads(fh.read())
            if (isinstance(data, dict) and data.get("version") == CONSENT_VERSION
                    and isinstance(data.get("grants"), list)):
                return data
        except (OSError, json.JSONDecodeError):
            pass  # unreadable / malformed / wrong version -> treat as empty (blocks, which is safe)
    return {"version": CONSENT_VERSION, "grants": []}


def _save(state: dict) -> None:
    d = lib.ensure_config_dir()
    p = consent_path()
    if os.path.islink(p):
        raise OSError(f"refusing to write consent through a symlink: {p}")
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".consent-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)  # not a secret, but tampering matters
        os.replace(tmp, p)    # atomic; a crash can't leave a half-written consent file
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def has_grant(destination_id: str) -> bool:
    """A grant counts only if it matches the destination AND the current notice version."""
    for g in _load()["grants"]:
        if g.get("destination_id") == destination_id and g.get("notice_version") == NOTICE_VERSION:
            return True
    return False


def grant(destination_id: str, backend_type: str = "", endpoint: str = "", provider: str = "") -> None:
    state = _load()
    grants = [g for g in state["grants"] if g.get("destination_id") != destination_id]
    grants.append({
        "destination_id": destination_id,
        "backend_type": backend_type,
        "provider": provider,
        "endpoint": endpoint,
        "granted_at": _now(),
        "notice_version": NOTICE_VERSION,
    })
    state["grants"] = grants
    _save(state)


def revoke(destination_id: str) -> bool:
    state = _load()
    before = len(state["grants"])
    state["grants"] = [g for g in state["grants"] if g.get("destination_id") != destination_id]
    _save(state)
    return len(state["grants"]) < before


def data_boundary_notice(backend: "lib.Backend") -> str:
    return (
        f'Data boundary: sending selected artifact content to {backend.provider} '
        f'({backend.endpoint}) via backend "{backend.name}".'
    )


def manifest_for_bytes(data: bytes, untracked_included: bool = False,
                       includes_command_output: bool = False) -> dict:
    """Manifest built from the EXACT bytes that will be sent (with a digest), so
    approval is meaningful and not a stale path-size (avoids TOCTOU)."""
    return {
        "total_bytes": len(data),
        "approx_tokens": len(data) // 4,  # rough 4-bytes/token heuristic
        "digest": lib.sha256_prefixed(data),
        "files": [],
        "untracked_included": untracked_included,
        "includes_command_output": includes_command_output,
    }


def render_manifest(m: dict) -> str:
    lines = [f"  Payload: ~{m.get('total_bytes', 0)} bytes (~{m.get('approx_tokens', 0)} tokens)"]
    if m.get("digest"):
        lines.append(f"    digest: {m['digest']}")
    for f in m.get("files", []):
        lines.append(f"    - {f['path']} ({f['bytes']} bytes)")
    if m.get("untracked_included"):
        lines.append("    ! includes untracked files")
    if m.get("includes_command_output"):
        lines.append("    ! includes command output")
    return "\n".join(lines)


def check(backend: "lib.Backend", manifest: dict | None = None,
          approve_send: str | None = None) -> tuple[bool, str]:
    """Return (approved, message). Message always contains the data-boundary notice."""
    dest = backend.destination_id
    env_approval = os.environ.get("IMPASSE_APPROVE_SEND")
    approved = (approve_send == dest) or (env_approval == dest) or has_grant(dest)

    parts = [data_boundary_notice(backend)]
    if manifest:
        parts.append(render_manifest(manifest))
    if approved:
        return True, "\n".join(parts)

    parts.append(
        "NOT approved — this run is blocked until you approve sending data to this destination.\n"
        f"  Per-run:   pass --approve-send {dest}  (or set IMPASSE_APPROVE_SEND={dest})\n"
        f"  Permanent: python3 scripts/impasse_consent.py grant {dest} "
        f"--endpoint '{backend.endpoint}' --backend-type {backend.type}"
    )
    return False, "\n".join(parts)


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="impasse_consent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("grant")
    g.add_argument("destination")
    g.add_argument("--endpoint", default="")
    g.add_argument("--backend-type", default="")
    g.add_argument("--provider", default="")
    r = sub.add_parser("revoke")
    r.add_argument("destination")
    sub.add_parser("list")
    c = sub.add_parser("check")
    c.add_argument("destination")
    c.add_argument("--endpoint", default="")
    c.add_argument("--backend-type", default="codex-cli")
    c.add_argument("--provider", default="openai")
    c.add_argument("--approve-send", default=None)
    args = ap.parse_args(argv)

    if args.cmd == "list":
        for gr in _load()["grants"]:
            print(f"  {gr['destination_id']}  ({gr.get('endpoint', '')})  granted {gr.get('granted_at', '')}")
        return 0

    # grant / revoke / check key on the NORMALIZED destination, matching the runtime check —
    # otherwise a grant for "https://API.OpenAI.com/v1" would never match "https://api.openai.com".
    try:
        dest = lib.normalize_destination(args.destination)
    except ValueError as e:
        print(f"invalid destination (expected an https URL): {e}", file=sys.stderr)
        return 2

    if args.cmd == "grant":
        grant(dest, args.backend_type, args.endpoint or dest, args.provider)
        print(f"granted: {dest}")
        return 0
    if args.cmd == "revoke":
        print("revoked" if revoke(dest) else "no grant found")
        return 0
    if args.cmd == "check":
        be = lib.Backend(name="codex", type=args.backend_type, provider=args.provider,
                         destination_id=dest, endpoint=args.endpoint or dest, command=[])
        ok, msg = check(be, approve_send=args.approve_send)
        print(msg)
        return 0 if ok else 3
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
