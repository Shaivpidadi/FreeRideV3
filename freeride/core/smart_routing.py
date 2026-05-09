"""Smart auto-model routing.

When a request arrives with ``model: "auto"`` the resolver in
:mod:`freeride.core.auto_model` has to pick *something* from the live
catalog. The naïve version walks the catalog in registration order
and returns the first entry whose providers have non-cooled keys.
That works but ignores everything we know about which models are
actually performing well in the wild.

This module adds a soft popularity signal sourced from the public
``/v1/stats`` endpoint on ``api.free-ride.xyz``. That endpoint
publishes per-model token totals scraped from OpenRouter's app
activity pages, aggregated across every FreeRide install on Earth
that's routed through OR — i.e. it's the closest thing FreeRide
has to a community-wide "what's working" signal.

We pull it on demand, cache it on disk for an hour, and use it as
ONE of several inputs to a score function. The score is a weighted
sum of:

  - failover headroom (more providers serving the same model = better)
  - global popularity (the leaderboard signal)

Catalog order is preserved as the implicit tiebreaker by virtue of
Python's stable sort.

If the leaderboard fetch fails (network down, ``api.free-ride.xyz``
unreachable, malformed response, etc.) we degrade gracefully to a
provider-count-only score — i.e. exactly the old behavior, no
visible regression. Smart routing is a strict superset of the naïve
path, never a hard dependency.
"""

from __future__ import annotations

import json
import logging
import math
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


logger = logging.getLogger(__name__)


_STATS_URL = "https://api.free-ride.xyz/v1/stats"
_CACHE_PATH = Path.home() / ".freeride" / "cache" / "leaderboard.json"
_CACHE_TTL_SEC = 3600  # 1 hour
_FETCH_TIMEOUT_SEC = 2.0  # cheap; resolver is on the request hot path


def _read_cache() -> dict[str, int] | None:
    """Return a cached leaderboard if present and fresh, else None."""
    try:
        if not _CACHE_PATH.exists():
            return None
        raw = json.loads(_CACHE_PATH.read_text())
        if not isinstance(raw, dict):
            return None
        as_of = raw.get("as_of")
        models = raw.get("models")
        if not isinstance(as_of, (int, float)) or not isinstance(models, dict):
            return None
        if time.time() - as_of > _CACHE_TTL_SEC:
            return None
        # Coerce to {str: int} defensively.
        return {str(k): int(v) for k, v in models.items() if isinstance(v, (int, float))}
    except (OSError, ValueError, TypeError):
        return None


def _write_cache(models: dict[str, int]) -> None:
    """Persist the leaderboard so subsequent processes / requests don't
    re-fetch. Best-effort; cache write failure is not surfaced to the
    caller.
    """
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(
            json.dumps({"as_of": time.time(), "models": models})
        )
    except OSError as e:
        logger.debug("smart_routing: cache write failed: %s", e)


def _fetch_remote() -> dict[str, int] | None:
    """Pull the live leaderboard from /v1/stats. Returns ``{model_id:
    tokens}`` from the ``openrouter_daily.top_models_30d`` block. None
    on any error — the resolver gracefully falls back to provider-count
    only ranking when this returns None.
    """
    try:
        with urlopen(
            Request(_STATS_URL, headers={"user-agent": "freeride-gateway"}),
            timeout=_FETCH_TIMEOUT_SEC,
        ) as r:
            data = json.load(r)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as e:
        logger.info("smart_routing: leaderboard fetch failed: %s", e)
        return None

    od = data.get("openrouter_daily") if isinstance(data, dict) else None
    top = od.get("top_models_30d") if isinstance(od, dict) else None
    if not isinstance(top, list):
        return None

    out: dict[str, int] = {}
    for entry in top:
        if not isinstance(entry, dict):
            continue
        mid = entry.get("model_id")
        tokens = entry.get("tokens")
        if isinstance(mid, str) and isinstance(tokens, (int, float)):
            out[mid] = int(tokens)
    return out


def fetch_leaderboard() -> dict[str, int]:
    """Cached + remote-fallback fetch of the global model popularity
    leaderboard. Always returns a dict; an empty dict signals "no
    signal available right now" and the caller should treat it as
    "popularity = 0 for everyone."
    """
    cached = _read_cache()
    if cached is not None:
        return cached
    fresh = _fetch_remote()
    if fresh is not None:
        _write_cache(fresh)
        return fresh
    return {}


def score_model(
    entry: dict[str, Any],
    available_providers: list[str],
    leaderboard: dict[str, int],
    *,
    health_cache: "dict[str, Any] | None" = None,
) -> float:
    """Score one catalog entry. Higher = better.

    Inputs:

    - ``entry`` is one row from the ``/v1/models`` aggregator —
      requires ``id`` and ``available_providers`` keys.
    - ``available_providers`` is the subset of the entry's providers
      that have non-cooled keys in env right now. Pre-computed by
      the caller so we don't redo the cooldown lookup per row.
    - ``leaderboard`` is ``{model_id: tokens_30d}`` from
      :func:`fetch_leaderboard`. May be empty.
    - ``health_cache`` is the model-health verdict from
      :func:`freeride.core.model_health.load_cache`. If provided,
      providers known-broken for this model id (per the most recent
      ``freeride audit-models`` run) are filtered before headroom
      is computed. ``None`` = no health filter.

    Score components (chosen so each one's max contribution is the
    same order of magnitude — neither dominates the other):

    - 10 points per **healthy** available provider (failover headroom)
    - log10(popularity + 1) * 5 bonus from the leaderboard

    With current numbers (top model ~5M tokens, log10≈6.7), the
    popularity bonus tops out at ~33 — about the same weight as
    having 3 available providers. That's intentional: the
    leaderboard nudges, the topology decides.

    A model whose every available provider is known-broken returns
    score 0.0 — same as having no providers at all.
    """
    if not available_providers:
        return 0.0
    if health_cache is not None:
        from freeride.core.model_health import is_model_known_broken

        mid = entry.get("id", "")
        healthy = [
            p
            for p in available_providers
            if not is_model_known_broken(p, mid, cache=health_cache)
        ]
        if not healthy:
            return 0.0
        available_providers = healthy
    headroom = 10.0 * len(available_providers)
    pop = leaderboard.get(entry.get("id", ""), 0)
    popularity = math.log10(pop + 1) * 5.0
    return headroom + popularity


def rank_catalog(
    catalog: list[dict[str, Any]],
    available_provider_names: set[str],
    leaderboard: dict[str, int],
    *,
    health_cache: "dict[str, Any] | None" = None,
) -> list[tuple[dict[str, Any], list[str], float]]:
    """Return ``[(entry, intersect, score), ...]`` sorted by score
    descending. Entries with no overlap between their
    ``available_providers`` and ``available_provider_names`` are
    dropped.

    Caller passes the leaderboard in (rather than us calling
    :func:`fetch_leaderboard` here) so a single resolver run shares
    one snapshot — and so tests can inject a deterministic
    leaderboard without monkeypatching the network layer.
    """
    scored: list[tuple[dict[str, Any], list[str], float]] = []
    for entry in catalog:
        ent_providers = entry.get("available_providers") or []
        intersect = [p for p in ent_providers if p in available_provider_names]
        if not intersect:
            continue
        s = score_model(entry, intersect, leaderboard, health_cache=health_cache)
        scored.append((entry, intersect, s))
    # Stable sort by negative score so the existing catalog order is the
    # implicit tiebreaker — same model class twice in the catalog
    # keeps its registration order on equal scores.
    scored.sort(key=lambda t: -t[2])
    return scored
