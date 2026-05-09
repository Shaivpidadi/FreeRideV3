"""Cerebras Cloud provider plugin.

OpenAI-compatible at ``https://api.cerebras.ai/v1`` — chat completions,
streaming, and a /models endpoint. Cerebras's free tier is rate-limited
by request count + tokens-per-minute (the public docs put it around
30 RPM / 1M tokens-per-minute on `llama3.1-8b` for the free plan).

Two provider-specific quirks worth flagging:

1. **Catalog is small + curated.** Unlike OpenRouter, Cerebras only
   exposes a handful of Llama / Qwen models. We treat the entire
   catalog as free-tier (their pricing page shows everything they list
   has a free quota). ``CEREBRAS_FREE_MODELS_OVERRIDE`` env var lets
   paid-plan users restrict to specific ids.

2. **Embeddings are NOT supported** (they're chat-only as of 2026-05).
   ``embeddings_supported = False`` so the embeddings route skips it.

Auth: ``Authorization: Bearer CEREBRAS_API_KEY``.
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


CEREBRAS_API_BASE = "https://api.cerebras.ai/v1"
CEREBRAS_MODELS_URL = f"{CEREBRAS_API_BASE}/models"
CEREBRAS_CHAT_URL = f"{CEREBRAS_API_BASE}/chat/completions"


# Cerebras's /models endpoint advertises ids that the inference API
# itself rejects with model_not_found. Confirmed via end-to-end audit
# 2026-05-09 (see internal-docs / model-availability run): every probe
# against these returns a 404, even though `freeride list` and
# `/v1/models` would otherwise surface them as routable. Drop them
# at catalog-construction time so the smart-routing resolver and the
# /v1/models response never advertise a model that can't actually
# serve a request.
#
# This is a maintenance-coupled list. To refresh it, re-run
# burn-test/audit.py and add any newly-confirmed ghosts here.
_CEREBRAS_KNOWN_BROKEN_IDS: frozenset[str] = frozenset(
    {
        "zai-glm-4.7",
        "gpt-oss-120b",
    }
)


def _free_model_set() -> frozenset[str] | None:
    """``None`` means "no allowlist, surface every model from the catalog".
    A non-None value means restrict to exactly that set.
    """
    override = os.environ.get("CEREBRAS_FREE_MODELS_OVERRIDE", "").strip()
    if not override:
        return None
    return frozenset(s.strip() for s in override.split(",") if s.strip())


class CerebrasProvider:
    """Cerebras Cloud provider plugin."""

    name: str = "cerebras"
    api_version: int = PROVIDER_API_VERSION
    embeddings_supported: bool = False

    def __init__(self, *, http_timeout: float = 30.0) -> None:
        self._timeout = http_timeout

    # ----- request stamping ----------------------------------------------
    def auth_header(self, key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {key}"}

    def attribution_headers(self) -> dict[str, str]:
        # Cerebras has no documented app-attribution header. They use
        # the standard Bearer + project tracking on their dashboard.
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

        # OpenAI-shape error envelope; their 4xx body looks like
        # {"error": {"message": "...", "type": "...", "code": "..."}}.
        try:
            body = resp.json()
        except Exception:
            return ErrorKind.UNKNOWN
        if isinstance(body, dict):
            err = body.get("error", body)
            if isinstance(err, dict):
                msg = str(err.get("message", "")).lower()
                code = str(err.get("code", "")).lower()
                if code == "model_not_found" or "model not found" in msg or "no such model" in msg:
                    return ErrorKind.MODEL_NOT_FOUND
                if "quota" in msg or "limit exceeded" in msg:
                    return ErrorKind.QUOTA_EXHAUSTED
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
        """Hit /models, drop known-broken ids, intersect with override
        allowlist if set."""
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.get(CEREBRAS_MODELS_URL, headers=self._outbound_headers(key))
        resp.raise_for_status()
        raw = resp.json().get("data", []) or []

        allow = _free_model_set()
        seen: set[str] = set()
        out: list[Model] = []
        for m in raw:
            mid = m.get("id", "")
            if not mid or mid in seen:
                continue
            if mid in _CEREBRAS_KNOWN_BROKEN_IDS:
                continue
            if allow is not None and mid not in allow:
                continue
            seen.add(mid)
            ctx = int(m.get("context_length") or m.get("max_context_length") or 0) or 8_192
            out.append(
                Model(
                    api_id=mid,
                    provider=self.name,
                    context_length=ctx,
                    output_modalities=("text",),
                    supported_parameters=(),
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
                    CEREBRAS_CHAT_URL,
                    headers=self._outbound_headers(key, json_content=True),
                    json=payload,
                )
        except httpx.TimeoutException as e:
            return ProbeResult(
                ok=False,
                error=self.classify_error(e),
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        except httpx.RequestError as e:
            return ProbeResult(
                ok=False,
                error=self.classify_error(e),
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
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

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                CEREBRAS_CHAT_URL,
                headers=self._outbound_headers(key, json_content=True),
                json=payload,
            )
        resp.raise_for_status()
        return ChatResponse.model_validate(resp.json())

    async def forward_chat_stream(
        self, request: ChatRequest, model_id: str, key: str
    ) -> AsyncIterator[ChatStreamEvent]:
        payload = request.model_dump(exclude_none=True)
        payload["model"] = model_id
        payload["stream"] = True

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            async with client.stream(
                "POST",
                CEREBRAS_CHAT_URL,
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
                    yield ChatStreamEvent.model_validate(obj)
