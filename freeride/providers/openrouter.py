"""OpenRouter provider plugin.

Ports the v2 ``main.py`` logic into a :class:`Provider` implementation.
This module owns OpenRouter-specific concerns only — free-model
detection, error classification, attribution headers — and depends on
nothing from other provider plugins.

Free-detection rule
-------------------
OpenRouter exposes two signals that disagree occasionally:

* ``model.pricing.prompt == 0`` — the canonical billing-side flag
* ``":free"`` suffix in ``model.id`` — the routing-side flag

We treat a model as free if **either** signal says free. Direct port
of v2's behavior (carry-forward principle 1: dual-signal free detection).

Chat-shape filter
-----------------
We surface only text-output models. OpenRouter mixes image-gen,
audio-gen, and multi-modal-output models into the same catalog; without
this filter, ranking has historically picked Lyria (text+image →
text+audio) as a top "chat" model. The filter inspects
``architecture.output_modalities`` first, falls back to parsing
``architecture.modality`` strings of the form ``"text+image->text"``,
and keeps unknown shapes (the live probe catches false positives).
"""

from __future__ import annotations

import time
from typing import Any, AsyncIterator

import httpx

from freeride.core.chat_schema import ChatRequest, ChatResponse, ChatStreamEvent
from freeride.core.embedding_schema import EmbeddingRequest, EmbeddingResponse
from freeride.core.errors import ErrorKind
from freeride.core.provider import PROVIDER_API_VERSION
from freeride.core.types import Model, ProbeResult

OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"
OPENROUTER_MODELS_URL = f"{OPENROUTER_API_BASE}/models"
OPENROUTER_CHAT_URL = f"{OPENROUTER_API_BASE}/chat/completions"
OPENROUTER_EMBEDDINGS_URL = f"{OPENROUTER_API_BASE}/embeddings"

# Attribution stamped on every outbound request so all FreeRide traffic
# rolls up under one identity on OpenRouter's App Activity page.
# https://openrouter.ai/docs/api-reference/overview#headers
OPENROUTER_REFERER = "https://github.com/Shaivpidadi/FreeRideV3"
OPENROUTER_APP_TITLE = "FreeRide Gateway"


def is_chat_model(model: dict) -> bool:
    """True if ``model`` is a text-output model suitable for /chat/completions.

    Filters out image-gen, audio-gen, and multi-modal-output models that
    aren't chat-shaped. Direct port of v2 ``_is_chat_model``.
    """
    arch = model.get("architecture") or {}

    # Preferred: explicit output_modalities array.
    out_mods = arch.get("output_modalities")
    if isinstance(out_mods, list) and out_mods:
        return out_mods == ["text"]

    # Fallback: parse a "text+image->text" / "text->text+audio" string.
    modality = arch.get("modality", "")
    if isinstance(modality, str) and "->" in modality:
        output_part = modality.split("->", 1)[1].strip()
        return output_part == "text"

    # Unknown shape — keep it; live probe will catch false positives.
    return True


def is_free_model(model: dict) -> bool:
    """True if either OpenRouter free-signal fires for this model.

    Prefer ``pricing.prompt == 0`` (the billing-side flag, definitive).
    Fall back to ``":free"`` suffix in id (the routing-side flag,
    sometimes set without the pricing being zero).
    """
    prompt_cost = model.get("pricing", {}).get("prompt")
    if prompt_cost is not None:
        try:
            if float(prompt_cost) == 0:
                return True
        except (ValueError, TypeError):
            pass
    return ":free" in model.get("id", "")


def filter_free_chat_models(models: list[dict]) -> list[dict]:
    """Return the chat-shaped, free subset of ``models``.

    Direct port of v2 ``filter_free_models`` plus the chat filter.
    Dedupes by ``id`` (OpenRouter occasionally returns duplicates).
    """
    seen: set[str] = set()
    out: list[dict] = []
    for m in models:
        mid = m.get("id", "")
        if mid in seen:
            continue
        if not is_chat_model(m):
            continue
        if not is_free_model(m):
            continue
        out.append(m)
        seen.add(mid)
    return out


def _to_model(raw: dict) -> Model:
    """Convert one OpenRouter catalog entry to a :class:`Model`."""
    arch = raw.get("architecture") or {}
    out_mods = arch.get("output_modalities")
    if not isinstance(out_mods, list) or not out_mods:
        out_mods = ["text"]
    return Model(
        api_id=raw.get("id", ""),
        provider="openrouter",
        context_length=int(raw.get("context_length") or 0),
        output_modalities=tuple(out_mods),
        supported_parameters=tuple(raw.get("supported_parameters") or ()),
        raw=raw,
    )


class OpenRouterProvider:
    """OpenRouter Provider plugin (incremental impl, full surface lands in 1.4.6)."""

    name: str = "openrouter"
    api_version: int = PROVIDER_API_VERSION
    embeddings_supported: bool = True

    def __init__(self, *, http_timeout: float = 30.0) -> None:
        self._timeout = http_timeout
        # Streaming responses keep connect/write tight but extend `read`
        # to 10 min — extended-thinking models can go >30s between
        # visible tokens, which would otherwise trip httpx's default
        # read-timeout and kill the SSE connection mid-stream.
        self._stream_timeout = httpx.Timeout(
            connect=http_timeout,
            read=600.0,
            write=http_timeout,
            pool=http_timeout,
        )

    # ----- request stamping (Provider Protocol) --------------------------
    def auth_header(self, key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {key}"}

    def attribution_headers(self) -> dict[str, str]:
        return {
            "HTTP-Referer": OPENROUTER_REFERER,
            "X-Title": OPENROUTER_APP_TITLE,
        }

    def _outbound_headers(self, key: str, *, json_content: bool = False) -> dict[str, str]:
        h: dict[str, str] = {**self.auth_header(key), **self.attribution_headers()}
        if json_content:
            h["Content-Type"] = "application/json"
        return h

    # ----- error classification (Provider Protocol) ----------------------
    def classify_error(self, response_or_exc: Any) -> ErrorKind:
        """Map an httpx response or exception to an :class:`ErrorKind`.

        OpenRouter conventions:

        - 401 → AUTH (key invalid)
        - 429 → RATE_LIMIT
        - 503 → UNAVAILABLE
        - other 5xx → UNAVAILABLE
        - 4xx with ``error.code == "model_not_found"`` or message containing
          "Unknown model" → MODEL_NOT_FOUND
        - any other 4xx → UNKNOWN
        - httpx.TimeoutException → TIMEOUT
        - httpx.RequestError (other network) → UNAVAILABLE (transient)
        - any other exception → UNKNOWN
        """
        # Exception path
        if isinstance(response_or_exc, httpx.TimeoutException):
            return ErrorKind.TIMEOUT
        if isinstance(response_or_exc, httpx.RequestError):
            return ErrorKind.UNAVAILABLE
        if isinstance(response_or_exc, BaseException):
            return ErrorKind.UNKNOWN

        # Response path
        resp = response_or_exc
        status = getattr(resp, "status_code", None)
        if status is None:
            return ErrorKind.UNKNOWN
        if 200 <= status < 300:
            return ErrorKind.OK
        if status == 401:
            return ErrorKind.AUTH
        if status == 429:
            return ErrorKind.RATE_LIMIT
        if status == 503:
            return ErrorKind.UNAVAILABLE
        if 500 <= status < 600:
            return ErrorKind.UNAVAILABLE

        # 4xx (other): inspect body for model_not_found.
        # Streaming responses raise httpx.ResponseNotRead unless aread()
        # was called first; we treat that as "no body to inspect" and
        # fall through to UNKNOWN. The streaming caller is responsible
        # for reading before classify if it wants body-pattern matching.
        try:
            body = resp.json()
        except Exception:
            return ErrorKind.UNKNOWN
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            if err.get("code") == "model_not_found":
                return ErrorKind.MODEL_NOT_FOUND
            msg = str(err.get("message", ""))
            # Patterns observed from real OpenRouter responses (2026-05-07):
            #   400 with "<id> is not a valid model ID" — typo'd model
            #   404 with "Unknown model" — different shape, also seen
            #   "model_not_found" — sometimes echoed in message
            for needle in ("Unknown model", "model_not_found", "not a valid model"):
                if needle in msg:
                    return ErrorKind.MODEL_NOT_FOUND
        return ErrorKind.UNKNOWN

    def retry_after_hint(self, response: Any) -> int | None:
        """Parse a ``Retry-After`` header. OpenRouter rarely sends it; the
        gateway falls back to its own cooldown when this returns ``None``.
        """
        if response is None:
            return None
        headers = getattr(response, "headers", None)
        if headers is None:
            return None
        raw = headers.get("retry-after") if hasattr(headers, "get") else None
        if not raw:
            return None
        try:
            value = int(raw)
            return value if value >= 0 else None
        except (TypeError, ValueError):
            return None

    # ----- request forwarding (gateway hot-path) -------------------------
    async def forward_chat(
        self, request: ChatRequest, model_id: str, key: str
    ) -> ChatResponse:
        """Non-streaming chat completion. Forward the request to OpenRouter
        verbatim (modulo the model swap — the gateway lets clients use a
        different model name than the upstream id), then validate the
        response into our :class:`ChatResponse` schema. Permissive parsing
        means provider extensions like vLLM's ``reasoning_content`` and
        ``nvext`` round-trip without loss.
        """
        # Build the outbound payload: copy the inbound request, override
        # model with the resolved upstream id, force stream=False.
        payload = request.model_dump(exclude_none=True)
        payload["model"] = model_id
        payload["stream"] = False

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                OPENROUTER_CHAT_URL,
                headers=self._outbound_headers(key, json_content=True),
                json=payload,
            )
        # Let raise_for_status surface 4xx/5xx — the gateway's retry loop
        # will catch via classify_error and pick another tuple. Phase 2
        # simplest path: surface the error to the client.
        resp.raise_for_status()
        return ChatResponse.model_validate(resp.json())

    async def forward_chat_stream(
        self, request: ChatRequest, model_id: str, key: str
    ) -> AsyncIterator[ChatStreamEvent]:
        """Streaming chat completion. Forward as SSE; yield each parsed
        :class:`ChatStreamEvent`. Swallows the ``data: [DONE]`` sentinel
        so consumers see clean iteration ending.

        OpenRouter sends standard OpenAI-compatible SSE: lines prefixed
        ``data: `` with JSON payloads, terminated by ``data: [DONE]``.
        Permissive parsing (``extra='allow'``) means provider extensions
        round-trip untouched.
        """
        import json as _json

        payload = request.model_dump(exclude_none=True)
        payload["model"] = model_id
        payload["stream"] = True

        async with httpx.AsyncClient(timeout=self._stream_timeout) as client:
            async with client.stream(
                "POST",
                OPENROUTER_CHAT_URL,
                headers=self._outbound_headers(key, json_content=True),
                json=payload,
            ) as resp:
                resp.raise_for_status()
                async for raw_line in resp.aiter_lines():
                    if not raw_line:
                        continue
                    if not raw_line.startswith("data:"):
                        # Some providers send comments (lines starting with ":")
                        # for keepalive — ignore.
                        continue
                    data_str = raw_line[len("data:"):].strip()
                    if data_str == "[DONE]":
                        return
                    try:
                        obj = _json.loads(data_str)
                    except _json.JSONDecodeError:
                        # Malformed line — skip, keep streaming.
                        continue
                    yield ChatStreamEvent.model_validate(obj)

    # ----- embeddings -----------------------------------------------------
    async def forward_embeddings(
        self, request: EmbeddingRequest, model_id: str, key: str
    ) -> EmbeddingResponse:
        """OpenAI-shape /v1/embeddings forward. Same pattern as forward_chat:
        copy the inbound request, override the model, POST upstream,
        validate the response. Permissive parsing keeps provider extensions
        passing through.
        """
        payload = request.model_dump(exclude_none=True)
        payload["model"] = model_id

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                OPENROUTER_EMBEDDINGS_URL,
                headers=self._outbound_headers(key, json_content=True),
                json=payload,
            )
        resp.raise_for_status()
        return EmbeddingResponse.model_validate(resp.json())

    # ----- probing --------------------------------------------------------
    def probe(self, model_id: str, key: str) -> ProbeResult:
        """Live-test ``model_id`` with a tiny chat completion (``max_tokens=5``).

        Returns ``ok=True`` on 200, otherwise ``ok=False`` with the
        :class:`ErrorKind` in ``error``. ``latency_ms`` is wall-clock from
        request start to response (or exception). Direct port of v2
        ``_test_model`` semantics.
        """
        payload = {
            "model": model_id,
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 5,
            "stream": False,
        }
        started = time.perf_counter()
        try:
            with httpx.Client(timeout=self._timeout) as client:
                resp = client.post(
                    OPENROUTER_CHAT_URL,
                    headers=self._outbound_headers(key, json_content=True),
                    json=payload,
                )
        except httpx.TimeoutException as e:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            return ProbeResult(ok=False, error=self.classify_error(e), latency_ms=elapsed_ms)
        except httpx.RequestError as e:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            return ProbeResult(ok=False, error=self.classify_error(e), latency_ms=elapsed_ms)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        kind = self.classify_error(resp)
        return ProbeResult(ok=(kind is ErrorKind.OK), error=None if kind is ErrorKind.OK else kind, latency_ms=elapsed_ms)

    # ----- discovery ------------------------------------------------------
    def list_free_models(self, key: str) -> list[Model]:
        """Fetch OpenRouter's catalog with ``key`` and return the free-tier
        chat-shaped subset as :class:`Model` instances.

        Single-key per the Protocol — multi-key rotation belongs in the
        gateway's key pool (lands in Phase 1.6). The CLI's ``freeride list``
        will iterate over keys via that pool, calling this method per-key
        and short-circuiting on first success.
        """
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.get(OPENROUTER_MODELS_URL, headers=self._outbound_headers(key))
        resp.raise_for_status()
        raw_models: list[dict] = resp.json().get("data", []) or []
        free = filter_free_chat_models(raw_models)
        return [_to_model(r) for r in free]
