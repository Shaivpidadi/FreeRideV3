"""HuggingFace Inference Providers plugin.

Implements the v3 :class:`Provider` Protocol against HF's router endpoint:
``https://router.huggingface.co/v1`` (chat-completions OpenAI-compat).

HF-specific shape (per ``docs/providers/SURVEY.md``):

1. **Free tier is a small monthly credit budget** ($0.10/mo on Free,
   $2/mo on PRO), not a per-model flag. After exhaustion the API
   returns 4xx until the budget refreshes or the user buys credits.
   We return the catalog as-is and let the budget run out organically;
   the resolver advances on QUOTA_EXHAUSTED.

2. **Routing-policy suffixes on model ids.** Append `:fastest`,
   `:cheapest`, `:preferred`, or `:<provider>` (e.g.
   ``deepseek-ai/DeepSeek-R1:sambanova``) to pin upstream behavior.
   These are part of the model id; they round-trip through us
   transparently.

3. **`X-HF-Bill-To` org-billing header** — optional opt-in. Skipped
   unless ``HUGGINGFACE_BILL_TO`` env var is set.

Auth: ``Authorization: Bearer HF_TOKEN``.
"""

from __future__ import annotations

import json as _json
import os
import time
from typing import Any, AsyncIterator

import httpx

from freeride.core.chat_schema import ChatRequest, ChatResponse, ChatStreamEvent
from freeride.core.embedding_schema import EmbeddingRequest, EmbeddingResponse
from freeride.core.errors import ErrorKind
from freeride.core.provider import PROVIDER_API_VERSION
from freeride.core.types import Model, ProbeResult


HF_API_BASE = "https://router.huggingface.co/v1"
HF_MODELS_URL = f"{HF_API_BASE}/models"
HF_CHAT_URL = f"{HF_API_BASE}/chat/completions"
HF_EMBEDDINGS_URL = f"{HF_API_BASE}/embeddings"


def _bill_to() -> str | None:
    return os.environ.get("HUGGINGFACE_BILL_TO", "").strip() or None


class HuggingFaceProvider:
    """HuggingFace Inference Providers plugin."""

    name: str = "huggingface"
    api_version: int = PROVIDER_API_VERSION
    # HF's OpenAI-compat router (router.huggingface.co/v1) does NOT
    # expose /embeddings — that endpoint returns 404. Embedding
    # inference goes through HF's older per-model Inference API
    # (api-inference.huggingface.co/models/{id}), which has a
    # different request shape. We don't bridge that here, so
    # embeddings_supported = False and the embeddings route filter
    # naturally skips this provider.
    embeddings_supported: bool = False

    def __init__(self, *, http_timeout: float = 30.0) -> None:
        self._timeout = http_timeout

    # ----- request stamping ----------------------------------------------
    def auth_header(self, key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {key}"}

    def attribution_headers(self) -> dict[str, str]:
        # Optional org-billing header, opt-in via env. Not really
        # attribution (HF doesn't have an app-attribution flag), but
        # the bill-to fits the same conceptual slot.
        b = _bill_to()
        return {"X-HF-Bill-To": b} if b else {}

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
        # HF returns 402 Payment Required when the monthly free credit
        # is exhausted — distinct from rate-limit. Map to QUOTA_EXHAUSTED
        # so the resolver advances to a different provider.
        if status == 402:
            return ErrorKind.QUOTA_EXHAUSTED
        if 500 <= status < 600:
            return ErrorKind.UNAVAILABLE

        try:
            body = resp.json()
        except Exception:
            return ErrorKind.UNKNOWN
        if isinstance(body, dict):
            err = body.get("error", body)
            msg = str(err.get("message", "")).lower() if isinstance(err, dict) else ""
            if "credit" in msg and ("exhausted" in msg or "exceeded" in msg or "insufficient" in msg):
                return ErrorKind.QUOTA_EXHAUSTED
            if "model" in msg and ("not found" in msg or "does not exist" in msg or "unavailable" in msg):
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
        """HF's catalog at /v1/models lists all chat-routed models;
        we return them all and let the user's $0.10/mo (Free) or
        $2/mo (PRO) budget govern access. The resolver advances on
        QUOTA_EXHAUSTED if the budget runs out.
        """
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.get(HF_MODELS_URL, headers=self._outbound_headers(key))
        resp.raise_for_status()
        raw = resp.json().get("data", []) or []

        seen: set[str] = set()
        out: list[Model] = []
        for m in raw:
            mid = m.get("id", "")
            if not mid or mid in seen:
                continue
            seen.add(mid)
            # HF's catalog usually exposes context_length on chat models
            # and supported routing as part of the id; capabilities
            # aren't programmatically enumerated.
            ctx = int(m.get("context_length") or m.get("max_model_len") or 0) or 8_192
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
                    HF_CHAT_URL,
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
                HF_CHAT_URL,
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
                HF_CHAT_URL,
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

    # ----- embeddings -----------------------------------------------------
    async def forward_embeddings(
        self, request: EmbeddingRequest, model_id: str, key: str
    ) -> EmbeddingResponse:
        """OpenAI-shape /v1/embeddings forward to HuggingFace router."""
        payload = request.model_dump(exclude_none=True)
        payload["model"] = model_id

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                HF_EMBEDDINGS_URL,
                headers=self._outbound_headers(key, json_content=True),
                json=payload,
            )
        resp.raise_for_status()
        return EmbeddingResponse.model_validate(resp.json())
