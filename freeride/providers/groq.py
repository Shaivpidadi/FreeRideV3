"""Groq provider plugin.

Implements the v3 :class:`Provider` Protocol for ``api.groq.com``.
Groq is OpenAI-compatible at ``/openai/v1/`` with a few quirks per
``docs/providers/SURVEY.md``:

1. **No programmatic free-tier signal.** The catalog exposes
   ``context_window`` but not a "free" flag — what's accessible depends
   on the user's plan + per-model RPM/TPM caps. We ship a hardcoded
   allowlist (`groq_model_metadata.py`) plus a
   ``GROQ_FREE_MODELS_OVERRIDE`` env override for users with custom tiers.
2. **``x_groq`` extension field on responses.** Stripped before forwarding
   to clients to keep wire-clean OpenAI compat.
3. **Standard 429 + Retry-After header** for rate limits — the
   ``retry_after_hint`` parses it cleanly.

Auth: ``Authorization: Bearer GROQ_API_KEY``. Streaming: SSE +
``data: [DONE]`` terminator (OpenAI-shape).
"""

from __future__ import annotations

import json as _json
import os
import time
from typing import Any, AsyncIterator

import httpx

from freeride.core.chat_schema import ChatRequest, ChatResponse, ChatStreamEvent
from freeride.core.errors import ErrorKind
from freeride.core.provider import PROVIDER_API_VERSION
from freeride.core.types import Model, ProbeResult
from freeride.providers.groq_model_metadata import GROQ_MODEL_METADATA
from freeride.providers.groq_model_metadata import lookup as _lookup_meta


GROQ_API_BASE = "https://api.groq.com/openai/v1"
GROQ_MODELS_URL = f"{GROQ_API_BASE}/models"
GROQ_CHAT_URL = f"{GROQ_API_BASE}/chat/completions"


DEFAULT_FREE_MODEL_IDS: frozenset[str] = frozenset(GROQ_MODEL_METADATA.keys())


def _free_model_set() -> frozenset[str]:
    override = os.environ.get("GROQ_FREE_MODELS_OVERRIDE", "").strip()
    if not override:
        return DEFAULT_FREE_MODEL_IDS
    return frozenset(s.strip() for s in override.split(",") if s.strip())


# Groq-private response keys to strip before forwarding to clients.
_GROQ_TOPLEVEL_STRIP = {"x_groq"}


def _strip_groq_extensions(obj: dict) -> dict:
    for k in list(obj.keys()):
        if k in _GROQ_TOPLEVEL_STRIP:
            del obj[k]
    return obj


class GroqProvider:
    """Groq Provider plugin (api.groq.com)."""

    name: str = "groq"
    api_version: int = PROVIDER_API_VERSION
    # Groq does not currently offer embedding endpoints — chat-only.
    embeddings_supported: bool = False

    def __init__(self, *, http_timeout: float = 30.0) -> None:
        self._timeout = http_timeout
        # Streaming responses bump `read` to 10 min so extended-thinking
        # models can stall >30s between chunks without httpx killing the
        # SSE connection.
        self._stream_timeout = httpx.Timeout(
            connect=http_timeout,
            read=600.0,
            write=http_timeout,
            pool=http_timeout,
        )

    # ----- request stamping ----------------------------------------------
    def auth_header(self, key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {key}"}

    def attribution_headers(self) -> dict[str, str]:
        # Groq has no app-attribution mechanism we can use.
        return {}

    def _outbound_headers(self, key: str, *, json_content: bool = False) -> dict[str, str]:
        h: dict[str, str] = {**self.auth_header(key), **self.attribution_headers()}
        if json_content:
            h["Content-Type"] = "application/json"
        return h

    # ----- error classification ------------------------------------------
    def classify_error(self, response_or_exc: Any) -> ErrorKind:
        if isinstance(response_or_exc, httpx.TimeoutException):
            return ErrorKind.TIMEOUT
        if isinstance(response_or_exc, httpx.RequestError):
            return ErrorKind.UNAVAILABLE
        if isinstance(response_or_exc, BaseException):
            return ErrorKind.UNKNOWN

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
        if 500 <= status < 600:
            return ErrorKind.UNAVAILABLE

        # 4xx fallback: try JSON body for model-not-found
        try:
            body = resp.json()
        except Exception:
            return ErrorKind.UNKNOWN
        if isinstance(body, dict):
            err = body.get("error", body)
            msg = str(err.get("message", "")).lower() if isinstance(err, dict) else ""
            code = err.get("code") if isinstance(err, dict) else ""
            if code == "model_not_found":
                return ErrorKind.MODEL_NOT_FOUND
            if "model" in msg and ("not found" in msg or "does not exist" in msg or "decommissioned" in msg):
                return ErrorKind.MODEL_NOT_FOUND
        return ErrorKind.UNKNOWN

    def retry_after_hint(self, response: Any) -> int | None:
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

    # ----- discovery & probing -------------------------------------------
    def list_free_models(self, key: str) -> list[Model]:
        """Hit Groq /openai/v1/models, intersect with the free allowlist,
        attach out-of-band metadata.
        """
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.get(GROQ_MODELS_URL, headers=self._outbound_headers(key))
        resp.raise_for_status()
        raw = resp.json().get("data", []) or []

        free_set = _free_model_set()
        seen: set[str] = set()
        out: list[Model] = []
        for m in raw:
            mid = m.get("id", "")
            if mid in seen:
                continue
            if mid not in free_set:
                continue
            seen.add(mid)
            meta = _lookup_meta(mid)
            # Groq exposes context_window in its catalog; trust that if larger
            # than our metadata (means we're behind on the sidecar).
            ctx = max(int(m.get("context_window", 0) or 0), meta.context_length)
            out.append(
                Model(
                    api_id=mid,
                    provider=self.name,
                    context_length=ctx,
                    output_modalities=("text",),
                    supported_parameters=meta.supported_parameters,
                    raw=m,
                )
            )
        return out

    def probe(self, model_id: str, key: str) -> ProbeResult:
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
                    GROQ_CHAT_URL,
                    headers=self._outbound_headers(key, json_content=True),
                    json=payload,
                )
        except httpx.TimeoutException as e:
            elapsed = int((time.perf_counter() - started) * 1000)
            return ProbeResult(ok=False, error=self.classify_error(e), latency_ms=elapsed)
        except httpx.RequestError as e:
            elapsed = int((time.perf_counter() - started) * 1000)
            return ProbeResult(ok=False, error=self.classify_error(e), latency_ms=elapsed)
        elapsed = int((time.perf_counter() - started) * 1000)
        kind = self.classify_error(resp)
        return ProbeResult(
            ok=(kind is ErrorKind.OK),
            error=None if kind is ErrorKind.OK else kind,
            latency_ms=elapsed,
        )

    # ----- request forwarding -------------------------------------------
    async def forward_chat(
        self, request: ChatRequest, model_id: str, key: str
    ) -> ChatResponse:
        payload = request.model_dump(exclude_none=True)
        payload["model"] = model_id
        payload["stream"] = False
        # Strip Groq-private extension on the way OUT too — we never enable
        # x_groq features toward upstream.
        payload.pop("x_groq", None)

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                GROQ_CHAT_URL,
                headers=self._outbound_headers(key, json_content=True),
                json=payload,
            )
        resp.raise_for_status()
        body = _strip_groq_extensions(resp.json())
        return ChatResponse.model_validate(body)

    async def forward_chat_stream(
        self, request: ChatRequest, model_id: str, key: str
    ) -> AsyncIterator[ChatStreamEvent]:
        payload = request.model_dump(exclude_none=True)
        payload["model"] = model_id
        payload["stream"] = True
        payload.pop("x_groq", None)

        async with httpx.AsyncClient(timeout=self._stream_timeout) as client:
            async with client.stream(
                "POST",
                GROQ_CHAT_URL,
                headers=self._outbound_headers(key, json_content=True),
                json=payload,
            ) as resp:
                resp.raise_for_status()
                async for raw_line in resp.aiter_lines():
                    if not raw_line:
                        continue
                    if not raw_line.startswith("data:"):
                        continue
                    data_str = raw_line[len("data:"):].strip()
                    if data_str == "[DONE]":
                        return
                    try:
                        obj = _json.loads(data_str)
                    except _json.JSONDecodeError:
                        continue
                    obj = _strip_groq_extensions(obj)
                    yield ChatStreamEvent.model_validate(obj)
