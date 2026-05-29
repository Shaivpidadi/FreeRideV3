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

from freeride.core.auto_model import is_auto_model, resolve_auto_model
from freeride.core.chat_schema import ChatRequest, ChatResponse, ChatStreamEvent
from freeride.core.cooldown import KeyCooldown
from freeride.core.errors import ErrorKind
from freeride.core.events import emit as emit_event
from freeride.core.events import new_request_id
from freeride.core.health import ProviderHealth, sort_by_health, sort_keys_by_health
from freeride.core.provider import Provider
from freeride.server.routes.models import get_or_fetch_catalog, invalidate_catalog


def _record_health(
    provider_name: str, *, ok: bool, duration_ms: int, key: str | None = None
) -> None:
    """Called alongside every ``provider_response`` emit so the health
    tracker reflects what just happened. Next request's failover order
    biases toward providers (and keys) that have been responding well
    recently. ``key`` is the raw secret — it's hashed before storage
    inside ProviderHealth.
    """
    ProviderHealth.instance().record(
        provider_name, ok=ok, duration_ms=duration_ms, key=key
    )


logger = logging.getLogger(__name__)
router = APIRouter()


def _env_var_for(provider_name: str) -> str:
    return {
        "openrouter": "OPENROUTER_API_KEY",
        "nvidia_nim": "NVIDIA_API_KEY",
        "groq": "GROQ_API_KEY",
        "cloudflare_wai": "CLOUDFLARE_API_TOKEN",
        "huggingface": "HF_TOKEN",
        "cerebras": "CEREBRAS_API_KEY",
        # Ollama is local and unauthenticated; we use the base URL as the
        # "key" in the failover chain so the chain code's available_keys
        # logic still applies (one URL = one "key", multiple URLs = JSON
        # array for round-robin across multiple Ollama hosts).
        "ollama": "OLLAMA_BASE_URL",
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
    """Pick the most actionable hint based on the failure mix.

    Each branch points at the CLI command that surfaces the most
    relevant diagnostic info — ``freeride doctor`` for setup issues,
    ``freeride keys`` for cooldown state, ``freeride watch`` for
    real-time tracing, ``freeride list`` for the model catalog.
    """
    if not tried:
        return (
            "No providers had usable keys. Run `freeride doctor` to see "
            "which env vars are set and what's missing."
        )
    kinds = {t.last_error for t in tried if t.last_error is not None}
    cooling = [t for t in tried if t.retry_after_s]
    if kinds == {ErrorKind.RATE_LIMIT} and cooling:
        soonest = min(t.retry_after_s for t in cooling if t.retry_after_s)
        return (
            f"All providers rate-limited. Soonest retry-after: ~{soonest}s. "
            "Run `freeride keys` to see cooldown state across all providers, "
            "or add another free-tier key for more failover headroom."
        )
    if ErrorKind.AUTH in kinds:
        bad = [t.provider for t in tried if t.last_error == ErrorKind.AUTH]
        env_vars = ", ".join(_env_var_for(p) for p in bad)
        return (
            f"Auth failed on {', '.join(bad)}. Verify {env_vars} — "
            "`freeride doctor` confirms which env vars are seen by the gateway."
        )
    if ErrorKind.MODEL_NOT_FOUND in kinds:
        return (
            "Model not available on any provider. Run `freeride list` for the "
            "current free catalog, or check `freeride providers` to see what "
            "the gateway has registered."
        )
    if ErrorKind.QUOTA_EXHAUSTED in kinds:
        return (
            "Free-tier budgets exhausted across providers. Wait for the next "
            "billing cycle, add another provider's key, or run "
            "`freeride keys` to see which provider is closest to recovering."
        )
    return (
        "All providers failed. Run `freeride watch` in another terminal to "
        "see live transitions, or `freeride providers` for current health stats."
    )


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


def _force_stream_usage(body: ChatRequest) -> None:
    """Force ``stream_options.include_usage = true`` on the outgoing
    OpenAI-compat request so providers ship a final usage chunk.

    Most OpenAI-compatible providers omit usage on the SSE stream by
    default, returning ``usage: null`` on every chunk. Asking for it
    explicitly adds one extra ``choices: []`` chunk at the end carrying
    ``prompt_tokens`` + ``completion_tokens`` — that's what the
    telemetry layer reads to fix the long-standing streaming-undercount
    bug. If the caller already passed ``stream_options``, we preserve
    their other fields and only set ``include_usage``.

    No-op on non-streaming requests (the response object already
    carries usage).
    """
    if not body.is_streaming():
        return
    extra = body.__pydantic_extra__ or {}
    existing = extra.get("stream_options")
    if isinstance(existing, dict):
        new_so = {**existing, "include_usage": True}
    else:
        new_so = {"include_usage": True}
    body.__pydantic_extra__ = {**extra, "stream_options": new_so}


async def _try_stream_with_failover(
    chain: list[tuple[Provider, list[str]]],
    body: ChatRequest,
    cooldown: KeyCooldown,
    ctx: FailoverContext,
) -> (
    tuple[Provider, ChatStreamEvent, AsyncIterator[ChatStreamEvent]]
    | tuple[None, None, ErrorKind]
):
    _force_stream_usage(body)
    last_error: ErrorKind | None = None
    for provider, keys in chain:
        summary = ctx.attempt(provider.name)
        provider_done = False
        # Sort within-provider keys by per-key health so a single flaky
        # key gets demoted relative to its siblings without affecting
        # the provider's overall ordering.
        ordered_keys = sort_keys_by_health(provider.name, keys)
        for key_idx, key in enumerate(ordered_keys):
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
                _record_health(provider.name, ok=False, duration_ms=duration_ms, key=key)
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
                if kind == ErrorKind.MODEL_NOT_FOUND:
                    # Provider claims the model is gone. Drop the cached
                    # catalog so the next /v1/models or auto-resolve
                    # rebuilds against the current upstream catalog
                    # rather than handing out the dead id again.
                    invalidate_catalog()
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
                _record_health(provider.name, ok=False, duration_ms=duration_ms, key=key)
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
                _record_health(provider.name, ok=False, duration_ms=duration_ms, key=key)
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
            _record_health(provider.name, ok=True, duration_ms=duration_ms, key=key)
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
        # Track the latest usage seen on the wire. Most OpenAI-compat
        # providers emit a single usage-bearing chunk near the end of
        # the stream (the penultimate event on NIM with ``choices: []``,
        # or any event when the request included
        # ``stream_options.include_usage=true``). We grab whichever
        # arrives last so a provider that updates mid-stream still
        # gets its final number recorded.
        from freeride.core.usage import Kind, extract_usage

        last_usage = extract_usage(Kind.OPENAI, first_event.model_dump())
        yield _format_sse(first_event)
        try:
            async for evt in rest:
                u = extract_usage(Kind.OPENAI, evt.model_dump())
                if u.has_any:
                    last_usage = u
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
            input_tokens=last_usage.input,
            output_tokens=last_usage.output,
        )
        # Bump the local counters the hourly beacon ships. If the
        # upstream emitted usage (most do when we ask for it via
        # stream_options.include_usage=true), we record input + output
        # exactly. Otherwise both default to 0 and only request_count
        # ticks — the install still shows activity even when usage
        # data was unavailable.
        from freeride.core.telemetry import record_request

        record_request(
            input_tokens=last_usage.input,
            output_tokens=last_usage.output,
            provider=chosen.name,
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


def _apply_force_provider(
    providers: list[Provider], request: Request
) -> tuple[list[Provider], str | None]:
    """If the request carries ``X-FreeRide-Force-Provider``, filter the
    chain down to that provider only. Returns the filtered chain plus
    the requested name (so the route can 503 with an actionable error
    when the name doesn't match anything registered).
    """
    forced = request.headers.get("X-FreeRide-Force-Provider", "").strip()
    if not forced:
        return providers, None
    matched = [p for p in providers if p.name == forced]
    return matched, forced


@router.post("/v1/chat/completions")
async def chat_completions(request: Request, body: ChatRequest):
    # Health-aware ordering: providers that have been responding well
    # recently float to the top of the failover chain. Stable sort with
    # neutral default scores means a fresh process keeps registration
    # order until enough data accumulates.
    providers: list[Provider] = sort_by_health(list(request.app.state.providers))

    # Per-request override: X-FreeRide-Force-Provider pins the chain to
    # one specific provider for benchmarking or debugging. No failover.
    providers, forced = _apply_force_provider(providers, request)
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

    # If the request asked for "auto" (or sent no model id at all),
    # turn that into a concrete provider-specific id from the live
    # catalog. Mutating body.model in place lets every downstream
    # call site — non-streaming loop, streaming loop, observability
    # events — keep treating model as a plain string.
    if is_auto_model(body.model):
        catalog = await get_or_fetch_catalog(providers, group=True)
        resolved_id, resolved_provider = resolve_auto_model(providers, catalog)
        if resolved_id is None:
            emit_event(
                "request_failed",
                request_id=ctx.request_id,
                phase="auto_resolution_failed",
            )
            raise HTTPException(
                status_code=503,
                detail={
                    "error": {
                        "type": "no_model_available",
                        "message": "model='auto' was requested but no provider has a usable model + key right now.",
                        "request_id": ctx.request_id,
                        "suggestion": "Run `freeride list` to see the catalog and `freeride keys` to see cooldowns.",
                    }
                },
            )
        body.model = resolved_id
        emit_event(
            "auto_model_resolved",
            request_id=ctx.request_id,
            resolved_model=resolved_id,
            resolved_provider=resolved_provider,
        )

    if body.is_streaming():
        return await _build_stream_response(chain, body, cooldown, ctx)

    # Non-streaming with cross-provider failover.
    chosen_provider: Provider | None = None
    response: ChatResponse | None = None
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
                _record_health(provider.name, ok=False, duration_ms=duration_ms, key=key)
                if kind == ErrorKind.MODEL_NOT_FOUND:
                    # See invalidate_catalog() comment in
                    # _try_stream_with_failover for rationale — same
                    # situation, non-streaming path.
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
                )
                _record_health(provider.name, ok=False, duration_ms=duration_ms, key=key)
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
            _record_health(provider.name, ok=True, duration_ms=duration_ms, key=key)
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

    # Non-streaming: the full response object carries ``usage``.
    # Split into input + output so the beacon counts both halves and
    # the marketing site can break down prompt vs. completion volume.
    from freeride.core.telemetry import record_request
    from freeride.core.usage import Kind, extract_usage

    usage = extract_usage(Kind.OPENAI, response.model_dump())
    emit_event(
        "request_complete",
        request_id=ctx.request_id,
        provider=chosen_provider.name,
        streaming=False,
        input_tokens=usage.input,
        output_tokens=usage.output,
    )
    record_request(
        input_tokens=usage.input,
        output_tokens=usage.output,
        provider=chosen_provider.name,
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
