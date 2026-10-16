"""Generic OpenAI-compatible provider, driven by an :class:`OpenAICompatSpec`.

One class serves every gateway whose only differences are data (base
URL, auth env var, which models are free, front-door headers, quota
shape). The bespoke plugins (OpenRouter, NIM, Cloudflare, Groq, …) stay
as they are; this adapter is how the long tail gets in without a
module each. See ``docs/providers/EXPANSION-RESEARCH.md`` §5.

Behaviours the spec switches on:

* **keyless** — ``auth_header`` returns ``{}``; the chain carries the
  :data:`KEYLESS_SENTINEL` pseudo-key.
* **free-model detection** — override env → allowlist → predicate →
  suffix → zero pricing → everything (see the spec docstring).
* **fallback catalog** — when ``GET /models`` fails (some keyless
  gateways gate the list behind auth) the spec's ``fallback_models``
  are surfaced so the provider still routes.
* **retry hints from bodies** — Gemini's ``retryDelay: "17s"``,
  LongCat's ``retry_after: 60`` and "try again in 30 seconds" prose are
  read when the ``Retry-After`` header is absent.
* **cooldown scope** — ``cooldown_scope == "model"`` tells the failover
  loop a 429 is about one model, not the key.
* **model substitution detection** — a response whose ``model`` is
  not the one requested is logged and counted; some gateways answer a
  named free route with a different model under HTTP 200.
"""

from __future__ import annotations

import json as _json
import logging
import os
import re
import time
from email.utils import parsedate_to_datetime
from typing import Any, AsyncIterator, Iterable

import httpx

from freeride.core.chat_schema import ChatRequest, ChatResponse, ChatStreamEvent
from freeride.core.embedding_schema import EmbeddingRequest, EmbeddingResponse
from freeride.core.errors import ErrorKind
from freeride.core.provider import PROVIDER_API_VERSION
from freeride.core.provider_specs import (
    KEYLESS_SENTINEL,
    ModelMeta,
    OpenAICompatSpec,
)
from freeride.core.types import Model, ProbeResult

logger = logging.getLogger(__name__)

_MAX_RETRY_AFTER_S = 24 * 3600

# "try again in 30 seconds", "retry after 2m", "wait 1.5s", "in 45 sec".
_PROSE_DELAY = re.compile(
    r"(?:try again|retry|wait|available)\D{0,20}?(\d+(?:\.\d+)?)\s*"
    r"(ms|milliseconds?|s|sec|secs|seconds?|m|min|mins|minutes?|h|hr|hrs|hours?)\b",
    re.IGNORECASE,
)
_DURATION_FIELD = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h)?\s*$", re.IGNORECASE)
_UNIT_SECONDS = {
    "ms": 0.001,
    "millisecond": 0.001,
    "milliseconds": 0.001,
    "s": 1,
    "sec": 1,
    "secs": 1,
    "second": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "mins": 60,
    "minute": 60,
    "minutes": 60,
    "h": 3600,
    "hr": 3600,
    "hrs": 3600,
    "hour": 3600,
    "hours": 3600,
}
_RETRY_KEYS = ("retrydelay", "retry_after", "retryafter", "retryafterseconds", "retry_after_seconds", "retry_in")

_MODEL_NOT_FOUND_NEEDLES = (
    "model not found",
    "model_not_found",
    "unknown model",
    "not a valid model",
    "does not exist",
    "no such model",
    "decommissioned",
    "model_retired",
    "is not available",
    "not supported",
    "requires a subscription",
    "premium model",
    "requires a paid",
    "upgrade your plan",
)
_QUOTA_NEEDLES = (
    "quota",
    "insufficient balance",
    "insufficient credits",
    "out of credits",
    "credit balance",
    "exceeded your current",
    "daily limit",
    "payment required",
    "billing",
)


def parse_retry_after_value(raw: Any) -> int | None:
    """Seconds from a ``Retry-After`` header: delta-seconds or HTTP-date."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        value = int(float(text))
        return max(value, 0) if value <= _MAX_RETRY_AFTER_S else _MAX_RETRY_AFTER_S
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    delta = when.timestamp() - time.time()
    if delta <= 0:
        return 0
    return int(min(delta, _MAX_RETRY_AFTER_S))


def _duration_to_seconds(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        secs = float(value)
    elif isinstance(value, str):
        m = _DURATION_FIELD.match(value)
        if not m:
            return None
        secs = float(m.group(1)) * _UNIT_SECONDS.get((m.group(2) or "s").lower(), 1)
    elif isinstance(value, dict) and "seconds" in value:
        try:
            secs = float(value["seconds"])
        except (TypeError, ValueError):
            return None
    else:
        return None
    if secs < 0:
        return None
    return int(min(round(secs), _MAX_RETRY_AFTER_S))


def retry_hint_from_body(body: Any, *, _depth: int = 0) -> int | None:
    """Walk an error body (depth-capped) for a stated retry delay.

    Structured fields first (``retryDelay``, ``retry_after``,
    ``retryAfterSeconds`` …), then anchored prose in any ``message`` /
    ``detail`` string. Returns seconds, clamped to 24 h, or ``None``.
    """
    if _depth > 6 or body is None:
        return None
    if isinstance(body, dict):
        for k, v in body.items():
            if str(k).replace("-", "_").lower() in _RETRY_KEYS:
                secs = _duration_to_seconds(v)
                if secs is not None:
                    return secs
        for v in body.values():
            found = retry_hint_from_body(v, _depth=_depth + 1)
            if found is not None:
                return found
        return None
    if isinstance(body, list):
        for item in body:
            found = retry_hint_from_body(item, _depth=_depth + 1)
            if found is not None:
                return found
        return None
    if isinstance(body, str):
        m = _PROSE_DELAY.search(body)
        if not m:
            return None
        secs = float(m.group(1)) * _UNIT_SECONDS.get(m.group(2).lower(), 1)
        return int(min(round(secs), _MAX_RETRY_AFTER_S))
    return None


def normalize_model_id(model_id: str) -> str:
    """Comparable form of a model id: lower-case, provider prefix and
    ``:free``/``-free`` suffix dropped, separators unified. Used only
    for the substitution check, never for routing."""
    s = (model_id or "").strip().lower()
    for suffix in (":free", "-free"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    if s.startswith("models/"):
        s = s[len("models/") :]
    if "/" in s:
        s = s.rsplit("/", 1)[1]
    return re.sub(r"[^a-z0-9]+", "", s)


# Ids that are plainly not chat models. Gateways that mix TTS, ASR,
# image, embedding and guard models into /models (OVH does) would
# otherwise hand the ladder a stable-diffusion id as the "best" model.
_NON_CHAT_ID = re.compile(
    r"(tts|whisper|stable-diffusion|sdxl|flux|embedding|embed-|-embed|bge-|rerank|"
    r"moderation|guard|safety|ocr|transcri|speech|voice|audio|image-gen|imagen|"
    r"veo|sora|dall-e|text-to-|-vision-encoder)",
    re.IGNORECASE,
)
_NON_CHAT_TYPES = {"image", "audio", "embedding", "embeddings", "tts", "asr", "rerank", "moderation", "video"}


def is_chat_shaped(raw: dict[str, Any], mid: str) -> bool:
    """False for catalog entries that cannot serve /chat/completions."""
    for k in ("type", "model_type", "task", "modality"):
        v = raw.get(k)
        if isinstance(v, str) and v.lower() in _NON_CHAT_TYPES:
            return False
    cats = raw.get("categories")
    if isinstance(cats, list) and cats and not any(str(c).lower() in ("chat", "text", "completion", "llm") for c in cats):
        return False
    arch = raw.get("architecture")
    if isinstance(arch, dict):
        out = arch.get("output_modalities")
        if isinstance(out, list) and out and out != ["text"]:
            return False
    return not _NON_CHAT_ID.search(mid)


def _error_text(body: Any) -> str:
    """Lower-cased concatenation of the human-readable bits of an error
    body, whatever its shape."""
    if isinstance(body, dict):
        err = body.get("error", body)
        parts: list[str] = []
        if isinstance(err, dict):
            for k in ("message", "detail", "code", "type", "title"):
                v = err.get(k)
                if isinstance(v, (str, int)):
                    parts.append(str(v))
        elif isinstance(err, str):
            parts.append(err)
        for k in ("message", "detail"):
            v = body.get(k)
            if isinstance(v, str):
                parts.append(v)
        return " ".join(parts).lower()
    if isinstance(body, str):
        return body.lower()
    return ""


class OpenAICompatProvider:
    """A :class:`~freeride.core.provider.Provider` built from a spec."""

    api_version: int = PROVIDER_API_VERSION
    # Class-level default; instances override from the spec. The
    # embeddings route and the conformance suite both read this.
    embeddings_supported: bool = False

    def __init__(self, spec: OpenAICompatSpec) -> None:
        self.spec = spec
        self.name = spec.name
        self.embeddings_supported = spec.embeddings_path is not None
        self.cooldown_scope = spec.cooldown_scope
        self.keyless = spec.keyless
        self.data_use = spec.data_use
        self._timeout = httpx.Timeout(
            connect=spec.timeout_s,
            read=max(spec.read_timeout_s, spec.timeout_s),
            write=spec.timeout_s,
            pool=spec.timeout_s,
        )
        # Streaming keeps a long read budget: reasoning models can sit
        # silent for a minute between visible tokens.
        self._stream_timeout = httpx.Timeout(
            connect=spec.timeout_s,
            read=max(600.0, spec.read_timeout_s),
            write=spec.timeout_s,
            pool=spec.timeout_s,
        )
        self._models_url = spec.url(spec.models_path)
        self._chat_url = spec.url(spec.chat_path)
        self._embeddings_url = spec.url(spec.embeddings_path) if spec.embeddings_path else None
        self.substitutions_seen = 0

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"OpenAICompatProvider({self.name!r})"

    # ----- request stamping ----------------------------------------------
    def auth_header(self, key: str) -> dict[str, str]:
        if self.spec.keyless or not key or key == KEYLESS_SENTINEL:
            return {}
        return {"Authorization": f"Bearer {key}"}

    def attribution_headers(self) -> dict[str, str]:
        return dict(self.spec.extra_headers)

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
        if status in (401, 403) and self.spec.keyless:
            # There is no key to be wrong. An anonymous gateway answers
            # 401/403 for routes outside its free roster, which is a
            # model problem: skip the id, keep the provider.
            return ErrorKind.MODEL_NOT_FOUND
        if status == 401:
            return ErrorKind.AUTH
        if status == 402:
            return ErrorKind.QUOTA_EXHAUSTED
        if status == 429:
            return ErrorKind.RATE_LIMIT
        if status in (408, 504):
            return ErrorKind.TIMEOUT
        if 500 <= status < 600:
            return ErrorKind.UNAVAILABLE

        text = _error_text(self._body_of(resp))
        if status == 403:
            # Tier gates ("premium model", "requires a subscription") are
            # about the model, not the key; benching the key would take
            # every other free model with it.
            if any(n in text for n in _MODEL_NOT_FOUND_NEEDLES):
                return ErrorKind.MODEL_NOT_FOUND
            if any(n in text for n in _QUOTA_NEEDLES):
                return ErrorKind.QUOTA_EXHAUSTED
            return ErrorKind.AUTH
        if status == 404:
            # Unknown model ids come back as 404 on most gateways, often
            # as a bare text page (NIM-style). A wrong base URL also 404s,
            # and skipping that provider is the right outcome too.
            return ErrorKind.MODEL_NOT_FOUND
        if status in (400, 410, 422):
            if any(n in text for n in _MODEL_NOT_FOUND_NEEDLES) or (
                "model" in text and ("invalid" in text or "unknown" in text)
            ):
                return ErrorKind.MODEL_NOT_FOUND
            if any(n in text for n in _QUOTA_NEEDLES):
                return ErrorKind.QUOTA_EXHAUSTED
        return ErrorKind.UNKNOWN

    @staticmethod
    def _body_of(resp: Any) -> Any:
        try:
            return resp.json()
        except Exception:
            try:
                return resp.text
            except Exception:
                return None

    def retry_after_hint(self, response: Any) -> int | None:
        if response is None:
            return None
        headers = getattr(response, "headers", None)
        if headers is not None and hasattr(headers, "get"):
            for name in ("retry-after", "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"):
                raw = headers.get(name)
                if raw:
                    secs = parse_retry_after_value(raw) if name == "retry-after" else _duration_to_seconds(raw)
                    if secs is not None:
                        return secs
        return retry_hint_from_body(self._body_of(response))

    # ----- discovery -----------------------------------------------------
    def _override_ids(self) -> frozenset[str] | None:
        raw = os.environ.get(f"{self.name.upper()}_FREE_MODELS_OVERRIDE", "").strip()
        if not raw:
            return None
        return frozenset(s.strip() for s in raw.split(",") if s.strip())

    @staticmethod
    def _is_zero_priced(raw: dict[str, Any]) -> bool | None:
        pricing = raw.get("pricing")
        if not isinstance(pricing, dict):
            return None
        for k in ("prompt", "input"):
            if k in pricing:
                try:
                    return float(pricing[k] or 0) == 0
                except (TypeError, ValueError):
                    return None
        return None

    def _is_free(self, raw: dict[str, Any], mid: str, override: frozenset[str] | None) -> bool:
        spec = self.spec
        if override is not None:
            return mid in override
        if spec.free_ids is not None:
            return mid in spec.free_ids
        if spec.free_predicate is not None:
            return bool(spec.free_predicate(raw))
        if spec.free_id_suffix:
            return mid.endswith(spec.free_id_suffix)
        zero = self._is_zero_priced(raw)
        if zero is not None:
            return zero
        return True

    @staticmethod
    def _context_length(raw: dict[str, Any]) -> int:
        for k in ("context_length", "context_window", "max_context_length", "max_input_tokens"):
            v = raw.get(k)
            if isinstance(v, (int, float)) and v > 0:
                return int(v)
        top = raw.get("top_provider")
        if isinstance(top, dict) and isinstance(top.get("context_length"), int):
            return int(top["context_length"])
        return 0

    @staticmethod
    def _catalog_tools_flag(raw: dict[str, Any]) -> bool | None:
        """Read whatever self-reported tool-support flag the catalog
        carries. ``None`` when the entry says nothing."""
        sp = raw.get("supported_parameters")
        if isinstance(sp, list):
            return "tools" in sp
        caps = raw.get("capabilities")
        if isinstance(caps, dict) and "tools" in caps:
            return bool(caps["tools"])
        if isinstance(caps, list):
            return "tools" in caps or "function_calling" in caps
        for k in ("supports_tool_calling", "function_call", "function_calling", "tool_calling", "tools"):
            if isinstance(raw.get(k), bool):
                return bool(raw[k])
        return None

    def _to_model(self, raw: dict[str, Any], mid: str) -> Model:
        meta: ModelMeta = self.spec.metadata.get(mid, ModelMeta())
        ctx = max(self._context_length(raw), meta.context_length) or self.spec.default_context_length
        params = set(meta.supported_parameters)
        flag = self._catalog_tools_flag(raw)
        if flag is True or (flag is None and self.spec.tools_default and not meta.supported_parameters):
            params.add("tools")
        if flag is False:
            params.discard("tools")
        return Model(
            api_id=mid,
            provider=self.name,
            context_length=ctx,
            output_modalities=("text",),
            supported_parameters=tuple(sorted(params)),
            raw=raw,
        )

    def _fallback_models(self) -> list[Model]:
        override = self._override_ids()
        ids: Iterable[str] = override if override is not None else self.spec.fallback_models
        return [self._to_model({"id": mid}, mid) for mid in ids if mid not in self.spec.broken_ids]

    def list_free_models(self, key: str) -> list[Model]:
        try:
            with httpx.Client(timeout=self._timeout) as client:
                resp = client.get(self._models_url, headers=self._outbound_headers(key))
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            if self.spec.fallback_models or self._override_ids():
                logger.info("%s: catalog unavailable (%s); using fallback list", self.name, e)
                return self._fallback_models()
            raise
        raw_list = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(raw_list, list):
            raw_list = []
        override = self._override_ids()
        seen: set[str] = set()
        out: list[Model] = []
        for raw in raw_list:
            if not isinstance(raw, dict):
                continue
            mid = str(raw.get("id") or raw.get("name") or "")
            if self.spec.id_prefix_strip and mid.startswith(self.spec.id_prefix_strip):
                mid = mid[len(self.spec.id_prefix_strip) :]
            if not mid or mid in seen or mid in self.spec.broken_ids:
                continue
            if not is_chat_shaped(raw, mid):
                continue
            if not self._is_free(raw, mid, override):
                continue
            seen.add(mid)
            out.append(self._to_model(raw, mid))
        if not out and self.spec.fallback_models:
            return self._fallback_models()
        return out

    # ----- probing -------------------------------------------------------
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
        except httpx.HTTPError as e:
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

    # ----- request shaping -----------------------------------------------
    def _prepare_payload(self, request: ChatRequest, model_id: str, *, stream: bool) -> dict[str, Any]:
        payload = request.model_dump(exclude_none=True)
        payload["model"] = model_id
        payload["stream"] = stream
        for k in self.spec.drop_params:
            payload.pop(k, None)
        if self.spec.force_single_tool_call and payload.get("tools"):
            payload["parallel_tool_calls"] = False
        return payload

    def _check_model(self, requested: str, served: Any) -> None:
        """Detect a response that names a different model than the one
        requested. Routers with ``auto`` / ``*_smart`` routes
        legitimately report the upstream id, so those are exempt.

        By default this logs and counts (a signal for the event log and
        ``freeride audit-models``). With ``spec.strict_model_match`` it
        raises an :class:`httpx.HTTPStatusError` carrying a synthetic
        404, which ``classify_error`` reads as MODEL_NOT_FOUND: the
        failover loop then skips the id instead of handing an agent a
        model it did not ask for.
        """
        if not isinstance(served, str) or not served:
            return
        if requested in ("auto", "free_smart") or requested.endswith("/free") or requested.endswith("/auto"):
            return
        if normalize_model_id(served) == normalize_model_id(requested):
            return
        self.substitutions_seen += 1
        logger.warning(
            "%s answered model %r with %r (substitution #%d)",
            self.name,
            requested,
            served,
            self.substitutions_seen,
        )
        if self.spec.strict_model_match:
            request = httpx.Request("POST", self._chat_url)
            response = httpx.Response(
                404,
                json={"error": {"code": "model_not_found", "message": f"{self.name} substituted {served!r} for {requested!r}"}},
                request=request,
            )
            raise httpx.HTTPStatusError("model substituted", request=request, response=response)

    # ----- request forwarding --------------------------------------------
    async def forward_chat(self, request: ChatRequest, model_id: str, key: str) -> ChatResponse:
        payload = self._prepare_payload(request, model_id, stream=False)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                self._chat_url,
                headers=self._outbound_headers(key, json_content=True),
                json=payload,
            )
        resp.raise_for_status()
        body = resp.json()
        if isinstance(body, dict):
            self._check_model(model_id, body.get("model"))
            self._on_response(body)
        return ChatResponse.model_validate(body)

    async def forward_chat_stream(
        self, request: ChatRequest, model_id: str, key: str
    ) -> AsyncIterator[ChatStreamEvent]:
        payload = self._prepare_payload(request, model_id, stream=True)
        checked = False
        async with httpx.AsyncClient(timeout=self._stream_timeout) as client:
            async with client.stream(
                "POST",
                self._chat_url,
                headers=self._outbound_headers(key, json_content=True),
                json=payload,
            ) as resp:
                resp.raise_for_status()
                async for raw_line in resp.aiter_lines():
                    if not raw_line or not raw_line.startswith("data:"):
                        continue
                    data_str = raw_line[len("data:") :].strip()
                    if data_str == "[DONE]":
                        return
                    try:
                        obj = _json.loads(data_str)
                    except _json.JSONDecodeError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    if not checked and obj.get("model"):
                        self._check_model(model_id, obj.get("model"))
                        checked = True
                    self._on_stream_event(obj)
                    yield ChatStreamEvent.model_validate(obj)

    def _on_response(self, body: dict[str, Any]) -> None:
        """Hook for subclasses (Gemini signature cache). No-op here."""

    def _on_stream_event(self, obj: dict[str, Any]) -> None:
        """Hook for subclasses. No-op here."""

    # ----- embeddings ------------------------------------------------------
    async def forward_embeddings(
        self, request: EmbeddingRequest, model_id: str, key: str
    ) -> EmbeddingResponse:
        if self._embeddings_url is None:
            raise RuntimeError(f"{self.name} does not expose an embeddings endpoint")
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


def make_provider(spec: OpenAICompatSpec) -> OpenAICompatProvider:
    """Build the right adapter class for ``spec`` (Gemini gets its
    signature-caching subclass)."""
    if spec.name == "gemini":
        from freeride.providers.gemini_compat import GeminiCompatProvider

        return GeminiCompatProvider(spec)
    return OpenAICompatProvider(spec)
