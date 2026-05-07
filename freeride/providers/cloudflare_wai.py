"""Cloudflare Workers AI provider plugin.

Implements the v3 :class:`Provider` Protocol against CF Workers AI's
OpenAI-compatible surface:
``https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1/``

Two CF-specific quirks worth flagging (per ``docs/providers/SURVEY.md``):

1. **Account ID is part of the URL, not the key.** The plugin needs both
   an API token and the account UUID. Account ID is read from
   ``CLOUDFLARE_ACCOUNT_ID`` (env) at construction time. The Provider
   Protocol doesn't change — this is a per-plugin construction concern.

2. **Free-tier is a daily Neuron budget**, not a per-model flag. We
   ship a curated allowlist of cheap-per-neuron chat models in
   ``cloudflare_wai_model_metadata.py``. ``CF_WAI_FREE_MODELS_OVERRIDE``
   env var lets users on paid plans expand it.

Auth: ``Authorization: Bearer CLOUDFLARE_API_TOKEN``.
Streaming: SSE + ``data: [DONE]`` (OpenAI-shape).
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
from freeride.providers.cloudflare_wai_model_metadata import CF_WAI_MODEL_METADATA
from freeride.providers.cloudflare_wai_model_metadata import lookup as _lookup_meta


CF_API_BASE_TEMPLATE = "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1"


DEFAULT_FREE_MODEL_IDS: frozenset[str] = frozenset(CF_WAI_MODEL_METADATA.keys())


def _free_model_set() -> frozenset[str]:
    override = os.environ.get("CF_WAI_FREE_MODELS_OVERRIDE", "").strip()
    if not override:
        return DEFAULT_FREE_MODEL_IDS
    return frozenset(s.strip() for s in override.split(",") if s.strip())


class CloudflareWAIProvider:
    """Cloudflare Workers AI provider plugin."""

    name: str = "cloudflare_wai"
    api_version: int = PROVIDER_API_VERSION
    embeddings_supported: bool = True

    def __init__(
        self,
        *,
        account_id: str | None = None,
        http_timeout: float = 30.0,
    ) -> None:
        # Account ID is required and not in the API token. Fail loudly
        # at construction time rather than letting the URL build with a
        # blank segment and 404 later.
        self._account_id = account_id or os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
        if not self._account_id:
            raise ValueError(
                "CloudflareWAIProvider requires an account_id (passed in __init__ "
                "or via CLOUDFLARE_ACCOUNT_ID env var)."
            )
        self._timeout = http_timeout
        self._base = CF_API_BASE_TEMPLATE.format(account_id=self._account_id)
        self._models_url = f"{self._base}/models"
        self._chat_url = f"{self._base}/chat/completions"
        self._embeddings_url = f"{self._base}/embeddings"

    # ----- request stamping ----------------------------------------------
    def auth_header(self, key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {key}"}

    def attribution_headers(self) -> dict[str, str]:
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
        if status == 401 or status == 403:
            # CF can return 403 for auth issues (account suspended,
            # token doesn't have AI permission).
            return ErrorKind.AUTH
        if status == 429:
            return ErrorKind.RATE_LIMIT
        if 500 <= status < 600:
            return ErrorKind.UNAVAILABLE

        # CF often returns 4xx with a `success: false, errors: [...]`
        # envelope rather than the OpenAI `error` shape.
        try:
            body = resp.json()
        except Exception:
            return ErrorKind.UNKNOWN
        if isinstance(body, dict):
            if body.get("success") is False:
                errs = body.get("errors") or []
                if isinstance(errs, list) and errs:
                    msg = str(errs[0].get("message", "")).lower() if isinstance(errs[0], dict) else ""
                    if "model" in msg and ("not found" in msg or "not exist" in msg):
                        return ErrorKind.MODEL_NOT_FOUND
                    if "neuron" in msg or "quota" in msg or "limit" in msg:
                        return ErrorKind.QUOTA_EXHAUSTED
            err = body.get("error", body)
            if isinstance(err, dict):
                msg = str(err.get("message", "")).lower()
                if "model" in msg and "not found" in msg:
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
        """Hit CF /ai/v1/models, intersect with the free allowlist,
        attach out-of-band metadata.
        """
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.get(self._models_url, headers=self._outbound_headers(key))
        resp.raise_for_status()
        # CF wraps OpenAI-shape responses in a `result` envelope
        # sometimes; handle both `data` (top-level) and `result.data`.
        body = resp.json()
        raw = body.get("data") or (body.get("result") or {}).get("data") or []

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
            out.append(
                Model(
                    api_id=mid,
                    provider=self.name,
                    context_length=meta.context_length,
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
                    self._chat_url,
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
                self._chat_url,
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
                self._chat_url,
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
        """OpenAI-shape /v1/embeddings forward to Cloudflare Workers AI."""
        payload = request.model_dump(exclude_none=True)
        payload["model"] = model_id

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                self._embeddings_url,
                headers=self._outbound_headers(key, json_content=True),
                json=payload,
            )
        resp.raise_for_status()
        return EmbeddingResponse.model_validate(resp.json())
