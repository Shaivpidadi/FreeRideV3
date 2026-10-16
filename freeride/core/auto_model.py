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
    cd = KeyCooldown.shared()
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

    candidates = resolve_auto_candidates(
        providers, catalog, leaderboard=leaderboard, health_cache=health_cache
    )
    if not candidates:
        return None, None
    return candidates[0]


def resolve_auto_candidates(
    providers: list[Provider],
    catalog: list[dict[str, Any]] | None,
    *,
    leaderboard: dict[str, int] | None = None,
    health_cache: dict[str, Any] | None = None,
) -> list[tuple[str, str]]:
    """Ranked ``[(model_id, provider_name), ...]`` for an ``auto`` request:
    the best model on **each** available provider, best provider first.

    This is what lets the failover chain switch providers for real. A
    single pick carried across the chain fails everywhere but its own
    provider (the others answer model-not-found); a per-provider
    candidate means the second provider is asked for *its* best model.
    Each ``model_id`` is already the picked provider's own spelling
    (``provider_ids`` on grouped entries).
    """
    if not catalog:
        logger.info("auto-model: catalog empty, cannot resolve")
        return []

    available = _available_provider_names(providers)
    if not available:
        logger.info("auto-model: no providers have usable keys right now")
        return []

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
        return []

    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for entry, intersect, score in ranked:
        provider_ids = entry.get("provider_ids") or {}
        for provider_name in intersect:
            if provider_name in seen:
                continue
            if health_cache:
                from freeride.core.model_health import is_model_known_broken

                if is_model_known_broken(provider_name, entry["id"], cache=health_cache):
                    continue
            seen.add(provider_name)
            out.append((provider_ids.get(provider_name, entry["id"]), provider_name))
    if out:
        logger.debug(
            "auto-model: %d candidates, leading with %s via %s",
            len(out),
            out[0][0],
            out[0][1],
        )
    return out


def alias_map_for(
    catalog: list[dict[str, Any]] | None,
    model_id: str,
    provider_names: list[str],
) -> dict[str, str]:
    """How each provider spells an explicitly requested model.

    Looks the id up in the grouped catalog (primary id or alias) and
    returns ``{provider: that provider's id}`` for every name in
    ``provider_names``; providers the catalog does not list for this
    model get the literal id, which keeps the pre-existing behaviour
    (they answer model-not-found and the chain moves on).
    """
    out = {name: model_id for name in provider_names}
    for entry in catalog or []:
        ids = {entry.get("id", "")} | set(entry.get("aliases") or [])
        if model_id not in ids:
            continue
        for name, pid in (entry.get("provider_ids") or {}).items():
            if name in out:
                out[name] = pid
        break
    return out
