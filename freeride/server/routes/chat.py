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
        "groq": "GROQ_API_KEY",
    }.get(provider_name, f"{provider_name.upper()}_API_KEY")


def _all_keys_for(provider_name: str) -> list[str]:
    raw = os.environ.get(_env_var_for(provider_name), "")
    if not raw:
        return []
    from freeride.v2compat.models import _parse_api_keys

    return _parse_api_keys(raw)


def _resolve_provider_chain(providers: list[Provider]) -> list[tuple[Provider, list[str]]]:
    """Return ``[(provider, available_keys), ...]`` in fallback order.

    Phase 3 cross-provider failover: try every provider with at least
    one available key. The retry loop advances along this chain when
    a provider's keys are exhausted.

    Phase 2.6+ may add health-awareness (latency, recent failure rate)
    to reorder providers per request; for now order matches registration.
    """
    cooldown = KeyCooldown()
    chain: list[tuple[Provider, list[str]]] = []
    for p in providers:
        keys = _all_keys_for(p.name)
        if not keys:
            continue
        available = cooldown.available_keys(p.name, keys)
        if not available:
            continue
        chain.append((p, available))
    return chain


async def _try_stream_with_failover(
    chain: list[tuple[Provider, list[str]]],
    body: ChatRequest,
    cooldown: KeyCooldown,
) -> (
    tuple[Provider, ChatStreamEvent, AsyncIterator[ChatStreamEvent]]
    | tuple[None, None, ErrorKind]
):
    """Buffer-first-chunk failover across providers.

    Walks the (provider, keys) chain; for each (provider, key) tuple,
    attempts to pull the first SSE event. On success returns the
    chosen provider plus the stream. On all-failure returns the last
    classified ErrorKind. PLAN §8.1: once first chunk has shipped to
    client, mid-stream errors propagate.
    """
    last_error: ErrorKind | None = None
    for provider, keys in chain:
        provider_done = False  # set True on MODEL_NOT_FOUND to advance providers
        for key in keys:
            if provider_done:
                break
            gen = provider.forward_chat_stream(body, body.model, key)
            try:
                first = await gen.__anext__()
            except StopAsyncIteration:
                last_error = ErrorKind.UNKNOWN
                continue
            except httpx.HTTPStatusError as e:
                # Read the streaming body before classifying so message
                # patterns (model_not_found, etc.) are visible.
                try:
                    await e.response.aread()
                except Exception:
                    pass
                kind = provider.classify_error(e.response)
                if kind in (ErrorKind.RATE_LIMIT, ErrorKind.AUTH):
                    cooldown.mark_rate_limited(provider.name, key)
                if kind == ErrorKind.MODEL_NOT_FOUND:
                    provider_done = True
                last_error = kind
                continue
            except httpx.HTTPError as e:
                last_error = provider.classify_error(e)
                continue
            return provider, first, gen
    return None, None, last_error or ErrorKind.UNKNOWN


def _format_sse(event: ChatStreamEvent) -> bytes:
    return f"data: {event.model_dump_json(exclude_none=True)}\n\n".encode("utf-8")


def _format_done() -> bytes:
    return b"data: [DONE]\n\n"


async def _build_stream_response(
    chain: list[tuple[Provider, list[str]]],
    body: ChatRequest,
    cooldown: KeyCooldown,
) -> StreamingResponse:
    chosen, first_event, rest_or_err = await _try_stream_with_failover(chain, body, cooldown)
    if chosen is None:
        kind = rest_or_err if isinstance(rest_or_err, ErrorKind) else ErrorKind.UNKNOWN
        raise HTTPException(
            status_code=503,
            detail=f"All providers/keys failed before first chunk; last error: {kind.value}",
        )
    rest = rest_or_err  # AsyncIterator at this point

    async def emit() -> AsyncIterator[bytes]:
        yield _format_sse(first_event)
        try:
            async for evt in rest:
                yield _format_sse(evt)
        except Exception as e:
            logger.warning("mid-stream upstream error after first chunk shipped: %s", e)
        yield _format_done()

    return StreamingResponse(
        emit(),
        media_type="text/event-stream",
        headers={"X-FreeRide-Provider": chosen.name},
    )


@router.post("/v1/chat/completions")
async def chat_completions(request: Request, body: ChatRequest):
    providers: list[Provider] = list(request.app.state.providers)
    if not providers:
        raise HTTPException(status_code=503, detail="No providers configured.")

    cooldown = KeyCooldown()
    chain = _resolve_provider_chain(providers)
    if not chain:
        raise HTTPException(
            status_code=503,
            detail="No providers have usable, non-cooling API keys for this request.",
        )

    if body.is_streaming():
        return await _build_stream_response(chain, body, cooldown)

    # Non-streaming with cross-provider failover.
    last_error: ErrorKind | None = None
    chosen_provider: Provider | None = None
    response: ChatResponse | None = None
    for provider, keys in chain:
        for key in keys:
            try:
                response = await provider.forward_chat(body, body.model, key)
            except httpx.HTTPStatusError as e:
                kind = provider.classify_error(e.response)
                if kind in (ErrorKind.RATE_LIMIT, ErrorKind.AUTH):
                    cooldown.mark_rate_limited(provider.name, key)
                last_error = kind
                # Same-provider key advance only on RATE_LIMIT / AUTH;
                # MODEL_NOT_FOUND advances to next provider since other
                # keys won't help.
                if kind == ErrorKind.MODEL_NOT_FOUND:
                    break
                continue
            except httpx.HTTPError as e:
                last_error = provider.classify_error(e)
                continue
            chosen_provider = provider
            break
        if response is not None:
            break

    if response is None or chosen_provider is None:
        detail = (
            f"All providers/keys failed; last error kind: "
            f"{last_error.value if last_error else 'unknown'}"
        )
        raise HTTPException(status_code=503, detail=detail)

    out = response.model_dump(exclude_none=False)
    # Surface which provider actually served the request — useful for
    # the Phase 3 failover demo and for operator debugging.
    out["_freeride_provider"] = chosen_provider.name
    return out
