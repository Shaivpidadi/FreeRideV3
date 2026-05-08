"""Auto-model resolution.

Agents that don't know (or shouldn't have to know) which provider hosts
which model can pass ``"auto"`` (or an empty string / null) as the
model id. The chat route turns that into a concrete provider-specific
id by reading the catalog FreeRide already aggregates for ``GET
/v1/models``.

Selection policy is intentionally simple in v1:

  1. Walk the catalog in its existing rank order (aggregator already
     orders by health / provider preference).
  2. Skip entries whose ``available_providers`` are all currently
     missing keys or have all keys on cooldown.
  3. Return the first matching entry's ``id`` plus the provider name
     to attribute it to.

If nothing matches, return ``(None, None)`` and let the caller surface
a structured 503. We deliberately don't fall back to a hard-coded
"default" model — those go stale, which is the very thing this
subsystem exists to fix.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from freeride.core.cooldown import KeyCooldown

if TYPE_CHECKING:
    from freeride.core.provider import Provider


logger = logging.getLogger(__name__)


_AUTO_SENTINELS = frozenset({"", "auto", "freeride/auto", "default"})


_PROVIDER_ENV_VAR: dict[str, str] = {
    "openrouter": "OPENROUTER_API_KEY",
    "nvidia_nim": "NVIDIA_API_KEY",
    "groq": "GROQ_API_KEY",
    "cloudflare_wai": "CLOUDFLARE_API_TOKEN",
    "huggingface": "HF_TOKEN",
    "cerebras": "CEREBRAS_API_KEY",
    "ollama": "OLLAMA_BASE_URL",
}


def is_auto_model(model: str | None) -> bool:
    """True when the request asked us to choose."""
    if model is None:
        return True
    return model.strip().lower() in _AUTO_SENTINELS


def _env_var_for(provider_name: str) -> str:
    return _PROVIDER_ENV_VAR.get(provider_name, f"{provider_name.upper()}_API_KEY")


def _provider_keys(provider_name: str) -> list[str]:
    raw = os.environ.get(_env_var_for(provider_name), "")
    if not raw:
        return []
    from freeride.v2compat.models import _parse_api_keys

    return _parse_api_keys(raw)


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
) -> tuple[str | None, str | None]:
    """Pick (model_id, provider_name) for an ``auto`` request.

    ``catalog`` is the aggregated list returned by
    :func:`freeride.server.routes.models.get_or_fetch_catalog`. Callers
    pass an empty list (not None) to signal "no catalog yet" so the
    resolver short-circuits cleanly without trying to iterate.
    """
    if not catalog:
        logger.info("auto-model: catalog empty, cannot resolve")
        return None, None

    available = _available_provider_names(providers)
    if not available:
        logger.info("auto-model: no providers have usable keys right now")
        return None, None

    for entry in catalog:
        entry_providers = entry.get("available_providers") or []
        for prov in entry_providers:
            if prov in available:
                return entry["id"], prov

    logger.info(
        "auto-model: catalog has %d entries but none overlap with available "
        "providers %s",
        len(catalog),
        sorted(available),
    )
    return None, None
