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
import sys
from pathlib import Path
from typing import Any

from freeride.core.cooldown import LEGACY_TTL_SECONDS, hash_key
from freeride.core.provider_env import BUILTIN_PROVIDERS, all_keys_for

_COOLDOWN_PATH = Path.home() / ".freeride" / "cooldown.json"

# Used by `_key_status` when the caller passes a legacy *start*
# timestamp. Matches the pre-hash on-disk format.
_COOLDOWN_TTL = int(LEGACY_TTL_SECONDS)


def _hash_id(secret: str) -> str:
    """Short display id — 8 chars, independent of the 12-char storage hash."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:8]


def _load_cooldown() -> dict[str, dict[str, Any]]:
    """Read cooldown.json. Returns {} on any read/parse failure."""
    try:
        with _COOLDOWN_PATH.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for prov, keys in data.items():
        if not isinstance(prov, str) or not isinstance(keys, dict):
            continue
        out[prov] = dict(keys)
    return out


def _until_of(entry: Any) -> float | None:
    if isinstance(entry, dict) and "until" in entry:
        try:
            return float(entry["until"])
        except (TypeError, ValueError):
            return None
    if isinstance(entry, (int, float)):
        return float(entry) + LEGACY_TTL_SECONDS
    return None


def _key_status(
    *,
    cooldown_ts: float | None,
    now: float,
) -> tuple[str, int | None]:
    """('available', None) | ('cooling', remaining_seconds).

    ``cooldown_ts`` is a *start* timestamp (legacy convention used by
    tests). Remaining = start + 120s - now.
    """
    if cooldown_ts is None:
        return "available", None
    elapsed = now - cooldown_ts
    if elapsed > _COOLDOWN_TTL:
        return "available", None
    remaining = int(_COOLDOWN_TTL - elapsed)
    return "cooling", remaining


def collect_status(now: float) -> list[dict[str, Any]]:
    """Build a per-provider snapshot of (n_keys, available, cooling, soonest)."""
    cd_state = _load_cooldown()
    out: list[dict[str, Any]] = []
    for spec in BUILTIN_PROVIDERS:
        provider = spec.name
        keys = all_keys_for(provider)
        if not keys:
            continue
        prov_cd = cd_state.get(provider, {})
        per_key: list[dict[str, Any]] = []
        for idx, key in enumerate(keys):
            entry = prov_cd.get(hash_key(key), prov_cd.get(key))
            until = _until_of(entry)
            if until is None:
                status, remaining = "available", None
            else:
                rem = int(until - now)
                if rem > 0:
                    status, remaining = "cooling", rem
                else:
                    status, remaining = "available", None
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

    from freeride.core.dotenv import load_dotenv_into_environ

    load_dotenv_into_environ()
    status = collect_status(now=time.time())
    print(format_summary(status, no_color=no_color, verbose=verbose))
    return 0
