"""Out-of-band metadata for Groq's free-tier chat models.

Groq's catalog (`GET /openai/v1/models`) exposes ``id``, ``owned_by``,
and ``context_window`` but not a "free-tier" flag — what's free depends
on the user's plan and is rate-limited per-model. We maintain an
explicit allowlist here, refreshed against Groq's published rate-limits
page (https://console.groq.com/docs/rate-limits).

If a model isn't in this map, defaults are used: a small context length
and no special capabilities. The live probe catches genuinely
unsupported models. Add an entry when a new Groq free-tier model
becomes worth surfacing.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GroqModelMeta:
    context_length: int
    supported_parameters: tuple[str, ...] = ()


GROQ_MODEL_METADATA: dict[str, GroqModelMeta] = {
    # Current free-tier chat surface (verified live 2026-08-21 against
    # GET /openai/v1/models). Groq retires Llama 3.x ids periodically.
    "openai/gpt-oss-20b": GroqModelMeta(
        context_length=131_072, supported_parameters=("tools",)
    ),
    "openai/gpt-oss-120b": GroqModelMeta(
        context_length=131_072, supported_parameters=("tools",)
    ),
    "qwen/qwen3.6-27b": GroqModelMeta(context_length=131_072),
    "allam-2-7b": GroqModelMeta(context_length=8_192),
    "groq/compound": GroqModelMeta(context_length=131_072),
    "groq/compound-mini": GroqModelMeta(context_length=131_072),
    # Historical Llama / Gemma / Mixtral ids — kept so mocked unit tests
    # and older GROQ_FREE_MODELS_OVERRIDE values still intersect. Harmless
    # if Groq no longer lists them.
    "llama-3.1-8b-instant": GroqModelMeta(
        context_length=131_072, supported_parameters=("tools",)
    ),
    "llama-3.3-70b-versatile": GroqModelMeta(
        context_length=131_072, supported_parameters=("tools",)
    ),
    "llama3-8b-8192": GroqModelMeta(context_length=8_192),
    "llama3-70b-8192": GroqModelMeta(context_length=8_192),
    "llama-3.2-1b-preview": GroqModelMeta(context_length=131_072),
    "llama-3.2-3b-preview": GroqModelMeta(context_length=131_072),
    "gemma2-9b-it": GroqModelMeta(context_length=8_192),
    "mixtral-8x7b-32768": GroqModelMeta(context_length=32_768),
    "deepseek-r1-distill-llama-70b": GroqModelMeta(context_length=131_072),
}


_DEFAULT_META = GroqModelMeta(context_length=8_192)


def lookup(model_id: str) -> GroqModelMeta:
    return GROQ_MODEL_METADATA.get(model_id, _DEFAULT_META)
