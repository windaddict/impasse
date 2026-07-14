"""Shared, stdlib-only helpers for Impasse: config dir, backend resolution, hashing.

No third-party dependencies — this ships with the skill. (Schema validation, which
needs `jsonschema`, is a dev/CI concern under tests/, not a runtime dependency.)

POSIX (macOS/Linux) is the supported runtime; Windows is a documented roadmap.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from urllib.parse import urlsplit

APP = "impasse"


def config_dir() -> str:
    """Absolute platform config directory (consent, local state).

    Honors IMPASSE_CONFIG_DIR, then the platform convention (Linux XDG, macOS
    Application Support, Windows APPDATA). Always returned as an absolute path.
    """
    override = os.environ.get("IMPASSE_CONFIG_DIR")
    if override:
        return os.path.abspath(override)
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    elif os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.abspath(os.path.join(base, APP))


def ensure_config_dir() -> str:
    d = config_dir()
    os.makedirs(d, exist_ok=True)
    try:
        os.chmod(d, 0o700)  # best-effort; not supported everywhere
    except OSError:
        pass
    return d


@dataclass(frozen=True)
class Backend:
    """A resolved reviewer backend and its data destination (for consent)."""
    name: str            # "codex"
    type: str            # "codex-cli"
    provider: str        # display label, e.g. "OpenAI"
    destination_id: str  # normalized endpoint consent is keyed on, e.g. "https://api.openai.com"
    endpoint: str        # the raw configured endpoint
    command: list[str]   # argv to invoke, e.g. ["/path/to/codex"]
    independence: str = "cross_provider"  # "cross_provider" (Codex) | "same_provider" (Claude fallback)


def _resolve_from_env(*names: str) -> list[str] | None:
    for name in names:
        v = os.environ.get(name)
        if not v:
            continue
        if os.path.isfile(v) and os.access(v, os.X_OK):
            return [v]
        w = shutil.which(v)
        if w:
            return [w]
        raise FileNotFoundError(f"{name} is set but not a runnable executable: {v}")
    return None


def resolve_codex_command() -> list[str] | None:
    """Resolve the codex binary cross-platform. See docs/backends/codex.md.

    Order: IMPASSE_CODEX_BIN / CODEX_BIN override -> PATH -> known locations.
    nvm/fnm installs are on PATH in a normal shell; a stripped PATH should set the
    override rather than guess a Node version. Returns argv, or None if not found.
    """
    override = _resolve_from_env("IMPASSE_CODEX_BIN", "CODEX_BIN")
    if override:
        return override
    on_path = shutil.which("codex")
    if on_path:
        return [on_path]
    home = os.environ.get("HOME") or os.path.expanduser("~")
    appdata = (os.environ.get("APPDATA") or "").replace("\\", "/")
    candidates = [
        "/opt/homebrew/bin/codex",
        "/usr/local/bin/codex",
        os.path.join(home, ".local/bin/codex"),
        os.path.join(home, ".npm-global/bin/codex"),
        f"{appdata}/npm/codex" if appdata else "",
        "/Applications/Codex.app/Contents/Resources/codex",
        os.path.join(home, "Applications/Codex.app/Contents/Resources/codex"),
        "/opt/Codex/codex",
    ]
    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return [c]
    return None


def resolve_claude_command() -> list[str] | None:
    """Resolve the claude (Claude Code) binary cross-platform. See docs/backends/claude.md.

    Order: IMPASSE_CLAUDE_BIN / CLAUDE_BIN override -> PATH -> known locations. Returns argv,
    or None if not found. Used only for the same-provider fallback backend.
    """
    override = _resolve_from_env("IMPASSE_CLAUDE_BIN", "CLAUDE_BIN")
    if override:
        return override
    on_path = shutil.which("claude")
    if on_path:
        return [on_path]
    home = os.environ.get("HOME") or os.path.expanduser("~")
    appdata = (os.environ.get("APPDATA") or "").replace("\\", "/")
    candidates = [
        "/opt/homebrew/bin/claude",
        "/usr/local/bin/claude",
        os.path.join(home, ".local/bin/claude"),
        os.path.join(home, ".npm-global/bin/claude"),
        f"{appdata}/npm/claude" if appdata else "",
    ]
    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return [c]
    return None


def normalize_destination(endpoint: str) -> str:
    """Canonical destination id from an endpoint URL, for keying consent.

    Consent is keyed on this, so a changed endpoint (Azure, a proxy, localhost)
    invalidates an old grant automatically. Rejects embedded credentials and any
    non-http(s) scheme. Returns 'scheme://host[:port]' lowercased.
    """
    u = urlsplit(endpoint.strip())
    if u.username or u.password:
        raise ValueError("endpoint must not contain embedded credentials")
    if u.scheme not in ("http", "https"):
        raise ValueError(f"unsupported endpoint scheme: {u.scheme or '(none)'}")
    if not u.hostname:
        raise ValueError("endpoint has no host")
    port = f":{u.port}" if u.port else ""
    return f"{u.scheme}://{u.hostname.lower()}{port}"


def _provider_label(destination_id: str) -> str:
    # Exact host suffix, not a substring — 'evil-openai.com.attacker.net' must not read as OpenAI.
    host = urlsplit(destination_id).hostname or ""
    if host == "api.openai.com" or host.endswith(".openai.com") or host == "openai.com":
        return "OpenAI"
    if host == "api.anthropic.com" or host.endswith(".anthropic.com") or host == "anthropic.com":
        return "Anthropic"
    return destination_id


def get_backend(name: str = "codex") -> Backend:
    """Return a resolved Backend.

    'codex' (default) is the cross-provider reviewer — real independence. 'claude' is a
    same-provider FALLBACK for users without Codex: it shares the host's blind spots, so it
    buys breadth / an adversarial second pass, NOT independence. See docs/backends/claude.md
    and the independence ladder in the Guardrails.
    """
    if name == "codex":
        cmd = resolve_codex_command()
        if not cmd:
            raise FileNotFoundError(
                "codex CLI not found. Install it (npm i -g @openai/codex, or the Codex "
                "desktop app), or set CODEX_BIN / IMPASSE_CODEX_BIN."
            )
        # A custom base URL (Azure, an enterprise gateway, localhost) changes where data
        # actually goes; normalize it so consent is keyed to the real destination.
        endpoint = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com")
        destination_id = normalize_destination(endpoint)
        return Backend(
            name="codex", type="codex-cli", provider=_provider_label(destination_id),
            destination_id=destination_id, endpoint=endpoint, command=cmd,
            independence="cross_provider",
        )
    if name == "claude":
        # Claude Code can route to AWS Bedrock / GCP Vertex via these env vars. Then the data does
        # NOT go to api.anthropic.com, so keying consent to the Anthropic endpoint would be a lie.
        # Refuse rather than mis-key the consent gate (the whole point of the gate is honesty).
        if os.environ.get("CLAUDE_CODE_USE_BEDROCK") or os.environ.get("CLAUDE_CODE_USE_VERTEX"):
            raise ValueError(
                "the claude backend keys consent to the Anthropic API, but Claude Code is "
                "configured for Bedrock/Vertex (CLAUDE_CODE_USE_BEDROCK/VERTEX) — data would go "
                "to AWS/GCP instead. Use the codex backend, or unset those to route via "
                "api.anthropic.com."
            )
        cmd = resolve_claude_command()
        if not cmd:
            raise FileNotFoundError(
                "claude CLI not found. Install Claude Code, or set CLAUDE_BIN / IMPASSE_CLAUDE_BIN."
            )
        # A custom ANTHROPIC_BASE_URL (a gateway/proxy) still keys consent to wherever data goes.
        endpoint = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
        destination_id = normalize_destination(endpoint)
        return Backend(
            name="claude", type="claude-cli", provider=_provider_label(destination_id),
            destination_id=destination_id, endpoint=endpoint, command=cmd,
            independence="same_provider",
        )
    raise ValueError(f"unknown backend '{name}' (supported: codex, claude)")


def sha256_prefixed(data: bytes) -> str:
    """'sha256:<hex>' — the form used by evidence digests in the schemas."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def artifact_revision(data: bytes) -> dict:
    """The schema's artifact.revision object for the exact bytes reviewed."""
    return {"algorithm": "sha256", "value": hashlib.sha256(data).hexdigest()}


# --- Run records (the audit trail) -------------------------------------------------
# A run is persisted under config_dir()/runs/<run_id>/ as reviewer-response.json and
# reconciliation-result.json, keyed by the review_id that links them. These files
# contain artifact content and are sensitive — 0600 in a 0700 dir, and never committed
# (see .gitignore). `forget_run` deletes one.

def _safe_id(run_id: str) -> str:
    return (re.sub(r"[^A-Za-z0-9._-]", "_", run_id or "")[:120]) or "unknown"


def runs_dir() -> str:
    return os.path.join(config_dir(), "runs")


def save_run_doc(run_id: str, name: str, doc: dict) -> str:
    """Persist one run document (name = 'reviewer-response' | 'reconciliation-result')."""
    d = os.path.join(runs_dir(), _safe_id(run_id))
    os.makedirs(d, exist_ok=True)
    for path in (runs_dir(), d):
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    target = os.path.join(d, f"{name}.json")
    fd, tmp = tempfile.mkstemp(dir=d, prefix=f".{name}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(doc, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return target


def list_runs() -> list:
    d = runs_dir()
    if not os.path.isdir(d):
        return []
    out = []
    for name in os.listdir(d):
        rd = os.path.join(d, name)
        if not os.path.isdir(rd):
            continue
        out.append({
            "run_id": name,
            "has_review": os.path.isfile(os.path.join(rd, "reviewer-response.json")),
            "has_reconciliation": os.path.isfile(os.path.join(rd, "reconciliation-result.json")),
            "mtime": os.path.getmtime(rd),
        })
    return sorted(out, key=lambda r: r["mtime"], reverse=True)


def load_run(run_id: str) -> dict:
    d = os.path.join(runs_dir(), _safe_id(run_id))

    def _load(p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

    return {
        "run_id": _safe_id(run_id),
        "reviewer_response": _load(os.path.join(d, "reviewer-response.json")),
        "reconciliation_result": _load(os.path.join(d, "reconciliation-result.json")),
    }


def forget_run(run_id: str) -> bool:
    d = os.path.join(runs_dir(), _safe_id(run_id))
    if os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)
        return True
    return False
