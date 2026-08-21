"""``POST /v1/embeddings`` — OpenAI-compatible embeddings with cross-provider failover.

Mirrors the failover semantics of ``/v1/chat/completions`` but only over
the providers that opt in via ``embeddings_supported = True``. Groq
currently does not expose an embeddings endpoint, so it's skipped.

Same event-emission and structured-503 contract as chat: every transition
lands in ``~/.freeride/events.jsonl`` for ``freeride watch`` to tail, and
on all-failure the response is a structured JSON with a ``tried`` array
plus an actionable ``suggestion``.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from freeride.core.cooldown import KeyCooldown
from freeride.core.embedding_schema import EmbeddingRequest
from freeride.core.events import emit as emit_event
from freeride.core.events import new_request_id
from freeride.core.failover import (
    FailoverContext,
    apply_force_provider,
    build_503_detail,
    resolve_provider_chain,
    try_call_with_failover,
)
from freeride.core.health import sort_by_health
from freeride.core.provider import Provider

router = APIRouter()


def _embedding_capable(p: Provider) -> bool:
    """A provider opts into embeddings by setting ``embeddings_supported = True``
    on the class. Anything else (False, missing attr) is treated as no-support
    and the failover loop skips it.
    """
    return bool(getattr(p, "embeddings_supported", False))


@router.post("/v1/embeddings")
async def embeddings(request: Request, body: EmbeddingRequest):
    providers: list[Provider] = sort_by_health(list(request.app.state.providers))
    providers, forced = apply_force_provider(providers, request)
    if forced is not None and not providers:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "type": "force_provider_unknown",
                    "message": f"X-FreeRide-Force-Provider={forced!r} is not a registered provider.",
                    "registered": [p.name for p in request.app.state.providers],
                }
            },
        )

    ctx = FailoverContext(request_id=new_request_id())

    emit_event(
        "request_start",
        request_id=ctx.request_id,
        model=body.model,
        endpoint="embeddings",
    )

    if not providers:
        emit_event("request_failed", request_id=ctx.request_id, phase="no_providers")
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "type": "no_providers",
                    "message": "No provider plugins registered.",
                    "request_id": ctx.request_id,
                }
            },
        )

    # Filter to providers that opt into embeddings BEFORE walking the chain.
    capable = [p for p in providers if _embedding_capable(p)]
    if not capable:
        emit_event(
            "request_failed",
            request_id=ctx.request_id,
            phase="no_embedding_provider",
            providers=[p.name for p in providers],
        )
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "type": "no_embedding_provider",
                    "message": "No registered provider supports embeddings.",
                    "request_id": ctx.request_id,
                    "configured_providers": [p.name for p in providers],
                    "embedding_capable": [p.name for p in providers if _embedding_capable(p)],
                    "suggestion": (
                        "Add a key for OpenRouter, NVIDIA NIM, Cloudflare Workers AI, "
                        "or HuggingFace — Groq does not currently offer embeddings."
                    ),
                }
            },
        )

    cooldown = KeyCooldown()
    chain = resolve_provider_chain(capable)
    if not chain:
        emit_event(
            "request_failed",
            request_id=ctx.request_id,
            phase="no_usable_keys",
            providers=[p.name for p in capable],
        )
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "type": "no_usable_keys",
                    "message": "No embedding-capable providers have usable (non-cooling) API keys.",
                    "request_id": ctx.request_id,
                    "configured_providers": [p.name for p in capable],
                    "suggestion": (
                        "Either set a provider env var (e.g. OPENROUTER_API_KEY) "
                        "or wait for cooldowns to expire."
                    ),
                }
            },
        )

    chosen_provider, response = await try_call_with_failover(
        chain,
        cooldown,
        ctx,
        call=lambda p, k: p.forward_embeddings(body, body.model, k),
        model=body.model,
        extra_event={"endpoint": "embeddings"},
    )

    if response is None or chosen_provider is None:
        emit_event(
            "request_failed",
            request_id=ctx.request_id,
            phase="all_attempts_exhausted",
            tried=[t.provider for t in ctx.tried],
        )
        return JSONResponse(status_code=503, content=build_503_detail(ctx))

    emit_event(
        "request_complete",
        request_id=ctx.request_id,
        provider=chosen_provider.name,
        endpoint="embeddings",
    )
    out = response.model_dump(exclude_none=False)
    out["_freeride_provider"] = chosen_provider.name
    out["_freeride_request_id"] = ctx.request_id
    return JSONResponse(
        content=out,
        headers={
            "X-FreeRide-Provider": chosen_provider.name,
            "X-FreeRide-Request-ID": ctx.request_id,
        },
    )
