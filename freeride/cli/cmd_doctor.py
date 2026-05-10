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


def run_checks() -> list[_Check]:
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
    checks.extend(_check_port_or_gateway())
    checks.append(_check_telemetry())
    return checks


def cmd_doctor(args) -> int:
    no_color = bool(getattr(args, "no_color", False)) or not sys.stdout.isatty()
    checks = run_checks()

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
