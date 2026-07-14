"""Shared, stdlib-only helpers for Impasse: config dir, backend resolution, hashing.

No third-party dependencies — this ships with the skill. (Schema validation, which
needs `jsonschema`, is a dev/CI concern under tests/, not a runtime dependency.)

POSIX (macOS/Linux) is the supported runtime; Windows is a documented roadmap.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
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
    return destination_id


def get_backend(name: str = "codex") -> Backend:
    """Return a resolved Backend. v1 supports only 'codex'."""
    if name != "codex":
        raise ValueError(f"unknown backend '{name}' (v1 supports: codex)")
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
    )


def sha256_prefixed(data: bytes) -> str:
    """'sha256:<hex>' — the form used by evidence digests in the schemas."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def artifact_revision(data: bytes) -> dict:
    """The schema's artifact.revision object for the exact bytes reviewed."""
    return {"algorithm": "sha256", "value": hashlib.sha256(data).hexdigest()}
