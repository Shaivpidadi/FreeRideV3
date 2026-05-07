"""Tests for freeride.core.canonicalize.

Each provider has its own way of writing "Llama 3.1 8B Instruct".
canonicalize() should reduce them all to the same key so ``/v1/models``
can collapse them into one logical entry.
"""

from __future__ import annotations

import pytest

from freeride.core.canonicalize import canonicalize


class TestCaseInsensitive:
    def test_lowercases(self):
        assert canonicalize("Meta-Llama/Llama-3.1-8B-Instruct") == "llama-3.1-8b-instruct"

    def test_preserves_already_lowercase(self):
        assert canonicalize("llama-3.1-8b-instruct") == "llama-3.1-8b-instruct"


class TestVendorPrefixes:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("meta-llama/Llama-3.1-8B-Instruct", "llama-3.1-8b-instruct"),
            ("meta/llama-3.1-8b-instruct", "llama-3.1-8b-instruct"),
            ("@cf/meta/llama-3.1-8b-instruct", "llama-3.1-8b-instruct"),
            ("@cf/qwen/qwen2.5-coder-32b-instruct", "qwen2.5-coder-32b-instruct"),
            ("deepseek-ai/DeepSeek-R1", "deepseek-r1"),
            ("Qwen/Qwen2.5-Coder-32B-Instruct", "qwen2.5-coder-32b-instruct"),
            ("mistralai/Mistral-Small-3.1-24B-Instruct", "mistral-small-3.1-24b-instruct"),
            ("google/gemma-3-12b-it", "gemma-3-12b-it"),
            ("ibm-granite/granite-3.0-2b-instruct", "granite-3.0-2b-instruct"),
        ],
    )
    def test_strips_vendor(self, raw, expected):
        assert canonicalize(raw) == expected


class TestQuantizationSuffixes:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("@cf/meta/llama-3.1-8b-instruct-fp8", "llama-3.1-8b-instruct"),
            ("@cf/meta/llama-3.3-70b-instruct-fp8-fast", "llama-3.3-70b-instruct"),
            ("llama-3.1-8b-instruct-int8", "llama-3.1-8b-instruct"),
            ("llama-3.1-8b-instruct-q4", "llama-3.1-8b-instruct"),
            ("llama-3.1-8b-instruct-awq", "llama-3.1-8b-instruct"),
        ],
    )
    def test_strips_quant(self, raw, expected):
        assert canonicalize(raw) == expected


class TestGroqReleaseTrain:
    """Groq publishes ``llama-3.1-8b-instant`` and ``-versatile`` instead
    of ``-instruct``. Canonicalize rewrites them to match the upstream id.
    """

    def test_instant_to_instruct(self):
        assert canonicalize("llama-3.1-8b-instant") == "llama-3.1-8b-instruct"

    def test_versatile_to_instruct(self):
        assert canonicalize("llama-3.3-70b-versatile") == "llama-3.3-70b-instruct"

    def test_tool_use_preview_to_instruct(self):
        assert canonicalize("llama-3.1-70b-tool-use-preview") == "llama-3.1-70b-instruct"


class TestHFRoutingSuffix:
    """HF appends :fastest / :cheapest / :preferred / :<provider> to model
    ids to pin upstream behavior. Strip those so the underlying model
    matches across providers.
    """

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("deepseek-ai/DeepSeek-R1:sambanova", "deepseek-r1"),
            ("deepseek-ai/DeepSeek-R1:fastest", "deepseek-r1"),
            ("meta-llama/Llama-3.3-70B-Instruct:cheapest", "llama-3.3-70b-instruct"),
            ("meta-llama/Llama-3.3-70B-Instruct:preferred", "llama-3.3-70b-instruct"),
        ],
    )
    def test_strips_routing(self, raw, expected):
        assert canonicalize(raw) == expected


class TestCrossProviderConvergence:
    """The load-bearing assertion: all five providers' representations of
    the same logical model must canonicalize to the same key.
    """

    def test_llama_3_1_8b(self):
        keys = {
            canonicalize("meta-llama/llama-3.1-8b-instruct"),  # OR
            canonicalize("meta/llama-3.1-8b-instruct"),  # NIM
            canonicalize("llama-3.1-8b-instant"),  # Groq
            canonicalize("meta-llama/Llama-3.1-8B-Instruct"),  # HF
            canonicalize("@cf/meta/llama-3.1-8b-instruct-fp8"),  # CF
        }
        assert keys == {"llama-3.1-8b-instruct"}, (
            f"expected single canonical key, got: {keys}"
        )


class TestEdgeCases:
    def test_empty_string(self):
        assert canonicalize("") == ""

    def test_whitespace(self):
        assert canonicalize("   ") == ""

    def test_idempotent(self):
        ids = [
            "meta-llama/Llama-3.1-8B-Instruct",
            "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
            "deepseek-ai/DeepSeek-R1:sambanova",
            "llama-3.1-8b-instant",
        ]
        for raw in ids:
            once = canonicalize(raw)
            twice = canonicalize(once)
            assert once == twice, f"not idempotent for {raw!r}: {once!r} → {twice!r}"

    def test_unknown_prefix_preserved(self):
        # Unknown vendor prefix should NOT be stripped — that would conflate
        # different models. We only strip prefixes we explicitly know about.
        assert canonicalize("acme-corp/secret-model-v1") == "acme-corp/secret-model-v1"
