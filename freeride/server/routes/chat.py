"""``POST /v1/chat/completions`` — OpenAI-compatible chat completions.

Cross-provider failover: walks the (provider, key) chain. On
``RATE_LIMIT``/``AUTH`` advance keys; on ``MODEL_NOT_FOUND`` /
``QUOTA_EXHAUSTED`` advance providers; on success ship the response
and stamp ``X-FreeRide-Provider``.

Streaming uses buffer-first-chunk failover: hold the first SSE event
until upstream confirms 200 + first chunk. If upstream fails before
the first chunk, retry on the next (provider, key). Once the first
chunk has shipped to the client, mid-stream errors propagate as a
truncated stream (rare in practice; documented limitation).

Observability: every transition emits a JSONL event to
``~/.freeride/events.jsonl`` for ``freeride watch`` to tail. On
all-failure, the 503 response carries a structured ``tried`` array
per-provider so clients (and humans) can see exactly which providers
were attempted, how many keys, and the last error per provider.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from freeride.core.chat_schema import ChatRequest, ChatResponse, ChatStreamEvent
from freeride.core.cooldown import KeyCooldown
from freeride.core.errors import ErrorKind
from freeride.core.events import emit as emit_event
from freeride.core.events import new_request_id
from freeride.core.provider import Provider


logger = logging.getLogger(__name__)
router = APIRouter()


def _env_var_for(provider_name: str) -> str:
    return {
        "openrouter": "OPENROUTER_API_KEY",
        "nvidia_nim": "NVIDIA_API_KEY",
        "groq": "GROQ_API_KEY",
        "cloudflare_wai": "CLOUDFLARE_API_TOKEN",
        "huggingface": "HF_TOKEN",
    }.get(provider_name, f"{provider_name.upper()}_API_KEY")


def _all_keys_for(provider_name: str) -> list[str]:
    raw = os.environ.get(_env_var_for(provider_name), "")
    if not raw:
        return []
    from freeride.v2compat.models import _parse_api_keys

    return _parse_api_keys(raw)


def _resolve_provider_chain(providers: list[Provider]) -> list[tuple[Provider, list[str]]]:
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


@dataclass
class AttemptSummary:
    """One row in the per-provider attempt log surfaced in 503 responses."""

    provider: str
    keys_tried: int = 0
    last_error: ErrorKind | None = None
    retry_after_s: int | None = None


@dataclass
class FailoverContext:
    request_id: str
    tried: list[AttemptSummary] = field(default_factory=list)

    def attempt(self, provider_name: str) -> AttemptSummary:
        for s in self.tried:
            if s.provider == provider_name:
                return s
        s = AttemptSummary(provider=provider_name)
        self.tried.append(s)
        return s


def _suggestion(tried: list[AttemptSummary]) -> str:
    """Pick the most actionable hint based on the failure mix."""
    if not tried:
        return "No providers had usable keys. Set at least one provider env var (e.g. OPENROUTER_API_KEY)."
    kinds = {t.last_error for t in tried if t.last_error is not None}
    cooling = [t for t in tried if t.retry_after_s]
    if kinds == {ErrorKind.RATE_LIMIT} and cooling:
        soonest = min(t.retry_after_s for t in cooling if t.retry_after_s)
        return f"All providers rate-limited. Soonest retry-after: ~{soonest}s. Add a Groq or HF key for more failover headroom."
    if ErrorKind.AUTH in kinds:
        bad = [t.provider for t in tried if t.last_error == ErrorKind.AUTH]
        env_vars = ", ".join(_env_var_for(p) for p in bad)
        return f"Auth failed on {', '.join(bad)}. Verify {env_vars} is set correctly."
    if ErrorKind.MODEL_NOT_FOUND in kinds:
        return "Model not available on any provider. Run `freeride list` to see what's currently free."
    if ErrorKind.QUOTA_EXHAUSTED in kinds:
        return "Free-tier budgets exhausted across providers. Wait for the next billing cycle, or add another provider's free key."
    return "All providers failed. Run `freeride watch` in another terminal to see live transitions."


def _build_503_detail(ctx: FailoverContext) -> dict[str, Any]:
    return {
        "error": {
            "type": "all_upstreams_failed",
            "message": "All providers/keys exhausted before a successful response.",
            "request_id": ctx.request_id,
            "tried": [
                {
                    "provider": t.provider,
                    "keys_tried": t.keys_tried,
                    "last_error": t.last_error.value if t.last_error else None,
                    **(
                        {"retry_after_s": t.retry_after_s}
                        if t.retry_after_s is not None
                        else {}
                    ),
                }
                for t in ctx.tried
            ],
            "suggestion": _suggestion(ctx.tried),
        }
    }


async def _try_stream_with_failover(
    chain: list[tuple[Provider, list[str]]],
    body: ChatRequest,
    cooldown: KeyCooldown,
    ctx: FailoverContext,
) -> (
    tuple[Provider, ChatStreamEvent, AsyncIterator[ChatStreamEvent]]
    | tuple[None, None, ErrorKind]
):
    last_error: ErrorKind | None = None
    for provider, keys in chain:
        summary = ctx.attempt(provider.name)
        provider_done = False
        for key_idx, key in enumerate(keys):
            if provider_done:
                break
            summary.keys_tried += 1
            emit_event(
                "provider_attempt",
                request_id=ctx.request_id,
                provider=provider.name,
                key_index=key_idx,
                model=body.model,
                streaming=True,
            )
            t0 = time.perf_counter()
            gen = provider.forward_chat_stream(body, body.model, key)
            try:
                first = await gen.__anext__()
            except StopAsyncIteration:
                duration_ms = int((time.perf_counter() - t0) * 1000)
                last_error = ErrorKind.UNKNOWN
                summary.last_error = last_error
                emit_event(
                    "provider_response",
                    request_id=ctx.request_id,
                    provider=provider.name,
                    key_index=key_idx,
                    duration_ms=duration_ms,
                    status=last_error.value,
                )
                continue
            except httpx.HTTPStatusError as e:
                duration_ms = int((time.perf_counter() - t0) * 1000)
                try:
                    await e.response.aread()
                except Exception:
                    pass
                kind = provider.classify_error(e.response)
                if kind in (ErrorKind.RATE_LIMIT, ErrorKind.AUTH):
                    cooldown.mark_rate_limited(provider.name, key)
                if kind == ErrorKind.MODEL_NOT_FOUND or kind == ErrorKind.QUOTA_EXHAUSTED:
                    provider_done = True
                summary.last_error = kind
                if kind == ErrorKind.RATE_LIMIT:
                    summary.retry_after_s = provider.retry_after_hint(e.response)
                last_error = kind
                emit_event(
                    "provider_response",
                    request_id=ctx.request_id,
                    provider=provider.name,
                    key_index=key_idx,
                    duration_ms=duration_ms,
                    status=kind.value,
                    **({"retry_after_s": summary.retry_after_s} if summary.retry_after_s else {}),
                )
                continue
            except httpx.HTTPError as e:
                duration_ms = int((time.perf_counter() - t0) * 1000)
                last_error = provider.classify_error(e)
                summary.last_error = last_error
                emit_event(
                    "provider_response",
                    request_id=ctx.request_id,
                    provider=provider.name,
                    key_index=key_idx,
                    duration_ms=duration_ms,
                    status=last_error.value,
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
                first_chunk=True,
            )
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
    ctx: FailoverContext,
) -> StreamingResponse:
    chosen, first_event, rest_or_err = await _try_stream_with_failover(
        chain, body, cooldown, ctx
    )
    if chosen is None:
        emit_event(
            "request_failed",
            request_id=ctx.request_id,
            phase="pre_first_chunk",
            tried=[t.provider for t in ctx.tried],
        )
        raise HTTPException(status_code=503, detail=_build_503_detail(ctx))
    rest = rest_or_err  # AsyncIterator at this point

    async def emit() -> AsyncIterator[bytes]:
        yield _format_sse(first_event)
        try:
            async for evt in rest:
                yield _format_sse(evt)
        except Exception as e:
            logger.warning("mid-stream upstream error after first chunk shipped: %s", e)
            emit_event(
                "request_mid_stream_error",
                request_id=ctx.request_id,
                provider=chosen.name,
                error=str(e)[:200],
            )
        emit_event(
            "request_complete",
            request_id=ctx.request_id,
            provider=chosen.name,
            streaming=True,
        )
        yield _format_done()

    return StreamingResponse(
        emit(),
        media_type="text/event-stream",
        headers={
            "X-FreeRide-Provider": chosen.name,
            "X-FreeRide-Request-ID": ctx.request_id,
        },
    )


@router.post("/v1/chat/completions")
async def chat_completions(request: Request, body: ChatRequest):
    providers: list[Provider] = list(request.app.state.providers)
    ctx = FailoverContext(request_id=new_request_id())

    emit_event(
        "request_start",
        request_id=ctx.request_id,
        model=body.model,
        streaming=body.is_streaming(),
    )

    if not providers:
        emit_event("request_failed", request_id=ctx.request_id, phase="no_providers")
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "type": "no_providers",
                    "message": "No provider plugins registered. This is a server-config bug.",
                    "request_id": ctx.request_id,
                }
            },
        )

    cooldown = KeyCooldown()
    chain = _resolve_provider_chain(providers)
    if not chain:
        emit_event(
            "request_failed",
            request_id=ctx.request_id,
            phase="no_usable_keys",
            providers=[p.name for p in providers],
        )
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "type": "no_usable_keys",
                    "message": "No providers have usable (non-cooling) API keys for this request.",
                    "request_id": ctx.request_id,
                    "configured_providers": [p.name for p in providers],
                    "suggestion": "Either set a provider env var (e.g. OPENROUTER_API_KEY) or wait for cooldowns to expire (~120s).",
                }
            },
        )

    if body.is_streaming():
        return await _build_stream_response(chain, body, cooldown, ctx)

    # Non-streaming with cross-provider failover.
    chosen_provider: Provider | None = None
    response: ChatResponse | None = None
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
                streaming=False,
            )
            t0 = time.perf_counter()
            try:
                response = await provider.forward_chat(body, body.model, key)
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
        streaming=False,
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
