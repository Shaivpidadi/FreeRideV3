"""``POST /v1/messages`` — Anthropic Messages API compatibility shim.

Lets Anthropic-format clients (Claude Code, ``@anthropic-ai/sdk``,
LiteLLM with the Anthropic adapter, etc.) talk to FreeRide as if it
were ``api.anthropic.com``. Internally we translate the request into
OpenAI Chat Completions, dispatch it through the same provider
failover machinery `/v1/chat/completions` uses, and translate the
response back into Anthropic shape on the way out.

**Phase 1 (shipped):** non-streaming chat, system prompt hoisting,
text content blocks, stop_reason and usage mapping, tool-definition
request-side translation.

**Phase 2 (this commit):** streaming SSE. We pre-flight the first
chunk through ``_try_stream_with_failover`` (same buffer-first-chunk
guarantee the chat route uses), then a translator generator turns
OpenAI streaming chunks into Anthropic SSE events
(message_start / content_block_start / content_block_delta /
content_block_stop / message_delta / message_stop). Text-only blocks
in this phase; tool_use streaming lands in Phase 3.

**Phase 3 (deferred):** tool_use blocks in messages, tool_result
handling, the ``input_json_delta`` partial-JSON streaming state
machine.

The non-streaming failover loop is inlined to keep scope tight; a
Phase 4 cleanup will extract the loop into a shared helper so both
routes stay in lockstep.
"""

from __future__ import annotations

import json
import logging
import time

from typing import AsyncIterator

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from freeride.core.anthropic_passthrough import relay_to_anthropic
from freeride.core.anthropic_schema import AnthropicMessagesRequest
from freeride.core.anthropic_translate import (
    UnsupportedContentBlock,
    anthropic_to_openai_request,
    openai_to_anthropic_response,
    request_unsupported_for_phase_1,
    stream_openai_to_anthropic,
)
from freeride.core.auto_model import is_auto_model, resolve_auto_model
from freeride.core.cooldown import KeyCooldown
from freeride.core.errors import ErrorKind
from freeride.core.events import emit as emit_event
from freeride.core.events import new_request_id
from freeride.core.health import sort_by_health, sort_keys_by_health
from freeride.core.model_router import decide as decide_route
from freeride.core.model_router import preset_provider_order
from freeride.core.provider import Provider
from freeride.server.routes.chat import (
    FailoverContext,
    _apply_force_provider,
    _build_503_detail,
    _record_health,
    _resolve_provider_chain,
    _try_stream_with_failover,
)
from freeride.server.routes.models import get_or_fetch_catalog, invalidate_catalog


logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/v1/messages")
async def messages(request: Request):
    """Anthropic-format ``POST /v1/messages`` endpoint.

    Two execution paths, chosen by the model id + inbound auth:

    - **Passthrough** — ``claude-*`` model id + inbound auth header
      (Authorization or x-api-key). The request body is relayed
      verbatim to ``api.anthropic.com``; FreeRide stays invisible.
      Native Claude Code subscriptions work untouched.
    - **Free route** — ``freeride/*`` model ids, or ``claude-*`` with
      no auth, or anything else. Translates to OpenAI shape, dispatches
      through provider failover, translates back to Anthropic shape.
      All of Phases 1–3 land here.

    The decision is made by
    :func:`freeride.core.model_router.decide`. We peek at the raw
    body bytes once to extract ``model`` (and ``stream`` for the
    passthrough), then either relay raw or parse + translate.
    """
    request_id = new_request_id()
    body_bytes = await request.body()
    inbound_headers = dict(request.headers)

    # Cheap peek to extract the model id without full Pydantic
    # validation. For passthrough we MUST NOT validate — that would
    # risk dropping fields Anthropic accepts that our schema hasn't
    # caught up to. For the free route we validate below.
    try:
        peek = json.loads(body_bytes) if body_bytes else {}
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=400,
            detail={
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": "Request body is not valid JSON.",
                },
            },
        )
    if not isinstance(peek, dict):
        raise HTTPException(
            status_code=400,
            detail={
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": "Request body must be a JSON object.",
                },
            },
        )

    model_id = peek.get("model") or ""
    decision = decide_route(model_id, inbound_headers)

    emit_event(
        "messages_routing_decision",
        request_id=request_id,
        model=model_id,
        mode=decision.mode,
        preset=decision.preset,
        reason=decision.reason,
        endpoint="messages",
    )

    # ─── passthrough ───────────────────────────────────────────────
    # Relay to api.anthropic.com verbatim. Body bytes, auth header,
    # and a small allowlist of Anthropic-specific headers forward
    # unchanged. The native subscription experience is preserved.
    if decision.mode == "passthrough":
        return await relay_to_anthropic(
            body_bytes=body_bytes,
            inbound_headers=inbound_headers,
            request_id=request_id,
            model_id=model_id,
        )

    # ─── free route ────────────────────────────────────────────────
    # Validate via Pydantic now. We deferred this so passthrough
    # could ship the raw body without risk of re-serialization
    # losing fields.
    try:
        body = AnthropicMessagesRequest.model_validate(peek)
    except ValidationError as e:
        raise HTTPException(
            status_code=422,
            detail={
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": f"Request body failed validation: {e!s}",
                },
            },
        )

    # Gate features we haven't shipped (images, documents). Tools
    # and streaming are supported.
    block_reason = request_unsupported_for_phase_1(body)
    if block_reason is not None:
        raise HTTPException(
            status_code=501,
            detail={
                "type": "error",
                "error": {
                    "type": "not_implemented",
                    "message": (
                        f"FreeRide /v1/messages: {block_reason}. Track Phase "
                        f"progress at https://github.com/Shaivpidadi/FreeRideV3."
                    ),
                },
            },
        )

    requested_model = body.model

    # Translate to OpenAI shape. UnsupportedContentBlock surfaces as a
    # 400 — caller sent a content type we can't handle.
    try:
        openai_request = anthropic_to_openai_request(body)
    except UnsupportedContentBlock as e:
        raise HTTPException(
            status_code=400,
            detail={
                "type": "error",
                "error": {"type": "invalid_request_error", "message": str(e)},
            },
        )

    # ─── borrow the chat-route's failover plumbing ─────────────────
    # Health-rank, force-provider override, build chain — all the
    # same shape as /v1/chat/completions so smart-routing, cooldown,
    # and per-key health all apply uniformly.
    providers: list[Provider] = sort_by_health(list(request.app.state.providers))
    providers, forced = _apply_force_provider(providers, request)
    if forced is not None and not providers:
        raise HTTPException(
            status_code=400,
            detail={
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": (
                        f"X-FreeRide-Force-Provider={forced!r} is not "
                        "a registered provider."
                    ),
                    "registered": [p.name for p in request.app.state.providers],
                },
            },
        )

    ctx = FailoverContext(request_id=request_id)
    emit_event(
        "request_start",
        request_id=ctx.request_id,
        model=openai_request.model,
        streaming=body.stream,
        endpoint="messages",
    )

    if not providers:
        raise HTTPException(
            status_code=503,
            detail={
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": "No provider plugins registered.",
                    "request_id": ctx.request_id,
                },
            },
        )

    cooldown = KeyCooldown()

    # ─── preset → provider re-order + auto-resolution scope ────────
    # For freeride/fast|quality|coding, two things change:
    #
    #   1. The failover chain is re-ordered so preferred providers
    #      come first (existing health-rank fills ties).
    #   2. Auto-model resolution is RESTRICTED to preferred providers
    #      only. Without this restriction, the smart-router would
    #      pick the highest-ranked model across the whole catalog —
    #      often a groq-specific id — and the failover would try the
    #      preferred provider first, get a MODEL_NOT_FOUND, and fall
    #      back to groq anyway. That defeats the preset's purpose.
    #
    # ``auto_resolution_providers`` is what gets passed to the
    # catalog fetch + resolver. ``providers`` (the full preset-
    # ordered chain) is what gets used for the failover loop, so a
    # rare key failure on a preferred provider still has the tail
    # as a last-resort fallback.
    preferred = preset_provider_order(decision.preset)
    auto_resolution_providers = providers
    if preferred:
        preferred_set = set(preferred)
        head = [p for name in preferred for p in providers if p.name == name]
        tail = [p for p in providers if p.name not in preferred_set]
        providers = head + tail
        # Typed preset: restrict catalog ranking to preferred
        # providers (head) so the resolved model id is actually
        # available there. If none of the preferred providers are
        # registered/healthy, fall back to the full list rather
        # than 503 — better degraded than down.
        auto_resolution_providers = head or providers
        # The id "freeride/<preset>" isn't a real model on any
        # provider — rewrite it to "auto" so the existing smart-
        # router picks something. freeride/free already maps to
        # auto via the _AUTO_SENTINELS frozenset, but typed
        # presets don't.
        openai_request.model = "auto"
        emit_event(
            "messages_preset_applied",
            request_id=ctx.request_id,
            preset=decision.preset,
            preferred_order=list(preferred),
            auto_resolution_restricted=bool(head),
            endpoint="messages",
        )
    elif decision.preset == "free":
        # Bare "freeride/free" — rewrite to "auto" so the smart
        # router doesn't see an unknown model id. No restriction;
        # full catalog is fair game.
        openai_request.model = "auto"

    chain = _resolve_provider_chain(providers)
    if not chain:
        raise HTTPException(
            status_code=503,
            detail={
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": (
                        "No providers have usable (non-cooling) API keys "
                        "for this request."
                    ),
                    "request_id": ctx.request_id,
                    "suggestion": (
                        "Set a provider env var (e.g. OPENROUTER_API_KEY) "
                        "or wait for cooldowns to expire."
                    ),
                },
            },
        )

    # auto-model resolution — same logic as the chat route, but
    # scoped to ``auto_resolution_providers`` when a typed preset
    # is in play (set above). Without the scoping, a typed preset
    # only re-orders the chain; here it also restricts WHICH
    # provider catalogs the resolver considers.
    if is_auto_model(openai_request.model):
        catalog = await get_or_fetch_catalog(auto_resolution_providers, group=True)
        resolved_id, resolved_provider = resolve_auto_model(
            auto_resolution_providers, catalog
        )
        if resolved_id is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "type": "error",
                    "error": {
                        "type": "api_error",
                        "message": (
                            "model='auto' was requested but no provider has "
                            "a usable model + key right now."
                        ),
                        "request_id": ctx.request_id,
                    },
                },
            )
        openai_request.model = resolved_id
        emit_event(
            "auto_model_resolved",
            request_id=ctx.request_id,
            resolved_model=resolved_id,
            resolved_provider=resolved_provider,
            endpoint="messages",
        )

    # ─── streaming branch — Phase 2 ────────────────────────────────
    # Reuses chat.py's _try_stream_with_failover for the
    # buffer-first-chunk-then-fail-over guarantee. Translation is
    # done by stream_openai_to_anthropic which consumes the
    # ChatStreamEvent iterator and emits Anthropic-shape SSE events.
    if body.stream:
        return await _build_anthropic_stream_response(
            chain=chain,
            openai_request=openai_request,
            cooldown=cooldown,
            ctx=ctx,
            requested_model=requested_model,
        )

    # ─── failover loop — non-streaming ─────────────────────────────
    # Mirrors chat_completions(). Phase 4 will extract this into a
    # shared helper so the chat and messages routes stay in lockstep.
    chosen_provider: Provider | None = None
    response_obj = None
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
                endpoint="messages",
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
                if kind == ErrorKind.RATE_LIMIT:
                    summary.retry_after_s = provider.retry_after_hint(e.response)
                emit_event(
                    "provider_response",
                    request_id=ctx.request_id,
                    provider=provider.name,
                    key_index=key_idx,
                    duration_ms=duration_ms,
                    status=kind.value,
                    endpoint="messages",
                    **(
                        {"retry_after_s": summary.retry_after_s}
                        if summary.retry_after_s
                        else {}
                    ),
                )
                _record_health(
                    provider.name, ok=False, duration_ms=duration_ms, key=key
                )
                if kind == ErrorKind.MODEL_NOT_FOUND:
                    invalidate_catalog()
                if kind in (ErrorKind.MODEL_NOT_FOUND, ErrorKind.QUOTA_EXHAUSTED):
                    break
                continue
            except httpx.HTTPError as e:
                duration_ms = int((time.perf_counter() - t0) * 1000)
                summary.last_error = provider.classify_error(e)
                emit_event(
                    "provider_response",
                    request_id=ctx.request_id,
                    provider=provider.name,
                    key_index=key_idx,
                    duration_ms=duration_ms,
                    status=summary.last_error.value,
                    endpoint="messages",
                )
                _record_health(
                    provider.name, ok=False, duration_ms=duration_ms, key=key
                )
                continue
            except Exception as e:  # noqa: BLE001
                duration_ms = int((time.perf_counter() - t0) * 1000)
                logger.warning(
                    "messages: provider %s raised %s", provider.name, e
                )
                summary.last_error = ErrorKind.UNKNOWN
                _record_health(
                    provider.name, ok=False, duration_ms=duration_ms, key=key
                )
                continue
            duration_ms = int((time.perf_counter() - t0) * 1000)
            emit_event(
                "provider_response",
                request_id=ctx.request_id,
                provider=provider.name,
                key_index=key_idx,
                duration_ms=duration_ms,
                status="ok",
                endpoint="messages",
            )
            _record_health(
                provider.name, ok=True, duration_ms=duration_ms, key=key
            )
            chosen_provider = provider
            break
        if response_obj is not None:
            break

    if response_obj is None or chosen_provider is None:
        # Surface the 503 in Anthropic shape so SDK clients see a
        # familiar error envelope.
        raw = _build_503_detail(ctx)
        raw_err = raw.get("error", {})
        raise HTTPException(
            status_code=503,
            detail={
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": raw_err.get("message", "All providers failed."),
                    "request_id": ctx.request_id,
                    "tried": raw_err.get("tried"),
                    "suggestion": raw_err.get("suggestion"),
                },
            },
        )

    # Translate the response back into Anthropic shape. Echo the
    # caller's requested model id (e.g. ``claude-sonnet-4-6``) so SDK
    # clients see a familiar string; the actual routed provider is
    # exposed via the ``X-FreeRide-Provider`` header.
    anthropic_response = openai_to_anthropic_response(response_obj, requested_model)

    return JSONResponse(
        content=anthropic_response.model_dump(exclude_none=True),
        headers={
            "X-FreeRide-Provider": chosen_provider.name,
            "X-FreeRide-Request-Id": ctx.request_id,
        },
    )


# ─── streaming response builder ────────────────────────────────────


async def _build_anthropic_stream_response(
    *,
    chain: list,
    openai_request,
    cooldown: KeyCooldown,
    ctx: FailoverContext,
    requested_model: str,
) -> StreamingResponse:
    """Pre-flight first chunk through the chat-route's
    ``_try_stream_with_failover``, then wrap the resulting
    ChatStreamEvent iterator in a translator that emits Anthropic SSE.

    The buffer-first-chunk semantics matter: if the first upstream
    chunk fails, we can still failover to a different provider /
    key. Once any byte has shipped to the client, we're committed —
    a mid-stream upstream failure becomes a truncated stream from
    the client's perspective (rare in practice; documented limit).
    """
    chosen, first_event, rest_or_err = await _try_stream_with_failover(
        chain, openai_request, cooldown, ctx
    )
    if chosen is None:
        # All providers failed before producing a first chunk.
        emit_event(
            "request_failed",
            request_id=ctx.request_id,
            phase="pre_first_chunk",
            tried=[t.provider for t in ctx.tried],
            endpoint="messages",
        )
        raw = _build_503_detail(ctx)
        raw_err = raw.get("error", {})
        raise HTTPException(
            status_code=503,
            detail={
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": raw_err.get("message", "All providers failed."),
                    "request_id": ctx.request_id,
                    "tried": raw_err.get("tried"),
                    "suggestion": raw_err.get("suggestion"),
                },
            },
        )

    rest_iter = rest_or_err  # AsyncIterator[ChatStreamEvent]

    async def merged_chunks() -> AsyncIterator:
        """Re-thread the first event back in front of the rest, so the
        translator sees a single contiguous stream."""
        yield first_event
        try:
            async for evt in rest_iter:
                yield evt
        except Exception as e:  # noqa: BLE001
            # Mid-stream upstream error after the first chunk shipped.
            # We can't undo bytes already on the wire, so we let the
            # translator complete its event sequence (it'll emit a
            # message_stop with whatever state it has) and log.
            import logging

            logging.getLogger(__name__).warning(
                "messages: mid-stream upstream error after first chunk: %s", e
            )
            emit_event(
                "request_mid_stream_error",
                request_id=ctx.request_id,
                provider=chosen.name,
                error=str(e)[:200],
                endpoint="messages",
            )

    async def emit_anthropic_sse() -> AsyncIterator[bytes]:
        async for byte_chunk in stream_openai_to_anthropic(
            merged_chunks(), request_model=requested_model
        ):
            yield byte_chunk
        emit_event(
            "request_complete",
            request_id=ctx.request_id,
            provider=chosen.name,
            streaming=True,
            endpoint="messages",
        )

    return StreamingResponse(
        emit_anthropic_sse(),
        media_type="text/event-stream",
        headers={
            "X-FreeRide-Provider": chosen.name,
            "X-FreeRide-Request-Id": ctx.request_id,
            # Anthropic clients sometimes check for these — match the
            # behavior of api.anthropic.com closely.
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
