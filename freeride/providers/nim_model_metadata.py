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
    # Live catalog on 2026-10-23; context lengths from NVIDIA's model
    # cards, tool support per family (the live probe is the authority).
    "nvidia/nemotron-3.5-lightning-30b-a3b": NimModelMeta(context_length=1_000_000, supported_parameters=("tools",)),
    "nvidia/nemotron-3.5-super-vl-120b-a12b": NimModelMeta(context_length=262_144, supported_parameters=("tools",)),
    "nvidia/nemotron-3-super-120b-a12b": NimModelMeta(context_length=262_144, supported_parameters=("tools",)),
    "nvidia/nemotron-3-ultra-550b-a55b": NimModelMeta(context_length=1_000_000, supported_parameters=("tools",)),
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning": NimModelMeta(context_length=262_144, supported_parameters=("tools",)),
    "nvidia/nemotron-nano-3-30b-a3b": NimModelMeta(context_length=262_144, supported_parameters=("tools",)),
    "nvidia/llama-3.1-nemotron-70b-instruct": NimModelMeta(context_length=131_072, supported_parameters=("tools",)),
    "nvidia/llama-3.1-nemotron-ultra-253b-v1": NimModelMeta(context_length=131_072, supported_parameters=("tools",)),
    "google/gemma-4-31b-it": NimModelMeta(context_length=131_072, supported_parameters=("tools",)),
    "openai/gpt-oss-20b": NimModelMeta(context_length=131_072, supported_parameters=("tools",)),
    "deepseek-ai/deepseek-v4.1-flash": NimModelMeta(context_length=131_072, supported_parameters=("tools",)),
    "z-ai/glm-5.3-flash": NimModelMeta(context_length=131_072, supported_parameters=("tools",)),
    "z-ai/glm-5.3": NimModelMeta(context_length=131_072, supported_parameters=("tools",)),
    "moonshotai/kimi-k2.6": NimModelMeta(context_length=262_144, supported_parameters=("tools",)),
    "minimaxai/minimax-m3.1-flash": NimModelMeta(context_length=262_144, supported_parameters=("tools",)),
    "mistralai/mistral-large-2-instruct": NimModelMeta(context_length=131_072, supported_parameters=("tools",)),
    "mistralai/mistral-7b-instruct-v0.3": NimModelMeta(context_length=32_768),
    "meta/llama-3.2-90b-vision-instruct": NimModelMeta(context_length=131_072, supported_parameters=("tools",)),
    "stepfun-ai/step-5-preview": NimModelMeta(context_length=131_072),
}


_DEFAULT_META = NimModelMeta(context_length=8_192)


def lookup(model_id: str) -> NimModelMeta:
    """Return curated metadata or a sane default."""
    return NIM_MODEL_METADATA.get(model_id, _DEFAULT_META)
