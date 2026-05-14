"""``POST /v1/responses`` — OpenAI Responses API shim for the Codex CLI.

The Codex CLI (Rust ``openai/codex``) only speaks the Responses API —
``wire_api = "chat"`` is explicitly rejected on its side. When it's
pointed at our gateway via the ``-c openai_base_url=<gateway>/v1`` flag
that ``freeride run codex`` injects, every chat turn lands here.

This route translates the Responses-shape request into Chat Completions,
dispatches through the same provider failover machinery the rest of
the gateway uses, and translates back into Responses shape on the way
out. Streaming uses the Responses event protocol
(``response.created`` → ``response.output_item.added`` →
per-item ``.delta`` / ``.done`` → ``response.completed``) with
explicit framing events the CLI gates on.

Model id from the request body (``gpt-5-codex``, ``gpt-5``, etc.) is
rewritten to ``auto`` before dispatch — we don't host OpenAI's
proprietary models, so the smart-router picks a free model. The
originally-requested id is echoed back on the response so the CLI's
UI stays coherent.
"""

from __future__ import annotations

import logging
import time
from typing import AsyncIterator

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from freeride.core.auto_model import is_auto_model, resolve_auto_model
from freeride.core.codex_schema import ResponsesRequest
from freeride.core.codex_translate import (
    chat_to_responses_response,
    responses_to_chat_request,
    stream_chat_to_responses,
)
from freeride.core.cooldown import KeyCooldown
from freeride.core.errors import ErrorKind
from freeride.core.events import emit as emit_event
from freeride.core.events import new_request_id
from freeride.core.health import sort_by_health, sort_keys_by_health
from freeride.core.provider import Provider
from freeride.server.routes.chat import (
    FailoverContext,
    _apply_force_provider,
    _record_health,
    _resolve_provider_chain,
    _try_stream_with_failover,
)
from freeride.server.routes.models import get_or_fetch_catalog


logger = logging.getLogger(__name__)
router = APIRouter()


def _error_payload(message: str, code: str = "invalid_request_error") -> dict:
    """OpenAI's error envelope shape — what the Responses API returns
    natively. The Codex CLI parses ``.error.message`` for display."""
    return {
        "error": {
            "message": message,
            "type": code,
            "param": None,
            "code": None,
        }
    }


@router.post("/v1/responses")
async def responses(request: Request):
    """Responses API endpoint. Body shape is documented in
    ``freeride.core.codex_schema.ResponsesRequest``; key fields:
    ``model``, ``input`` (str or list of typed items), ``stream``,
    ``tools`` (flat), ``instructions``, ``max_output_tokens``."""
    request_id = new_request_id()

    try:
        peek = await request.json()
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=400,
            detail=_error_payload("Request body is not valid JSON."),
        )
    if not isinstance(peek, dict):
        raise HTTPException(
            status_code=400,
            detail=_error_payload("Request body must be a JSON object."),
        )

    try:
        body = ResponsesRequest.model_validate(peek)
    except ValidationError as e:
        raise HTTPException(
            status_code=400,
            detail=_error_payload(f"Request body failed validation: {e!s}"),
        )

    requested_model = body.model
    is_streaming = bool(body.stream)

    emit_event(
        "responses_routing_decision",
        request_id=request_id,
        model=requested_model,
        streaming=is_streaming,
        endpoint="responses",
    )

    # Translate to Chat Completions shape so the existing provider
    # chain can route it.
    openai_request = responses_to_chat_request(body)
    # The request body's ``stream`` is what the route should honor; the
    # downstream provider call doesn't need it propagated for the
    # non-streaming branch. For the streaming branch _try_stream_with_failover
    # sets stream=True on its own copy of the body, so this is moot.
    openai_request.stream = False  # the wrapper helpers manage this

    # Rewrite to auto — we don't host OpenAI's models. The originally-
    # requested id is preserved on ``requested_model`` and echoed back
    # on the response so the CLI's UI stays coherent.
    openai_request.model = "auto"

    providers: list[Provider] = sort_by_health(list(request.app.state.providers))
    providers, forced = _apply_force_provider(providers, request)
    if forced is not None and not providers:
        raise HTTPException(
            status_code=400,
            detail=_error_payload(
                f"X-FreeRide-Force-Provider={forced!r} is not a registered provider."
            ),
        )

    ctx = FailoverContext(request_id=request_id)
    emit_event(
        "request_start",
        request_id=ctx.request_id,
        model=openai_request.model,
        streaming=is_streaming,
        endpoint="responses",
    )

    if not providers:
        raise HTTPException(
            status_code=503,
            detail=_error_payload(
                "No provider plugins registered.", "service_unavailable"
            ),
        )

    cooldown = KeyCooldown()
    chain = _resolve_provider_chain(providers)
    if not chain:
        raise HTTPException(
            status_code=503,
            detail=_error_payload(
                "No providers have usable (non-cooling) API keys for this request.",
                "service_unavailable",
            ),
        )

    if is_auto_model(openai_request.model):
        catalog = await get_or_fetch_catalog(providers, group=True)
        resolved_id, resolved_provider = resolve_auto_model(providers, catalog)
        if resolved_id is None:
            raise HTTPException(
                status_code=503,
                detail=_error_payload(
                    "No provider has a usable model + key right now.",
                    "service_unavailable",
                ),
            )
        openai_request.model = resolved_id
        emit_event(
            "auto_model_resolved",
            request_id=ctx.request_id,
            resolved_model=resolved_id,
            resolved_provider=resolved_provider,
            endpoint="responses",
        )

    if is_streaming:
        # _try_stream_with_failover sets stream=True on its copy
        openai_request.stream = True
        return await _build_responses_stream(
            chain=chain,
            openai_request=openai_request,
            cooldown=cooldown,
            ctx=ctx,
            requested_model=requested_model,
        )

    # ─── non-streaming failover loop ───────────────────────────────
    response_obj = None
    chosen_provider: Provider | None = None
    for provider, keys in chain:
        summary = ctx.attempt(provider.name)
        ordered_keys = sort_keys_by_health(provider.name, keys)
        for key_idx, key in enumerate(ordered_keys):
            summary.keys_tried += 1
            emit_event(
                "provider_attempt",
                request_id=ctx.request_id,
                provider=provider.name,
                key_index=key_idx,
                model=openai_request.model,
                streaming=False,
                endpoint="responses",
            )
            t0 = time.perf_counter()
            try:
                response_obj = await provider.forward_chat(
                    openai_request, openai_request.model, key
                )
            except httpx.HTTPStatusError as e:
                duration_ms = int((time.perf_counter() - t0) * 1000)
                kind = provider.classify_error(e.response)
                if kind in (ErrorKind.RATE_LIMIT, ErrorKind.AUTH):
                    cooldown.mark_rate_limited(provider.name, key)
                summary.last_error = kind
                emit_event(
                    "provider_response",
                    request_id=ctx.request_id,
                    provider=provider.name,
                    key_index=key_idx,
                    duration_ms=duration_ms,
                    status=kind.value,
                    endpoint="responses",
                )
                _record_health(provider.name, key, status=kind.value)
                continue
            except (httpx.RequestError, httpx.TimeoutException) as e:
                duration_ms = int((time.perf_counter() - t0) * 1000)
                kind = provider.classify_error(e)
                summary.last_error = kind
                emit_event(
                    "provider_response",
                    request_id=ctx.request_id,
                    provider=provider.name,
                    key_index=key_idx,
                    duration_ms=duration_ms,
                    status=kind.value,
                    endpoint="responses",
                )
                _record_health(provider.name, key, status=kind.value)
                continue

            duration_ms = int((time.perf_counter() - t0) * 1000)
            emit_event(
                "provider_response",
                request_id=ctx.request_id,
                provider=provider.name,
                key_index=key_idx,
                duration_ms=duration_ms,
                status="OK",
                endpoint="responses",
            )
            _record_health(provider.name, key, status="OK")
            chosen_provider = provider
            break
        if response_obj is not None:
            break

    if response_obj is None:
        raise HTTPException(
            status_code=503,
            detail=_error_payload(
                "All providers exhausted without a successful response.",
                "service_unavailable",
            ),
        )

    emit_event(
        "request_complete",
        request_id=ctx.request_id,
        provider=chosen_provider.name if chosen_provider else None,
        streaming=False,
        endpoint="responses",
    )

    resp_obj = chat_to_responses_response(response_obj, requested_model)
    return JSONResponse(
        content=resp_obj.model_dump(exclude_none=True),
        headers={
            "X-FreeRide-Provider": chosen_provider.name if chosen_provider else "unknown",
            "X-FreeRide-Request-Id": ctx.request_id,
        },
    )


async def _build_responses_stream(
    *,
    chain,
    openai_request,
    cooldown: KeyCooldown,
    ctx: FailoverContext,
    requested_model: str,
) -> StreamingResponse:
    """Pre-flight first chunk through the chat-route failover helper,
    then re-frame Chat-shape deltas into Responses-shape SSE events.
    Same buffer-first-chunk semantics the messages route uses."""
    chosen, first_event, rest_or_err = await _try_stream_with_failover(
        chain, openai_request, cooldown, ctx
    )
    if chosen is None:
        emit_event(
            "request_failed",
            request_id=ctx.request_id,
            phase="pre_first_chunk",
            tried=[t.provider for t in ctx.tried],
            endpoint="responses",
        )
        raise HTTPException(
            status_code=503,
            detail=_error_payload(
                "All providers exhausted without producing a streaming response.",
                "service_unavailable",
            ),
        )

    async def _merged_chunks() -> AsyncIterator:
        yield first_event
        try:
            async for evt in rest_or_err:
                yield evt
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "responses: mid-stream upstream error after first chunk: %s", e
            )
            emit_event(
                "request_mid_stream_error",
                request_id=ctx.request_id,
                provider=chosen.name,
                error=str(e)[:200],
                endpoint="responses",
            )

    async def _emit_sse() -> AsyncIterator[bytes]:
        async for byte_chunk in stream_chat_to_responses(
            _merged_chunks(), requested_model=requested_model
        ):
            yield byte_chunk
        emit_event(
            "request_complete",
            request_id=ctx.request_id,
            provider=chosen.name,
            streaming=True,
            endpoint="responses",
        )

    return StreamingResponse(
        _emit_sse(),
        media_type="text/event-stream",
        headers={
            "X-FreeRide-Provider": chosen.name,
            "X-FreeRide-Request-Id": ctx.request_id,
            "Cache-Control": "no-cache",
        },
    )
