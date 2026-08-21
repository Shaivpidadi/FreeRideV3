"""Default-on anonymous aggregate telemetry beacon.

**Default: ON.** A first-run disclosure banner prints whenever telemetry
is enabled and the user has not yet acknowledged. Opt out:
``freeride telemetry off``.

What gets sent (only when enabled, hourly):

.. code-block:: json

    {
      "installation_id": "uuid-v4",
      "version": "0.3.0.dev0",
      "os": "darwin",
      "tokens_served": 412034,
      "request_count": 187,
      "providers_active": ["openrouter", "nvidia_nim"],
      "uptime_hours": 8
    }

What NEVER gets sent: prompts, completions, model IDs, API keys,
hostnames, IP (the HTTPS request reveals an IP at the network layer;
the FreeRide endpoint discards it server-side).

This module owns the data plumbing. The beacon scheduler that POSTs
on a 1h tick lives in :mod:`freeride.server.app` (`_telemetry_loop`).

the design plan is the canonical spec.
"""

from __future__ import annotations

import json
import os
import platform
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from freeride.core.state import atomic_write, read_json_or, write_json_atomic

# ----- paths ---------------------------------------------------------------

CONFIG_DIR = Path.home() / ".freeride"
CONFIG_FILE = CONFIG_DIR / "config.json"
INSTALLATION_FILE = CONFIG_DIR / "installation_id"
STATS_FILE = CONFIG_DIR / "stats.json"

# Beacon endpoint. Hosted under free-ride.xyz; backend lives at
# services/telemetry/ in this repo (Cloudflare Worker + D1). Override
# via FREERIDE_TELEMETRY_ENDPOINT env var for tests or for users who
# want to redirect to their own self-hosted observability.
DEFAULT_BEACON_URL = "https://telemetry.free-ride.xyz/v1/beacon"


def beacon_url() -> str:
    return os.environ.get("FREERIDE_TELEMETRY_ENDPOINT", DEFAULT_BEACON_URL)


# ----- installation id -----------------------------------------------------

def installation_id() -> str:
    """Return this installation's persistent random UUIDv4. Generated on
    first call; persisted to ``~/.freeride/installation_id``. Resettable
    by deleting the file.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if INSTALLATION_FILE.exists():
        text = INSTALLATION_FILE.read_text().strip()
        if text:
            return text
    new = str(uuid.uuid4())
    atomic_write(INSTALLATION_FILE, new)
    return new


# ----- opt-in state --------------------------------------------------------

def is_enabled() -> bool:
    """True unless the user has explicitly opted out via
    ``freeride telemetry off``. Default-on; honest disclosure handled
    by :func:`should_show_disclosure` + first-run banner.
    """
    cfg = read_json_or(CONFIG_FILE, {})
    if not isinstance(cfg, dict):
        return True
    # Explicit False = user opted out. Anything else (missing key, True,
    # or garbage) means default-on.
    return cfg.get("telemetry") is not False


def set_enabled(enabled: bool) -> None:
    """Persist the on/off state. Reads the existing config, sets the
    telemetry key, atomic-writes back. Other config keys round-trip.
    """
    cfg = read_json_or(CONFIG_FILE, {})
    if not isinstance(cfg, dict):
        cfg = {}
    cfg["telemetry"] = bool(enabled)
    write_json_atomic(CONFIG_FILE, cfg, indent=2)


def should_show_disclosure() -> bool:
    """True when the first-run banner should print: telemetry is on
    AND the user has not yet acknowledged seeing the disclosure."""
    if not is_enabled():
        return False
    cfg = read_json_or(CONFIG_FILE, {})
    if not isinstance(cfg, dict):
        return True
    return not cfg.get("telemetry_disclosure_shown")


def mark_disclosure_shown() -> None:
    """Persist that the disclosure banner has been shown to this install
    so it doesn't print on every command. Stored in
    ``~/.freeride/config.json`` alongside the telemetry flag.
    """
    cfg = read_json_or(CONFIG_FILE, {})
    if not isinstance(cfg, dict):
        cfg = {}
    cfg["telemetry_disclosure_shown"] = True
    write_json_atomic(CONFIG_FILE, cfg, indent=2)


DISCLOSURE_BANNER = """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FreeRide telemetry: ENABLED (default).

Sent hourly to {endpoint} (silent on failure):
  installation_id, version, os, tokens_served, request_count,
  providers_active, uptime_hours

Never sent: prompts, completions, model IDs, API keys, hostname, IP.

  Audit payload:  freeride telemetry
  Opt out:        freeride telemetry off

This banner shows once. Configure under ~/.freeride/config.json.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""


def show_disclosure_banner_once() -> None:
    """If telemetry is on and disclosure unseen, print the banner and
    persist that we did. Idempotent — safe to call from every CLI
    command's entry point.
    """
    if not should_show_disclosure():
        return
    print(DISCLOSURE_BANNER.format(endpoint=beacon_url()))
    mark_disclosure_shown()


# ----- payload -------------------------------------------------------------

@dataclass(frozen=True)
class Stats:
    """Aggregated counters surfaced in the beacon. Always-on locally,
    written to ``~/.freeride/stats.json`` by the gateway when running.
    Telemetry reads them and ships hourly when opted in.

    ``input_tokens`` and ``output_tokens`` track the prompt vs.
    completion split per response and are the values modern gateways
    actually report. ``tokens_served`` is preserved as their sum so
    older Worker code paths keep computing the same totals during the
    transition; it can be removed once we no longer surface the
    legacy field on /v1/stats.
    """

    tokens_served: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    request_count: int = 0
    providers_active: tuple[str, ...] = ()
    uptime_hours: int = 0

    @classmethod
    def load(cls) -> "Stats":
        raw = read_json_or(STATS_FILE, {})
        if not isinstance(raw, dict):
            raw = {}
        providers = raw.get("providers_active") or []
        if not isinstance(providers, list):
            providers = []
        input_t = int(raw.get("input_tokens", 0) or 0)
        output_t = int(raw.get("output_tokens", 0) or 0)
        # ``tokens_served`` was the only field before the input/output
        # split landed; load it as the back-compat default for any
        # stats.json files written by older gateways.
        legacy = int(raw.get("tokens_served", 0) or 0)
        return cls(
            tokens_served=max(legacy, input_t + output_t),
            input_tokens=input_t,
            output_tokens=output_t,
            request_count=int(raw.get("request_count", 0) or 0),
            providers_active=tuple(p for p in providers if isinstance(p, str)),
            uptime_hours=int(raw.get("uptime_hours", 0) or 0),
        )


def record_request(
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    tokens: int | None = None,
    provider: str | None = None,
) -> None:
    """Bump the local counters that the beacon will ship.

    Called from every route's success path. Writes ``~/.freeride/stats.json``
    atomically with the new totals. Safe to call from concurrent requests:
    the read-modify-write window is microseconds and ``atomic_write`` does
    a tmp+rename so a torn write can't leave partial JSON on disk.

    * ``input_tokens`` / ``output_tokens`` — prompt vs. completion tokens
      for this response. Pulled from the provider's response (or final
      stream chunk). Both default to 0 so callers that legitimately don't
      know — early failure paths, providers that don't expose usage —
      can still call this to bump ``request_count``.

    * ``tokens`` — legacy combined counter. Accepted for back-compat with
      older route code that hasn't been migrated to the split fields yet.
      If passed, it goes into ``tokens_served`` directly; input/output
      stay 0 for that request. Don't pass both ``tokens`` and the split
      fields — the split fields win.

    * ``provider`` — the resolved provider that served the request, added
      to ``providers_active`` (deduplicated).

    Local-only — does NOT involve any network call. The beacon ships
    these later, on its own schedule, only when telemetry is opted in. A
    user with ``freeride telemetry off`` still gets correct local
    counters; they just never leave the machine.
    """
    if input_tokens < 0:
        input_tokens = 0
    if output_tokens < 0:
        output_tokens = 0
    if input_tokens or output_tokens:
        # Split fields take precedence; ``tokens_served`` is computed
        # from them so the legacy column on the Worker stays consistent
        # with the new split columns.
        delta_total = input_tokens + output_tokens
    else:
        delta_total = max(int(tokens or 0), 0)

    try:
        existing = read_json_or(STATS_FILE, {})
        if not isinstance(existing, dict):
            existing = {}

        prev_tokens = int(existing.get("tokens_served", 0) or 0)
        prev_input = int(existing.get("input_tokens", 0) or 0)
        prev_output = int(existing.get("output_tokens", 0) or 0)
        prev_count = int(existing.get("request_count", 0) or 0)
        prev_providers = existing.get("providers_active") or []
        if not isinstance(prev_providers, list):
            prev_providers = []
        providers_set = {p for p in prev_providers if isinstance(p, str)}
        if provider:
            providers_set.add(provider)

        existing["tokens_served"] = prev_tokens + delta_total
        existing["input_tokens"] = prev_input + input_tokens
        existing["output_tokens"] = prev_output + output_tokens
        existing["request_count"] = prev_count + 1
        existing["providers_active"] = sorted(providers_set)
        # Preserve uptime_hours and any other field a future writer adds.
        write_json_atomic(STATS_FILE, existing)
    except OSError as e:
        # Disk full, permission, etc. Don't fail the user's request because
        # we couldn't write a counter — log and continue.
        import logging

        logging.getLogger(__name__).warning(
            "record_request: could not update %s: %s", STATS_FILE, e
        )


def build_payload(*, version: str | None = None) -> dict[str, Any]:
    """Build the telemetry beacon payload from current local state.

    ``version`` defaults to ``freeride.__version__``; allow injection
    so the test module can pin it.
    """
    if version is None:
        # Late import to avoid a circular at module-import time
        from freeride import __version__ as _v

        version = _v
    s = Stats.load()
    return {
        "installation_id": installation_id(),
        "version": version,
        "os": _normalized_os(),
        # Always include the legacy combined field so older receivers
        # keep working; new field names sit alongside it.
        "tokens_served": s.tokens_served,
        "input_tokens": s.input_tokens,
        "output_tokens": s.output_tokens,
        "request_count": s.request_count,
        "providers_active": list(s.providers_active),
        "uptime_hours": s.uptime_hours,
    }


def _normalized_os() -> str:
    """Map ``platform.system()`` to {darwin, linux, windows, other}."""
    sys_name = platform.system().lower()
    if sys_name == "darwin":
        return "darwin"
    if sys_name == "linux":
        return "linux"
    if sys_name == "windows":
        return "windows"
    return "other"


# ----- beacon ship ---------------------------------------------------------

def ship_beacon(*, timeout: float = 5.0) -> bool:
    """POST the current payload to the beacon endpoint.

    Returns True on a 2xx, False on any non-2xx, network failure, or
    when telemetry is disabled. Failures are silent — they never raise
    or block real traffic.
    """
    if not is_enabled():
        return False
    try:
        import httpx
    except ImportError:
        return False

    payload = build_payload()
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(beacon_url(), json=payload)
        return 200 <= resp.status_code < 300
    except Exception:
        return False


# ----- payload preview (for `freeride telemetry`) --------------------------

def preview_payload(*, indent: int = 2) -> str:
    """Render the payload as JSON for inspection. Used by the no-args
    `freeride telemetry` command so users can audit exactly what we'd
    ship before deciding to opt in.
    """
    return json.dumps(build_payload(), indent=indent, sort_keys=True)
