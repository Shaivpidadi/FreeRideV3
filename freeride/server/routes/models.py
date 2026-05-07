"""``GET /v1/models`` — OpenAI-compatible model list aggregated across providers.

Aggregates :meth:`Provider.list_free_models` results across all registered
providers, ranks them, and returns the OpenAI ``{"object": "list", "data": [...]}``
shape. Cached for 6h by default; ``?refresh=true`` bypasses.

Per PLAN_GATEWAY.md D11, when the same canonical model is exposed by
multiple providers, we surface ONE logical entry. The resolver
(Phase 2.6) decides which provider to dispatch to per request. For
now, dedup is based on equality of ``model.api_id`` — we don't yet
have a canonical-model-name normalization step (D11 punts the
provider-by-provider variant problem).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from fastapi import APIRouter, Query, Request

from freeride.core.cache import TTLCache
from freeride.core.cooldown import KeyCooldown
from freeride.core.provider import Provider


logger = logging.getLogger(__name__)
router = APIRouter()
_CACHE: TTLCache[list[dict[str, Any]]] = TTLCache(ttl_seconds=6 * 3600)
_CACHE_KEY = "v1.models"


def _model_to_openai_obj(provider_name: str, model: Any) -> dict[str, Any]:
    """Map a v3 Model dataclass to OpenAI's ``model`` object shape."""
    raw = getattr(model, "raw", {}) or {}
    return {
        "id": model.api_id,
        "object": "model",
        "created": int(raw.get("created", 0)) or 0,
        "owned_by": provider_name,
        "context_length": model.context_length,
        "supported_parameters": list(model.supported_parameters or ()),
    }


def _key_for(provider: Provider) -> str | None:
    """Pick a usable API key for the given provider from env, skipping
    keys currently in cooldown. Centralized here so the resolver and the
    models endpoint pick keys the same way.
    """
    env_var = (
        "OPENROUTER_API_KEY"
        if provider.name == "openrouter"
        else "NVIDIA_API_KEY"
        if provider.name == "nvidia_nim"
        else f"{provider.name.upper()}_API_KEY"
    )
    raw = os.environ.get(env_var, "")
    if not raw:
        return None
    # Reuse the v2compat parser since multi-key JSON syntax is shared.
    from freeride.v2compat.models import _parse_api_keys

    keys = _parse_api_keys(raw)
    if not keys:
        return None
    cd = KeyCooldown()
    available = cd.available_keys(provider.name, keys)
    return available[0] if available else None


async def _aggregate_models(providers: list[Provider]) -> list[dict[str, Any]]:
    """Run each provider's sync ``list_free_models`` in a thread so we
    don't block the event loop, dedupe by api_id, return the union as
    OpenAI-formatted dicts.
    """
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for p in providers:
        key = _key_for(p)
        if key is None:
            logger.warning("provider %s has no usable API key in env; skipping", p.name)
            continue
        try:
            models = await asyncio.to_thread(p.list_free_models, key)
        except Exception as e:
            logger.warning("provider %s list_free_models failed: %s", p.name, e)
            continue
        for m in models:
            if m.api_id in seen:
                continue
            seen.add(m.api_id)
            results.append(_model_to_openai_obj(p.name, m))
    return results


@router.get("/v1/models")
async def list_models(
    request: Request,
    refresh: bool = Query(False, description="Bypass the 6h cache and re-fetch from providers"),
) -> dict[str, Any]:
    if not refresh:
        cached = _CACHE.get(_CACHE_KEY)
        if cached is not None:
            return {"object": "list", "data": cached}

    providers: list[Provider] = list(request.app.state.providers)
    data = await _aggregate_models(providers)
    _CACHE.set(_CACHE_KEY, data)
    return {"object": "list", "data": data}
