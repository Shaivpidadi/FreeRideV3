"""Per-model runtime health cache.

The catalog at ``GET /v1/models`` is built from each provider's
``list_free_models()`` response. Most of those entries are valid, but
some are ghosts — provider catalog endpoints occasionally advertise
ids the inference API itself rejects with ``model_not_found``, or the
account behind the key is in a depleted state and every model returns
``quota_exhausted``. The naive auto-resolver hands those out anyway,
which produces wasted failover walks and visible latency.

This module is the on-disk companion to :func:`Provider.probe`. It
runs each provider's probe against its catalog, records the result,
and persists the verdict so future ``model: "auto"`` resolution can
skip known-broken models without re-probing on the request hot path.

Storage shape (``~/.freeride/cache/model_health.json``):

    {
      "as_of": <unix_seconds>,
      "ttl_sec": 86400,
      "results": {
        "<provider>::<model_id>": {
          "status": "ok" | "model_not_found" | "rate_limit" | ...,
          "latency_ms": 274,
          "checked_at": <unix_seconds>
        },
        ...
      }
    }

The cache is consumed by :mod:`freeride.core.smart_routing` — see
``score_model`` for the integration. Cold reads of the cache are
cheap (one JSON parse on first call); we don't keep an in-memory
mirror because the file is small (a few KB) and the resolver is not
hot-loop-frequent.
"""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from freeride.core.errors import ErrorKind

if TYPE_CHECKING:
    from freeride.core.provider import Provider


logger = logging.getLogger(__name__)


CACHE_PATH = Path.home() / ".freeride" / "cache" / "model_health.json"
CACHE_TTL_SEC = 24 * 3600  # 24h — daily-cap providers (OR free) recover sooner


# Statuses that should disqualify a model from auto-resolution. Note
# that RATE_LIMIT is *included* — a rate-limited model isn't broken
# forever, but the cache TTL is short enough (1 day) that the next
# audit will lift the flag once daily-cap windows reset.
_BROKEN_STATUSES: frozenset[str] = frozenset(
    {
        "model_not_found",
        "quota_exhausted",
        "auth",
        "unavailable",
        "timeout",
        "rate_limit",
        "unknown",
    }
)


def _key(provider: str, model_id: str) -> str:
    return f"{provider}::{model_id}"


# ─── cache I/O ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class HealthEntry:
    status: str
    latency_ms: int
    checked_at: int


def load_cache() -> dict[str, HealthEntry]:
    """Read the persisted health cache. Returns ``{}`` if the file is
    missing, corrupt, or older than ``CACHE_TTL_SEC``. Always safe to
    call — never raises on bad input.
    """
    try:
        if not CACHE_PATH.exists():
            return {}
        raw = json.loads(CACHE_PATH.read_text())
        if not isinstance(raw, dict):
            return {}
        as_of = raw.get("as_of")
        ttl = raw.get("ttl_sec", CACHE_TTL_SEC)
        results = raw.get("results")
        if not isinstance(as_of, (int, float)) or not isinstance(results, dict):
            return {}
        if time.time() - as_of > ttl:
            return {}
        out: dict[str, HealthEntry] = {}
        for k, v in results.items():
            if not isinstance(k, str) or not isinstance(v, dict):
                continue
            status = v.get("status")
            if not isinstance(status, str):
                continue
            out[k] = HealthEntry(
                status=status,
                latency_ms=int(v.get("latency_ms") or 0),
                checked_at=int(v.get("checked_at") or 0),
            )
        return out
    except (OSError, ValueError, TypeError) as e:
        logger.debug("model_health: cache load failed: %s", e)
        return {}


def save_cache(results: dict[str, HealthEntry]) -> None:
    """Persist the health cache atomically (write-then-rename so a
    partial write never leaves the file in a half-parsed state)."""
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "as_of": int(time.time()),
            "ttl_sec": CACHE_TTL_SEC,
            "results": {
                k: {
                    "status": v.status,
                    "latency_ms": v.latency_ms,
                    "checked_at": v.checked_at,
                }
                for k, v in results.items()
            },
        }
        tmp = CACHE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(CACHE_PATH)
    except OSError as e:
        logger.warning("model_health: cache save failed: %s", e)


# ─── lookup helpers (consumed by smart_routing) ───────────────────


def is_model_known_broken(
    provider: str,
    model_id: str,
    cache: dict[str, HealthEntry] | None = None,
) -> bool:
    """True if the cache marks (provider, model_id) as not-currently-
    routable. ``cache=None`` causes a fresh cache load — fine for
    one-off lookups; performance-sensitive callers should load the
    cache once and reuse it.
    """
    if cache is None:
        cache = load_cache()
    e = cache.get(_key(provider, model_id))
    if e is None:
        return False
    return e.status in _BROKEN_STATUSES


# ─── audit core ───────────────────────────────────────────────────


def _classify(error_kind: ErrorKind | None, ok: bool) -> str:
    if ok:
        return "ok"
    if error_kind is None:
        return "unknown"
    return error_kind.value


def _probe_one(
    provider: "Provider",
    model_id: str,
    key: str,
) -> tuple[str, HealthEntry]:
    try:
        result = provider.probe(model_id, key)
    except Exception as e:  # noqa: BLE001 — provider.probe is third-party
        logger.debug(
            "model_health: probe raised for %s/%s: %s", provider.name, model_id, e
        )
        return _key(provider.name, model_id), HealthEntry(
            status="unknown",
            latency_ms=0,
            checked_at=int(time.time()),
        )
    status = _classify(result.error, result.ok)
    return _key(provider.name, model_id), HealthEntry(
        status=status,
        latency_ms=result.latency_ms,
        checked_at=int(time.time()),
    )


def audit_providers(
    providers: Iterable["Provider"],
    keys_for: dict[str, str],
    *,
    workers: int = 4,
    on_progress: Any = None,
) -> dict[str, HealthEntry]:
    """Probe every model in every provider's catalog, returning a fresh
    health map. ``keys_for`` maps provider name → API key.

    Skips providers whose name isn't in ``keys_for``.

    ``on_progress(provider, model_id, entry)`` (optional) is called
    after each probe completes — for CLI status lines.
    """
    out: dict[str, HealthEntry] = {}

    targets: list[tuple["Provider", str, str]] = []
    for p in providers:
        key = keys_for.get(p.name)
        if not key:
            logger.info("model_health: no key for %s, skipping", p.name)
            continue
        try:
            models = p.list_free_models(key)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "model_health: list_free_models raised for %s: %s", p.name, e
            )
            continue
        for m in models:
            targets.append((p, m.api_id, key))

    if not targets:
        return out

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = {
            ex.submit(_probe_one, p, mid, k): (p, mid)
            for (p, mid, k) in targets
        }
        for fut in as_completed(futures):
            cache_key, entry = fut.result()
            out[cache_key] = entry
            if on_progress is not None:
                p, mid = futures[fut]
                try:
                    on_progress(p.name, mid, entry)
                except Exception:  # noqa: BLE001
                    pass

    return out
