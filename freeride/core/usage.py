"""Provider-format-aware usage extraction.

Every freeride request ends with either an aggregated response object
(non-streaming) or a final SSE chunk (streaming). Both carry token
usage but the field names differ by upstream API family:

  - **OpenAI-compatible** (OpenRouter, Groq, NVIDIA, Cerebras, HF
    Inference, Cloudflare Workers AI, Ollama): non-streaming responses
    have ``usage.prompt_tokens`` + ``usage.completion_tokens``. Streams
    emit a final chunk with the same shape ONLY when the caller
    requested it via ``stream_options.include_usage=true``.
  - **Anthropic** (/v1/messages): non-streaming responses have
    ``usage.input_tokens`` + ``usage.output_tokens``. Streams emit a
    ``message_delta`` event carrying the same fields.
  - **Gemini** (generateContent / streamGenerateContent): responses
    carry ``usageMetadata.promptTokenCount`` + ``candidatesTokenCount``.
    Streams emit it on the final chunk.

``extract_usage`` normalizes all three into ``Usage(input, output)``.
Callers don't have to remember which adapter to use for which route;
they pass the ``Kind`` enum value and an event/response dict and get
back a single shape. The result feeds straight into
``telemetry.record_request(input_tokens=..., output_tokens=...)``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class Kind(str, Enum):
    """The upstream API family the bytes came from. Matches the
    ``Provider.api_family`` value for forwarding providers and the
    ``RouteKind`` for the inbound side."""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"


@dataclass(frozen=True)
class Usage:
    """Token usage normalized across provider families.

    Both values default to 0 — never negative. ``total`` is computed
    so callers can treat it like a value-object without re-summing.
    """

    input: int = 0
    output: int = 0

    @property
    def total(self) -> int:
        return self.input + self.output

    @property
    def has_any(self) -> bool:
        return self.input > 0 or self.output > 0


def extract_usage(kind: Kind | str, body: Any) -> Usage:
    """Parse usage out of one response or stream chunk.

    ``body`` is typically a dict (a parsed JSON response or a parsed
    SSE event payload). Strings and bytes are accepted as a convenience
    when the caller has the raw JSON; we parse them. Anything else
    yields an empty ``Usage(0, 0)``.

    Never raises — bad shapes return ``Usage()`` so route code can call
    this on every chunk without try/except noise.
    """
    if isinstance(body, (bytes, bytearray)):
        try:
            body = body.decode("utf-8")
        except UnicodeDecodeError:
            return Usage()
    if isinstance(body, str):
        import json

        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            return Usage()
    if not isinstance(body, dict):
        return Usage()

    if isinstance(kind, Kind):
        kind_val = kind.value
    else:
        kind_val = str(kind).lower()

    if kind_val == Kind.OPENAI.value:
        return _from_openai(body)
    if kind_val == Kind.ANTHROPIC.value:
        return _from_anthropic(body)
    if kind_val == Kind.GEMINI.value:
        return _from_gemini(body)

    # Unknown family: try every shape in order so a misconfigured route
    # still surfaces *some* usage rather than silently dropping it.
    for fn in (_from_openai, _from_anthropic, _from_gemini):
        u = fn(body)
        if u.has_any:
            return u
    return Usage()


# ─── per-family adapters ────────────────────────────────────────────


def _from_openai(body: dict) -> Usage:
    """OpenAI-compatible: ``usage.prompt_tokens`` / ``completion_tokens``.

    Both the non-streaming ``ChatResponse`` and the final chunk of a
    ``stream_options.include_usage=true`` stream share this shape.
    NVIDIA NIM is a slight variation: it ships its final ``usage`` on
    the *penultimate* event with empty ``choices: []``, then a
    ``[DONE]`` sentinel. The penultimate event still hits this code
    path because the dict-with-usage shape is identical."""
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return Usage()
    return Usage(
        input=_int(usage.get("prompt_tokens")),
        output=_int(usage.get("completion_tokens")),
    )


def _from_anthropic(body: dict) -> Usage:
    """Anthropic: ``usage.input_tokens`` / ``usage.output_tokens``.

    Lives on the top-level response object and on ``message_delta``
    events mid-stream. Anthropic also reports ``cache_creation_input_tokens``
    and ``cache_read_input_tokens`` for prompt caching; those are
    accounted for in the prompt-side budget and we sum them into
    ``input`` so cache-heavy workloads aren't undercounted."""
    usage = body.get("usage")
    if not isinstance(usage, dict):
        # Some stream events carry usage at the top of the delta block.
        delta = body.get("delta")
        if isinstance(delta, dict):
            usage = delta.get("usage")
        if not isinstance(usage, dict):
            return Usage()
    input_t = _int(usage.get("input_tokens"))
    input_t += _int(usage.get("cache_creation_input_tokens"))
    input_t += _int(usage.get("cache_read_input_tokens"))
    return Usage(
        input=input_t,
        output=_int(usage.get("output_tokens")),
    )


def _from_gemini(body: dict) -> Usage:
    """Gemini: ``usageMetadata.promptTokenCount`` /
    ``candidatesTokenCount``. Same field names on the final stream
    chunk. ``totalTokenCount`` is reported but we recompute from the
    pair so cache cases don't double-count it."""
    meta = body.get("usageMetadata") or body.get("usage_metadata")
    if not isinstance(meta, dict):
        return Usage()
    return Usage(
        input=_int(
            meta.get("promptTokenCount") or meta.get("prompt_token_count"),
        ),
        output=_int(
            meta.get("candidatesTokenCount")
            or meta.get("candidates_token_count"),
        ),
    )


def _int(v: Any) -> int:
    """Defensive int coercion that floors negatives. Some providers
    occasionally serialize counts as strings or floats; we accept both
    and clamp to ≥ 0 so a bad chunk can't poison the running totals."""
    if v is None:
        return 0
    try:
        n = int(v)
    except (TypeError, ValueError):
        try:
            n = int(float(v))
        except (TypeError, ValueError):
            return 0
    return n if n > 0 else 0
