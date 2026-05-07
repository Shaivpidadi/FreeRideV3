"""Out-of-band metadata for NVIDIA NIM models.

NIM's catalog endpoint (``/v1/models``) returns only ``id``,
``object``, ``created``, and ``owned_by`` per entry — no
``context_length``, ``output_modalities``, or ``supported_parameters``
fields, unlike OpenRouter. We maintain those values here so the
gateway can rank, dedupe, and expose them via :class:`Model`.

If a model isn't in this map, defaults are used (a small context
length, text-only modality, no special capabilities) — the live probe
catches genuinely unsupported models. Add an entry when a new model
becomes worth surfacing.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NimModelMeta:
    context_length: int
    supported_parameters: tuple[str, ...] = ()


# Curated subset of the NIM catalog that's known to work for free
# personal-use credits. Matches the allowlist in ``nvidia_nim.py`` —
# adding a new model here should also add the model id pattern to
# ``DEFAULT_FREE_MODEL_PREFIXES`` in nvidia_nim.py.
NIM_MODEL_METADATA: dict[str, NimModelMeta] = {
    # Llama 3.x family
    "meta/llama-3.1-8b-instruct": NimModelMeta(context_length=131_072, supported_parameters=("tools",)),
    "meta/llama-3.1-70b-instruct": NimModelMeta(context_length=131_072, supported_parameters=("tools",)),
    "meta/llama-3.1-405b-instruct": NimModelMeta(context_length=131_072, supported_parameters=("tools",)),
    "meta/llama-3.2-3b-instruct": NimModelMeta(context_length=131_072),
    "meta/llama-3.3-70b-instruct": NimModelMeta(context_length=131_072, supported_parameters=("tools",)),
    # DeepSeek family
    "deepseek-ai/deepseek-r1": NimModelMeta(context_length=131_072),
    "deepseek-ai/deepseek-v3": NimModelMeta(context_length=64_000),
    # Mistral family
    "mistralai/mistral-7b-instruct-v0.3": NimModelMeta(context_length=32_768),
    "mistralai/mixtral-8x7b-instruct-v0.1": NimModelMeta(context_length=32_768),
    "mistralai/mixtral-8x22b-instruct-v0.1": NimModelMeta(context_length=64_000),
    # Qwen family
    "qwen/qwen2.5-7b-instruct": NimModelMeta(context_length=32_768),
    "qwen/qwen2.5-coder-32b-instruct": NimModelMeta(context_length=128_000, supported_parameters=("tools",)),
    # Gemma family
    "google/gemma-3-27b-it": NimModelMeta(context_length=128_000),
}


_DEFAULT_META = NimModelMeta(context_length=8_192)


def lookup(model_id: str) -> NimModelMeta:
    """Return curated metadata or a sane default."""
    return NIM_MODEL_METADATA.get(model_id, _DEFAULT_META)
