"""WHAT IT'S FOR: the stdlib-only core Impasse shares across its CLIs — resolves which reviewer
backend to run and where its data goes (consent keying), decides how independent that reviewer is
from the host, and persists run records + settings. No third-party deps; ships with the skill.

The real subsystems living here:
  - config dir + backend resolution — where local state lives, and turning a backend name into a
    runnable command + its normalized data destination (get_backend / resolve_codex_command /
    resolve_claude_command / normalize_destination);
  - host detection + host-relative independence policy — identify the agent DRIVING the protocol
    and grade a reviewer's independence RELATIVE to it (host_detection / independence_tier /
    review_mode / the independence_notice disclosure);
  - the run-record audit trail + a small persisted settings store (reserve_run_id / save_run_doc /
    list_runs / load_run / forget_run; load_settings + the set/get_default_* accessors);
  - content hashing for evidence digests and manifests.

(Schema validation, which needs `jsonschema`, is a dev/CI concern under tests/, not a runtime
dependency.)

POSIX (macOS/Linux) is the supported runtime; Windows is a documented roadmap.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

APP = "impasse"


# --- Skill version -----------------------------------------------------------------------------
#
# WHAT IT'S FOR: answering "which Impasse is running?" WITHOUT the operator having to run anything.
# The version lives in one file (`VERSION` at the skill root) and is surfaced in three places that
# reach a reader for free: the `SKILL.md` header (which every host loads into context when the skill
# is invoked, so the agent simply knows it), every review result, and the local timing store.
#
# The single source of truth matters more than the surfacing. A version string copied into several
# files is a new way to be wrong — the same doc-drift failure the repo's documentation standards
# exist to prevent — so `tests/test_helpers.py` asserts the file, the SKILL.md header line and the
# frontmatter all agree, and a release that forgets one fails the gate rather than shipping a lie.

_VERSION_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "VERSION")
_VERSION_CACHED: dict = {}


def _git_revision() -> str | None:
    """The short commit of the checkout this code is running from, or None if that can't be told.

    WHY: a released version alone is ambiguous for the common dev setup, where a host's skills dir
    SYMLINKS a working clone — "0.5.0" there could be any of a hundred commits, including uncommitted
    edits. Best-effort by construction: no git, not a repo, or a slow/hanging git all degrade to
    None, because a provenance nicety must never break a review. It is bounded rather than free: the
    git calls carry a 2s timeout each and the result is cached for the process, so a pathological
    git can add a one-off delay to the first version lookup — not to every call."""
    root = os.path.dirname(_VERSION_FILE)

    def _git(*args) -> str | None:
        try:
            p = subprocess.run(["git", "-C", root, *args], capture_output=True, timeout=2)
        except (OSError, subprocess.SubprocessError):
            # Deliberately NARROW: git missing (OSError), or a timeout/odd exit
            # (SubprocessError). No bare `except Exception` — an unexpected error here is a real
            # defect and should surface rather than be relabelled "no revision".
            return None
        if p.returncode != 0:
            return None    # not a repo, or git can't read it — a normal outcome for an install
        return (p.stdout or b"").decode("utf-8", "replace").strip()

    # `git -C <dir>` SEARCHES PARENT DIRECTORIES. A skill COPIED into an unrelated checkout — a
    # dotfiles repo, a monorepo — would otherwise inherit that repo's commit and report confident,
    # completely wrong provenance. (`~/.claude` is itself a git repo, so this is the normal case,
    # not a corner one.) Two guards: the repository top level must BE the skill root, and VERSION
    # must actually be tracked in it. Either failing means "this checkout is not this skill".
    top = _git("rev-parse", "--show-toplevel")
    if not top or os.path.realpath(top) != os.path.realpath(root):
        return None
    if _git("ls-files", "--error-unmatch", "VERSION") is None:
        return None

    rev = _git("describe", "--always", "--dirty", "--exclude", "*")
    if not rev:
        return None
    # Shape-check git's output before it can reach a report or the metrics store: this is a
    # subprocess's stdout, and every other externally-sourced string here is bounded and validated
    # rather than trusted.
    return rev if re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.\-]{0,39}", rev) else None


def version(*, with_revision: bool = True) -> str:
    """The skill's version, e.g. '0.5.0' or '0.5.0+g48f2b1e' (or '0.5.0+g48f2b1e-dirty').

    `with_revision=False` returns the bare released version — that is the value the consistency gate
    compares against `SKILL.md`, since the git suffix is environment-specific and must not be baked
    into documentation. Returns 'unknown' if the VERSION file is missing or unreadable: an install
    that cannot identify itself says so rather than guessing or raising."""
    key = "full" if with_revision else "base"
    if key in _VERSION_CACHED:
        return _VERSION_CACHED[key]
    base = "unknown"
    try:
        # Read a bounded amount but MORE than any valid version, so trailing junk is seen and
        # rejected rather than silently truncated away into something that looks valid.
        with open(_VERSION_FILE, encoding="utf-8") as f:
            candidate = f.read(256).strip()
        # Shape-checked, not trusted: this string is interpolated into reports and stored in the
        # metrics store, so bound it the way every other externally-sourced string is bounded.
        # Bounded components too: an unbounded digit run is still "valid semver" by shape but is
        # not a version anyone released, and it ends up interpolated into reports and written to
        # the metrics store.
        if re.fullmatch(r"[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}(?:[-+][0-9A-Za-z.\-]{1,32})?",
                        candidate or ""):
            base = candidate
    except (OSError, UnicodeDecodeError):
        # UnicodeDecodeError as well as OSError: a VERSION file that isn't valid UTF-8 is a
        # malformed file, exactly like an unreadable one, and `version()` is called from `_fail` —
        # the failure path — where raising would replace a real diagnosis with a traceback.
        base = "unknown"
    _VERSION_CACHED["base"] = base
    full = base
    if base != "unknown":
        rev = _git_revision()
        if rev:
            full = f"{base}+{rev}"
    _VERSION_CACHED["full"] = full
    return _VERSION_CACHED[key]


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
    # NOTE: the independence TIER is intentionally NOT a Backend field. It is a relation between the
    # host and this backend's provider, computed per-run from a single host snapshot by the caller
    # (impasse_run.review) via independence_tier(host, provider) — so host/tier/notice can't drift.
    # Backend carries only the provider; the tier is never cached on it (was: a vestigial field that
    # duplicated the computation via a second detect_host() — core-review F011).


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
        # The Codex desktop app rebranded its bundle to ChatGPT.app (observed 2026-07 on
        # codex-cli 0.145.0-alpha.18); keep the legacy Codex.app path too for older installs.
        "/Applications/ChatGPT.app/Contents/Resources/codex",
        os.path.join(home, "Applications/ChatGPT.app/Contents/Resources/codex"),
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
    or None if not found. Used for the `claude` reviewer backend — the same-provider fallback
    for a Claude host, and the cross-provider choice for a Codex host.
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
    """Return a resolved Backend (name, provider, destination, command). The independence TIER is NOT
    a field on the returned object — it is a relation between the host and this backend's provider, so
    the caller computes it per-run via `independence_tier(host, backend.provider)` (F011).

    Independence is host-relative: to a Claude host, 'codex' (default) is the cross-provider reviewer
    and 'claude' the same-provider fallback — a same-provider reviewer shares the host's blind spots,
    so it buys breadth / an adversarial second pass, NOT independence. To a Codex host the ladder
    inverts: 'claude' is the cross-provider choice. See docs/backends/claude.md and the Guardrails.
    """
    if name == "codex":
        cmd = resolve_codex_command()
        if not cmd:
            raise FileNotFoundError(
                "codex CLI not found. Install it (npm i -g @openai/codex, or the Codex "
                "desktop app), or set CODEX_BIN / IMPASSE_CODEX_BIN."
            )
        # A custom base URL (Azure, an enterprise gateway, localhost) changes where data actually
        # goes; normalize it so consent is keyed to the real destination. The codex backend always runs
        # --ignore-user-config so ~/.codex/config.toml can't reroute (F001); OPENAI_BASE_URL is the
        # authoritative destination on a standard install. A system/managed Codex config layer that
        # overrides the endpoint below the env var is outside Impasse's visibility — see the codex-
        # backend routing caveat in docs/security-model.md. An explicitly-empty value -> default (F008).
        endpoint = os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com"
        destination_id = normalize_destination(endpoint)
        provider = _provider_label(destination_id)
        return Backend(
            name="codex", type="codex-cli", provider=provider,
            destination_id=destination_id, endpoint=endpoint, command=cmd,
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
        # A custom ANTHROPIC_BASE_URL (a gateway/proxy) still keys consent to wherever data goes; an
        # explicitly-empty value is treated as the default (matching the pre-flight — F008).
        endpoint = os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com"
        destination_id = normalize_destination(endpoint)
        provider = _provider_label(destination_id)
        return Backend(
            name="claude", type="claude-cli", provider=provider,
            destination_id=destination_id, endpoint=endpoint, command=cmd,
        )
    raise ValueError(f"unknown backend '{name}' (supported: codex, claude)")


# --- Host identity + host-relative independence ---------------------------------------------
#
# Independence is a RELATION between the host's provider and the reviewer's provider, not a
# static property of a backend: to a Claude host, Codex is the cross-provider reviewer; to a
# Codex host, the `claude` backend is. The host declares itself with IMPASSE_HOST
# (authoritative; non-Claude host adapters MUST set it — the runner can only auto-detect Claude
# surfaces). A host that DOESN'T declare itself gets 'undetermined', never a positive
# cross-provider claim: a subprocess cannot identify a driver that won't identify itself, so
# the only fail-safe answer for an unattributed driver is "we don't know".
KNOWN_HOSTS = ("claude", "codex", "gemini", "grok", "composer", "cursor", "other")
# Hosts attributable to a single model provider. cursor/other run an operator-selected
# underlying model (a Cursor agent may BE Claude or GPT), so no provider can be attributed.
#
# `grok` (xAI) is here as a HOST only. It is reachable today exactly one way — an operator asserting
# IMPASSE_HOST=grok because a Grok model is driving their session (typically inside Cursor, whose
# model picker offers the Grok family). There is no Grok *marker* to auto-detect and no Grok
# *backend* to review with; naming the host is what lets a Grok-driven session label a Codex or
# Claude reviewer honestly as cross-provider instead of settling for `undetermined`.
#
# `composer` (Anysphere, the company behind Cursor) is Cursor's own in-house model family, and is
# likewise assertion-only. Naming it is what separates "I am on Cursor's own model" — a different
# lab from OpenAI and Anthropic, so a Codex or Claude reviewer is genuinely cross-provider — from
# "I am on Cursor's Auto router", which is NOT a lab at all and must stay `undetermined`.
# CAVEAT, deliberately recorded: Composer's base-model provenance is not fully public. Anysphere
# describes it as trained in-house, but if it were derived from someone else's base model its blind
# spots would correlate with that base rather than with Anysphere. Treat a Composer-host
# cross_provider tier as sound on organizational separation and less firmly established on training
# correlation than, say, claude-vs-codex. The operator asserted it; the notice says so.
_HOST_PROVIDERS = {"claude": "Anthropic", "codex": "OpenAI", "gemini": "Google", "grok": "xAI",
                   "composer": "Anysphere"}
# Providers that can appear as a REVIEWER BACKEND. Only OpenAI (codex) and Anthropic (claude) ship
# a backend, so Google and xAI are intentionally absent even though both are known HOST providers
# above: a host provider needs no backend to be nameable, and adding a provider no backend reports
# would be dead code today.
#
# THE LIMITATION THIS CREATES, stated exactly: independence_tier compares providers for equality
# FIRST, so a same_provider verdict is always right. But a DIFFERENT provider is only promoted to
# cross_provider when it appears in this tuple — so if a Google or xAI backend were ever added and
# this tuple were not updated with it, that backend would score `undetermined` against every host
# except its own. That is fail-safe (it understates independence, never overstates it), but it
# would be wrong. Adding a backend MUST add its provider here; the test suite pins the current
# reachable behavior, not the hypothetical.
_KNOWN_PROVIDERS = ("Anthropic", "OpenAI")


# Host markers are matched by STRICT VALUE, not mere presence: env vars are unauthenticated
# inherited strings, so an inherited GEMINI_CLI=0 or a stray CODEX_SANDBOX=off must NOT count as a
# host. Codex ships no branded "I am Codex" flag — only sandbox-state vars that signal "running
# inside Codex's sandbox" and are ABSENT under --dangerously-bypass-approvals-and-sandbox — so codex
# is a best-effort HEURISTIC (its absence is a safe false-negative), never a strong identity contract.
_FALSY_MARKER = frozenset({"", "0", "false", "off", "no"})


def _affirmatively_set(var: str) -> bool:
    """A presence-style marker counts only when AFFIRMATIVELY set — a stray inherited '0'/'off'/''
    must not read as present (strict-value rule; env vars are unauthenticated inherited strings)."""
    v = os.environ.get(var)
    return v is not None and v.strip().lower() not in _FALSY_MARKER


_TRUEISH_MARKER = frozenset({"1", "true", "yes", "on"})


def _boolish_true(var: str) -> bool:
    """A strict boolean-true check (allowlist), for a security-sensitive gate where accepting an
    arbitrary non-falsy value would be unsafe — e.g. authorizing self-review. Stricter than
    _affirmatively_set, which is a denylist that would accept 'garbage' (core-review F003)."""
    v = os.environ.get(var)
    return v is not None and v.strip().lower() in _TRUEISH_MARKER


def _claude_confidence() -> str | None:
    # Returns the confidence of a Claude-host detection, or None if no marker. STRONG only from the
    # strict, empirically-confirmed primary (CLAUDECODE=="1") or the CLAUDE_SURFACE allowlist. The
    # remaining surface flags are presence-style — an arbitrary inherited value ("garbage") satisfies
    # them, so they must NOT mint a STRONG cross-provider claim (integration-review F001): a stray
    # CLAUDE_CODE_ENTRYPOINT on an actual Codex host would otherwise yield a SILENT false cross_provider.
    # They contribute HEURISTIC instead, so any resulting positive tier carries the soft notice.
    if os.environ.get("CLAUDECODE") == "1" or os.environ.get("CLAUDE_SURFACE") in ("cowork", "chat", "sandbox"):
        return "strong"
    if (_affirmatively_set("CLAUDE_CODE_ENTRYPOINT") or _affirmatively_set("CLAUDE_COWORK")
            or _affirmatively_set("CLAUDE_CHAT_SANDBOX")):
        return "heuristic"
    return None


def _attributable_hosts() -> dict:
    """Provider-attributable hosts whose marker is present, mapped to detection CONFIDENCE
    ('strong'|'heuristic') — a subset of {claude, codex, gemini}. gemini is strict→strong; codex is a
    sandbox-state HEURISTIC (no branded flag); claude is strong from its strict primary, heuristic from
    a presence-style surface flag. Cursor is excluded: it wraps an operator-chosen model, no provider."""
    hosts = {}
    cc = _claude_confidence()
    if cc:
        hosts["claude"] = cc
    if os.environ.get("GEMINI_CLI") == "1":
        hosts["gemini"] = "strong"
    if (os.environ.get("CODEX_SANDBOX") == "seatbelt"
            or os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED") == "1"):
        hosts["codex"] = "heuristic"
    return hosts


def host_detection() -> dict:
    """Identify the agent DRIVING the protocol, WITH detection provenance.

    Returns {"host", "method", "confidence"} where host ∈ KNOWN_HOSTS ∪ {"unknown"},
    method ∈ {"override", "auto", "none"}, confidence ∈ {"asserted", "strong", "heuristic", "none"}.
    Fail-safe by construction: marker ambiguity (≥2 attributable, or one attributable + Cursor), an
    invalid override, or an override that disagrees with an observed *attributable* marker ALL resolve
    to "unknown" — never a guessed positive host, and confidence "none" never rides a positive tier.
    (An `IMPASSE_HOST` override is honored alongside a bare `CURSOR_AGENT`: Cursor is non-attributable
    — it names no provider — so it cannot contradict the override's provider claim, and the override
    is exactly the operator's tool for resolving the Cursor-ambiguity auto-mode refuses to guess.
    Operator decision 2026-07-20.)

    Keys off GENUINE host markers only — deliberately NOT detect_environment(), whose IMPASSE_ENV
    override is a surface-policy knob and must not be able to manufacture a host identity."""
    conf = _attributable_hosts()                    # {host: confidence} ⊆ {claude, codex, gemini}
    A = set(conf)
    cursor = os.environ.get("CURSOR_AGENT") == "1"

    # 1. IMPASSE_HOST override — authoritative, but validated and conflict-checked.
    forced = os.environ.get("IMPASSE_HOST")
    if forced:                                      # nonempty; absent/"" falls through to markers
        if forced not in KNOWN_HOSTS:
            # A present-but-invalid override is evidence of operator misconfiguration; refuse rather
            # than silently continue and let a weaker inherited marker manufacture a positive tier.
            return {"host": "unknown", "method": "override", "confidence": "none"}
        if A and A != {forced}:
            # Override names one host but an observed attributable marker names another — disagree.
            return {"host": "unknown", "method": "override", "confidence": "none"}
        return {"host": forced, "method": "override", "confidence": "asserted"}

    # 2. No override — resolve from markers, failing safe on any ambiguity.
    if len(A) >= 2:
        return {"host": "unknown", "method": "auto", "confidence": "none"}
    if len(A) == 1:
        if cursor:
            # One attributable marker AND Cursor coexist: an unordered inherited env set carries no
            # nesting depth, so we cannot tell which agent is the inner driver. Refuse to guess.
            return {"host": "unknown", "method": "auto", "confidence": "none"}
        host = next(iter(A))
        return {"host": host, "method": "auto", "confidence": conf[host]}
    if cursor:
        return {"host": "cursor", "method": "auto", "confidence": "none"}
    return {"host": "unknown", "method": "auto", "confidence": "none"}


def detect_host() -> str:
    """Best-effort identity of the agent DRIVING the protocol (host string only; see host_detection()
    for provenance). `IMPASSE_HOST` overrides (authoritative, but a nonempty unrecognized value or one
    that disagrees with an observed marker yields 'unknown', not a fallthrough). Anything unresolved
    is 'unknown'."""
    return host_detection()["host"]


def independence_tier(host: str, backend_provider: str) -> str:
    """The independence tier of a reviewer backend RELATIVE to a host.

    - Host with a known provider: compare providers — same_provider on a match,
      cross_provider only when the backend's provider is a KNOWN different vendor; a custom
      endpoint/gateway (unattributable provider label) is 'undetermined', never overstated.
    - Everything else is 'undetermined': cursor/other hosts (operator-chosen underlying model)
      and an unknown/undeclared host alike. A positive independence claim requires BOTH sides
      to be attributable — a human at the CLI can export IMPASSE_HOST if the driver is known.
    """
    hp = _HOST_PROVIDERS.get(host)
    if hp:
        if backend_provider == hp:
            return "same_provider"
        if backend_provider in _KNOWN_PROVIDERS:
            return "cross_provider"
    return "undetermined"


# Per-host provenance caveats that must ride on a POSITIVE tier, in the notice the operator actually
# reads — not only in a source comment or a doc page. A tier is a claim made in CODE; qualifying it
# in prose elsewhere leaves the executable result unqualified, which is how a hedged judgement turns
# into an unhedged one by the time it reaches a person.
_HOST_TIER_CAVEATS = {
    "composer": (
        " Provenance caveat: '{host}' is Anysphere's own model, so this label rests on ORGANIZATIONAL "
        "separation from {provider}. Composer's base-model provenance is not fully public — if it "
        "derives from another lab's base model, its blind spots may correlate with that lab rather "
        "than with Anysphere. Weigh agreement accordingly; this is a weaker basis than a "
        "claude-vs-codex pairing."
    ),
}


def independence_notice(tier: str, host: str, backend_name: str, provider: str,
                        confidence: str | None = None) -> str | None:
    """The mandatory disclosure for a tier, or None when none is owed. ONE formatter shared by
    review() and review_mode(), so no surface that reports a downgraded — or heuristically-detected —
    tier can forget the notice that must ride with it.

    `confidence` is the host_detection() confidence. A cross_provider tier owes no *downgrade* notice,
    but when it rests on anything WEAKER THAN A DETECTED IDENTITY it carries a SOFT notice, so a
    positive claim that was guessed or asserted can't read as a confirmed one:

    - "heuristic" — inferred (today: codex via sandbox-state vars, with no branded flag).
    - "asserted"  — the OPERATOR set IMPASSE_HOST. Impasse verified nothing; it took their word.
      This is the only way a Cursor session (whose host model is operator-chosen and unattributable)
      can reach a positive tier at all, so the claim is real but only as good as the assertion —
      and it goes stale silently if they switch the session's model afterwards.

    Success for an asserted tier is NOT "notice is null"; it is "the tier is honest AND the operator
    can see it was asserted." A host that prints only this field would otherwise show nothing."""
    if tier == "cross_provider":
        # A host-specific caveat makes the notice MANDATORY even on a basis that would otherwise owe
        # nothing, so an acknowledged uncertainty can never be silently dropped from the result.
        caveat = _HOST_TIER_CAVEATS.get(host, "").format(host=host, provider=provider)
        if confidence == "asserted":
            return (
                f"⚠ Cross-provider label rests on an ASSERTION, not a detection: the '{host}' host "
                f"was set via IMPASSE_HOST (reviewer '{backend_name}' via {provider}). Impasse did "
                "not verify it — if the model actually driving this session is not " + host + ", "
                "this label is wrong, and it does not update if you switch models mid-session. "
                "Re-check it when the session's model changes." + caveat
            )
        if confidence == "heuristic":
            return (
                f"⚠ Cross-provider label INFERRED from a heuristic: the '{host}' host was detected "
                f"from a sandbox-state condition, not a branded identity flag (reviewer '{backend_name}' "
                f"via {provider}). The independence is likely real but not firmly established — a "
                "sandbox-bypassed run would be undetectable. Setting IMPASSE_HOST=" + host + " replaces "
                "this inference with YOUR assertion (recorded as such, and still disclosed) — it "
                "removes the guess, not the need to state a basis." + caveat
            )
        if caveat:
            # Reached only if a caveated host were ever DETECTED rather than asserted. The caveat
            # still owes disclosure, so emit it standalone rather than returning None.
            return (f"⚠ Cross-provider label for host '{host}' (reviewer '{backend_name}' via "
                    f"{provider}).".rstrip() + caveat)
        return None
    if tier == "same_provider":
        return (
            f"⚠ Same-provider review via {provider} (backend '{backend_name}', host '{host}'): "
            "the reviewer shares the host's provider — and so its training and blind spots — so "
            "this is an adversarial second pass / breadth, NOT cross-provider independence. "
            "Agreement is weak evidence. Prefer a different-provider backend when available."
        )
    if tier == "undetermined":
        return (
            f"⚠ Independence undetermined (backend '{backend_name}' via {provider}, host "
            f"'{host}'): the host's underlying model or the backend's real destination can't be "
            "attributed to a single provider, so the reviewer may share the host's provider. "
            "Treat agreement cautiously; if the host is actually a single-provider agent, set "
            "IMPASSE_HOST."
        )
    return None


# --- Environment-aware review-mode policy ---------------------------------------------------
#
# Independence tiers, strongest to weakest:
#   cross_provider — a reviewer from a DIFFERENT provider than the host. Real independence.
#   undetermined   — provider correlation can't be established (mixed-model host, or a
#                    custom endpoint whose provider can't be attributed). Trust accordingly.
#   same_provider  — a reviewer sharing the host's provider, in a FRESH process. Breadth;
#                    shared blind spots.
#   self_review    — the HOST model reviews in its OWN context (no separate reviewer can run).
#                    Near-zero independence. Permitted ONLY where no subprocess reviewer exists —
#                    the claude.ai chat sandbox or Claude Cowork — and NEVER for code.
INDEPENDENCE_TIERS = ("cross_provider", "undetermined", "same_provider", "self_review")

# Surfaces that cannot spawn a reviewer subprocess, so self-review is the only fallback.
SELF_REVIEW_ENVIRONMENTS = ("chat_sandbox", "cowork")

_ENV_LABELS = {
    "claude_code": "Claude Code",
    "chat_sandbox": "the Claude chat sandbox",
    "cowork": "Claude Cowork",
    "unknown": "an unknown environment",
}

CLAUDE_CODE_RECOMMENDATION = (
    "Claude Code is the best environment for Impasse: it runs a reviewer subprocess in a real "
    "shell, so a Claude host gets a cross-provider reviewer (Codex) there. Weaker Claude "
    "surfaces can self-review at best."
)

# For non-Claude hosts the Claude Code pitch is wrong (their own shell already runs a reviewer
# subprocess, and to them `claude -p` is the cross-provider choice) — recommend the capability,
# not the surface.
SUBPROCESS_RECOMMENDATION = (
    "Run Impasse where a reviewer subprocess (codex exec / claude -p) can execute — any real "
    "shell. Independence is computed relative to the host; see docs/environments.md."
)


def detect_environment() -> str:
    """Best-effort detection of the runtime surface. `IMPASSE_ENV` overrides (authoritative).
    Returns 'claude_code' | 'chat_sandbox' | 'cowork' | 'unknown'. Auto-detection keys off
    documented env markers; when unsure it returns 'unknown', which does NOT permit self-review
    (fail safe — never silently degrade to self-review when we can't confirm the sandbox)."""
    forced = os.environ.get("IMPASSE_ENV")
    if forced in _ENV_LABELS:
        return forced
    # The cowork/chat_sandbox surfaces are the ONLY ones that permit self-review, so their markers use
    # a STRICT boolean-true allowlist (_boolish_true): an arbitrary CLAUDE_COWORK=garbage must not
    # authorize self-review (core-review F007/F003) — the fail-safe is 'unknown' (no self-review).
    # CLAUDE_CODE_ENTRYPOINT gates claude_code (which does NOT permit self-review), so its value marker
    # stays affirmative-nonfalsy.
    if os.environ.get("CLAUDECODE") == "1" or _affirmatively_set("CLAUDE_CODE_ENTRYPOINT"):
        return "claude_code"
    if _boolish_true("CLAUDE_COWORK") or os.environ.get("CLAUDE_SURFACE") == "cowork":
        return "cowork"
    if _boolish_true("CLAUDE_CHAT_SANDBOX") or os.environ.get("CLAUDE_SURFACE") in ("chat", "sandbox"):
        return "chat_sandbox"
    return "unknown"


def self_review_allowed(environment: str) -> bool:
    """Self-review is permitted ONLY on surfaces that can't run a reviewer subprocess — the chat
    sandbox or Cowork. Never in Claude Code (run a real backend), never in an unknown env."""
    return environment in SELF_REVIEW_ENVIRONMENTS


def self_review_notice(environment: str) -> str:
    env = _ENV_LABELS.get(environment, environment)
    return (
        f"⚠ SELF-REVIEW ({env}): no separate reviewer can run here, so the SAME assistant helping "
        "you is checking its own work in its own context. This is NOT an independent second opinion "
        "— it shares that assistant's blind spots and prior reasoning, so agreement is almost no "
        "evidence. It can still catch arithmetic slips, unsupported claims, and internal "
        f"contradictions. {CLAUDE_CODE_RECOMMENDATION}"
    )


def _configured_provider(env_var: str, default: str) -> str | None:
    """The provider label of a backend's CONFIGURED destination (its base-URL env var), for the
    review_mode pre-flight — or None when the endpoint doesn't normalize (malformed, embedded
    credentials): get_backend() would refuse it, so the pre-flight must not offer it. The raw
    value is never echoed — a malformed endpoint is exactly where credentials live. A VALID but
    unattributable endpoint (a custom gateway) still returns its label; the tier degrades to
    'undetermined' rather than overstating."""
    endpoint = os.environ.get(env_var) or default
    try:
        return _provider_label(normalize_destination(endpoint))
    except ValueError:
        return None


def review_mode(kind: str, *, environment: str | None = None, codex_available: bool = False,
                claude_available: bool = False, host: str | None = None,
                detection: dict | None = None) -> dict:
    """The single policy entry point: pick the strongest HONEST review mode for this environment,
    the available backends, and the host, and carry the mandatory disclosure. Capability-first,
    env-gated, host-relative.

    Returns {mode, tier, allowed, notice, recommendation, reason, host}, where
    mode ∈ {'codex','claude','self_review','refuse'}:
      - among available subprocess backends, prefer the one most INDEPENDENT of the host's
        provider (cross_provider > undetermined > same_provider; ties keep codex first — its
        hermetic, OS-sandboxed invocation is the stronger runtime posture), on ANY surface.
        Tiers are computed against each backend's CONFIGURED endpoint (a custom gateway is
        'undetermined', mirroring the actual run), and a backend get_backend() would refuse
        (claude under Bedrock/Vertex routing) is never recommended. A downgraded tier carries
        its independence_notice here too — this pre-flight is its own disclosure surface;
      - if none resolves: self-review is allowed ONLY in the chat sandbox or Cowork, and NEVER for
        code (its verification needs to run tests, impossible there); otherwise refuse.
    """
    kind = (kind or "").strip().lower()   # normalize so 'Code'/'CODE' can't slip past the code gate
    env = environment or detect_environment()
    # Provenance precedence: an explicit `detection` snapshot (from a caller that already ran
    # host_detection — review()'s `auto` path) is used VERBATIM, so its method/confidence (e.g. a
    # heuristic Codex host) survive rather than being laundered to 'asserted' (integration-review F003).
    # Else an explicit --host arg is operator-asserted; else auto-detect. An explicitly-passed "unknown"
    # is honored authoritatively (NOT re-detected) so selection matches the host the caller reports.
    if detection is not None:
        hd = detection
    elif host in KNOWN_HOSTS:
        hd = {"host": host, "method": "override", "confidence": "asserted"}
    elif host == "unknown":
        hd = {"host": "unknown", "method": "auto", "confidence": "none"}
    else:
        hd = host_detection()
    hst = hd["host"]
    hdblock = {"method": hd["method"], "confidence": hd["confidence"]}
    # The Claude Code pitch is only apt for a Claude(-ish) host; other hosts get the
    # capability-framed recommendation (their own shell already qualifies).
    surface_rec = CLAUDE_CODE_RECOMMENDATION if hst in ("claude", "unknown") else SUBPROCESS_RECOMMENDATION
    rec = None if env == "claude_code" else surface_rec
    # get_backend() refuses claude under Bedrock/Vertex routing (consent would be mis-keyed) —
    # a pre-flight must not recommend a backend the run is guaranteed to refuse.
    claude_refused = bool(os.environ.get("CLAUDE_CODE_USE_BEDROCK") or os.environ.get("CLAUDE_CODE_USE_VERTEX"))
    codex_provider = _configured_provider("OPENAI_BASE_URL", "https://api.openai.com")
    claude_provider = _configured_provider("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    backends = (
        # provider None = the configured endpoint won't normalize, so get_backend() would refuse
        # this backend — a pre-flight must not offer what the run is guaranteed to reject.
        ("codex", codex_provider, codex_available and codex_provider is not None),
        ("claude", claude_provider,
         claude_available and not claude_refused and claude_provider is not None),
    )
    candidates = [(n, p, independence_tier(hst, p)) for n, p, avail in backends if avail]
    rank = {"cross_provider": 0, "undetermined": 1, "same_provider": 2}
    candidates.sort(key=lambda c: rank[c[2]])   # stable: codex stays first on a tie
    if candidates:
        name, provider, tier = candidates[0]
        return {"mode": name, "tier": tier, "allowed": True,
                "notice": independence_notice(tier, hst, name, provider, hd["confidence"]),
                "recommendation": rec, "host": hst, "host_detection": hdblock,
                "reason": f"strongest available reviewer relative to the {hst} host: "
                          f"{name} ({tier})"}
    # No subprocess reviewer available on this surface.
    if kind == "code":
        return {"mode": "refuse", "tier": None, "allowed": False, "notice": None,
                "recommendation": surface_rec, "host": hst, "host_detection": hdblock,
                "reason": "code review needs a runnable reviewer and executable verification, "
                          "which requires a surface that can run one"}
    if self_review_allowed(env):
        return {"mode": "self_review", "tier": "self_review", "allowed": True,
                "notice": self_review_notice(env), "recommendation": CLAUDE_CODE_RECOMMENDATION,
                "host": hst, "host_detection": hdblock,
                "reason": f"no reviewer subprocess in {_ENV_LABELS.get(env, env)}; self-review permitted"}
    return {"mode": "refuse", "tier": None, "allowed": False, "notice": None,
            "recommendation": surface_rec, "host": hst, "host_detection": hdblock,
            "reason": f"no reviewer subprocess and self-review not permitted in {_ENV_LABELS.get(env, env)}"}


def sha256_prefixed(data: bytes) -> str:
    """'sha256:<hex>' — the form used by evidence digests in the schemas."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def artifact_revision(data: bytes) -> dict:
    """The schema's artifact.revision object for the exact bytes reviewed."""
    return {"algorithm": "sha256", "value": hashlib.sha256(data).hexdigest()}


def revision_from_digest(digest) -> dict | None:
    """Turn a consent manifest's `digest` ("sha256:<hex>") into the schema's {algorithm, value}.

    WHAT IT'S FOR: the reconciliation needs the reviewed bytes' identity, and the manifest is where
    a host can see it — but the manifest stores ONE string while the schema wants two fields, and
    hosts were observed guessing at key names and then recomputing the hash from a temp file rather
    than finding it. This is the one supported way to cross that gap; `review()` also puts the
    finished object on its result as `artifact_revision`, which is easier still.

    Returns None for anything that isn't a recognizable "<algorithm>:<hex>" pair, so a malformed
    manifest degrades to "no revision" instead of minting a false identity for reviewed content."""
    if not isinstance(digest, str) or ":" not in digest:
        return None
    algorithm, _, value = digest.partition(":")
    # Length is checked PER ALGORITHM. A shared 7-128 range would accept `sha256:aaaaaaa` as a
    # SHA-256 digest and a 7-character git abbreviation as an object id — neither is the immutable,
    # collision-resistant identity this field is documented to be, and an abbreviation is exactly
    # the kind of value that silently stops distinguishing two revisions.
    expected = {"sha256": (64,), "git": (40, 64)}.get(algorithm)   # git: SHA-1 or SHA-256 object id
    if not expected or len(value) not in expected or not re.fullmatch(r"[0-9a-f]+", value):
        return None
    return {"algorithm": algorithm, "value": value}


# --- Run records (the audit trail) -------------------------------------------------
# A run is persisted under config_dir()/runs/<run_id>/ as reviewer-response.json and
# reconciliation-result.json, keyed by the review_id that links them. These files
# contain artifact content and are sensitive — 0600 in a 0700 dir, and never committed
# (see .gitignore). `forget_run` deletes one.

def _safe_id(run_id) -> str:
    """Map a possibly-UNTRUSTED run/review id (the reviewer supplies review_id) to a single safe
    directory name. Coerce to str (a non-string id must not crash), strip to a conservative charset,
    collapse ''/'.'/'..'-style all-dot names to 'unknown' (else '..' traverses out of runs_dir), and
    when sanitization or truncation CHANGED the id, append a hash of the original so distinct hostile
    ids can't collide onto the same record directory."""
    orig = "" if run_id is None else str(run_id)
    s = re.sub(r"[^A-Za-z0-9._-]", "_", orig)[:120]
    if s.strip(".") == "":
        return "unknown"
    if s != orig:   # lossy transform -> disambiguate to keep the mapping injective
        s = f"{s[:104]}-{hashlib.sha256(orig.encode('utf-8', 'replace')).hexdigest()[:12]}"
    return s


def runs_dir() -> str:
    return os.path.join(config_dir(), "runs")


def _run_dir(run_id: str) -> str:
    """The record directory for a run, guaranteed to be a direct child of runs_dir() (defense in
    depth on top of _safe_id: reject anything that isn't a single contained component)."""
    base = runs_dir()
    d = os.path.join(base, _safe_id(run_id))
    if os.path.dirname(os.path.normpath(d)) != os.path.normpath(base):
        raise ValueError("unsafe run id")
    return d


# Codex reasoning-effort allowlist ('minimal' is rejected by codex). Lives here so both the
# runner (per-run validation) and the settings store (set-effort validation) share one source.
ALLOWED_EFFORT = ("none", "low", "medium", "high", "xhigh")

# Codex execution-speed / service-tier allowlist. "fast" == Fast mode ON (a higher serving tier at
# higher credit cost); "standard" is the default (Fast mode OFF). Independent of reasoning effort.
# Same one-source discipline as ALLOWED_EFFORT: shared by the runner and the set-speed settings store.
ALLOWED_SPEED = ("standard", "fast")   # codex service tier; "fast" == Fast mode ON. Default OFF.


# --- Persisted settings (a small config store, e.g. the operator's default reviewer model) ------

def _settings_path() -> str:
    return os.path.join(config_dir(), "settings.json")


_MAX_STORE_BYTES = 4_000_000   # cap on a persisted JSON store read into memory


def load_settings() -> dict:
    try:
        with open(_settings_path(), encoding="utf-8") as f:
            d = json.loads(f.read(_MAX_STORE_BYTES))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):   # ValueError covers json.JSONDecodeError and UnicodeDecodeError
        return {}


def _get_default_setting(key: str, backend: str) -> str | None:
    """A persisted per-backend default (settings.json {key: {backend: value}}), or None. Tolerant
    of a malformed settings file: a non-mapping entry (or non-string value) yields None, never a
    crash — review() calls this on the hot path and must not fail because settings.json is bad."""
    dm = load_settings().get(key)
    if not isinstance(dm, dict):
        return None
    m = dm.get(backend)
    return m if isinstance(m, str) and m else None


def get_default_model(backend: str) -> str | None:
    """The persisted default reviewer model for a backend, or None. Lower precedence than a
    per-run --model and than IMPASSE_{CODEX,CLAUDE}_MODEL — see impasse_run.review()."""
    return _get_default_setting("default_model", backend)


def get_default_effort(backend: str) -> str | None:
    """The persisted default reasoning effort for a backend, or None. Lower precedence than a
    per-run --effort and than IMPASSE_{CODEX,CLAUDE}_EFFORT — see impasse_run.review(). A
    hand-edited value outside ALLOWED_EFFORT is dropped here (fail safe on the read path);
    set_default_effort refuses to write one."""
    e = _get_default_setting("default_effort", backend)
    return e if e in ALLOWED_EFFORT else None


def get_default_speed(backend: str) -> str | None:
    """The persisted default execution speed (service tier) for a backend, or None. Lower precedence
    than a per-run --speed and than IMPASSE_CODEX_SPEED — see impasse_run.review(). A hand-edited
    value outside ALLOWED_SPEED is dropped here (fail safe on the read path); set_default_speed
    refuses to write one."""
    s = _get_default_setting("default_speed", backend)
    return s if s in ALLOWED_SPEED else None


# Lock paths this interpreter currently holds — see the re-entrancy note in _interprocess_lock.
_HELD_LOCKS: set = set()


def _interprocess_lock(lock_name: str):
    """An exclusive interprocess lock file in the config dir, so two hosts (e.g. a Claude Code and a
    Codex host sharing one config dir) can't lose an update via interleaved read-modify-replace
    (core-review F005). POSIX flock (like the process-group teardown, POSIX-only); a no-op context on
    platforms without fcntl. Returns a context manager."""
    ensure_config_dir()
    lock_path = os.path.join(config_dir(), lock_name)
    try:
        import fcntl
    except ImportError:
        import contextlib
        return contextlib.nullcontext()

    # REENTRANT WITHIN THIS PROCESS. flock is per open-file-description, so taking the same lock
    # twice in one process on two fds blocks forever — and these locks legitimately nest now:
    # forget_run() takes the per-run lock, and a caller already holding it (the reconciliation
    # writer) can reach forget_run through a cleanup path. A self-deadlock in a records tool is
    # worse than the race it prevents, so a re-entry is a no-op context while the outermost holder
    # keeps the real lock for the whole nested duration. Cross-PROCESS exclusion is unchanged: this
    # set is per-interpreter.
    if lock_path in _HELD_LOCKS:
        import contextlib
        return contextlib.nullcontext()

    class _Lock:
        def __enter__(self):
            self._fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(self._fd, fcntl.LOCK_EX)
            _HELD_LOCKS.add(lock_path)
            return self

        def __exit__(self, *exc):
            _HELD_LOCKS.discard(lock_path)
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
    return _Lock()


def _settings_lock():
    """The interprocess lock guarding the settings read-modify-write (see _interprocess_lock)."""
    return _interprocess_lock("settings.lock")


def _set_default_setting(key: str, backend: str, value: str | None) -> None:
    """Persist (value set) or clear (value None) a per-backend default under `key`.
    Atomic + fsynced write, 0600 — same discipline as the run-record store. A malformed existing
    entry is repaired (rebuilt) rather than crashing. The whole read-modify-replace runs under an
    interprocess lock (F005) so concurrent writers can't lose an update."""
    path = _settings_path()
    with _settings_lock():
        s = load_settings()
        dm = s.get(key)
        dm = dict(dm) if isinstance(dm, dict) else {}
        if value:
            dm[backend] = value
        else:
            dm.pop(backend, None)
        s[key] = dm
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".settings-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(s, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
            fsync_dir(os.path.dirname(path))
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass


def set_default_model(backend: str, model: str | None) -> None:
    """Persist (model set) or clear (model None) the default reviewer model for a backend."""
    _set_default_setting("default_model", backend, model)


def set_default_effort(backend: str, effort: str | None) -> None:
    """Persist (effort set) or clear (effort None) the default reasoning effort for a backend.
    Refuses a value outside ALLOWED_EFFORT — never persist something the runner would reject.
    Only codex HAS an effort knob, so a non-null write for any other backend is refused at the library
    level too (not just the CLI — F012); clearing (effort=None) is allowed for any backend so a legacy
    persisted value can always be removed (migration path)."""
    if effort is not None and effort not in ALLOWED_EFFORT:
        raise ValueError(f"effort must be one of {sorted(ALLOWED_EFFORT)}")
    if effort is not None and backend != "codex":
        raise ValueError(f"only the codex backend has a reasoning-effort knob (got backend={backend!r})")
    _set_default_setting("default_effort", backend, effort)


def set_default_speed(backend: str, speed: str | None) -> None:
    """Persist (speed set) or clear (speed None) the default execution speed / service tier for a
    backend. Refuses a value outside ALLOWED_SPEED — never persist something the runner would reject.
    Only codex HAS a service-tier/Fast-mode knob, so a non-null write for any other backend is refused
    at the library level too (not just the CLI); clearing (speed=None) is allowed for any backend so a
    legacy persisted value can always be removed (migration path)."""
    if speed is not None and speed not in ALLOWED_SPEED:
        raise ValueError(f"speed must be one of {sorted(ALLOWED_SPEED)}")
    if speed is not None and backend != "codex":
        raise ValueError(f"only the codex backend has a service-tier/Fast-mode knob (got backend={backend!r})")
    _set_default_setting("default_speed", backend, speed)


def fsync_dir(path: str) -> None:
    """fsync a directory so a preceding os.replace into it is durable across a crash. Best-effort:
    not every platform/filesystem supports it (Windows raises), so failures are swallowed."""
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def reserve_run_id(review_id: str) -> str:
    """Atomically reserve a UNIQUE run directory for a new run and return the run_id to use for it.
    The reviewer sets `review_id` (untrusted, NOT guaranteed unique), and it keys the record dir — so
    without this, a reviewer that reuses an id, or two concurrent runs (e.g. a Claude host and a Codex
    host sharing one config dir), would SILENTLY overwrite each other's record (core-review F004). We
    create the dir with exclusive `os.mkdir` and, on collision, append -2, -3, … until a fresh slot is
    claimed. Race-safe: mkdir is atomic, so two processes can't both claim the same suffix."""
    base = _safe_id(review_id)
    os.makedirs(runs_dir(), exist_ok=True)
    for i in range(1, 10000):
        candidate = base if i == 1 else f"{base}-{i}"
        d = _run_dir(candidate)
        try:
            os.mkdir(d, 0o700)
            return candidate
        except FileExistsError:
            continue
    raise OSError(f"could not reserve a unique run directory for {base!r}")


def save_run_meta(run_id: str) -> str | None:
    """Stamp the run directory with WHICH IMPASSE produced it.

    A sibling file rather than a field: `reviewer-response` and `reconciliation-result` are both
    `additionalProperties: false`, so host provenance cannot live inside them without a schema
    change — and it does not belong there anyway (the reviewer does not produce it). Records are
    kept and compared for months, so "which code wrote this" is exactly the question a stored record
    should be able to answer about itself. Best-effort: never raises, because failing to record
    provenance must not fail a review that otherwise succeeded."""
    try:
        d = _run_dir(run_id)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "run-meta.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"impasse_version": version(), "recorded_at": time.time()}, f, indent=2)
        os.chmod(path, 0o600)
        return path
    except (OSError, ValueError, TypeError):
        return None


# A private sentinel, not a boolean: a caller cannot pass it by accident, and it cannot be reached
# from JSON, a CLI flag, or reviewer output. Only save_reconciliation_doc() holds it.
_RECONCILIATION_TOKEN = object()


def save_run_doc(run_id: str, name: str, doc: dict, *, _sanctioned=None) -> str:
    """Persist one run document (name = 'reviewer-response' | 'reconciliation-result').
    The initial reviewer-response should use a run_id from reserve_run_id() so it can't clobber another
    run; reconciliation reuses that same run_id to land in the same directory.

    This is a general, UNVALIDATED write primitive — it writes exactly what it is given, including
    into a run directory that does not yet exist (`makedirs(exist_ok=True)`). That is correct for a
    reviewer-response (the runner creates the directory via reserve_run_id() first). It is NOT the
    sanctioned way to write a reconciliation-result: calling it directly for one can silently create
    an orphan run directory holding a reconciliation with no reviewer-response beside it (issue #17).
    Use save_reconciliation_doc() for that — it validates the pair, enforces coverage/overwrite
    policy, and backs up before replacing, THEN calls this.

    That instruction used to be advice, and advice is not an invariant: a caller could still write a
    reconciliation here and re-create the exact orphan of issue #17, outside the per-run lock. So it
    is now ENFORCED — a `reconciliation-result` write raises unless it comes from
    save_reconciliation_doc(), which passes the private token. An invariant guarded in one command is
    guarded for one caller; guarded in the storage primitive it is guarded for all of them."""
    if name == "reconciliation-result" and _sanctioned is not _RECONCILIATION_TOKEN:
        raise ValueError(
            "save_run_doc cannot write a reconciliation-result: that would bypass pair validation, "
            "the overwrite/backup policy and the per-run lock (issues #17/#18). "
            "Use save_reconciliation_doc(doc, partial=..., force=...) instead."
        )
    d = _run_dir(run_id)
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
        fsync_dir(d)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return target


# --- Reconciliation integrity (issues #16/#17/#18) -----------------------------------------------
#
# WHAT IT'S FOR: a reconciliation-result on disk is only meaningful paired with the reviewer-response
# it claims to reconcile — the findings it disposes of have to actually have been raised, once each,
# by that review. Before this, nothing checked that pairing at the point a reconciliation was written
# (save_run_doc validates nothing), so a mistyped review_id created an ORPHAN run directory holding a
# reconciliation with no sibling findings, a fabricated finding_id was accepted and later rendered as
# a real resolved finding, and a report over such a record fell back to counting the host's own
# dispositions as if they were the reviewer's raised count — a broken record that reads as a passed
# gate. reconciliation_problems() is the one check every write AND every read-side report shares, so
# an orphan can't slip past the CLI, the library, or a stored record's lifetime totals by three
# different routes.

RECOGNIZED_ITEM_STATES = frozenset({"accepted", "rejected", "resolved", "deadlocked", "withdrawn"})
RECOGNIZED_OUTCOMES = frozenset({"converged", "deadlocked", "incomplete", "failed"})

# Shared verbatim with impasse_report._escalation_problems' identical refusal, so an operator sees the
# same words whether the pairing failure surfaces from `save-reconciliation`, `escalations`, or `show`.
MISSING_REVIEWER_RESPONSE_MSG = (
    "reviewer-response not found for review_id {rid!r} — run the FULL protocol so "
    "the findings are recorded, or point at the correct review_id"
)


def reconciliation_items(rec) -> list:
    """The reconciliation's items as a list of DICTS — the only shape every reader assumes.

    WHAT IT'S FOR: readers used to write `rec.get("items") or []` and then `it.get(...)`, which
    raises AttributeError the moment `items` is a string, a dict, or a list holding a non-dict.
    Those files exist (a record is a hand-editable file on disk), and the crash landed in `show` and
    `list` — the very commands you would run to FIND the bad record. Validation reports the problem;
    this makes reading it survivable in the first place, so the report can get far enough to say so.
    TOTAL: never raises. A malformed collection yields [], which the validator separately flags."""
    items = rec.get("items") if isinstance(rec, dict) else None
    if not isinstance(items, list):
        return []
    return [it for it in items if isinstance(it, dict)]


def _safe_repr(v, limit: int = 80) -> str:
    """A bounded repr for a diagnostic message. `repr` on untrusted nested data can raise
    RecursionError (or anything at all, via a hostile __repr__), and these strings are built inside
    functions documented TOTAL — so a diagnostic must never be the thing that crashes the report."""
    try:
        text = repr(v)
    except Exception:      # noqa: BLE001 — a diagnostic string is never worth propagating an error
        return f"<unreprable {type(v).__name__}>"
    return text if len(text) <= limit else text[:limit] + "…"


def reconciliation_problems(rec, rev) -> list:
    """The reasons a reconciliation-result CANNOT be trusted as a complete record correctly paired
    with the reviewer-response it claims to reconcile — empty means safe to persist or report on.
    This is the STORAGE-BOUNDARY guard behind issues #16/#17/#18: save_reconciliation_doc (the only
    sanctioned way to write a reconciliation) refuses on any of these, and impasse_report's `show`
    and `lifetime_recap` use the same emptiness test to decide whether a STORED record's totals can
    be trusted at all — so a fabricated finding_id or a missing sibling disqualifies a record from
    every surface that reads it, not only the write path that created it.

    Deliberately narrower than full JSON-Schema validation (see tests/validate_schemas.py, which is
    where a real validation ENGINE belongs — stdlib-only in scripts/ forbids shipping one, not hand-
    written validation, see CLAUDE.md). This hand-checks the bounded, runtime-critical subset a
    report or a save would otherwise trust blindly: required top-level fields and their types, the
    `outcome` enum, item shape, the deadlock-needs-escalation and rejection-needs-contradicting-
    evidence protocol invariants, `outcome` consistency with the items, and — given `rev`, the
    sibling reviewer-response — that this reconciliation is the one that review produced, and every
    finding_id it disposes of was actually raised by it.

    TOTAL: never raises, for any input, including malformed/hostile shapes — a hand-edited
    reconciliation file and the reviewer's own output are both UNTRUSTED. `rev` is None when no
    sibling reviewer-response is on disk, a dict when one is, or (malformed) anything else."""
    problems = []
    if not isinstance(rec, dict):
        return ["reconciliation is not an object"]

    for key in ("schema_version", "reconciliation_id", "review_id", "outcome"):
        v = rec.get(key)
        if not (isinstance(v, str) and v):
            problems.append(f"'{key}' is missing or not a non-empty string")
    outcome = rec.get("outcome")
    if isinstance(outcome, str) and outcome and outcome not in RECOGNIZED_OUTCOMES:
        problems.append(f"outcome {_safe_repr(outcome)} is not one of {sorted(RECOGNIZED_OUTCOMES)}")

    items = rec.get("items")
    if not isinstance(items, list):
        problems.append("reconciliation 'items' is not a list")
        return problems

    seen = {}
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            problems.append(f"item[{i}] is not an object")
            continue
        fid = it.get("finding_id")
        if isinstance(fid, str) and fid:
            seen[fid] = seen.get(fid, 0) + 1
        else:
            problems.append(f"item[{i}] is missing a string finding_id")
        state = it.get("state")
        # isinstance guard first: a non-string state (e.g. a JSON list) is unhashable and would raise
        # on the set membership test below — this function must stay total.
        if not (isinstance(state, str) and state in RECOGNIZED_ITEM_STATES):
            problems.append(f"item[{i}] (finding {_safe_repr(fid)}) has an unrecognized state {_safe_repr(state)}")
            continue
        if state == "deadlocked":
            esc = it.get("escalation")
            if not isinstance(esc, dict):
                problems.append(f"item {_safe_repr(fid)} is deadlocked but has no escalation object")
            else:
                for k in ("dispute_kind", "stop_reason", "operator_question"):
                    if not (isinstance(esc.get(k), str) and esc[k]):
                        problems.append(f"item {_safe_repr(fid)} escalation is missing '{k}'")
        elif state == "rejected":
            vers = it.get("verification")
            ok = isinstance(vers, list) and any(
                isinstance(v, dict) and v.get("result") == "contradicts" for v in vers)
            if not ok:
                problems.append(f"item {_safe_repr(fid)} is rejected but carries no verification with "
                                "result: contradicts — a refutation resting only on host judgment "
                                "is a deadlock (dispute_kind unverified_refutation), not a rejection")
    problems += [f"duplicate finding_id {_safe_repr(k)} across items" for k, n in sorted(seen.items()) if n > 1]

    if isinstance(outcome, str):
        has_deadlock = any(isinstance(it, dict) and it.get("state") == "deadlocked" for it in items)
        if outcome == "converged" and has_deadlock:
            problems.append("outcome is 'converged' but at least one item is still deadlocked")
        if outcome == "deadlocked" and not has_deadlock:
            problems.append("outcome is 'deadlocked' but no item is in state deadlocked")

    # Pairing: this reconciliation must be tied to the ONE reviewer-response it claims to reconcile.
    rid = rec.get("review_id")
    if isinstance(rid, str) and rid:
        if rev is None:
            problems.append(MISSING_REVIEWER_RESPONSE_MSG.format(rid=rid))
        elif not isinstance(rev, dict):
            problems.append("reviewer-response is malformed (not an object)")
        else:
            # A sibling FILE is not the same as a usable reviewer-response. Without these checks a
            # structurally broken response (no findings list, entries without ids) certified a
            # reconciliation as a verified pair — so "verified" could rest on a document that could
            # not itself have come from a real review.
            #
            # Scoped deliberately to what PAIRING needs: the findings list and its ids are what
            # coverage is checked against. Other schema-required fields (assessment, summary,
            # artifact) are not re-checked here — whole-document conformance is CI's job, and
            # duplicating it would make this validator refuse records over fields that have no
            # bearing on whether the two halves belong together.
            _f = rev.get("findings")
            if not isinstance(_f, list):
                problems.append("reviewer-response has no 'findings' list — it cannot establish "
                                "what was raised, so this pair cannot be verified")
            elif any(not (isinstance(f, dict) and isinstance(f.get("id"), str) and f.get("id"))
                     for f in _f):
                problems.append("reviewer-response has finding(s) without a string 'id' — coverage "
                                "cannot be checked against them")
            if rev.get("review_id") != rid:
                problems.append(f"reviewer-response review_id {rev.get('review_id')!r} does not "
                                f"match the reconciliation's {rid!r} — refusing to treat them as a pair")
            # Reuses `_f` from the shape check above rather than re-testing it — the duplicate
            # test appended two different messages for one condition (review F5).
            if isinstance(_f, list):
                revf = _f
                known = {f["id"] for f in revf if isinstance(f, dict) and isinstance(f.get("id"), str)}
                unknown = sorted({it.get("finding_id") for it in items
                                  if isinstance(it, dict) and isinstance(it.get("finding_id"), str)
                                  and it["finding_id"] not in known})
                if unknown:
                    problems.append(f"unknown finding_id(s) not raised by this review: {unknown}")
                # A converged outcome asserts every raised finding was settled — that assertion is
                # checkable only here, once the sibling's real finding count is known. This is the
                # exact issue #16 shape (9-of-13 stored as converged), caught for good regardless of
                # how the record reached this state: save_reconciliation_doc refuses it at write time
                # (below), and this makes an already-stored one just as unverifiable to `show`/
                # `lifetime_recap` as a missing sibling would be.
                if outcome == "converged" and len(items) < len(revf):
                    problems.append(f"outcome is 'converged' but only {len(items)} of {len(revf)} "
                                    "raised findings are dispositioned")
    # else: already reported above as a missing/invalid top-level 'review_id'.
    return problems


def _backup_reconciliation(d: str, target: str) -> str:
    """Copy the reconciliation-result about to be replaced to the next free
    reconciliation-result.<n>.json in the same run directory (n starting at 1), so a forced replace
    (save_reconciliation_doc, force=True) never destroys the human-written verification notes and
    dispositions in the old record — findings can be re-derived from the reviewer-response; those
    cannot (issue #18). The backup slot is reserved with O_EXCL, not just picked as the first free
    name observed, so two concurrent forced replacements in the same run directory can't choose the
    same slot and clobber each other's copy. The caller must hold the run's interprocess lock for the
    whole reserve-copy-replace sequence. 0600 like every other record; permanent — kept by `prune`,
    removed only when the whole run is forgotten via `forget_run`'s rmtree."""
    with open(target, "rb") as f:
        data = f.read()
    i = 1
    while True:
        bpath = os.path.join(d, f"reconciliation-result.{i}.json")
        try:
            fd = os.open(bpath, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            i += 1
            continue
        try:
            with os.fdopen(fd, "wb") as bf:
                bf.write(data)
                bf.flush()
                os.fsync(bf.fileno())
            os.chmod(bpath, 0o600)
            fsync_dir(d)
            return bpath
        except BaseException:
            try:
                os.remove(bpath)
            except OSError:
                pass
            raise


def _item_loses_substance(old: dict, new: dict) -> bool:
    """True if `new` drops human-authored content that `old` carried for the same finding.

    WHAT IT'S FOR: deciding whether one reconciliation item genuinely supersedes another, or merely
    shares its `finding_id`. The fields checked are the ones a person writes and that cannot be
    re-derived from the reviewer-response — an escalation (the operator's question, and the ruling
    that answers it), the host's verification reasoning, and the resolution text. Gaining any of
    these is progress; losing one is the silent data loss `--force` exists to gate.

    TOTAL: never raises; a non-dict on either side is treated as carrying nothing."""
    if not isinstance(old, dict) or not isinstance(new, dict):
        return False

    def _has(d, key):
        v = d.get(key)
        if isinstance(v, dict):
            return bool(v)
        return isinstance(v, str) and bool(v.strip())

    return any(_has(old, k) and not _has(new, k)
               for k in ("escalation", "host_position", "resolution", "verification"))


def save_reconciliation_doc(doc: dict, *, partial: bool = False, force: bool = False) -> dict:
    """The ONLY sanctioned way to persist a reconciliation-result (issues #16/#17/#18). Loads the
    sibling reviewer-response itself, validates the pair with reconciliation_problems(), and refuses
    (rather than writing) on any structural problem, an unknown/duplicate finding_id, or a missing/
    mismatched reviewer-response. Coverage and overwrite are separate, opt-in escape hatches rather
    than hard errors, because both are legitimate mid-protocol: partial coverage needs `partial=True`
    (and then still refuses `outcome: 'converged'` — a partial record cannot claim convergence, the
    exact bug class this closes).

    OVERWRITE, exactly. A save SUPERSEDES an existing reconciliation with no flag when all of:
    the existing one does not claim to be finished (`outcome` != 'converged'); the new one
    dispositions every finding_id the old one did; no shared item loses human-written content
    (`_item_loses_substance`); and the existing record is readable enough to establish those things.
    That is the ordinary `partial` -> finish path, and it can only move the record forward.
    Everything else — a finished record, a save that drops dispositions or strips a ruling, or an
    existing record too damaged to compare — needs `force=True`. Either way the previous file is
    backed up first, never silently discarded. The result reports `superseded` separately from
    `replaced`, because "no work was lost" and "a file was overwritten" are different facts.

    A known limit, stated rather than hidden: the interim test reads the existing record's OWN
    `outcome`. A record could under-report itself to look interim — but the content and coverage
    checks above are what actually protect the work, and they do not rest on self-reporting.

    The whole read-validate-write sequence runs under the run's interprocess lock, so two concurrent
    callers can't interleave a lost update or a lost backup.

    save_run_doc remains the general write primitive (the runner still uses it for reviewer-
    responses); this is the boundary EVERY reconciliation write must go through — CLI or any other
    caller — so the orphan/overwrite defects can't resurface through a path other than the CLI.

    Returns a result dict. Never raises for an expected refusal (the input is UNTRUSTED — a hand-
    edited or reviewer-influenced file must produce a controlled refusal, not a traceback):
      {"ok": False, "reasons": [...]}                                                -- refused
      {"ok": False, "conflict": True, "reconciliation_id": ..., "item_count": ...}   -- exists, no force
      {"ok": True, "path": ..., "replaced": bool, "backup_path": str | None,
       "dispositioned": int, "raised": int}
    A genuine filesystem failure (disk full, permissions) still raises OSError — that is not an
    expected outcome and must not be swallowed."""
    if not isinstance(doc, dict) or not (isinstance(doc.get("review_id"), str) and doc["review_id"]):
        return {"ok": False, "reasons": ["reconciliation must be a JSON object with a review_id"]}
    rid = doc["review_id"]

    with _interprocess_lock(f"run-{_safe_id(rid)}.lock"):
        run = load_run(rid)   # re-read INSIDE the lock — see the primary fresh, not a stale read
        rev = run.get("reviewer_response")
        problems = reconciliation_problems(doc, rev)
        if problems:
            return {"ok": False, "reasons": problems}

        # reconciliation_problems() already refuses outcome:'converged' paired with partial coverage
        # (it can check that once the sibling's real finding count is known) — so the only coverage
        # gate left here is: a non-converged partial save (e.g. outcome:'incomplete') still needs an
        # explicit partial=True, since a deliberately partial reconciliation mid-protocol is
        # legitimate but must never be the silent default.
        # Both counts come from hand-editable files, so both use a shape-safe count: a truthy
        # non-sized value here would raise TypeError on the coverage path rather than refusing.
        _revf = rev.get("findings") if isinstance(rev, dict) else None
        raised = len(_revf) if isinstance(_revf, list) else 0
        dispositioned = len(reconciliation_items(doc))
        if raised and dispositioned < raised and not partial:
            return {"ok": False, "reasons": [
                f"only {dispositioned} of {raised} findings are dispositioned — pass partial=True "
                "(--partial on the CLI) if this is a deliberately partial reconciliation mid-protocol"]}

        d = _run_dir(rid)
        target = os.path.join(d, "reconciliation-result.json")
        replaced = os.path.isfile(target)
        backup_path = None
        superseded = False
        if replaced:
            existing = run.get("reconciliation_result") or {}
            # SUPERSEDING AN INTERIM RECORD IS NOT A CLOBBER. Completing a --partial reconciliation
            # otherwise ended in --force: the finished record conflicts with the operator's own
            # interim one, so the normal workflow's last step became the flag that exists to mark a
            # dangerous replace. Operators habituate to appending it, and that reflex is exactly what
            # re-creates issue #18's exposure — a guard everyone types by default guards nothing.
            #
            # So a save may replace WITHOUT --force when both hold, which together mean it can only
            # move the record forward:
            #   1. the existing record does not claim to be finished (outcome is not `converged`), and
            #   2. the new one dispositions every finding the existing one did — a superset, so no
            #      verification note, disposition or operator ruling can be dropped.
            # A backup is still written. Anything else — replacing a converged record, or one whose
            # dispositions this save would lose — still requires --force.
            _old_by_id = {it["finding_id"]: it for it in reconciliation_items(existing)
                          if isinstance(it.get("finding_id"), str)}
            _new_by_id = {it["finding_id"]: it for it in reconciliation_items(doc)
                          if isinstance(it.get("finding_id"), str)}
            _existing_ids, _new_ids = set(_old_by_id), set(_new_by_id)
            _interim = existing.get("outcome") != "converged"
            # IDENTITY BY ID IS NOT IDENTITY OF WORK. A superset of finding_ids says nothing about
            # what each item CONTAINS: a bare {"finding_id": "F001", "state": "resolved"} is a
            # superset of an item carrying an operator's ruling and a paragraph of verification
            # notes, and would have silently replaced it. Those are precisely the fields --force
            # exists to protect — findings can be re-derived from the reviewer-response, a human's
            # reasoning cannot. So an item may GAIN content, and a deadlock may become resolved (the
            # normal forward step once the operator answers), but it may not become poorer.
            _impoverished = sorted(fid for fid in _existing_ids & _new_ids
                                   if _item_loses_substance(_old_by_id[fid], _new_by_id[fid]))
            # An existing record we cannot READ is never supersedable. `reconciliation_items`
            # degrades a corrupt collection to [], which would make the superset test hold
            # VACUOUSLY — so the emptier and more damaged the old record, the easier it would be to
            # overwrite without a flag. The whole supersede argument is "no work is lost", and that
            # cannot be established about content that will not parse. Fall back to --force, whose
            # backup then preserves whatever was there.
            _existing_unreadable = (run.get("reconciliation_result_unreadable")
                                    or not isinstance(existing.get("items"), list))
            superseded = (_interim and _existing_ids <= _new_ids and not _impoverished
                          and not _existing_unreadable)
            if not force and not superseded:
                _lost = sorted(_existing_ids - _new_ids)
                return {"ok": False, "conflict": True,
                        "reconciliation_id": existing.get("reconciliation_id"),
                        "existing_outcome": existing.get("outcome"),
                        "would_drop": _lost,
                        "would_impoverish": _impoverished,
                        "existing_unreadable": bool(_existing_unreadable),
                        # reconciliation_items(), not len(... or []): `existing` is a hand-editable
                        # file, and a truthy non-sized `items` (e.g. `"items": 1`) made len() raise
                        # TypeError out of the branch whose whole job is a controlled refusal.
                        "item_count": len(reconciliation_items(existing))}
            backup_path = _backup_reconciliation(d, target)

        # RE-VERIFY the sibling immediately around the write. The per-run lock serializes other
        # PROCESSES, but it cannot help when a delete interleaves inside this one (forget_run is
        # reachable from a cleanup path, and re-entry is deliberately a no-op to avoid a
        # self-deadlock). Without this, `forget_run` landing between validation and write let
        # save_run_doc's makedirs(exist_ok=True) recreate the directory holding a reconciliation
        # ALONE — reproducing issue #17's orphan from two commands each behaving as documented.
        # Checking before AND after is what makes the pair invariant hold at the moment of writing
        # rather than at the moment of validating.
        sibling = os.path.join(d, "reviewer-response.json")
        if not os.path.isfile(sibling):
            return {"ok": False, "reasons": [MISSING_REVIEWER_RESPONSE_MSG.format(rid=rid)]}
        path = save_run_doc(rid, "reconciliation-result", doc,
                            _sanctioned=_RECONCILIATION_TOKEN)
        if not os.path.isfile(sibling):
            # It vanished DURING the write, so we just created the orphan ourselves. Undo it: a
            # refusal that leaves corrupt state behind is a louder version of the same bug.
            try:
                os.unlink(path)
            except OSError:
                pass
            return {"ok": False, "reasons": [
                "the reviewer-response disappeared while writing (a concurrent forget/prune?) — "
                "nothing was saved, so no orphan record was left behind"]}
        return {"ok": True, "path": path, "replaced": replaced, "backup_path": backup_path,
                "superseded": superseded,
                "dispositioned": dispositioned, "raised": raised}


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
    d = _run_dir(run_id)

    unreadable = set()

    def _load(p):
        if not os.path.exists(p):
            return None                 # genuinely absent — a different fact from "present but junk"
        try:
            with open(p, encoding="utf-8") as f:
                doc = json.loads(f.read(_MAX_STORE_BYTES))
        except (OSError, ValueError):   # JSONDecodeError + UnicodeDecodeError are ValueError subclasses
            unreadable.add(p)
            return None
        # A record that PARSES but isn't an object is unusable in the same way an unparseable one is,
        # and every caller here immediately does `.get(...)` on the result. Returning the raw value
        # made a hand-corrupted file (e.g. a JSON array) raise AttributeError out of `show` AND
        # `list` — so the tool you would run to FIND the bad record was the one that crashed on it.
        # Treat it as unreadable, which routes it into the same "unverifiable" reporting path.
        if not isinstance(doc, dict):
            unreadable.add(p)
            return None
        return doc

    rev_path = os.path.join(d, "reviewer-response.json")
    rec_path = os.path.join(d, "reconciliation-result.json")
    rev, rec = _load(rev_path), _load(rec_path)
    # `*_unreadable` distinguishes "no such file" from "the file is there and is junk". Both yield a
    # None document, but they are opposite facts about the run: absent means the step never happened,
    # unreadable means it DID and the evidence is damaged. Collapsing them made `show` report a
    # recorded, converged reconciliation as "not yet recorded" — a false statement of the exact kind
    # this whole change exists to stop, and one `list` contradicted on the same run.
    return {
        "run_id": _safe_id(run_id),
        "reviewer_response": rev,
        "reconciliation_result": rec,
        "reviewer_response_unreadable": rev_path in unreadable,
        "reconciliation_result_unreadable": rec_path in unreadable,
    }


def forget_run(run_id: str) -> bool:
    d = _run_dir(run_id)
    # Under the SAME per-run lock the reconciliation writer takes. Without it, a delete could land
    # between that writer's pair validation and its write — and `save_run_doc`'s
    # `makedirs(exist_ok=True)` would then recreate the directory holding a reconciliation alone,
    # reproducing issue #17's orphan from two commands each behaving exactly as documented.
    with _interprocess_lock(f"run-{_safe_id(run_id)}.lock"):
        # Don't rmtree THROUGH a symlinked record dir, and report success only if it's actually gone.
        if os.path.isdir(d) and not os.path.islink(d):
            shutil.rmtree(d, ignore_errors=True)
            return not os.path.exists(d)
        return False


# --- Duration telemetry: the metrics store ---------------------------------------------------
#
# WHAT IT'S FOR: a review can take 3 minutes or 30, and before this store Impasse had no way to tell
# an operator which — so a wall-clock cap was a guess, and a timeout threw away the evidence needed
# to guess better next time. This is a small append-only log of HOW LONG runs took, kept so the
# recommendation engine below can answer "how long will THIS review take on YOUR account" from your
# own history instead of a shipped constant.
#
# It is deliberately NOT a second copy of the run record. A run record (runs/<id>/) holds the
# reviewed content; this holds sizes, timings and outcomes. The privacy guarantee is STRUCTURAL, not
# a promise: `record_metrics` writes only the allowlisted fields in `_METRIC_FIELDS`, and types each
# one by name, so a caller cannot put artifact text here even by mistake. Two fields
# (`model_resolved`, `backend_version`) are read from the reviewer CLI's own output and so are
# backend-controlled within a 200-character bound -- see record_metrics for the exact guarantee. The one content-derived value stored is
# `artifact_digest` — a hash, not content; it already appears in the consent manifest and the run
# record, and it is what lets repeat attempts on the same artifact be correlated. A digest confirms
# whether a KNOWN artifact was reviewed; it does not reveal an unknown one.
#
# Failed runs are recorded too — a timeout is the single most informative sample for predicting the
# next wall, so dropping it would blind exactly the case this exists to fix.

METRICS_FILENAME = "metrics.jsonl"
_MAX_METRICS_RECORDS = 1000     # bound the store; oldest entries are dropped past this
_METRICS_TRIM_SLACK = 200       # rewrite only once this many past the cap (amortize the rewrite)
_MAX_METRICS_BYTES = 8_000_000  # refuse to read a pathologically large store into memory

# The write allowlist. Adding a key here is the ONLY way a field reaches disk — keep it scalar and
# content-free (see the privacy note above).
_METRIC_FIELDS = frozenset({
    "ts", "kind", "backend", "provider", "host", "independence",
    "model_requested", "model_resolved", "model_source", "backend_version",
    "effort", "speed",
    "artifact_bytes", "artifact_tokens_est", "instruction_tokens_est", "schema_tokens_est",
    "artifact_digest",
    "wall_s", "idle_s",
    "outcome", "failure_code", "termination",
    "duration_s", "ttfb_s", "bytes_received",
    "phases", "transient_retries", "output_retries", "findings_count",
    "impasse_version",
})


def metrics_path() -> str:
    return os.path.join(config_dir(), METRICS_FILENAME)


_MAX_METRIC_STR = 200     # bound every stored string; see _sanitize_metric_value
_MAX_METRIC_PHASES = 80   # bound the one nested map that is stored


# `phases` is the ONE field stored as a nested map ({phase_name: seconds}); every other allowlisted
# field is a scalar. Typing them separately is what makes the no-content guarantee structural: a
# scalar field cannot smuggle text in as dictionary KEYS, which a shape-only check would have let
# through (a dict's keys are values too).
_METRIC_MAP_FIELDS = frozenset({"phases"})
_MAX_METRIC_INT = 2 ** 53   # bound ints as well as strings: an unbounded int is unbounded TEXT on
                            # disk once serialized, and float() on a huge one raises OverflowError.


def _sanitize_metric_value(v, field: str | None = None):
    """Coerce one metric value to a bounded, content-free shape, or None to drop it.

    TOTAL: never raises, for any input. `record_metrics` calls this on every review's exit path,
    including the failure paths whose diagnosis it exists to serve.

    Keys alone are not a no-content guarantee: a value reaching an allowed key still has to be
    bounded in TYPE and LENGTH, or an unbounded backend-supplied string (e.g. a model name read out
    of the reviewer CLI's own output) could carry arbitrary text into the store and, incidentally,
    grow a single row past the trim threshold.

    Typing is PER FIELD, not merely per shape. `field` names the destination column; the one map
    field (`phases`) accepts only a bounded {name: number} map, and every other field accepts only a
    finite number, a bool, a short string, or None. Without that split, a dict handed to a scalar
    field would store its KEYS verbatim — artifact text arriving as `{"<artifact text>": 1}` — which
    is exactly the hole the shape-only version left open. An unknown/None `field` is treated as
    scalar: the conservative side, since unknown keys are dropped by `record_metrics` anyway.
    """
    is_map_field = field in _METRIC_MAP_FIELDS
    if isinstance(v, dict):
        if not is_map_field:
            return None     # a scalar field never takes a map: its keys would be stored text
        out = {}
        for k, val in list(v.items())[:_MAX_METRIC_PHASES]:
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                continue
            if isinstance(val, int) and abs(val) > _MAX_METRIC_INT:
                continue    # bounded BEFORE float(): math.isfinite on a huge int raises OverflowError
            if not math.isfinite(val):
                continue
            out[str(k)[:60]] = val
        return out
    if is_map_field:
        return None         # and the map field takes nothing else
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v if abs(v) <= _MAX_METRIC_INT else None
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, str):
        return v[:_MAX_METRIC_STR]
    return None


def record_metrics(entry: dict) -> bool:
    """Append ONE run's timing/outcome metrics to the local store. Returns True if written.

    TOTAL: never raises. This runs on every review's exit path — including the failure paths whose
    diagnosis it exists to serve — so a full disk or a read-only config dir must degrade to "no
    telemetry", never turn a reviewer timeout into a traceback.

    The no-artifact-content guarantee is structural on BOTH axes: only `_METRIC_FIELDS` keys are
    written, and every value is passed through `_sanitize_metric_value` WITH ITS FIELD NAME, so each
    column is typed — the one map field (`phases`) takes a bounded {name: number} map and every other
    field takes only a finite bounded number, a bool, a <=200-char string, or None. A caller cannot
    put artifact text here even by mistake, and no single row can grow without bound.

    THE EXACT GUARANTEE: no caller can write artifact content, and every stored string is bounded at
    200 characters. It is NOT a claim that stored strings are backend-independent — `model_resolved`
    and `backend_version` are read from the reviewer CLI's own output, so a misbehaving backend can
    place up to 200 characters of its choosing in those two fields. Bounded and attributable, not
    impossible; `--no-record`/`--raw` withhold the digest, and IMPASSE_NO_METRICS=1 disables the
    store entirely.
    """
    try:
        row = {}
        for k, v in entry.items():
            if k not in _METRIC_FIELDS:
                continue
            sv = _sanitize_metric_value(v, field=k)
            if sv is not None or v is None:
                row[k] = sv
        if not row:
            return False
        row.setdefault("ts", time.time())
        line = json.dumps(row, separators=(",", ":")) + "\n"
        ensure_config_dir()
        path = metrics_path()
        with _interprocess_lock("metrics.lock"):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)
            try:
                os.chmod(path, 0o600)   # best-effort; a pre-existing file may have looser bits
            except OSError:
                pass
            _trim_metrics(path)
        return True
    except (OSError, ValueError, TypeError, ArithmeticError):
        # ArithmeticError too (OverflowError is one, and is NOT a ValueError): this function
        # documents itself TOTAL, and it runs on the failure exit paths where a traceback would
        # replace the diagnosis the operator came for.
        return False


def _read_metrics_tail(path: str) -> bytes:
    """Read at most _MAX_METRICS_BYTES from the END of the store, dropping a leading partial line.

    The TAIL, not the head: this is a rolling log whose newest rows are the ones that matter — for
    trimming (keep the newest) and for percentiles (describe recent behavior). Reading the head of an
    oversized file would make trimming discard the newest rows and make `performance` report the
    oldest, both silently.
    """
    with open(path, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        if size > _MAX_METRICS_BYTES:
            f.seek(size - _MAX_METRICS_BYTES)
            data = f.read()
            nl = data.find(b"\n")   # the seek lands mid-row; drop that partial line
            return data[nl + 1:] if nl != -1 else b""
        return f.read()


def _trim_metrics(path: str) -> None:
    """Drop the oldest rows once the store drifts past its cap. Called under the metrics lock.
    Amortized: only rewrites once _METRICS_TRIM_SLACK rows past the cap, not on every append."""
    try:
        lines = _read_metrics_tail(path).splitlines(keepends=True)
        if len(lines) <= _MAX_METRICS_RECORDS + _METRICS_TRIM_SLACK:
            return
        keep = lines[-_MAX_METRICS_RECORDS:]
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".metrics-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(b"".join(keep))
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
            fsync_dir(os.path.dirname(path))
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    except OSError:
        return


def load_metrics(*, backend: str | None = None, model: str | None = None,
                 effort: str | None = None, speed: str | None = None) -> list:
    """Read the metrics store, newest last, optionally filtered. Malformed lines are SKIPPED rather
    than fatal — a partially-written row (crash mid-append) must not break `performance` reporting.

    `model` matches either the resolved or the requested model, so filtering by the name an operator
    actually typed still finds runs whose backend later reported a fully-qualified id.

    `effort`/`speed` matter because they change duration substantially: pooling a history of
    `--effort low` runs into an estimate for `--effort high` would under-recommend the wall. They
    filter only when given, so a caller that doesn't care still sees everything.
    """
    try:
        data = _read_metrics_tail(metrics_path())
    except OSError:
        return []
    rows = []
    for line in data.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        if backend and row.get("backend") != backend:
            continue
        if model and model not in (row.get("model_resolved"), row.get("model_requested")):
            continue
        if effort is not None and row.get("effort") != effort:
            continue
        if speed is not None and row.get("speed") != speed:
            continue
        rows.append(row)
    return rows


def forget_metrics() -> bool:
    """Delete the whole metrics store. Returns True if a file was removed."""
    try:
        os.remove(metrics_path())
        return True
    except OSError:
        return False


def _percentile(values: list, q: float) -> float | None:
    """Nearest-rank percentile of a list of numbers (q in 0..1). None for an empty list. Nearest-rank
    (not interpolated) is deliberate: on the handful of samples this store realistically holds, an
    interpolated value invents a duration nobody observed."""
    vals = sorted(v for v in values
                  if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v))
    if not vals:
        return None
    k = max(0, min(len(vals) - 1, int(math.ceil(q * len(vals))) - 1))
    return float(vals[k])


# --- Wall-clock recommendation ---------------------------------------------------------------
#
# WHAT IT'S FOR: turning "how long should --wall be?" from a guess into a number, so an operator
# doesn't discover a too-small cap by burning a full paid review on a timeout. It answers from the
# operator's own history when there is enough of it, and from a coarse shipped table when there
# isn't — and always says WHICH, because a seeded constant and a measured percentile deserve very
# different amounts of trust.
#
# HONESTY BOUND: the shipped tables below are NOT measurements of your account. They are coarse
# seeds seeded from a handful of observed runs (issue #11: a ~5.7K-token code review took 594s on
# Claude Sonnet, and the Claude backend default exceeded 605s at both ~5.7K and ~10.8K tokens),
# padded for margin. They are a starting point that keeps a first run from timing out, not a
# prediction. Once `_MIN_EMPIRICAL_SAMPLES` completed runs for a backend+model exist in the metrics
# store, the empirical fit supersedes them and `basis` says "empirical".

# Rough characters-per-token for English prose and code. A crude divisor, NOT a tokenizer — it is
# used only to size a timeout, where being 20% off changes nothing that matters.
_BYTES_PER_TOKEN = 4

# Per-backend seeds: (base_s, seconds_per_1k_artifact_tokens). `base_s` covers CLI startup, auth,
# connection and the fixed instruction+schema overhead; the rate covers reading the artifact and
# reasoning over it.
_WALL_SEEDS = {
    "claude": (300.0, 70.0),
    "codex": (180.0, 40.0),
}
_WALL_SEED_FALLBACK = (300.0, 70.0)   # an unknown backend gets the slower profile, never the faster

# Reasoning effort multiplies thinking time (codex only — the claude backend has no effort knob).
_EFFORT_MULTIPLIER = {"none": 0.6, "low": 0.75, "medium": 1.0, "high": 1.6, "xhigh": 2.2}
# Fast mode is a higher serving tier: real, but modest and not guaranteed. Claim only a small gain.
_SPEED_MULTIPLIER = {"standard": 1.0, "fast": 0.8}

_WALL_SAFETY_MARGIN = 1.25    # headroom over the central estimate — a timeout costs a whole review
_WALL_FLOOR_S = 300.0         # never recommend below the CLI default; startup+auth alone can eat minutes
_WALL_CEILING_S = 5400.0      # 90 min: past here, splitting the artifact beats waiting longer
_WALL_ROUNDING_S = 60.0       # recommendations are round minutes — false precision helps nobody
_MIN_EMPIRICAL_SAMPLES = 5    # below this, one slow run would dominate the fit; keep the seed table


def estimate_tokens(nbytes: int) -> int:
    """A crude token estimate from a byte count (bytes / 4). NOT a tokenizer and not exact — it
    exists to size a timeout and to describe payloads in a unit operators think in."""
    try:
        return max(0, int(nbytes) // _BYTES_PER_TOKEN)
    except (TypeError, ValueError):
        return 0


def _empirical_rate(rows: list, base_s: float) -> tuple:
    """Fit seconds-per-1k-artifact-tokens from COMPLETED runs in `rows`.

    Returns (rate_p90, sample_count, p50_duration, p90_duration, median_tokens), with rate_p90 None
    when there aren't enough completed samples. Normalizing to a per-1k-token RATE (rather than
    averaging raw durations) is what lets a history of small reviews inform a larger one — but only
    so far, which is why `median_tokens` comes back too: the caller needs to know how far it is
    extrapolating beyond anything actually observed.

    Timed-out runs are deliberately EXCLUDED from the fit: a timeout records when we stopped
    waiting, not how long the review needed, so treating it as a duration would bias every future
    recommendation DOWNWARD — the exact failure this feature exists to prevent. They are not
    ignored, though: `recommend_wall` separately floors its answer above the longest one.
    """
    done = [r for r in rows if r.get("outcome") == "completed"]
    durations = [r.get("duration_s") for r in done if isinstance(r.get("duration_s"), (int, float))]
    toks = [r.get("artifact_tokens_est") for r in done
            if isinstance(r.get("artifact_tokens_est"), (int, float))]
    median_tokens = _percentile(toks, 0.5)
    rates = []
    for r in done:
        d = r.get("duration_s")
        tok = r.get("artifact_tokens_est")
        if not isinstance(d, (int, float)) or not isinstance(tok, (int, float)):
            continue
        tok_k = max(float(tok) / 1000.0, 0.1)   # floor so a tiny artifact can't mint a huge rate
        rates.append(max(0.0, float(d) - base_s) / tok_k)
    if len(rates) < _MIN_EMPIRICAL_SAMPLES:
        return (None, len(done), _percentile(durations, 0.5), _percentile(durations, 0.9),
                median_tokens)
    return (_percentile(rates, 0.9), len(done),
            _percentile(durations, 0.5), _percentile(durations, 0.9), median_tokens)


def recommend_wall(*, backend: str, artifact_tokens: int, model: str | None = None,
                   effort: str | None = None, speed: str | None = None,
                   rows: list | None = None) -> dict:
    """Recommend a --wall (seconds) for a review of this size on this backend/model.

    Returns {recommended_wall_s, basis, sample_count, p50_s, p90_s, floor_reason, rationale}, where
    `basis` is "empirical" (fitted from >= _MIN_EMPIRICAL_SAMPLES of the operator's own completed
    runs at this backend+model+effort+speed) or "heuristic" (the shipped seed table — a padded
    starting point, not a measurement of this account). Callers MUST surface `basis`: the two
    deserve different trust.

    EXACT CLAIM, in each mode. Empirical: "runs recorded on this machine at this backend, model,
    effort and speed finished inside this, with margin" — it is NOT a claim about artifacts unlike
    those already seen, which is why an extrapolation beyond observed sizes falls back to the seed
    and says so. Heuristic: "this is a padded starting point chosen so a first run is unlikely to
    time out" — it is not a measurement of anything. Neither is a guarantee: no wall can bound a
    provider-side queue, and the ceiling clamp below can return a value known to be insufficient
    (it says so when it does).

    `rows` are prior metrics rows to fit from; pass None to read the store, which filters to this
    backend+model+effort+speed. A caller supplying `rows` is responsible for that filtering itself.
    """
    base_s, seed_rate = _WALL_SEEDS.get(backend, _WALL_SEED_FALLBACK)
    if rows is None:
        rows = load_metrics(backend=backend, model=model, effort=effort, speed=speed)
    tok_k = max(float(artifact_tokens) / 1000.0, 0.0)

    rate_p90, sample_count, p50, p90, median_tokens = _empirical_rate(rows, base_s)

    # The shipped estimate, always computed — it is the fallback AND the floor when the empirical
    # fit is asked to extrapolate (below). Effort/speed scale the REASONING half only; CLI startup
    # and auth don't get faster with a lower effort.
    seed_est = base_s + seed_rate * tok_k
    seed_est = base_s + (seed_est - base_s) * _EFFORT_MULTIPLIER.get(effort or "medium", 1.0)
    seed_est = base_s + (seed_est - base_s) * _SPEED_MULTIPLIER.get(speed or "standard", 1.0)

    extrapolated = False
    if rate_p90 is not None:
        basis = "empirical"
        # Effort/speed are NOT re-applied here: load_metrics filtered the history to this same
        # effort+speed, so the samples already embody them. (Re-applying a multiplier on top of
        # matched samples would double-count.)
        est = base_s + rate_p90 * tok_k
        # Never recommend below a duration actually observed at these settings. The rate fit
        # subtracts a fixed `base_s` intercept, so a history of runs SHORTER than that intercept
        # fits a rate of exactly zero and would otherwise collapse the estimate to the base alone.
        if p90 is not None:
            est = max(est, p90)
        # A history of small, fast reviews fits a near-zero rate (every duration sits below base_s),
        # which would confidently recommend a short wall for an artifact far larger than anything
        # measured — the exact failure this feature exists to prevent. Beyond ~2x the largest
        # typical observed size there is no local evidence, so fall back to the shipped estimate
        # whenever it is higher, and say that the number is an extrapolation.
        if median_tokens and artifact_tokens > 2 * median_tokens and seed_est > est:
            est, extrapolated = seed_est, True
    else:
        basis, est = "heuristic", seed_est
    rec = est * _WALL_SAFETY_MARGIN

    # A run that ALREADY timed out proves the review needs more than that cap. Never recommend a
    # value a comparable artifact has been observed to exceed — that would re-buy the same failure.
    # "Comparable" is bounded on BOTH sides (0.5x-2x this artifact): a timeout on something ten
    # times larger says nothing useful about this review and would inflate every estimate.
    floor_reason = None
    timeouts = [r.get("wall_s") for r in rows
                if r.get("outcome") == "timeout" and isinstance(r.get("wall_s"), (int, float))
                and isinstance(r.get("artifact_tokens_est"), (int, float))
                and 0.5 * artifact_tokens <= r["artifact_tokens_est"] <= 2.0 * artifact_tokens]
    worst = max(timeouts) if timeouts else None
    if worst is not None and rec <= worst * 1.2:
        rec = worst * 1.5
        floor_reason = (f"a comparable artifact already timed out at {worst:.0f}s, so the "
                        f"recommendation is raised above it")

    rec = max(_WALL_FLOOR_S, rec)
    capped = rec > _WALL_CEILING_S
    rec = min(_WALL_CEILING_S, rec)
    rec = math.ceil(rec / _WALL_ROUNDING_S) * _WALL_ROUNDING_S
    # The ceiling can clamp BELOW a cap already known to be insufficient. Say so plainly rather than
    # letting the floor_reason above imply a guarantee the returned number no longer keeps.
    if worst is not None and rec <= worst:
        floor_reason = (f"a comparable artifact already timed out at {worst:.0f}s, and the "
                        f"{_WALL_CEILING_S:.0f}s ceiling clamps the recommendation BELOW that — "
                        "this wall is not expected to be enough; split the artifact instead")

    if basis == "empirical":
        rationale = (f"from {sample_count} completed {backend}"
                     f"{'/' + model if model else ''} run(s) on this machine: "
                     f"p50 {p50:.0f}s, p90 {p90:.0f}s")
        if extrapolated:
            rationale += (f"; but this artifact (~{artifact_tokens} tokens) is far larger than "
                          f"those runs (~{median_tokens:.0f} tokens typical), so the shipped "
                          "estimate is used instead — there is no local evidence at this size")
    else:
        rationale = (f"no local history for {backend}{'/' + model if model else ''} yet "
                     f"({sample_count} completed run(s), need {_MIN_EMPIRICAL_SAMPLES}) — "
                     "using the shipped estimate, which is a padded starting point, not a "
                     "measurement of this account")
    if capped:
        rationale += (f"; capped at {_WALL_CEILING_S:.0f}s — past this, split the artifact rather "
                      "than wait longer")
    return {"recommended_wall_s": float(rec), "basis": basis, "sample_count": sample_count,
            "p50_s": p50, "p90_s": p90, "floor_reason": floor_reason, "rationale": rationale,
            "artifact_tokens_est": int(artifact_tokens)}
