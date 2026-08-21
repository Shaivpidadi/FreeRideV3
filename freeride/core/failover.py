"""Shared failover walk used by every protocol route.

Chat Completions, Anthropic Messages, Codex Responses, Gemini, and
embeddings all share the same (provider, key) chain, the same error
classification, and the same cooldown / health recording. Routes keep
their translators; this module owns the walk.

Streaming uses buffer-first-chunk semantics: hold the first SSE event
until upstream confirms it. If that fails, try the next pair. Once
the first chunk has shipped to the client, mid-stream errors are
logged — we cannot un-ship bytes.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, TypeVar

import httpx
from fastapi import Request

from freeride.core.chat_schema import ChatRequest, ChatStreamEvent
from freeride.core.cooldown import KeyCooldown
from freeride.core.errors import ErrorKind
from freeride.core.events import emit as emit_event
from freeride.core.health import ProviderHealth, sort_keys_by_health
from freeride.core.provider import Provider
from freeride.core.provider_env import all_keys_for, env_var_for

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Error kinds that cool the key (don't try it again until TTL).
_COOL_KINDS = frozenset(
    {ErrorKind.RATE_LIMIT, ErrorKind.AUTH, ErrorKind.QUOTA_EXHAUSTED}
)
# Error kinds that skip the rest of this provider's keys.
_SKIP_PROVIDER_KINDS = frozenset(
    {ErrorKind.MODEL_NOT_FOUND, ErrorKind.QUOTA_EXHAUSTED}
)


def record_health(
    provider_name: str, *, ok: bool, duration_ms: int, key: str | None = None
) -> None:
    """Called alongside every ``provider_response`` emit so the health
    tracker reflects what just happened. ``key`` is the raw secret —
    hashed before storage inside ProviderHealth.
    """
    ProviderHealth.instance().record(
        provider_name, ok=ok, duration_ms=duration_ms, key=key
    )


def resolve_provider_chain(
    providers: list[Provider],
) -> list[tuple[Provider, list[str]]]:
    cooldown = KeyCooldown()
    chain: list[tuple[Provider, list[str]]] = []
    for p in providers:
        keys = all_keys_for(p.name)
        if not keys:
            continue
        available = cooldown.available_keys(p.name, keys)
        if not available:
            continue
        chain.append((p, available))
    return chain


def apply_force_provider(
    providers: list[Provider], request: Request
) -> tuple[list[Provider], str | None]:
    """If the request carries ``X-FreeRide-Force-Provider``, filter the
    chain down to that provider only. Returns the filtered chain plus
    the requested name (so the route can 400 when nothing matches).
    """
    forced = request.headers.get("X-FreeRide-Force-Provider", "").strip()
    if not forced:
        return providers, None
    matched = [p for p in providers if p.name == forced]
    return matched, forced


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


def suggestion(tried: list[AttemptSummary]) -> str:
    """Pick the most actionable hint based on the failure mix."""
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
        env_vars = ", ".join(env_var_for(p) for p in bad)
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


def build_503_detail(ctx: FailoverContext) -> dict[str, Any]:
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
            "suggestion": suggestion(ctx.tried),
        }
    }


def force_stream_usage(body: ChatRequest) -> None:
    """Force ``stream_options.include_usage = true`` on the outgoing
    OpenAI-compat request so providers ship a final usage chunk.
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


def _cool(
    cooldown: KeyCooldown,
    provider: Provider,
    key: str,
    kind: ErrorKind,
    response: Any = None,
) -> int | None:
    """Apply cooldown for kinds that warrant it. Returns Retry-After
    seconds when the provider exposed one (RATE_LIMIT only, for the
    503 payload) — AUTH/QUOTA still cool, they just don't surface a
    retry-after hint.
    """
    retry_after: int | None = None
    if kind is ErrorKind.RATE_LIMIT and response is not None:
        retry_after = provider.retry_after_hint(response)
    if kind in _COOL_KINDS:
        cooldown.mark(provider.name, key, kind, retry_after_s=retry_after)
    return retry_after


def _record_failure(
    *,
    ctx: FailoverContext,
    summary: AttemptSummary,
    provider: Provider,
    key: str,
    key_idx: int,
    duration_ms: int,
    kind: ErrorKind,
    extra_event: dict[str, Any] | None,
) -> None:
    summary.last_error = kind
    payload: dict[str, Any] = dict(extra_event or {})
    if summary.retry_after_s:
        payload["retry_after_s"] = summary.retry_after_s
    emit_event(
        "provider_response",
        request_id=ctx.request_id,
        provider=provider.name,
        key_index=key_idx,
        duration_ms=duration_ms,
        status=kind.value,
        **payload,
    )
    record_health(provider.name, ok=False, duration_ms=duration_ms, key=key)


async def try_stream_with_failover(
    chain: list[tuple[Provider, list[str]]],
    body: ChatRequest,
    cooldown: KeyCooldown,
    ctx: FailoverContext,
    *,
    extra_event: dict[str, Any] | None = None,
    on_model_not_found: Callable[[], None] | None = None,
) -> (
    tuple[Provider, ChatStreamEvent, AsyncIterator[ChatStreamEvent]]
    | tuple[None, None, ErrorKind]
):
    force_stream_usage(body)
    last_error: ErrorKind | None = None
    extra = extra_event or {}
    for provider, keys in chain:
        summary = ctx.attempt(provider.name)
        provider_done = False
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
                **extra,
            )
            t0 = time.perf_counter()
            gen = provider.forward_chat_stream(body, body.model, key)
            try:
                first = await gen.__anext__()
            except StopAsyncIteration:
                duration_ms = int((time.perf_counter() - t0) * 1000)
                last_error = ErrorKind.UNKNOWN
                _record_failure(
                    ctx=ctx,
                    summary=summary,
                    provider=provider,
                    key=key,
                    key_idx=key_idx,
                    duration_ms=duration_ms,
                    kind=last_error,
                    extra_event=extra,
                )
                continue
            except httpx.HTTPStatusError as e:
                duration_ms = int((time.perf_counter() - t0) * 1000)
                try:
                    await e.response.aread()
                except Exception:
                    pass
                kind = provider.classify_error(e.response)
                summary.retry_after_s = _cool(
                    cooldown, provider, key, kind, e.response
                ) or summary.retry_after_s
                if kind is ErrorKind.MODEL_NOT_FOUND and on_model_not_found is not None:
                    on_model_not_found()
                if kind in _SKIP_PROVIDER_KINDS:
                    provider_done = True
                last_error = kind
                _record_failure(
                    ctx=ctx,
                    summary=summary,
                    provider=provider,
                    key=key,
                    key_idx=key_idx,
                    duration_ms=duration_ms,
                    kind=kind,
                    extra_event=extra,
                )
                continue
            except httpx.HTTPError as e:
                duration_ms = int((time.perf_counter() - t0) * 1000)
                last_error = provider.classify_error(e)
                _record_failure(
                    ctx=ctx,
                    summary=summary,
                    provider=provider,
                    key=key,
                    key_idx=key_idx,
                    duration_ms=duration_ms,
                    kind=last_error,
                    extra_event=extra,
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
                **extra,
            )
            record_health(provider.name, ok=True, duration_ms=duration_ms, key=key)
            return provider, first, gen
    return None, None, last_error or ErrorKind.UNKNOWN


async def try_call_with_failover(
    chain: list[tuple[Provider, list[str]]],
    cooldown: KeyCooldown,
    ctx: FailoverContext,
    *,
    call: Callable[[Provider, str], Awaitable[T]],
    model: str,
    extra_event: dict[str, Any] | None = None,
    on_model_not_found: Callable[[], None] | None = None,
) -> tuple[Provider | None, T | None]:
    """Walk the chain for a non-streaming call.

    ``call(provider, key)`` is the route-specific forwarder
    (``forward_chat`` or ``forward_embeddings``). Returns
    ``(provider, result)`` on the first success, or ``(None, None)``
    when the chain is exhausted.
    """
    extra = extra_event or {}
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
                model=model,
                streaming=False,
                **extra,
            )
            t0 = time.perf_counter()
            try:
                result = await call(provider, key)
            except httpx.HTTPStatusError as e:
                duration_ms = int((time.perf_counter() - t0) * 1000)
                kind = provider.classify_error(e.response)
                summary.retry_after_s = _cool(
                    cooldown, provider, key, kind, e.response
                ) or summary.retry_after_s
                if kind is ErrorKind.MODEL_NOT_FOUND and on_model_not_found is not None:
                    on_model_not_found()
                _record_failure(
                    ctx=ctx,
                    summary=summary,
                    provider=provider,
                    key=key,
                    key_idx=key_idx,
                    duration_ms=duration_ms,
                    kind=kind,
                    extra_event=extra,
                )
                if kind in _SKIP_PROVIDER_KINDS:
                    break
                continue
            except httpx.HTTPError as e:
                duration_ms = int((time.perf_counter() - t0) * 1000)
                kind = provider.classify_error(e)
                _record_failure(
                    ctx=ctx,
                    summary=summary,
                    provider=provider,
                    key=key,
                    key_idx=key_idx,
                    duration_ms=duration_ms,
                    kind=kind,
                    extra_event=extra,
                )
                continue
            except Exception as e:  # noqa: BLE001
                duration_ms = int((time.perf_counter() - t0) * 1000)
                logger.warning("provider %s raised %s", provider.name, e)
                _record_failure(
                    ctx=ctx,
                    summary=summary,
                    provider=provider,
                    key=key,
                    key_idx=key_idx,
                    duration_ms=duration_ms,
                    kind=ErrorKind.UNKNOWN,
                    extra_event=extra,
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
                **extra,
            )
            record_health(provider.name, ok=True, duration_ms=duration_ms, key=key)
            return provider, result
    return None, None
