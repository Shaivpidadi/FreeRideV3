"""NVIDIA NIM / NVIDIA Build provider plugin.

Implements the v3 :class:`Provider` Protocol for ``integrate.api.nvidia.com``.
Per ``knowledge/providers/nvidia_nim.md``, NIM has three classification
quirks that the gateway must absorb in ``classify_error`` (without any
core/ changes — Phase 3 seam-quality gate):

1. **HTTP 403, not 401, for invalid keys** — must map to ``AUTH``.
2. **``text/plain`` ``"404 page not found"`` for unknown models** —
   not a JSON envelope; classifier inspects ``Content-Type``.
3. **No programmatic free-tier signal** — all catalog models are callable
   until the personal-credit pool exhausts, so we ship a hardcoded
   allowlist plus a ``NVIDIA_NIM_FREE_MODELS_OVERRIDE`` env override.

Two response-shape variants observed in the wild (per nvidia_nim.md):
the 8b-instruct models return classic NIM shape with ``nvext``; 70b
models return a vLLM-extended shape. Both round-trip through us
because :class:`ChatResponse` permits ``extra='allow'``. Provider
plugins are responsible for stripping their own extensions before
forwarding to clients — we do that scrubbing here so wire output is
clean OpenAI shape.
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
from freeride.providers.nim_model_metadata import lookup as _lookup_meta


NIM_API_BASE = "https://integrate.api.nvidia.com/v1"
NIM_MODELS_URL = f"{NIM_API_BASE}/models"
NIM_CHAT_URL = f"{NIM_API_BASE}/chat/completions"


# Free-tier allowlist — see nim_model_metadata.py. We match exact ids
# rather than prefixes so we don't accidentally free-flag a paid model
# that shares a vendor namespace.
DEFAULT_FREE_MODEL_IDS: frozenset[str] = frozenset(
    [
        "meta/llama-3.1-8b-instruct",
        "meta/llama-3.1-70b-instruct",
        "meta/llama-3.1-405b-instruct",
        "meta/llama-3.2-3b-instruct",
        "meta/llama-3.3-70b-instruct",
        "deepseek-ai/deepseek-r1",
        "deepseek-ai/deepseek-v3",
        "mistralai/mistral-7b-instruct-v0.3",
        "mistralai/mixtral-8x7b-instruct-v0.1",
        "mistralai/mixtral-8x22b-instruct-v0.1",
        "qwen/qwen2.5-7b-instruct",
        "qwen/qwen2.5-coder-32b-instruct",
        "google/gemma-3-27b-it",
    ]
)


def _free_model_set() -> frozenset[str]:
    """Allowlist with optional env override. Comma-separated list of ids."""
    override = os.environ.get("NVIDIA_NIM_FREE_MODELS_OVERRIDE", "").strip()
    if not override:
        return DEFAULT_FREE_MODEL_IDS
    return frozenset(s.strip() for s in override.split(",") if s.strip())


# Provider-specific response keys to strip before forwarding to client.
# Per knowledge/providers/nvidia_nim.md: nvext is a NIM extension; the
# vLLM extras (refusal/annotations/audio/function_call/tool_calls/
# reasoning/reasoning_content/token_ids/stop_reason/prompt_logprobs/
# prompt_token_ids/kv_transfer_params/service_tier/system_fingerprint)
# appear on 70b-instruct responses. Some of these (tool_calls, refusal)
# are real OpenAI fields and should NOT be stripped — only the
# truly NIM/vLLM-private ones are.
_NIM_TOPLEVEL_STRIP = {"nvext"}
_NIM_MESSAGE_STRIP = {
    "reasoning_content",
    "reasoning",
    "token_ids",
    "prompt_logprobs",
    "prompt_token_ids",
    "kv_transfer_params",
}


def _strip_nim_extensions(obj: dict) -> dict:
    """Remove NIM/vLLM private extensions; keep OpenAI-shaped fields."""
    for k in list(obj.keys()):
        if k in _NIM_TOPLEVEL_STRIP:
            del obj[k]
    for choice in obj.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        msg = choice.get("message") or choice.get("delta") or {}
        if isinstance(msg, dict):
            for k in list(msg.keys()):
                if k in _NIM_MESSAGE_STRIP:
                    del msg[k]
    return obj


class NVIDIANIMProvider:
    """NVIDIA NIM Provider plugin."""

    name: str = "nvidia_nim"
    api_version: int = PROVIDER_API_VERSION

    def __init__(self, *, http_timeout: float = 30.0) -> None:
        self._timeout = http_timeout

    # ----- request stamping ---------------------------------------------
    def auth_header(self, key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {key}"}

    def attribution_headers(self) -> dict[str, str]:
        # NIM accepts but ignores HTTP-Referer / X-Title. Document the
        # absence explicitly rather than send dead headers.
        return {}

    def _outbound_headers(self, key: str, *, json_content: bool = False) -> dict[str, str]:
        h: dict[str, str] = {**self.auth_header(key), **self.attribution_headers()}
        if json_content:
            h["Content-Type"] = "application/json"
        return h

    # ----- error classification -----------------------------------------
    def classify_error(self, response_or_exc: Any) -> ErrorKind:
        # Exception path
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

        # NIM uses 403 (NOT 401) for invalid bearer tokens.
        if status == 403:
            return ErrorKind.AUTH
        if status == 401:
            return ErrorKind.AUTH  # for safety, in case behavior changes
        if status == 429:
            return ErrorKind.RATE_LIMIT
        if status == 402:
            # Some quota-exhausted responses use 402.
            return ErrorKind.QUOTA_EXHAUSTED
        if 500 <= status < 600:
            return ErrorKind.UNAVAILABLE

        # 404 with text/plain "404 page not found" -> MODEL_NOT_FOUND.
        # JSON 404s could be other things, so check Content-Type first.
        if status == 404:
            ct = ""
            headers = getattr(resp, "headers", None)
            if headers is not None and hasattr(headers, "get"):
                ct = (headers.get("content-type") or "").lower()
            if ct.startswith("text/plain"):
                return ErrorKind.MODEL_NOT_FOUND

        # 4xx fallback: try JSON body. Streaming responses raise
        # ResponseNotRead unless aread() called first; treat as
        # "nothing to inspect" and fall through to UNKNOWN.
        try:
            body = resp.json()
        except Exception:
            return ErrorKind.UNKNOWN
        if isinstance(body, dict):
            err = body.get("error", body)
            msg = str(err.get("message", "")) if isinstance(err, dict) else ""
            if "model" in msg.lower() and ("not found" in msg.lower() or "not exist" in msg.lower()):
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

    # ----- discovery & probing ------------------------------------------
    def list_free_models(self, key: str) -> list[Model]:
        """Hit NIM /v1/models, intersect with the free allowlist, dedupe by
        id, attach out-of-band metadata.
        """
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.get(NIM_MODELS_URL, headers=self._outbound_headers(key))
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
                    NIM_CHAT_URL,
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
        # Strip nvext from request if a client passed it through (we
        # don't enable NIM-private extensions toward upstream).
        payload.pop("nvext", None)

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                NIM_CHAT_URL,
                headers=self._outbound_headers(key, json_content=True),
                json=payload,
            )
        resp.raise_for_status()
        body = _strip_nim_extensions(resp.json())
        return ChatResponse.model_validate(body)

    async def forward_chat_stream(
        self, request: ChatRequest, model_id: str, key: str
    ) -> AsyncIterator[ChatStreamEvent]:
        payload = request.model_dump(exclude_none=True)
        payload["model"] = model_id
        payload["stream"] = True
        payload.pop("nvext", None)

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            async with client.stream(
                "POST",
                NIM_CHAT_URL,
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
                    obj = _strip_nim_extensions(obj)
                    yield ChatStreamEvent.model_validate(obj)
