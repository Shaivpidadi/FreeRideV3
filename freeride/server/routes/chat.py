"""``POST /v1/chat/completions`` — OpenAI-compatible chat completions.

Phase 2 (this commit): single-provider path with one retry on 429 / AUTH
across keys. Streaming and full multi-provider failover land in Phase 3.

Resolver and retry logic live inline here for simplicity until they grow
big enough to warrant their own modules (Phase 2.6).
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request

from freeride.core.chat_schema import ChatRequest, ChatResponse
from freeride.core.cooldown import KeyCooldown
from freeride.core.errors import ErrorKind
from freeride.core.provider import Provider


logger = logging.getLogger(__name__)
router = APIRouter()


def _env_var_for(provider_name: str) -> str:
    return {
        "openrouter": "OPENROUTER_API_KEY",
        "nvidia_nim": "NVIDIA_API_KEY",
    }.get(provider_name, f"{provider_name.upper()}_API_KEY")


def _all_keys_for(provider_name: str) -> list[str]:
    raw = os.environ.get(_env_var_for(provider_name), "")
    if not raw:
        return []
    from freeride.v2compat.models import _parse_api_keys

    return _parse_api_keys(raw)


def _pick_provider_for_model(providers: list[Provider], model_id: str) -> Provider | None:
    """Phase 2 simplest resolver: pick the first provider that lists this
    model_id as one of its free models. Falls back to the first provider
    if no match (so unknown model ids still get a chance — provider
    classify_error will MODEL_NOT_FOUND if they really aren't there).

    Phase 2.6 replaces this with a health-aware resolver.
    """
    for p in providers:
        keys = _all_keys_for(p.name)
        if not keys:
            continue
        # Lightly rely on the cached models from /v1/models if it ran;
        # otherwise just return the first keyed provider (typically only
        # OpenRouter in Phase 2).
        return p
    return None


@router.post("/v1/chat/completions")
async def chat_completions(request: Request, body: ChatRequest) -> dict[str, Any]:
    if body.is_streaming():
        # Phase 3 implements streaming; for now refuse loudly so clients
        # get a clear signal rather than a silent non-stream response.
        raise HTTPException(
            status_code=501,
            detail="Streaming responses land in Phase 3. Set stream=false for now.",
        )

    providers: list[Provider] = list(request.app.state.providers)
    if not providers:
        raise HTTPException(status_code=503, detail="No providers configured.")

    provider = _pick_provider_for_model(providers, body.model)
    if provider is None:
        raise HTTPException(
            status_code=503,
            detail="No provider has a usable API key for this request.",
        )

    keys = _all_keys_for(provider.name)
    cooldown = KeyCooldown()
    available = cooldown.available_keys(provider.name, keys)
    if not available:
        # All keys cooling — let the client retry later.
        raise HTTPException(
            status_code=429,
            detail="All API keys for the chosen provider are in cooldown. Try again shortly.",
        )

    # Try each available key in turn. Mark RATE_LIMIT/AUTH keys cooling
    # and advance; bail with a clean error if all fail.
    last_error: ErrorKind | None = None
    for key in available:
        try:
            response: ChatResponse = await provider.forward_chat(body, body.model, key)
        except httpx.HTTPStatusError as e:
            kind = provider.classify_error(e.response)
            if kind in (ErrorKind.RATE_LIMIT, ErrorKind.AUTH):
                cooldown.mark_rate_limited(provider.name, key)
                last_error = kind
                continue
            raise HTTPException(status_code=e.response.status_code, detail=str(e)) from e
        except httpx.HTTPError as e:
            kind = provider.classify_error(e)
            last_error = kind
            continue
        # Success path
        return response.model_dump(exclude_none=False)

    # All keys exhausted
    detail = (
        f"All keys for {provider.name} failed; last error kind: "
        f"{last_error.value if last_error else 'unknown'}"
    )
    raise HTTPException(status_code=503, detail=detail)
