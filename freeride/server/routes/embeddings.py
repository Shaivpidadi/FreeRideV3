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

import logging
import time

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from freeride.core.cooldown import KeyCooldown
from freeride.core.embedding_schema import EmbeddingRequest, EmbeddingResponse
from freeride.core.errors import ErrorKind
from freeride.core.events import emit as emit_event
from freeride.core.events import new_request_id
from freeride.core.provider import Provider
from freeride.server.routes.chat import (
    FailoverContext,
    _build_503_detail,
    _resolve_provider_chain,
)


logger = logging.getLogger(__name__)
router = APIRouter()


def _embedding_capable(p: Provider) -> bool:
    """A provider opts into embeddings by setting ``embeddings_supported = True``
    on the class. Anything else (False, missing attr) is treated as no-support
    and the failover loop skips it.
    """
    return bool(getattr(p, "embeddings_supported", False))


@router.post("/v1/embeddings")
async def embeddings(request: Request, body: EmbeddingRequest):
    providers: list[Provider] = list(request.app.state.providers)
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
    chain = _resolve_provider_chain(capable)
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
                    "suggestion": "Either set a provider env var (e.g. OPENROUTER_API_KEY) or wait for cooldowns to expire (~120s).",
                }
            },
        )

    # Cross-provider failover loop. Same shape as chat: RATE_LIMIT/AUTH
    # advance keys; MODEL_NOT_FOUND/QUOTA_EXHAUSTED advance providers.
    chosen_provider: Provider | None = None
    response: EmbeddingResponse | None = None
    for provider, keys in chain:
        summary = ctx.attempt(provider.name)
        for key_idx, key in enumerate(keys):
            summary.keys_tried += 1
            emit_event(
                "provider_attempt",
                request_id=ctx.request_id,
                provider=provider.name,
                key_index=key_idx,
                model=body.model,
                endpoint="embeddings",
            )
            t0 = time.perf_counter()
            try:
                response = await provider.forward_embeddings(body, body.model, key)
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
                    **(
                        {"retry_after_s": summary.retry_after_s}
                        if summary.retry_after_s
                        else {}
                    ),
                )
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
                )
                continue
            duration_ms = int((time.perf_counter() - t0) * 1000)
            emit_event(
                "provider_response",
                request_id=ctx.request_id,
                provider=provider.name,
                key_index=key_idx,
                duration_ms=duration_ms,
                status="OK",
            )
            chosen_provider = provider
            break
        if response is not None:
            break

    if response is None or chosen_provider is None:
        emit_event(
            "request_failed",
            request_id=ctx.request_id,
            phase="all_attempts_exhausted",
            tried=[t.provider for t in ctx.tried],
        )
        return JSONResponse(status_code=503, content=_build_503_detail(ctx))

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
