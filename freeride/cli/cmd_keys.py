"""``freeride keys`` — show which provider keys are available vs cooling.

Reads ``~/.freeride/cooldown.json`` directly (no need for the gateway
to be running), cross-references with the per-provider env vars in the
current process, and prints a per-provider summary plus an optional
verbose per-key breakdown.

Privacy: actual key values are never printed. Keys are referenced by
their index in the env-var array (``k0``, ``k1``, ...) plus a short
hash prefix so the same key consistently maps to the same display id
across runs even if the user reorders their array.

Pairs with:
  - ``freeride watch`` — see live failover events as they happen
  - ``freeride providers`` — see per-provider health stats from the gateway
  - ``freeride doctor`` — sanity-check the env-var setup itself
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any


# Mirror cmd_serve / routes.chat env-var maps. Keep in sync.
_PROVIDER_ENV_VARS: list[tuple[str, str]] = [
    ("openrouter", "OPENROUTER_API_KEY"),
    ("groq", "GROQ_API_KEY"),
    ("nvidia_nim", "NVIDIA_API_KEY"),
    ("cloudflare_wai", "CLOUDFLARE_API_TOKEN"),
    ("huggingface", "HF_TOKEN"),  # HUGGINGFACE_API_KEY also accepted
    ("cerebras", "CEREBRAS_API_KEY"),
    ("ollama", "OLLAMA_BASE_URL"),
]


_COOLDOWN_PATH = Path.home() / ".freeride" / "cooldown.json"

# Match cooldown.COOLDOWN_TTL_SECONDS — keep in sync if that changes.
_COOLDOWN_TTL = 120


def _hash_id(secret: str) -> str:
    """Stable, non-reversible 8-char id for a secret."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:8]


def _parse_keys(raw: str) -> list[str]:
    """Same parser as v2compat.models._parse_api_keys — split JSON-array
    form OR fall through to a single-string key. Reimplemented here to
    avoid dragging the v2compat import path.
    """
    raw = (raw or "").strip()
    if not raw:
        return []
    if raw.startswith("[") and raw.endswith("]"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(k).strip() for k in parsed if str(k).strip()]
        except json.JSONDecodeError:
            pass
    return [raw]


def _env_keys_for(provider: str) -> list[str]:
    """Resolve the configured keys for a provider from env. HuggingFace
    accepts either HF_TOKEN or HUGGINGFACE_API_KEY (HF_TOKEN wins).
    """
    if provider == "huggingface":
        raw = os.environ.get("HF_TOKEN", "") or os.environ.get("HUGGINGFACE_API_KEY", "")
    else:
        env_var = next(v for p, v in _PROVIDER_ENV_VARS if p == provider)
        raw = os.environ.get(env_var, "")
    return _parse_keys(raw)


def _load_cooldown() -> dict[str, dict[str, float]]:
    """Read cooldown.json. Returns {} on any read/parse failure (the
    user might never have started the gateway yet).
    """
    try:
        with _COOLDOWN_PATH.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict[str, float]] = {}
    for prov, keys in data.items():
        if not isinstance(prov, str) or not isinstance(keys, dict):
            continue
        out[prov] = {k: float(v) for k, v in keys.items() if isinstance(k, str)}
    return out


def _key_status(
    *,
    cooldown_ts: float | None,
    now: float,
) -> tuple[str, int | None]:
    """('available', None) | ('cooling', remaining_seconds)."""
    if cooldown_ts is None:
        return "available", None
    elapsed = now - cooldown_ts
    if elapsed > _COOLDOWN_TTL:
        return "available", None  # expired but not yet evicted
    remaining = int(_COOLDOWN_TTL - elapsed)
    return "cooling", remaining


def collect_status(now: float) -> list[dict[str, Any]]:
    """Build a per-provider snapshot of (n_keys, available, cooling, soonest).

    Tested function — pure given (env, cooldown.json, now).
    """
    cd_state = _load_cooldown()
    out: list[dict[str, Any]] = []
    for provider, _ in _PROVIDER_ENV_VARS:
        keys = _env_keys_for(provider)
        if not keys:
            continue
        prov_cd = cd_state.get(provider, {})
        per_key: list[dict[str, Any]] = []
        for idx, key in enumerate(keys):
            ts = prov_cd.get(key)
            status, remaining = _key_status(cooldown_ts=ts, now=now)
            per_key.append({
                "index": idx,
                "hash": _hash_id(key),
                "status": status,
                "remaining_s": remaining,
            })
        n_cooling = sum(1 for k in per_key if k["status"] == "cooling")
        soonest = None
        cooling = [k for k in per_key if k["status"] == "cooling" and k["remaining_s"] is not None]
        if cooling:
            soonest = min(cooling, key=lambda k: k["remaining_s"])
        out.append({
            "provider": provider,
            "n_keys": len(keys),
            "n_available": len(keys) - n_cooling,
            "n_cooling": n_cooling,
            "per_key": per_key,
            "soonest_back": soonest,
        })
    return out


def format_summary(status: list[dict[str, Any]], *, no_color: bool, verbose: bool) -> str:
    if not status:
        return "no provider env vars set — run `freeride init` to configure keys."

    lines: list[str] = []
    headers = ("provider", "keys", "available", "cooling", "soonest back")
    name_w = max(len("provider"), max(len(s["provider"]) for s in status)) + 2
    widths = [name_w, 6, 11, 9, 14]
    lines.append("".join(h.ljust(w) for h, w in zip(headers, widths)))
    lines.append("─" * sum(widths))
    for s in status:
        soonest = s["soonest_back"]
        soonest_cell = (
            f"k{soonest['index']} in {soonest['remaining_s']}s"
            if soonest else "—"
        )
        cells = [
            s["provider"].ljust(widths[0]),
            str(s["n_keys"]).ljust(widths[1]),
            str(s["n_available"]).ljust(widths[2]),
            str(s["n_cooling"]).ljust(widths[3]),
            soonest_cell.ljust(widths[4]),
        ]
        lines.append("".join(cells))

    if verbose:
        lines.append("")
        for s in status:
            lines.append(f"{s['provider']}:")
            for k in s["per_key"]:
                if k["status"] == "cooling":
                    suffix = f"  cooling — {k['remaining_s']}s remaining"
                    if not no_color:
                        suffix = f"  \033[33mcooling\033[0m — {k['remaining_s']}s remaining"
                else:
                    suffix = "  available"
                    if not no_color:
                        suffix = "  \033[32mavailable\033[0m"
                lines.append(f"  k{k['index']} ({k['hash']}){suffix}")
            lines.append("")

    # Footer summary.
    total = sum(s["n_keys"] for s in status)
    cooling = sum(s["n_cooling"] for s in status)
    if cooling:
        lines.append(f"\n{cooling}/{total} keys cooling.")
    else:
        lines.append(f"\nall {total} keys available.")
    return "\n".join(lines)


def cmd_keys(args) -> int:
    no_color = bool(getattr(args, "no_color", False)) or not sys.stdout.isatty()
    verbose = bool(getattr(args, "verbose", False))
    import time

    status = collect_status(now=time.time())
    print(format_summary(status, no_color=no_color, verbose=verbose))
    return 0
