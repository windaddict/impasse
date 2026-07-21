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
KNOWN_HOSTS = ("claude", "codex", "gemini", "cursor", "other")
# Hosts attributable to a single model provider. cursor/other run an operator-selected
# underlying model (a Cursor agent may BE Claude or GPT), so no provider can be attributed.
_HOST_PROVIDERS = {"claude": "Anthropic", "codex": "OpenAI", "gemini": "Google"}
# Providers that can appear as a REVIEWER BACKEND. Only OpenAI (codex) and Anthropic (claude) ship
# a backend, so Google is intentionally absent here even though it's a known HOST provider above —
# adding it would be dead code (independence_tier reads the host provider from _HOST_PROVIDERS).
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


def independence_notice(tier: str, host: str, backend_name: str, provider: str,
                        confidence: str | None = None) -> str | None:
    """The mandatory disclosure for a tier, or None when none is owed. ONE formatter shared by
    review() and review_mode(), so no surface that reports a downgraded — or heuristically-detected —
    tier can forget the notice that must ride with it.

    `confidence` is the host_detection() confidence. A cross_provider tier owes no *downgrade* notice,
    but when it rests on a HEURISTIC host detection (today: codex via sandbox-state vars, no branded
    flag) it carries a SOFT notice so a guessed positive claim can't read as a confirmed one."""
    if tier == "cross_provider":
        if confidence == "heuristic":
            return (
                f"⚠ Cross-provider label INFERRED from a heuristic: the '{host}' host was detected "
                f"from a sandbox-state condition, not a branded identity flag (reviewer '{backend_name}' "
                f"via {provider}). The independence is likely real but not firmly established — a "
                "sandbox-bypassed run would be undetectable. Set IMPASSE_HOST=" + host + " to confirm it."
            )
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


def _settings_lock():
    """An interprocess lock guarding the settings read-modify-write, so two hosts (e.g. a Claude Code
    and a Codex host sharing one config dir) can't lose an update via interleaved read-modify-replace
    (core-review F005). POSIX flock (like the process-group teardown, POSIX-only); a no-op context on
    platforms without fcntl. Returns a context manager."""
    ensure_config_dir()
    lock_path = os.path.join(config_dir(), "settings.lock")
    try:
        import fcntl
    except ImportError:
        import contextlib
        return contextlib.nullcontext()

    class _Lock:
        def __enter__(self):
            self._fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(self._fd, fcntl.LOCK_EX)
            return self

        def __exit__(self, *exc):
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
    return _Lock()


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


def save_run_doc(run_id: str, name: str, doc: dict) -> str:
    """Persist one run document (name = 'reviewer-response' | 'reconciliation-result').
    The initial reviewer-response should use a run_id from reserve_run_id() so it can't clobber another
    run; reconciliation reuses that same run_id to land in the same directory."""
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

    def _load(p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.loads(f.read(_MAX_STORE_BYTES))
        except (OSError, ValueError):   # JSONDecodeError + UnicodeDecodeError are ValueError subclasses
            return None

    return {
        "run_id": _safe_id(run_id),
        "reviewer_response": _load(os.path.join(d, "reviewer-response.json")),
        "reconciliation_result": _load(os.path.join(d, "reconciliation-result.json")),
    }


def forget_run(run_id: str) -> bool:
    d = _run_dir(run_id)
    # Don't rmtree THROUGH a symlinked record dir, and report success only if it's actually gone.
    if os.path.isdir(d) and not os.path.islink(d):
        shutil.rmtree(d, ignore_errors=True)
        return not os.path.exists(d)
    return False
