"""``freeride doctor`` — diagnose common setup issues.

Walks through the most-asked-about problems and prints a checklist:

  ✓ freeride is on PATH
  ✓ ~/.freeride/ exists and is writable
  ✓ python is 3.10+
  ! no provider env vars set — set at least one (see README)
  ✓ port 11343 is free (ready for `freeride serve`)
  ✓ http://localhost:11343/health responds (gateway already running)

Returns 0 if there are no errors (warnings are fine). Returns 1 if any
hard error blocks normal operation. Useful for "why isn't this working?"
debugging — one command instead of running 4 separate checks.
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
from pathlib import Path

import httpx


# ANSI escapes — single source so the formatter and tests agree.
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_DIM = "\033[2m"
_RESET = "\033[0m"


# Provider env var matrix. Keep in sync with cmd_serve.build_provider_registry().
_PROVIDER_ENV_VARS: list[tuple[str, list[str]]] = [
    ("openrouter", ["OPENROUTER_API_KEY"]),
    ("groq", ["GROQ_API_KEY"]),
    ("nvidia_nim", ["NVIDIA_API_KEY"]),
    ("cloudflare_wai", ["CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"]),
    ("huggingface", ["HF_TOKEN", "HUGGINGFACE_API_KEY"]),  # either accepted
    ("cerebras", ["CEREBRAS_API_KEY"]),
    ("ollama", ["OLLAMA_BASE_URL"]),
]


class _Check:
    """Single diagnostic line. Severity ranks: ok < warn < error."""

    __slots__ = ("severity", "label", "detail")

    def __init__(self, severity: str, label: str, detail: str = ""):
        assert severity in ("ok", "warn", "error", "info")
        self.severity = severity
        self.label = label
        self.detail = detail


def _check_freeride_on_path() -> _Check:
    if shutil.which("freeride"):
        return _Check("ok", "`freeride` is on PATH")
    return _Check(
        "warn",
        "`freeride` not on PATH",
        "use `python -m freeride` instead, or add ~/.local/bin to PATH",
    )


def _check_python_version() -> _Check:
    major, minor = sys.version_info[:2]
    if (major, minor) >= (3, 10):
        return _Check("ok", f"Python {major}.{minor} (>= 3.10)")
    return _Check(
        "error",
        f"Python {major}.{minor} is too old",
        "FreeRide requires Python >= 3.10. Upgrade or run via uv.",
    )


def _check_freeride_dir() -> _Check:
    p = Path.home() / ".freeride"
    if not p.exists():
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return _Check("error", "~/.freeride/ not writable", str(e))
        return _Check("ok", "~/.freeride/ created")
    # Existing — confirm writable.
    test = p / ".doctor-write-test"
    try:
        test.write_text("x")
        test.unlink()
    except OSError as e:
        return _Check("error", "~/.freeride/ exists but isn't writable", str(e))
    return _Check("ok", "~/.freeride/ exists and is writable")


def _check_provider_env_vars() -> list[_Check]:
    """One check per provider. ok if all required env vars are set,
    warn if some-but-not-all (CF needs both; HF accepts either),
    info if completely unset (don't need every provider).
    """
    out: list[_Check] = []
    any_set = False
    for provider, env_vars in _PROVIDER_ENV_VARS:
        if provider == "huggingface":
            # HF accepts either env var.
            set_var = next((v for v in env_vars if os.environ.get(v)), None)
            if set_var:
                any_set = True
                out.append(_Check("ok", f"{provider}: ${set_var} set"))
            else:
                out.append(_Check("info", f"{provider}: no env var set", f"set ${env_vars[0]} to enable"))
            continue

        unset = [v for v in env_vars if not os.environ.get(v)]
        if not unset:
            any_set = True
            out.append(_Check("ok", f"{provider}: " + ", ".join(f"${v} set" for v in env_vars)))
        elif len(unset) == len(env_vars):
            out.append(
                _Check("info", f"{provider}: not configured", f"set ${env_vars[0]} to enable"
                + (f" (also needs ${env_vars[1]})" if len(env_vars) > 1 else ""))
            )
        else:
            out.append(
                _Check("warn", f"{provider}: partially configured",
                       f"missing: {', '.join('$' + v for v in unset)}")
            )

    if not any_set:
        out.append(_Check(
            "error",
            "no provider env vars set",
            "set at least one (e.g. OPENROUTER_API_KEY) — get a free key at https://openrouter.ai/keys",
        ))
    return out


def _check_port_or_gateway(port: int = 11343) -> list[_Check]:
    """Either the port is free (gateway can start) OR the gateway is
    already running (we can reach /health). Anything else is a problem.
    """
    out: list[_Check] = []
    # Try a quick TCP probe. If something answers, see if it's our gateway.
    in_use = False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            in_use = True

    if not in_use:
        return [_Check("ok", f"port {port} is free (ready for `freeride serve`)")]

    # Port is in use — is it our gateway?
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2.0)
    except httpx.HTTPError:
        return [_Check(
            "warn",
            f"port {port} is in use but doesn't respond to /health",
            "another process is bound there — pick a different --port or stop it",
        )]
    if r.status_code == 200:
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        version = body.get("version", "?")
        provs = body.get("providers", [])
        out.append(_Check(
            "ok",
            f"gateway already running on port {port}",
            f"version={version} providers={len(provs)}",
        ))
    else:
        out.append(_Check(
            "warn",
            f"port {port} responds but with HTTP {r.status_code}",
            "may be a different service",
        ))
    return out


def _format_check(c: _Check, *, no_color: bool) -> str:
    glyph = {"ok": "✓", "warn": "!", "error": "✗", "info": "·"}[c.severity]
    color = {"ok": _GREEN, "warn": _YELLOW, "error": _RED, "info": ""}[c.severity]
    if no_color:
        line = f"  {glyph} {c.label}"
    else:
        reset = _RESET if color else ""
        line = f"  {color}{glyph}{reset} {c.label}"
    if c.detail:
        if no_color:
            line += f"\n      {c.detail}"
        else:
            line += f"\n      {_DIM}{c.detail}{_RESET}"
    return line


def _check_telemetry() -> _Check:
    """Surface telemetry status so users see what's being sent and can
    confirm the install-event / hourly-beacon loop is working.

    Reports: enabled/disabled, install_id (truncated to first 8 chars
    for privacy in screenshots), and the path of the persisted id.
    Errors here are non-fatal — telemetry is opt-in.
    """
    try:
        from freeride.core import telemetry

        enabled = telemetry.is_enabled()
        if not enabled:
            return _Check(
                "info",
                "telemetry: off",
                "opted out via `freeride telemetry off`",
            )
        try:
            iid = telemetry.installation_id()
            short = iid[:8] if iid else "?"
        except Exception as e:
            return _Check(
                "warn",
                "telemetry: enabled but installation_id read failed",
                f"{e}",
            )
        return _Check(
            "ok",
            f"telemetry: on  (install_id={short}…)",
            f"endpoint {telemetry.beacon_url()} · "
            f"audit: `freeride telemetry`",
        )
    except Exception as e:
        return _Check(
            "warn",
            "telemetry: status check failed",
            f"{e}",
        )


def _check_freeride_active_marker() -> _Check:
    """``freeride run`` stamps FREERIDE_ACTIVE=1 in the child env. If
    we see it, the user is inside the wrapper — show that so they
    know which lens to read the rest of the report through."""
    if os.environ.get("FREERIDE_ACTIVE") == "1":
        return _Check(
            "ok",
            "inside `freeride run` (FREERIDE_ACTIVE=1)",
            "ANTHROPIC_BASE_URL was set by the wrapper",
        )
    return _Check(
        "info",
        "not inside `freeride run`",
        "to opt in: `freeride run claude`",
    )


def _check_anthropic_base_url(port: int = 11343) -> _Check:
    """Is ANTHROPIC_BASE_URL pointed at a reachable gateway?

    Three cases:
      - unset → info (user hasn't bound; that's fine if they're
        about to run `freeride run`)
      - set to a URL we can probe → ok or warn depending on whether
        /health answers
      - set to an upstream we shouldn't proxy (e.g. api.anthropic.com)
        → warn that the gateway isn't in the path
    """
    url = os.environ.get("ANTHROPIC_BASE_URL")
    if not url:
        return _Check(
            "info",
            "ANTHROPIC_BASE_URL not set",
            "Claude Code will hit api.anthropic.com directly",
        )
    if "api.anthropic.com" in url:
        return _Check(
            "warn",
            f"ANTHROPIC_BASE_URL = {url}",
            "this bypasses FreeRide — drop the env var or use `freeride run`",
        )
    try:
        health = httpx.get(url.rstrip("/") + "/health", timeout=2.0)
    except httpx.HTTPError as e:
        return _Check(
            "error",
            f"ANTHROPIC_BASE_URL = {url} (unreachable)",
            f"/health failed: {type(e).__name__}",
        )
    if 200 <= health.status_code < 300:
        return _Check("ok", f"ANTHROPIC_BASE_URL = {url} (gateway reachable)")
    return _Check(
        "warn",
        f"ANTHROPIC_BASE_URL = {url} (responds, but /health → {health.status_code})",
        "may not be a freeride gateway",
    )


def _check_claude_cli_on_path() -> _Check:
    """Is the Claude Code CLI installed? Not required (user could be
    using `@anthropic-ai/sdk` directly), but informational — when
    it's there we can show the version, which matters because
    2.x has the hardcoded OAuth gate (160.79.104.10) that needs
    a workaround."""
    claude_bin = shutil.which("claude")
    if not claude_bin:
        return _Check(
            "info",
            "`claude` CLI not on PATH",
            "install via `npm i -g @anthropic-ai/claude-code`",
        )
    # Try to get the version; non-fatal if it fails.
    import subprocess

    try:
        out = subprocess.run(
            [claude_bin, "--version"], capture_output=True, text=True, timeout=5
        )
        version = (out.stdout or out.stderr or "").strip().splitlines()[0] if out.returncode == 0 else "?"
    except (OSError, subprocess.SubprocessError):
        version = "?"
    return _Check("ok", f"claude CLI: {version}", f"at {claude_bin}")


def _check_claude_routing() -> _Check:
    """Spot-check the routing decision module: claude-* + auth should
    passthrough; freeride/* should route free. If this drifts the
    whole point of Phase 4 is broken — surface it loudly."""
    from freeride.core.model_router import decide

    d1 = decide("claude-sonnet-4-6", {"authorization": "Bearer test"})
    d2 = decide("freeride/free", {})
    d3 = decide("claude-opus-4-5", {})  # no auth
    if (
        d1.mode == "passthrough"
        and d2.mode == "free"
        and d3.mode == "free"
    ):
        return _Check(
            "ok",
            "routing decision module is sane",
            "claude-*+auth→passthrough, freeride/*→free, claude-*-no-auth→free",
        )
    return _Check(
        "error",
        "routing decision module is broken",
        f"claude+auth={d1.mode!r}, freeride={d2.mode!r}, claude-no-auth={d3.mode!r}",
    )


def _check_freeride_free_via_gateway(port: int = 11343) -> _Check:
    """Live probe: if the gateway is reachable, POST a minimal
    freeride/free request and confirm we get a 200. Skipped (info)
    when no gateway is up — we don't auto-start one for the probe."""
    base = (
        os.environ.get("ANTHROPIC_BASE_URL", "").rstrip("/")
        or f"http://127.0.0.1:{port}"
    )
    if "api.anthropic.com" in base:
        return _Check(
            "info",
            "free-route live probe: skipped",
            "ANTHROPIC_BASE_URL points at Anthropic; can't probe free route",
        )
    try:
        h = httpx.get(base + "/health", timeout=1.0)
        if not (200 <= h.status_code < 300):
            return _Check(
                "info",
                "free-route live probe: skipped (no gateway)",
                f"start one with `freeride serve --port {port}`",
            )
    except httpx.HTTPError:
        return _Check(
            "info",
            "free-route live probe: skipped (no gateway)",
            f"start one with `freeride serve --port {port}`",
        )
    # Gateway is up — fire one tiny request.
    try:
        r = httpx.post(
            base + "/v1/messages",
            json={
                "model": "freeride/free",
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
            },
            timeout=30.0,
        )
    except httpx.HTTPError as e:
        return _Check(
            "warn",
            "free-route live probe failed (transport)",
            f"{type(e).__name__}: {e}",
        )
    if r.status_code != 200:
        return _Check(
            "warn",
            f"free-route live probe → HTTP {r.status_code}",
            (r.text[:160] + "…") if len(r.text) > 160 else r.text,
        )
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    provider = r.headers.get("X-FreeRide-Provider", "?")
    content = body.get("content", [])
    text_snippet = ""
    if content and isinstance(content, list) and content[0].get("type") == "text":
        text_snippet = (content[0].get("text") or "")[:60]
    return _Check(
        "ok",
        f"free-route live probe → 200 via {provider}",
        f"response: {text_snippet!r}",
    )


def run_checks(*, claude_code: bool = False, port: int = 11343) -> list[_Check]:
    # Mirror what `freeride serve` does — load ~/.freeride/.env BEFORE
    # checking provider env vars so doctor agrees with the gateway's
    # view of the world. OS env wins; we only fill gaps.
    from freeride.core.dotenv import load_dotenv_into_environ

    load_dotenv_into_environ()

    checks: list[_Check] = []
    checks.append(_check_python_version())
    checks.append(_check_freeride_on_path())
    checks.append(_check_freeride_dir())
    checks.extend(_check_provider_env_vars())
    checks.extend(_check_port_or_gateway(port=port))
    checks.append(_check_telemetry())

    if claude_code:
        checks.append(_Check("info", "── Claude Code integration ──"))
        checks.append(_check_freeride_active_marker())
        checks.append(_check_anthropic_base_url(port=port))
        checks.append(_check_claude_cli_on_path())
        checks.append(_check_claude_routing())
        checks.append(_check_freeride_free_via_gateway(port=port))

    return checks


def cmd_doctor(args) -> int:
    no_color = bool(getattr(args, "no_color", False)) or not sys.stdout.isatty()
    claude_code = bool(getattr(args, "claude_code", False))
    port = int(getattr(args, "port", 11343))
    checks = run_checks(claude_code=claude_code, port=port)

    print("FreeRide doctor")
    print()
    for c in checks:
        print(_format_check(c, no_color=no_color))
    print()

    n_error = sum(1 for c in checks if c.severity == "error")
    n_warn = sum(1 for c in checks if c.severity == "warn")
    n_ok = sum(1 for c in checks if c.severity == "ok")

    if n_error:
        summary = f"{n_error} error{'s' if n_error != 1 else ''}, {n_warn} warning{'s' if n_warn != 1 else ''}"
        if not no_color:
            summary = f"{_RED}{summary}{_RESET}"
        print(summary)
        return 1
    if n_warn:
        summary = f"{n_warn} warning{'s' if n_warn != 1 else ''}, otherwise OK"
        if not no_color:
            summary = f"{_YELLOW}{summary}{_RESET}"
        print(summary)
        return 0
    summary = f"all good — {n_ok} checks passed"
    if not no_color:
        summary = f"{_GREEN}{summary}{_RESET}"
    print(summary)
    return 0
