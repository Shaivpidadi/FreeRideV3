"""``POST /v1beta/models/<model>:generateContent`` and
``:streamGenerateContent`` — Google Generative Language API shim.

Lets the official ``gemini`` CLI (and any ``@google/genai``-backed
client) talk to FreeRide as if it were
``generativelanguage.googleapis.com``. Translates the Google
``{contents, tools, ...}`` shape into OpenAI Chat Completions,
dispatches through the same provider failover machinery
``/v1/chat/completions`` uses, then translates the response back into
Google's ``{candidates, usageMetadata, modelVersion}`` shape.

Streaming variant uses ``:streamGenerateContent`` (with ``?alt=sse``).
The translator turns OpenAI streaming chunks into Google's
incremental-complete-response SSE events.

Auto-routing: the model id from the URL path (e.g. ``gemini-2.5-pro``)
is rewritten to ``auto`` before dispatch — we don't host Google models,
so the smart-router picks whatever free model is healthiest. The CLI
sees the originally-requested id echoed back in ``modelVersion`` so
its UI stays coherent.
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
from freeride.core.cooldown import KeyCooldown
from freeride.core.errors import ErrorKind
from freeride.core.events import emit as emit_event
from freeride.core.events import new_request_id
from freeride.core.gemini_schema import GeminiGenerateRequest
from freeride.core.gemini_translate import (
    gemini_to_openai_request,
    openai_to_gemini_response,
    stream_openai_to_gemini,
)
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


def _error_payload(status: str, message: str) -> dict:
    """Google's error envelope shape — matches what
    generativelanguage.googleapis.com returns so SDK clients parse it
    natively."""
    return {
        "error": {
            "code": 400,
            "message": message,
            "status": status,
        }
    }


@router.post("/v1beta/models/{model_with_action}")
async def gemini_generate(model_with_action: str, request: Request):
    """Google's REST shape encodes the action (generateContent vs
    streamGenerateContent) as part of the path: the segment after the
    model id is ``:generateContent`` or ``:streamGenerateContent``.
    FastAPI doesn't have a clean way to capture this with multiple
    path segments, so we take the whole tail as one param and split
    on ``:``.
    """
    # "<model>:generateContent" → ("<model>", "generateContent")
    if ":" not in model_with_action:
        raise HTTPException(
            status_code=404,
            detail=_error_payload(
                "NOT_FOUND",
                f"Unknown endpoint '/v1beta/models/{model_with_action}'. "
                "Expected '<model>:generateContent' or '<model>:streamGenerateContent'.",
            ),
        )

    model_id, _, action = model_with_action.rpartition(":")
    if action not in ("generateContent", "streamGenerateContent"):
        raise HTTPException(
            status_code=404,
            detail=_error_payload(
                "NOT_FOUND",
                f"Unsupported action ':{action}'. Use ':generateContent' or "
                "':streamGenerateContent'.",
            ),
        )
    is_streaming = action == "streamGenerateContent"

    request_id = new_request_id()

    # Parse body. Google clients post JSON; permissive schema accepts
    # any extra fields without 400-ing.
    try:
        peek = await request.json()
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=400,
            detail=_error_payload(
                "INVALID_ARGUMENT", "Request body is not valid JSON."
            ),
        )
    if not isinstance(peek, dict):
        raise HTTPException(
            status_code=400,
            detail=_error_payload(
                "INVALID_ARGUMENT", "Request body must be a JSON object."
            ),
        )

    try:
        body = GeminiGenerateRequest.model_validate(peek)
    except ValidationError as e:
        raise HTTPException(
            status_code=400,
            detail=_error_payload(
                "INVALID_ARGUMENT", f"Request body failed validation: {e!s}"
            ),
        )

    emit_event(
        "gemini_routing_decision",
        request_id=request_id,
        model=model_id,
        streaming=is_streaming,
        endpoint="gemini",
    )

    # Translate to OpenAI shape. Auto-rewrite the model id so the
    # smart-router picks a free provider — we don't host Google
    # models. The originally-requested id is echoed back as
    # modelVersion on the response.
    openai_request = gemini_to_openai_request(body, model_id)
    requested_model = model_id
    # Always rewrite to auto — gemini-* aren't real ids on any of our
    # free providers, and we have no way to map them 1:1.
    openai_request.model = "auto"

    # ─── borrow chat-route plumbing ────────────────────────────────
    providers: list[Provider] = sort_by_health(list(request.app.state.providers))
    providers, forced = _apply_force_provider(providers, request)
    if forced is not None and not providers:
        raise HTTPException(
            status_code=400,
            detail=_error_payload(
                "INVALID_ARGUMENT",
                f"X-FreeRide-Force-Provider={forced!r} is not a registered provider.",
            ),
        )

    ctx = FailoverContext(request_id=request_id)
    emit_event(
        "request_start",
        request_id=ctx.request_id,
        model=openai_request.model,
        streaming=is_streaming,
        endpoint="gemini",
    )

    if not providers:
        raise HTTPException(
            status_code=503,
            detail=_error_payload(
                "UNAVAILABLE", "No provider plugins registered."
            ),
        )

    cooldown = KeyCooldown()
    chain = _resolve_provider_chain(providers)
    if not chain:
        raise HTTPException(
            status_code=503,
            detail=_error_payload(
                "UNAVAILABLE",
                "No providers have usable (non-cooling) API keys for this "
                "request.",
            ),
        )

    # Auto-model resolution.
    if is_auto_model(openai_request.model):
        catalog = await get_or_fetch_catalog(providers, group=True)
        resolved_id, resolved_provider = resolve_auto_model(providers, catalog)
        if resolved_id is None:
            raise HTTPException(
                status_code=503,
                detail=_error_payload(
                    "UNAVAILABLE",
                    "No provider has a usable model + key right now.",
                ),
            )
        openai_request.model = resolved_id
        emit_event(
            "auto_model_resolved",
            request_id=ctx.request_id,
            resolved_model=resolved_id,
            resolved_provider=resolved_provider,
            endpoint="gemini",
        )

    # ─── streaming branch ──────────────────────────────────────────
    if is_streaming:
        return await _build_gemini_stream_response(
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
                endpoint="gemini",
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
                    endpoint="gemini",
                )
                _record_health(provider.name, ok=False, duration_ms=duration_ms, key=key)
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
                    endpoint="gemini",
                )
                _record_health(provider.name, ok=False, duration_ms=duration_ms, key=key)
                continue

            # Success.
            duration_ms = int((time.perf_counter() - t0) * 1000)
            emit_event(
                "provider_response",
                request_id=ctx.request_id,
                provider=provider.name,
                key_index=key_idx,
                duration_ms=duration_ms,
                status="OK",
                endpoint="gemini",
            )
            _record_health(provider.name, ok=True, duration_ms=duration_ms, key=key)
            chosen_provider = provider
            break
        if response_obj is not None:
            break

    if response_obj is None:
        # All providers failed — return the structured 503 Google
        # error envelope. The CLI parses .error.message for display.
        raise HTTPException(
            status_code=503,
            detail=_error_payload(
                "UNAVAILABLE",
                "All providers exhausted without a successful response.",
            ),
        )

    emit_event(
        "request_complete",
        request_id=ctx.request_id,
        provider=chosen_provider.name if chosen_provider else None,
        streaming=False,
        endpoint="gemini",
    )
    from freeride.core.telemetry import record_request
    from freeride.core.usage import Kind, extract_usage

    g_usage = extract_usage(Kind.OPENAI, response_obj.model_dump())
    record_request(
        input_tokens=g_usage.input,
        output_tokens=g_usage.output,
        provider=chosen_provider.name if chosen_provider else None,
    )

    gemini_response = openai_to_gemini_response(response_obj, requested_model)
    # by_alias=True emits camelCase keys (modelVersion, usageMetadata,
    # functionCall, ...) that gemini-cli expects.
    return JSONResponse(
        content=gemini_response.model_dump(by_alias=True, exclude_none=True),
        headers={
            "X-FreeRide-Provider": chosen_provider.name if chosen_provider else "unknown",
        },
    )


async def _build_gemini_stream_response(
    *,
    chain,
    openai_request,
    cooldown: KeyCooldown,
    ctx: FailoverContext,
    requested_model: str,
) -> StreamingResponse:
    """Pre-flight the first OpenAI chunk through the chat route's
    failover helper, then stream the rest through the Gemini
    translator. Same buffer-first-chunk-then-fail-over guarantee
    /v1/messages uses.

    ``_try_stream_with_failover`` returns a 3-tuple:
    ``(chosen_provider, first_event, rest_iterator)`` on success, or
    ``(None, None, error_kind)`` when every provider failed before
    producing a first byte. Buffer-first-chunk means we can still
    failover on a pre-first-byte failure; once any byte ships we're
    committed.
    """
    chosen, first_event, rest_or_err = await _try_stream_with_failover(
        chain, openai_request, cooldown, ctx
    )
    if chosen is None:
        emit_event(
            "request_failed",
            request_id=ctx.request_id,
            phase="pre_first_chunk",
            tried=[t.provider for t in ctx.tried],
            endpoint="gemini",
        )
        raise HTTPException(
            status_code=503,
            detail=_error_payload(
                "UNAVAILABLE",
                "All providers exhausted without producing a streaming response.",
            ),
        )

    # Upstream is OpenAI-compat (we translate to Gemini SSE on the
    # way out), so the final usage chunk is OpenAI-shape. Capture it
    # the same way the other streaming routes do.
    from freeride.core.usage import Kind, extract_usage

    last_usage_box = [extract_usage(Kind.OPENAI, first_event.model_dump())]

    async def _merged_chunks() -> AsyncIterator:
        """Re-thread the first event back in front of the rest, so the
        translator sees a single contiguous stream."""
        yield first_event
        try:
            async for evt in rest_or_err:
                u = extract_usage(Kind.OPENAI, evt.model_dump())
                if u.has_any:
                    last_usage_box[0] = u
                yield evt
        except Exception as e:  # noqa: BLE001
            # Mid-stream failure after first chunk shipped — we can't
            # undo bytes already on the wire, so let the translator
            # finish its event sequence cleanly and log.
            logger.warning(
                "gemini: mid-stream upstream error after first chunk: %s", e
            )
            emit_event(
                "request_mid_stream_error",
                request_id=ctx.request_id,
                provider=chosen.name,
                error=str(e)[:200],
                endpoint="gemini",
            )

    async def _emit_sse() -> AsyncIterator[bytes]:
        async for byte_chunk in stream_openai_to_gemini(
            _merged_chunks(), request_model=requested_model
        ):
            yield byte_chunk
        final = last_usage_box[0]
        emit_event(
            "request_complete",
            request_id=ctx.request_id,
            provider=chosen.name,
            streaming=True,
            endpoint="gemini",
            input_tokens=final.input,
            output_tokens=final.output,
        )
        from freeride.core.telemetry import record_request

        record_request(
            input_tokens=final.input,
            output_tokens=final.output,
            provider=chosen.name,
        )

    return StreamingResponse(
        _emit_sse(),
        # gemini-cli's @google/genai SDK requests :streamGenerateContent
        # with ``?alt=sse`` and parses each ``data:`` line as JSON. SSE
        # is the right content type.
        media_type="text/event-stream",
        headers={
            "X-FreeRide-Provider": chosen.name,
            "X-FreeRide-Request-Id": ctx.request_id,
            "Cache-Control": "no-cache",
        },
    )
