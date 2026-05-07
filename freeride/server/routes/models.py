"""``GET /v1/models`` — OpenAI-compatible model list aggregated across providers.

Aggregates :meth:`Provider.list_free_models` results across all registered
providers, ranks them, and returns the OpenAI ``{"object": "list", "data": [...]}``
shape. Cached for 6h by default; ``?refresh=true`` bypasses.

Per the design plan D11, when the same canonical model is exposed by
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
from freeride.core.canonicalize import canonicalize
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
    env_var = {
        "openrouter": "OPENROUTER_API_KEY",
        "nvidia_nim": "NVIDIA_API_KEY",
        "groq": "GROQ_API_KEY",
        "cloudflare_wai": "CLOUDFLARE_API_TOKEN",
        "huggingface": "HF_TOKEN",
    }.get(provider.name, f"{provider.name.upper()}_API_KEY")
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


async def _aggregate_models(
    providers: list[Provider], *, group: bool
) -> list[dict[str, Any]]:
    """Run each provider's sync ``list_free_models`` in a thread so we
    don't block the event loop, then either:

    - ``group=True`` (default): collapse models that canonicalize to the
      same key into one entry. The first-seen entry wins for ``id``,
      ``context_length``, etc.; later ones contribute their
      provider-specific id to ``aliases`` and provider name to
      ``available_providers``. ``canonical_id`` is stamped on every entry.
    - ``group=False``: return one entry per (provider, api_id) pair with
      ``canonical_id`` stamped but no merging.
    """
    raw_entries: list[tuple[str, Any]] = []
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
            raw_entries.append((p.name, m))

    if not group:
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for provider_name, m in raw_entries:
            key = f"{provider_name}::{m.api_id}"
            if key in seen:
                continue
            seen.add(key)
            obj = _model_to_openai_obj(provider_name, m)
            obj["canonical_id"] = canonicalize(m.api_id)
            obj["aliases"] = []
            obj["available_providers"] = [provider_name]
            results.append(obj)
        return results

    # Grouped: dedupe by canonical key, merging aliases.
    groups: dict[str, dict[str, Any]] = {}
    for provider_name, m in raw_entries:
        canon = canonicalize(m.api_id) or m.api_id
        if canon not in groups:
            entry = _model_to_openai_obj(provider_name, m)
            entry["canonical_id"] = canon
            entry["aliases"] = []
            entry["available_providers"] = [provider_name]
            groups[canon] = entry
            continue
        # Merge: add this provider's id as an alias if it's not already
        # the primary id, and append the provider name (deduped).
        existing = groups[canon]
        if m.api_id != existing["id"] and m.api_id not in existing["aliases"]:
            existing["aliases"].append(m.api_id)
        if provider_name not in existing["available_providers"]:
            existing["available_providers"].append(provider_name)
    return list(groups.values())


@router.get("/v1/models")
async def list_models(
    request: Request,
    refresh: bool = Query(False, description="Bypass the 6h cache and re-fetch from providers"),
    group: bool = Query(
        True,
        description=(
            "Collapse models that canonicalize to the same logical model "
            "(e.g. Llama 3.1 8B Instruct from OR/Groq/HF/CF) into one entry "
            "with `aliases` listing the provider-specific ids. "
            "Pass `group=false` to return one entry per (provider, model)."
        ),
    ),
) -> dict[str, Any]:
    cache_key = f"{_CACHE_KEY}.grouped" if group else f"{_CACHE_KEY}.ungrouped"
    if not refresh:
        cached = _CACHE.get(cache_key)
        if cached is not None:
            return {"object": "list", "data": cached}

    providers: list[Provider] = list(request.app.state.providers)
    data = await _aggregate_models(providers, group=group)
    _CACHE.set(cache_key, data)
    return {"object": "list", "data": data}
