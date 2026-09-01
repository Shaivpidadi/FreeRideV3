"""fx gateway dialect — the wire protocol of the ridex coding agent.

ridex (our fork of vercel-labs/fx) keeps fx's stock gateway transport
and points it at this gateway instead of ai-gateway.vercel.sh. That
transport speaks the Vercel AI SDK "language model" protocol, so the
fork carries zero new wire code — the translation lives here, next to
the Anthropic/Codex/Gemini shims.

Endpoints (paths must match fx's hardcoded constants in
``src/builtins/gateway.zig``):

- ``POST /v3/ai/language-model`` — chat. Model id and streaming mode
  arrive as HTTP headers (``ai-language-model-id``,
  ``ai-language-model-streaming``), not in the body.
- ``GET /coding-agent/v1/models`` — model catalog,
  ``{"data": [{"id", "type": "language", "tags": [...]}]}``. Also
  doubles as fx's API-key validation probe (any 200 = key accepted),
  which is what lets ridex run with a dummy Bearer token.

fx treats a missing ``/coding-agent/v1/credits`` as "no credit info"
(its own e2e mock 404s it), so we don't serve it.

Agent traffic is tool-call-critical: a model that answers in prose
instead of emitting tool calls makes the agent useless. The
``freeride/coding`` preset (ridex's default) therefore pins to a
known tools-capable model the same way the Claude Code shim does —
override via ``FREERIDE_FX_MODEL`` / ``FREERIDE_FX_PROVIDER``,
falling back to the claude-code pin env vars, then to
openrouter/free. Other presets keep their provider-preference
semantics from :mod:`freeride.core.model_router`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from freeride.core.auto_model import is_auto_model, resolve_auto_model
from freeride.core.cooldown import KeyCooldown
from freeride.core.events import emit as emit_event
from freeride.core.events import new_request_id
from freeride.core.failover import (
    FailoverContext,
    apply_force_provider,
    resolve_provider_chain,
    try_call_with_failover,
    try_stream_with_failover,
)
from freeride.core.fx_schema import (
    FX_MODEL_HEADER,
    FX_STREAMING_HEADER,
    FxRequest,
)
from freeride.core.fx_translate import fx_to_chat_request, stream_chat_to_fx
from freeride.core.health import sort_by_health
from freeride.core.model_router import (
    PRESET_CODING,
    parse_freeride_model,
    preset_provider_order,
)
from freeride.core.provider import Provider
from freeride.server.routes.models import get_or_fetch_catalog, invalidate_catalog

logger = logging.getLogger(__name__)
router = APIRouter()

# The fx-side presets we advertise in the catalog. ridex defaults to
# freeride/coding (tool-call reliability beats catalog breadth for an
# agent loop — see internal-docs/RIDEX_PLAN.md).
_FX_PRESET_IDS = (
    "freeride/coding",
    "freeride/free",
    "freeride/fast",
    "freeride/quality",
    "auto",
)


def _error_payload(message: str, code: str = "invalid_request_error") -> dict:
    """fx's failure diagnostics read ``error.message`` when present and
    otherwise show the raw body; OpenAI's envelope covers both."""
    return {"error": {"message": message, "type": code}}


def _agent_pin() -> tuple[str, str]:
    """(model_id, provider_name) to pin agent traffic to. Same
    tools-capable default the Claude Code shim uses, with fx-specific
    env overrides taking precedence."""
    model = os.environ.get("FREERIDE_FX_MODEL") or os.environ.get(
        "FREERIDE_CLAUDE_CODE_MODEL", "openrouter/free"
    )
    provider = os.environ.get("FREERIDE_FX_PROVIDER") or os.environ.get(
        "FREERIDE_CLAUDE_CODE_PROVIDER", "openrouter"
    )
    return model, provider


# The pin plus at most this many tools-capable fallbacks. Each candidate
# already carries its own per-key failover, so the ladder bounds worst-
# case pre-flight latency rather than reliability.
_MAX_AGENT_CANDIDATES = 4


def _agent_candidates(
    providers: list[Provider], catalog: list[dict[str, Any]]
) -> list[tuple[str, str]]:
    """Ordered (provider_name, model_id) fallback ladder for agent
    traffic: the pinned agent model first, then the first tools-capable
    catalog model on each remaining provider, in health order.

    ``catalog`` must be UNGROUPED (one entry per provider+model) so ids
    are valid on the provider they're paired with — grouped entries lose
    the per-provider alias mapping. Providers with no tools-capable
    entries (or whose entries the model-health cache marks broken) are
    skipped: an agent without tool calls is useless, so falling over to
    a text-only model would trade a visible failure for a silent one.
    """
    from freeride.core.model_health import is_model_known_broken, load_cache

    registered = {p.name for p in providers}
    pinned_model, pinned_provider = _agent_pin()
    ladder: list[tuple[str, str]] = []
    if pinned_provider in registered:
        ladder.append((pinned_provider, pinned_model))

    health_cache = load_cache()
    tools_by_provider: dict[str, str] = {}
    for entry in catalog:
        entry_providers = entry.get("available_providers") or []
        if not entry_providers:
            continue
        prov = entry_providers[0]
        if prov in tools_by_provider or prov not in registered:
            continue
        if "tools" not in (entry.get("supported_parameters") or ()):
            continue
        if is_model_known_broken(prov, entry["id"], cache=health_cache):
            continue
        tools_by_provider[prov] = entry["id"]

    for p in providers:  # already health-sorted by the route
        if len(ladder) >= _MAX_AGENT_CANDIDATES:
            break
        if p.name == pinned_provider:
            continue
        if p.name in tools_by_provider:
            ladder.append((p.name, tools_by_provider[p.name]))
    return ladder


@router.get("/coding-agent/v1/models")
async def fx_models(request: Request) -> dict[str, Any]:
    """fx model catalog. Presets first (always present, so ridex works
    before any provider key is configured and fx's key-validation probe
    gets its 200), then the live provider catalog."""
    data: list[dict[str, Any]] = [
        {"id": preset_id, "type": "language", "tags": ["tool-use"]}
        for preset_id in _FX_PRESET_IDS
    ]

    providers: list[Provider] = list(request.app.state.providers)
    try:
        catalog = await get_or_fetch_catalog(providers, group=True)
    except Exception as e:  # noqa: BLE001 — catalog trouble must not break key validation
        logger.warning("fx models: catalog fetch failed: %s", e)
        catalog = []

    for entry in catalog:
        obj: dict[str, Any] = {"id": entry["id"], "type": "language", "tags": []}
        if "tools" in (entry.get("supported_parameters") or ()):
            obj["tags"] = ["tool-use"]
        context_length = entry.get("context_length")
        if isinstance(context_length, int) and context_length > 0:
            obj["context_window"] = context_length
        data.append(obj)

    return {"data": data}


@router.post("/v3/ai/language-model")
async def fx_chat(request: Request):
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
        body = FxRequest.model_validate(peek)
    except ValidationError as e:
        raise HTTPException(
            status_code=400,
            detail=_error_payload(f"Request body failed validation: {e!s}"),
        )

    requested_model = request.headers.get(FX_MODEL_HEADER) or "auto"
    is_streaming = request.headers.get(FX_STREAMING_HEADER, "true").lower() != "false"
    preset = parse_freeride_model(requested_model)

    emit_event(
        "fx_routing_decision",
        request_id=request_id,
        model=requested_model,
        preset=preset,
        streaming=is_streaming,
        endpoint="fx",
    )

    openai_request = fx_to_chat_request(body, requested_model)
    openai_request.stream = False  # the failover helpers manage this

    providers: list[Provider] = sort_by_health(list(request.app.state.providers))

    # ─── model resolution ───────────────────────────────────────────
    # coding preset / auto → an internal fallback LADDER of
    # (provider, tools-capable model) candidates, pinned model first.
    # The pin is a preference, not a cage: when the pinned provider
    # can't serve (rate limit, no free inference right now, dead key),
    # the walk falls through to the best tools-capable model on each
    # remaining provider — silently, inside the same response. Other
    # presets → provider re-order + auto-resolution scoped to the
    # preferred providers, same shape as the messages route. A
    # concrete model id passes through untouched.
    auto_resolution_providers = providers
    agent_candidates: list[tuple[str, str]] | None = None
    if preset == PRESET_CODING or is_auto_model(requested_model):
        try:
            ladder_catalog = await get_or_fetch_catalog(providers, group=False)
        except Exception as e:  # noqa: BLE001 — a cold catalog must not kill the turn
            logger.warning("fx: ladder catalog fetch failed: %s", e)
            ladder_catalog = []
        agent_candidates = _agent_candidates(providers, ladder_catalog)
        if agent_candidates:
            openai_request.model = agent_candidates[0][1]
        emit_event(
            "fx_agent_ladder",
            request_id=request_id,
            candidates=[f"{prov}:{model}" for prov, model in agent_candidates],
            original_model=requested_model,
            endpoint="fx",
        )
        if not agent_candidates:
            # No catalog and no registered pin provider — fall back to
            # plain auto-resolution over the full chain.
            agent_candidates = None
            openai_request.model = "auto"
    elif preset is not None:
        preferred = preset_provider_order(preset)
        if preferred:
            preferred_set = set(preferred)
            head = [p for name in preferred for p in providers if p.name == name]
            tail = [p for p in providers if p.name not in preferred_set]
            providers = head + tail
            auto_resolution_providers = head or providers
        openai_request.model = "auto"

    providers, forced = apply_force_provider(providers, request)
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
        endpoint="fx",
    )

    if not providers:
        raise HTTPException(
            status_code=503,
            detail=_error_payload(
                "No provider plugins registered.", "service_unavailable"
            ),
        )

    cooldown = KeyCooldown()
    chain = resolve_provider_chain(providers)
    if not chain:
        raise HTTPException(
            status_code=503,
            detail=_error_payload(
                "No providers have usable (non-cooling) API keys for this request. "
                "Run `freeride init` to configure keys.",
                "service_unavailable",
            ),
        )

    if is_auto_model(openai_request.model):
        catalog = await get_or_fetch_catalog(auto_resolution_providers, group=True)
        resolved_id, resolved_provider = resolve_auto_model(
            auto_resolution_providers, catalog
        )
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
            endpoint="fx",
        )

    # Materialize the ladder into per-candidate (scoped_chain, model)
    # attempts. A candidate whose provider has no usable keys right now
    # contributes nothing; without a ladder there is one attempt over
    # the whole chain (legacy behavior for presets/concrete ids).
    attempts: list[tuple[list, str]] = []
    if agent_candidates:
        for prov_name, cand_model in agent_candidates:
            scoped = [link for link in chain if link[0].name == prov_name]
            if scoped:
                attempts.append((scoped, cand_model))
    if not attempts:
        attempts = [(chain, openai_request.model)]

    if is_streaming:
        openai_request.stream = True
        return await _build_fx_stream(
            attempts=attempts,
            openai_request=openai_request,
            cooldown=cooldown,
            ctx=ctx,
        )

    # ─── non-streaming ──────────────────────────────────────────────
    # fx's parseGatewayCompletion reads the plain OpenAI
    # ``choices[0].message`` shape — the upstream body IS the response.
    chosen_provider, response_obj = None, None
    for scoped_chain, cand_model in attempts:
        openai_request.model = cand_model
        chosen_provider, response_obj = await try_call_with_failover(
            scoped_chain,
            cooldown,
            ctx,
            call=lambda p, k: p.forward_chat(openai_request, openai_request.model, k),
            model=cand_model,
            extra_event={"endpoint": "fx"},
            on_model_not_found=invalidate_catalog,
        )
        if response_obj is not None:
            break
        emit_event(
            "fx_candidate_failed",
            request_id=ctx.request_id,
            model=cand_model,
            endpoint="fx",
        )

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
        endpoint="fx",
    )
    from freeride.core.telemetry import record_request
    from freeride.core.usage import Kind, extract_usage

    cd_usage = extract_usage(Kind.OPENAI, response_obj.model_dump())
    record_request(
        input_tokens=cd_usage.input,
        output_tokens=cd_usage.output,
        provider=chosen_provider.name if chosen_provider else None,
    )

    return JSONResponse(
        content=response_obj.model_dump(exclude_none=True),
        headers={
            "X-FreeRide-Provider": chosen_provider.name if chosen_provider else "unknown",
            "X-FreeRide-Request-Id": ctx.request_id,
        },
    )


async def _build_fx_stream(
    *,
    attempts: list[tuple[list, str]],
    openai_request,
    cooldown: KeyCooldown,
    ctx: FailoverContext,
) -> StreamingResponse:
    """Walk the (scoped_chain, model) attempts through the shared
    failover helper until one produces a first chunk, then re-frame
    Chat-shape deltas into fx AI-SDK stream parts.

    Unlike the other streaming routes, the 200 + headers ship
    IMMEDIATELY and SSE keepalive comments flow while the failover
    pre-flight waits for the upstream's first token. fx enforces a
    time-to-first-byte budget and free-tier TTFB regularly exceeds it,
    which made every agent turn eat one or two visible retries; comment
    frames are fx's own hold-the-line mechanism (its e2e fake gateway
    sends ``: hold-response``). Candidate fallbacks ride the same
    mechanism, so a provider with no free inference right now costs
    keepalive time, never a visible failure. The cost: a total
    pre-flight failure is reported in-stream as an ``error`` part +
    ``finish`` with ``unified: "error"`` instead of an HTTP 503 — fx
    classifies both as a retryable provider failure."""
    from freeride.core.usage import Kind, extract_usage

    async def _emit_sse() -> AsyncIterator[bytes]:
        chosen, first_event, rest_or_err = None, None, None
        for scoped_chain, cand_model in attempts:
            openai_request.model = cand_model
            preflight = asyncio.ensure_future(
                try_stream_with_failover(
                    scoped_chain, openai_request, cooldown, ctx,
                    extra_event={"endpoint": "fx"},
                    on_model_not_found=invalidate_catalog,
                )
            )
            while True:
                try:
                    chosen, first_event, rest_or_err = await asyncio.wait_for(
                        asyncio.shield(preflight), timeout=5.0
                    )
                    break
                except asyncio.TimeoutError:
                    yield b": preflight\n\n"
            if chosen is not None:
                break
            emit_event(
                "fx_candidate_failed",
                request_id=ctx.request_id,
                model=cand_model,
                endpoint="fx",
            )

        if chosen is None:
            emit_event(
                "request_failed",
                request_id=ctx.request_id,
                phase="pre_first_chunk",
                tried=[t.provider for t in ctx.tried],
                endpoint="fx",
            )
            message = "All providers exhausted without producing a streaming response."
            yield _sse_error_events(message)
            return

        last_usage_box = [extract_usage(Kind.OPENAI, first_event.model_dump())]

        async def _merged_chunks() -> AsyncIterator:
            yield first_event
            try:
                async for evt in rest_or_err:
                    u = extract_usage(Kind.OPENAI, evt.model_dump())
                    if u.has_any:
                        last_usage_box[0] = u
                    yield evt
            except Exception as e:  # noqa: BLE001
                logger.warning("fx: mid-stream upstream error after first chunk: %s", e)
                emit_event(
                    "request_mid_stream_error",
                    request_id=ctx.request_id,
                    provider=chosen.name,
                    error=str(e)[:200],
                    endpoint="fx",
                )

        async for byte_chunk in stream_chat_to_fx(
            _merged_chunks(), resolved_model=openai_request.model
        ):
            yield byte_chunk
        final = last_usage_box[0]
        emit_event(
            "request_complete",
            request_id=ctx.request_id,
            provider=chosen.name,
            streaming=True,
            endpoint="fx",
            input_tokens=final.input,
            output_tokens=final.output,
        )
        from freeride.core.telemetry import record_request

        record_request(
            input_tokens=final.input,
            output_tokens=final.output,
            provider=chosen.name,
        )

    # Headers ship before the pre-flight resolves, so the chosen
    # provider can't be stamped here; telemetry has it via
    # request_complete and the debug event stream.
    return StreamingResponse(
        _emit_sse(),
        media_type="text/event-stream",
        headers={
            "X-FreeRide-Request-Id": ctx.request_id,
            "Cache-Control": "no-cache",
        },
    )


def _sse_error_events(message: str) -> bytes:
    """Terminal in-stream failure: an ``error`` part (fx captures the
    detail for its notice line) followed by ``finish`` with
    ``unified: "error"`` and ``[DONE]``."""
    error_event = json.dumps(
        {"type": "error", "error": {"type": "service_unavailable", "message": message}},
        separators=(",", ":"),
    )
    finish_event = json.dumps(
        {"type": "finish", "finishReason": {"unified": "error", "raw": "error"}},
        separators=(",", ":"),
    )
    return (
        f"data: {error_event}\n\ndata: {finish_event}\n\ndata: [DONE]\n\n".encode()
    )
