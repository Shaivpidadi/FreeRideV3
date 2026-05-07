"""Out-of-band metadata for Cloudflare Workers AI free-tier chat models.

CF Workers AI's free-tier semantics are unusual: there's a global
**10,000 Neurons/day** budget, not a per-model free flag. Different
models burn neurons at very different rates, so what's "free" depends
on how much of the day's budget remains AND which model the user picks.

The catalog endpoint at ``/ai/v1/models`` (OpenAI-compat surface)
exposes ``id`` only; richer metadata lives in CF's docs but isn't
queryable. We maintain the curated allowlist of cheap-enough chat
models here so list_free_models doesn't return paid-flagship-tier
options that'd burn through the daily budget in a few requests.

Refresh against https://developers.cloudflare.com/workers-ai/models/
when CF announces new free-eligible models.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CFWAIModelMeta:
    context_length: int
    supported_parameters: tuple[str, ...] = ()


# Curated subset emphasizing high-throughput-per-neuron models.
# CF docs publish per-model neuron costs; the cheapest text-out chat models
# are listed here. Update after each CF model release announcement.
CF_WAI_MODEL_METADATA: dict[str, CFWAIModelMeta] = {
    # IBM Granite — cheapest tier (~1.5K neurons / 1M input tokens)
    "@cf/ibm-granite/granite-3.0-1b-a400m-instruct": CFWAIModelMeta(context_length=8_192),
    "@cf/ibm-granite/granite-3.0-2b-instruct": CFWAIModelMeta(context_length=8_192),
    # Meta Llama 3 family
    "@cf/meta/llama-3.1-8b-instruct-fp8": CFWAIModelMeta(
        context_length=131_072, supported_parameters=("tools",)
    ),
    "@cf/meta/llama-3.2-1b-instruct": CFWAIModelMeta(context_length=131_072),
    "@cf/meta/llama-3.2-3b-instruct": CFWAIModelMeta(context_length=131_072),
    "@cf/meta/llama-3.3-70b-instruct-fp8-fast": CFWAIModelMeta(
        context_length=24_000, supported_parameters=("tools",)
    ),
    # Google Gemma
    "@cf/google/gemma-3-12b-it": CFWAIModelMeta(context_length=131_072),
    # Qwen
    "@cf/qwen/qwen2.5-coder-32b-instruct": CFWAIModelMeta(
        context_length=32_768, supported_parameters=("tools",)
    ),
    # Mistral
    "@cf/mistralai/mistral-small-3.1-24b-instruct": CFWAIModelMeta(
        context_length=131_072, supported_parameters=("tools",)
    ),
}


_DEFAULT_META = CFWAIModelMeta(context_length=8_192)


def lookup(model_id: str) -> CFWAIModelMeta:
    return CF_WAI_MODEL_METADATA.get(model_id, _DEFAULT_META)
