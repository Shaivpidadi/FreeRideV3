"""``POST /v1/chat/completions`` — OpenAI-compatible chat completions.

Phase 2: single-provider non-streaming with per-key retry on RATE_LIMIT/AUTH.
Phase 3: streaming with buffer-first-chunk failover (this commit).

The streaming failover policy is described in PLAN_GATEWAY.md §8.1: hold
the first chunk until upstream confirms 200 + first SSE event. If the
upstream fails before the first chunk, retry on next key. Once the
first chunk has shipped to the client, errors propagate.
"""

from __future__ import annotations

import json as _json
import logging
import os
from typing import Any, AsyncIterator

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from freeride.core.chat_schema import ChatRequest, ChatResponse, ChatStreamEvent
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


async def _try_stream_with_failover(
    provider: Provider,
    body: ChatRequest,
    keys: list[str],
    cooldown: KeyCooldown,
) -> tuple[ChatStreamEvent, AsyncIterator[ChatStreamEvent]] | tuple[None, ErrorKind]:
    """Buffer-first-chunk failover.

    Returns ``(first_event, rest_iterator)`` if any key produced a first
    event before erroring. Returns ``(None, last_error_kind)`` if all keys
    failed before producing one. The caller streams ``first_event`` plus
    everything from ``rest_iterator`` to the client; once the first chunk
    has shipped, mid-stream errors surface as a truncated stream (PLAN
    §8.1 known-limitation).
    """
    last_error: ErrorKind | None = None
    for key in keys:
        gen = provider.forward_chat_stream(body, body.model, key)
        try:
            first = await gen.__anext__()
        except StopAsyncIteration:
            # Empty stream — count as MODEL_NOT_FOUND-ish unknown
            last_error = ErrorKind.UNKNOWN
            continue
        except httpx.HTTPStatusError as e:
            kind = provider.classify_error(e.response)
            if kind in (ErrorKind.RATE_LIMIT, ErrorKind.AUTH):
                cooldown.mark_rate_limited(provider.name, key)
            last_error = kind
            continue
        except httpx.HTTPError as e:
            last_error = provider.classify_error(e)
            continue
        # Got a first event — commit to this key for the rest of the stream
        return first, gen
    return None, last_error or ErrorKind.UNKNOWN


def _format_sse(event: ChatStreamEvent) -> bytes:
    return f"data: {event.model_dump_json(exclude_none=True)}\n\n".encode("utf-8")


def _format_done() -> bytes:
    return b"data: [DONE]\n\n"


async def _build_stream_response(
    provider: Provider,
    body: ChatRequest,
    cooldown: KeyCooldown,
    keys: list[str],
) -> StreamingResponse:
    first_or_none, rest_or_err = await _try_stream_with_failover(provider, body, keys, cooldown)
    if first_or_none is None:
        kind = rest_or_err if isinstance(rest_or_err, ErrorKind) else ErrorKind.UNKNOWN
        raise HTTPException(
            status_code=503,
            detail=f"All keys for {provider.name} failed before first chunk; last error: {kind.value}",
        )
    first_event = first_or_none
    rest = rest_or_err  # type: ignore[assignment]

    async def emit() -> AsyncIterator[bytes]:
        yield _format_sse(first_event)
        try:
            async for evt in rest:
                yield _format_sse(evt)
        except Exception as e:
            # Mid-stream error: client already has bytes — best we can
            # do is end the stream. Log loudly for the operator.
            logger.warning("mid-stream upstream error after first chunk shipped: %s", e)
        yield _format_done()

    return StreamingResponse(emit(), media_type="text/event-stream")


@router.post("/v1/chat/completions")
async def chat_completions(request: Request, body: ChatRequest):
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
        raise HTTPException(
            status_code=429,
            detail="All API keys for the chosen provider are in cooldown. Try again shortly.",
        )

    if body.is_streaming():
        return await _build_stream_response(provider, body, cooldown, available)

    # Non-streaming path (Phase 2 behavior, unchanged)
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
        return response.model_dump(exclude_none=False)

    detail = (
        f"All keys for {provider.name} failed; last error kind: "
        f"{last_error.value if last_error else 'unknown'}"
    )
    raise HTTPException(status_code=503, detail=detail)
