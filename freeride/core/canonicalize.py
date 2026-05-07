"""Canonical model name normalization.

Different providers expose the same logical model under different ids:

    OpenRouter:   meta-llama/llama-3.1-8b-instruct
    NVIDIA NIM:   meta/llama-3.1-8b-instruct
    Groq:         llama-3.1-8b-instant
    HuggingFace:  meta-llama/Llama-3.1-8B-Instruct
    CF Workers:   @cf/meta/llama-3.1-8b-instruct-fp8

This module defines :func:`canonicalize` to reduce these to a single
key (here: ``llama-3.1-8b-instruct``) so ``/v1/models`` can dedupe and
report all the provider-specific aliases under one logical entry.

The transformation is pure-function-y:

1. Lowercase
2. Strip vendor/scope prefixes (``@cf/``, ``meta/``, ``meta-llama/``,
   ``deepseek-ai/``, ``mistralai/``, ``Qwen/``, etc.)
3. Strip routing-policy suffixes (HF: ``:fastest``, ``:cheapest``,
   ``:<provider>``)
4. Strip quantization / variant suffixes (``-fp8``, ``-fp16``, ``-q4``,
   ``-int8``, ``-fp8-fast``)
5. Normalize Groq's release-train suffixes (``-instant``, ``-versatile``)
   to the underlying model: ``llama-3.1-8b-instant`` →
   ``llama-3.1-8b-instruct``

The result isn't intended to be a valid id for any specific provider;
it's a stable hash key for grouping aliases.
"""

from __future__ import annotations

import re


# Vendor / org prefixes that don't disambiguate the underlying model.
# Keep this list conservative — when a prefix DOES disambiguate (e.g.
# ``mistralai/codestral`` is different from ``codestral``), the prefix
# stays as part of the canonical key.
_VENDOR_PREFIXES = (
    "@cf/meta/",
    "@cf/google/",
    "@cf/qwen/",
    "@cf/mistralai/",
    "@cf/ibm-granite/",
    "@cf/microsoft/",
    "@cf/deepseek/",
    "@cf/",
    "meta-llama/",
    "meta/",
    "mistralai/",
    "deepseek-ai/",
    "deepseek/",
    "qwen/",
    "google/",
    "ibm/",
    "ibm-granite/",
    "microsoft/",
    "anthropic/",
    "openai/",
    "nousresearch/",
    "01-ai/",
)


# Quantization & variant suffixes — strip these so two providers serving
# the same model at different precisions still group together. The order
# matters: longer suffixes first so we strip the right amount.
_VARIANT_SUFFIXES = (
    "-fp8-fast",
    "-bf16-fast",
    "-fp8",
    "-fp16",
    "-bf16",
    "-int8",
    "-int4",
    "-q4",
    "-q5",
    "-q8",
    "-awq",
    "-gptq",
    "-gguf",
)


# Groq's release-train aliases. The "-instant" / "-versatile" / "-tool-use"
# suffixes are model id postfixes that specific Groq deployments use, but
# the underlying weights are an upstream model with an "-instruct" suffix
# (or no suffix). We rewrite them to the upstream form so canonicalize()
# groups them with the rest.
_GROQ_REWRITES = {
    "-instant": "-instruct",
    "-versatile": "-instruct",
    "-tool-use-preview": "-instruct",
}


# HF routing-policy suffixes — appended to the model id to pin upstream
# behavior. Stripping returns the underlying model.
_HF_ROUTING_RE = re.compile(r":[a-z][a-z0-9_-]*$", re.IGNORECASE)


def canonicalize(model_id: str) -> str:
    """Return a stable canonical key for ``model_id``.

    Idempotent: ``canonicalize(canonicalize(x)) == canonicalize(x)``.
    Empty input returns empty string.
    """
    if not model_id:
        return ""
    s = model_id.strip()
    if not s:
        return ""

    # 1. HF routing suffix BEFORE lowercasing — the suffix syntax is
    #    case-insensitive in practice and we strip case-insensitively too.
    s = _HF_ROUTING_RE.sub("", s)

    # 2. Lowercase. Provider catalogs disagree on casing for the same model.
    s = s.lower()

    # 3. Strip vendor prefixes. Iterate to handle stacked prefixes like
    #    ``@cf/meta/llama-3.1-8b`` where two prefixes apply.
    changed = True
    while changed:
        changed = False
        for prefix in _VENDOR_PREFIXES:
            if s.startswith(prefix):
                s = s[len(prefix):]
                changed = True
                break

    # 4. Groq release-train rewrites.
    for src, dst in _GROQ_REWRITES.items():
        if s.endswith(src):
            s = s[: -len(src)] + dst
            break

    # 5. Quantization / variant suffixes.
    for suffix in _VARIANT_SUFFIXES:
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            break

    return s
