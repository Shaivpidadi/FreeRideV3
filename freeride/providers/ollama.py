"""Ollama provider plugin — exposes a local Ollama daemon as a provider.

Ollama (https://ollama.com) is a local LLM runtime that ships an
OpenAI-compatible HTTP surface at ``http://localhost:11434``. Adding it
as a FreeRide provider lets users mix local models with free-tier
remote providers in the same failover chain — e.g., "try local Llama
3.1 first, fall back to OpenRouter if it's slow or not loaded".

Configuration: opt in via ``OLLAMA_BASE_URL`` env var. The env var
doubles as the per-request "key" in the failover chain
(no real auth required for a local Ollama; if you proxy Ollama behind
an auth layer, point this at the proxy).

Defaults to ``http://localhost:11434`` when the env var is exactly that
literal value or "auto"/"default" — but cmd_serve only auto-loads
this provider when the env var is *set*, so the default is just a
fallback for explicit construction.

Endpoints used:
  GET  /v1/models           → catalog (Ollama mirrors OpenAI shape)
  POST /v1/chat/completions → chat (streaming + non-streaming)
  POST /v1/embeddings       → embeddings (Ollama 0.1.40+)

Auth: none. ``auth_header`` returns ``{}``. Ollama does not consume
the bearer token; the local socket is authority.
"""

from __future__ import annotations

import json as _json
import time
from typing import Any, AsyncIterator

import httpx

from freeride.core.chat_schema import ChatRequest, ChatResponse, ChatStreamEvent
from freeride.core.embedding_schema import EmbeddingRequest, EmbeddingResponse
from freeride.core.errors import ErrorKind
from freeride.core.provider import PROVIDER_API_VERSION
from freeride.core.types import Model, ProbeResult


DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"


class OllamaProvider:
    """Ollama provider plugin.

    The constructor accepts an explicit ``base_url`` so tests don't have
    to mutate env vars; in production the gateway passes the value of
    ``OLLAMA_BASE_URL`` (treated as the "key" in the chain) at request
    time, but the simpler model is to construct once with the URL and
    ignore the chain's key argument.
    """

    name: str = "ollama"
    api_version: int = PROVIDER_API_VERSION
    embeddings_supported: bool = True

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_OLLAMA_BASE_URL,
        http_timeout: float = 60.0,  # local can still be slow on first load
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = http_timeout
        self._models_url = f"{self._base_url}/v1/models"
        self._chat_url = f"{self._base_url}/v1/chat/completions"
        self._embeddings_url = f"{self._base_url}/v1/embeddings"

    def _resolve_base_url(self, key: str) -> str:
        """If the chain passes a non-default key (e.g. user set
        ``OLLAMA_BASE_URL=http://other-host:11434``), prefer that over
        the constructor-time default. Lets one running gateway target
        multiple Ollama hosts via env-var rotation, mirroring how other
        providers handle multi-key.
        """
        if key and key != "any" and (key.startswith("http://") or key.startswith("https://")):
            return key.rstrip("/")
        return self._base_url

    # ----- request stamping ----------------------------------------------
    def auth_header(self, key: str) -> dict[str, str]:
        # Ollama is local; no bearer required. If a user fronts Ollama
        # with an auth proxy that needs Authorization: Bearer <token>,
        # they should run a different upstream (e.g., the openai
        # provider via a custom base_url) — out of scope here.
        return {}

    def attribution_headers(self) -> dict[str, str]:
        return {}

    def _outbound_headers(self, *, json_content: bool = False) -> dict[str, str]:
        h: dict[str, str] = {}
        if json_content:
            h["Content-Type"] = "application/json"
        return h

    # ----- error classification ------------------------------------------
    def classify_error(self, response_or_exc: Any) -> ErrorKind:
        if isinstance(response_or_exc, httpx.TimeoutException):
            return ErrorKind.TIMEOUT
        if isinstance(response_or_exc, httpx.ConnectError):
            # Ollama isn't running locally → treat as UNAVAILABLE so the
            # resolver advances to the next provider.
            return ErrorKind.UNAVAILABLE
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
            return ErrorKind.AUTH
        if status == 429:
            return ErrorKind.RATE_LIMIT
        if 500 <= status < 600:
            return ErrorKind.UNAVAILABLE

        # Ollama returns 404 in OpenAI-shape ({"error": {"message":
        # "model 'X' not found, try pulling it first"}}) when the
        # requested model isn't downloaded.
        try:
            body = resp.json()
        except Exception:
            return ErrorKind.UNKNOWN
        if isinstance(body, dict):
            err = body.get("error", body)
            msg = str(err.get("message", "")).lower() if isinstance(err, dict) else ""
            if "not found" in msg or "try pulling" in msg or "no such file" in msg:
                return ErrorKind.MODEL_NOT_FOUND
        return ErrorKind.UNKNOWN

    def retry_after_hint(self, response: Any) -> int | None:
        # Ollama doesn't issue retry-after.
        return None

    # ----- discovery -----------------------------------------------------
    def list_free_models(self, key: str) -> list[Model]:
        """Return the locally pulled models. Everything Ollama exposes is
        free (it's local).
        """
        base = self._resolve_base_url(key)
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.get(f"{base}/v1/models")
        resp.raise_for_status()
        raw = resp.json().get("data", []) or []

        out: list[Model] = []
        seen: set[str] = set()
        for m in raw:
            mid = m.get("id", "")
            if not mid or mid in seen:
                continue
            seen.add(mid)
            # Ollama doesn't expose context_length on /v1/models; the
            # /api/show endpoint has it but requires another round trip.
            # 8K is the safe-ish default for most Ollama-shipped models;
            # power users who need accurate context can hit /api/show
            # themselves.
            out.append(
                Model(
                    api_id=mid,
                    provider=self.name,
                    context_length=8_192,
                    output_modalities=("text",),
                    supported_parameters=(),
                    raw=m,
                )
            )
        return out

    def probe(self, model_id: str, key: str) -> ProbeResult:
        base = self._resolve_base_url(key)
        payload = {
            "model": model_id,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 5,
            "stream": False,
        }
        started = time.perf_counter()
        try:
            with httpx.Client(timeout=self._timeout) as client:
                resp = client.post(
                    f"{base}/v1/chat/completions",
                    headers=self._outbound_headers(json_content=True),
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
        base = self._resolve_base_url(key)
        payload = request.model_dump(exclude_none=True)
        payload["model"] = model_id
        payload["stream"] = False

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{base}/v1/chat/completions",
                headers=self._outbound_headers(json_content=True),
                json=payload,
            )
        resp.raise_for_status()
        return ChatResponse.model_validate(resp.json())

    async def forward_chat_stream(
        self, request: ChatRequest, model_id: str, key: str
    ) -> AsyncIterator[ChatStreamEvent]:
        base = self._resolve_base_url(key)
        payload = request.model_dump(exclude_none=True)
        payload["model"] = model_id
        payload["stream"] = True

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            async with client.stream(
                "POST",
                f"{base}/v1/chat/completions",
                headers=self._outbound_headers(json_content=True),
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

    async def forward_embeddings(
        self, request: EmbeddingRequest, model_id: str, key: str
    ) -> EmbeddingResponse:
        base = self._resolve_base_url(key)
        payload = request.model_dump(exclude_none=True)
        payload["model"] = model_id

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{base}/v1/embeddings",
                headers=self._outbound_headers(json_content=True),
                json=payload,
            )
        resp.raise_for_status()
        return EmbeddingResponse.model_validate(resp.json())
