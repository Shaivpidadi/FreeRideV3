"""Auto-model resolution.

Agents that don't know (or shouldn't have to know) which provider hosts
which model can pass ``"auto"`` (or an empty string / null) as the
model id. The chat route turns that into a concrete provider-specific
id by reading the catalog FreeRide already aggregates for ``GET
/v1/models``.

The resolver now ranks the catalog by a smart score (see
:mod:`freeride.core.smart_routing`) that combines failover headroom
with a soft popularity signal pulled from the public ``/v1/stats``
endpoint. If that endpoint is unreachable, the score falls back to
provider-count-only — which is exactly the old naïve behavior, so
smart routing is a strict superset, never a hard dependency.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from freeride.core.cooldown import KeyCooldown
from freeride.core.provider_env import all_keys_for
from freeride.core.smart_routing import fetch_leaderboard, rank_catalog

if TYPE_CHECKING:
    from freeride.core.provider import Provider


logger = logging.getLogger(__name__)


_AUTO_SENTINELS = frozenset({"", "auto", "freeride/auto", "default"})


def is_auto_model(model: str | None) -> bool:
    """True when the request asked us to choose."""
    if model is None:
        return True
    return model.strip().lower() in _AUTO_SENTINELS


def _provider_keys(provider_name: str) -> list[str]:
    return all_keys_for(provider_name)


def _available_provider_names(providers: list[Provider]) -> set[str]:
    """Names of providers that have at least one usable, non-cooled key."""
    cd = KeyCooldown()
    out: set[str] = set()
    for p in providers:
        keys = _provider_keys(p.name)
        if not keys:
            continue
        if cd.available_keys(p.name, keys):
            out.add(p.name)
    return out


def resolve_auto_model(
    providers: list[Provider],
    catalog: list[dict[str, Any]] | None,
    *,
    leaderboard: dict[str, int] | None = None,
    health_cache: dict[str, Any] | None = None,
) -> tuple[str | None, str | None]:
    """Pick (model_id, provider_name) for an ``auto`` request.

    ``catalog`` is the aggregated list returned by
    :func:`freeride.server.routes.models.get_or_fetch_catalog`. Callers
    pass an empty list (not None) to signal "no catalog yet" so the
    resolver short-circuits cleanly without trying to iterate.

    Ranking uses :func:`freeride.core.smart_routing.rank_catalog`,
    which scores each entry by failover headroom + a soft global
    popularity signal pulled from the public ``/v1/stats`` endpoint,
    and (when ``health_cache`` is non-empty) a per-(provider,model)
    runtime-health filter populated by ``freeride audit-models``.

    Pass ``leaderboard=`` and ``health_cache=`` explicitly only in
    tests where you want deterministic inputs — production callers
    can pass ``None`` and get the cache-loaded defaults.
    """
    if not catalog:
        logger.info("auto-model: catalog empty, cannot resolve")
        return None, None

    available = _available_provider_names(providers)
    if not available:
        logger.info("auto-model: no providers have usable keys right now")
        return None, None

    if leaderboard is None:
        leaderboard = fetch_leaderboard()

    if health_cache is None:
        from freeride.core.model_health import load_cache

        health_cache = load_cache()  # may be {} — that's fine, equivalent to no filter

    ranked = rank_catalog(
        catalog,
        available,
        leaderboard,
        health_cache=health_cache or None,
    )
    if not ranked:
        logger.info(
            "auto-model: catalog has %d entries but none overlap with available "
            "providers %s",
            len(catalog),
            sorted(available),
        )
        return None, None

    entry, intersect, score = ranked[0]
    logger.debug(
        "auto-model: picked %s via %s (score=%.2f, %d candidates considered)",
        entry["id"],
        intersect[0],
        score,
        len(ranked),
    )
    return entry["id"], intersect[0]
