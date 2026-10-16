"""Gemini via Google's OpenAI-compatible endpoint, with the one quirk
that breaks multi-turn tool use fixed in-line.

Gemini 3 attaches a ``thought_signature`` to every function call it
emits (``tool_calls[i].extra_content.google.thought_signature``) and
rejects a later request that replays that call in history without the
signature. OpenAI-format clients — and FreeRide's own Anthropic / Codex
/ fx translators — have no slot for it, so it is lost on the round
trip. This subclass remembers each signature keyed by tool-call id and
by a name+arguments fingerprint (agents sometimes rewrite ids), and
puts it back on the way out. When nothing is cached it sends Google's
documented sentinel, which tells the server to skip the check for a
call the API did not produce.

It also strips JSON-Schema keywords Gemini's validator rejects from
tool definitions (``$schema``, ``strict``), which some agents send by
default.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from typing import Any

from freeride.core.chat_schema import ChatRequest
from freeride.core.provider_specs import OpenAICompatSpec
from freeride.providers.openai_compat import OpenAICompatProvider

# Google documents this exact value for calls the API did not produce.
SKIP_SIGNATURE_SENTINEL = "skip_thought_signature_validator"
_CACHE_MAX = 2048
_CACHE_TTL_S = 6 * 3600
_SCHEMA_STRIP = ("$schema", "strict", "additionalProperties", "$id", "$comment")


class _SignatureCache:
    def __init__(self) -> None:
        self._items: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self._lock = threading.Lock()

    def put(self, key: str, signature: str) -> None:
        if not key or not signature:
            return
        with self._lock:
            self._items[key] = (signature, time.time())
            self._items.move_to_end(key)
            while len(self._items) > _CACHE_MAX:
                self._items.popitem(last=False)

    def get(self, key: str) -> str | None:
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            sig, stamped = item
            if time.time() - stamped > _CACHE_TTL_S:
                del self._items[key]
                return None
            return sig


_CACHE = _SignatureCache()


def _fingerprint(name: Any, arguments: Any) -> str:
    raw = f"{name}\x00{arguments}".encode("utf-8", "replace")
    return "fp:" + hashlib.sha256(raw).hexdigest()[:24]


def _signature_of(tool_call: dict[str, Any]) -> str | None:
    extra = tool_call.get("extra_content")
    if isinstance(extra, dict):
        google = extra.get("google")
        if isinstance(google, dict) and isinstance(google.get("thought_signature"), str):
            return google["thought_signature"]
    return None


def _remember(tool_call: dict[str, Any]) -> None:
    sig = _signature_of(tool_call)
    if not sig:
        return
    fn = tool_call.get("function") or {}
    _CACHE.put(str(tool_call.get("id") or ""), sig)
    _CACHE.put(_fingerprint(fn.get("name"), fn.get("arguments")), sig)


def _scrub_schema(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _scrub_schema(v) for k, v in node.items() if k not in _SCHEMA_STRIP}
    if isinstance(node, list):
        return [_scrub_schema(v) for v in node]
    return node


class GeminiCompatProvider(OpenAICompatProvider):
    def __init__(self, spec: OpenAICompatSpec) -> None:
        super().__init__(spec)

    def _prepare_payload(self, request: ChatRequest, model_id: str, *, stream: bool) -> dict[str, Any]:
        payload = super()._prepare_payload(request, model_id, stream=stream)
        tools = payload.get("tools")
        if isinstance(tools, list):
            payload["tools"] = [_scrub_schema(t) for t in tools]
        for msg in payload.get("messages") or []:
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            calls = msg.get("tool_calls")
            if not isinstance(calls, list):
                continue
            for call in calls:
                if not isinstance(call, dict) or _signature_of(call):
                    continue
                fn = call.get("function") or {}
                sig = _CACHE.get(str(call.get("id") or "")) or _CACHE.get(
                    _fingerprint(fn.get("name"), fn.get("arguments"))
                )
                extra = call.setdefault("extra_content", {})
                if isinstance(extra, dict):
                    extra.setdefault("google", {})["thought_signature"] = sig or SKIP_SIGNATURE_SENTINEL
        return payload

    def _on_response(self, body: dict[str, Any]) -> None:
        for choice in body.get("choices") or []:
            msg = choice.get("message") if isinstance(choice, dict) else None
            for call in (msg or {}).get("tool_calls") or []:
                if isinstance(call, dict):
                    _remember(call)

    def _on_stream_event(self, obj: dict[str, Any]) -> None:
        for choice in obj.get("choices") or []:
            delta = choice.get("delta") if isinstance(choice, dict) else None
            for call in (delta or {}).get("tool_calls") or []:
                if isinstance(call, dict) and call.get("id"):
                    _remember(call)
