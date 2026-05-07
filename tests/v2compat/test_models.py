"""Hermetic tests for v2compat.models — keys, ranking, fetch with mocked HTTP."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from freeride.core.cooldown import KeyCooldown
from freeride.providers.openrouter import OPENROUTER_MODELS_URL
from freeride.v2compat.models import (
    _parse_api_keys,
    calculate_model_score,
    fetch_all_models,
    get_api_keys,
    get_free_models,
    rank_free_models,
    save_models_cache,
    get_cached_models,
)


@pytest.fixture
def tmpdir() -> Path:
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


class TestParseApiKeys:
    def test_single_string(self):
        assert _parse_api_keys("sk-or-v1-abc") == ["sk-or-v1-abc"]

    def test_json_array_literal(self):
        assert _parse_api_keys('["a", "b", "c"]') == ["a", "b", "c"]

    def test_real_python_list(self):
        assert _parse_api_keys(["x", "y"]) == ["x", "y"]

    def test_strips_whitespace(self):
        assert _parse_api_keys("  sk-abc  ") == ["sk-abc"]
        assert _parse_api_keys('[" a ", "b"]') == ["a", "b"]

    def test_drops_empty_and_non_strings(self):
        assert _parse_api_keys(["x", "", "  "]) == ["x"]
        assert _parse_api_keys(["x", 42, None, "y"]) == ["x", "y"]

    def test_empty_returns_empty(self):
        assert _parse_api_keys("") == []
        assert _parse_api_keys(None) == []
        assert _parse_api_keys([]) == []

    def test_malformed_json_falls_through_to_single(self):
        # "[bad" is not valid JSON; treated as a single string key
        assert _parse_api_keys("[bad") == ["[bad"]


class TestCalculateModelScore:
    """Verbatim from v2 weights — ensures the parity gate doesn't drift."""

    def test_context_length_dominates(self):
        long_ctx = {"id": "x/m", "context_length": 1_000_000, "supported_parameters": []}
        short_ctx = {"id": "x/m", "context_length": 1_000, "supported_parameters": []}
        assert calculate_model_score(long_ctx) > calculate_model_score(short_ctx)

    def test_capabilities_count(self):
        many = {"id": "x/m", "context_length": 0, "supported_parameters": ["a"] * 10}
        few = {"id": "x/m", "context_length": 0, "supported_parameters": []}
        assert calculate_model_score(many) > calculate_model_score(few)

    def test_recency_bumps_score(self):
        recent = {"id": "x/m", "context_length": 0, "supported_parameters": [],
                  "created": int(time.time())}
        ancient = {"id": "x/m", "context_length": 0, "supported_parameters": [],
                   "created": int(time.time()) - 365 * 86400 * 5}
        assert calculate_model_score(recent) > calculate_model_score(ancient)

    def test_trusted_provider_bonus(self):
        trusted = {"id": "google/m", "context_length": 0, "supported_parameters": []}
        random = {"id": "rando/m", "context_length": 0, "supported_parameters": []}
        assert calculate_model_score(trusted) > calculate_model_score(random)


class TestRankFreeModels:
    def test_sorts_descending(self):
        # Construct three models with deliberately different scores
        models = [
            {"id": "a/m", "context_length": 1_000, "supported_parameters": []},
            {"id": "b/m", "context_length": 100_000, "supported_parameters": []},
            {"id": "c/m", "context_length": 1_000_000, "supported_parameters": []},
        ]
        ranked = rank_free_models(models)
        assert ranked[0]["id"] == "c/m"  # biggest context, highest score
        assert ranked[-1]["id"] == "a/m"  # smallest context, lowest score
        # _score is attached
        assert all("_score" in m for m in ranked)


class TestFetchAllModels:
    def test_uses_first_available_key(self, tmpdir, httpx_mock):
        # Two keys; first works
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": '["k1", "k2"]'}):
            httpx_mock.add_response(url=OPENROUTER_MODELS_URL, json={"data": [{"id": "m1"}]})
            cd = KeyCooldown(tmpdir / "cd.json")
            out = fetch_all_models(cooldown=cd)
            assert out == [{"id": "m1"}]
            # k1 NOT marked as rate-limited
            assert not cd.is_in_cooldown("openrouter", "k1")

    def test_rotates_on_429(self, tmpdir, httpx_mock):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": '["k1", "k2"]'}):
            # k1 -> 429, k2 -> 200
            httpx_mock.add_response(url=OPENROUTER_MODELS_URL, status_code=429,
                                    json={"error": {}})
            httpx_mock.add_response(url=OPENROUTER_MODELS_URL,
                                    json={"data": [{"id": "m2"}]})
            cd = KeyCooldown(tmpdir / "cd.json")
            out = fetch_all_models(cooldown=cd)
            assert out == [{"id": "m2"}]
            # k1 marked as cooling
            assert cd.is_in_cooldown("openrouter", "k1")
            assert not cd.is_in_cooldown("openrouter", "k2")

    def test_skips_keys_in_cooldown(self, tmpdir, httpx_mock):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": '["cooling", "fresh"]'}):
            httpx_mock.add_response(url=OPENROUTER_MODELS_URL,
                                    json={"data": [{"id": "m"}]})
            cd = KeyCooldown(tmpdir / "cd.json")
            cd.mark_rate_limited("openrouter", "cooling")
            out = fetch_all_models(cooldown=cd)
            assert out == [{"id": "m"}]
            # Only one HTTP call happened (fresh key)
            assert len(httpx_mock.get_requests()) == 1


class TestCache:
    def test_save_and_load(self, monkeypatch, tmpdir):
        cache_path = tmpdir / "cache.json"
        monkeypatch.setattr("freeride.v2compat.models.CACHE_FILE", cache_path)
        save_models_cache([{"id": "x", "_score": 1.0}])
        cached = get_cached_models()
        assert cached == [{"id": "x", "_score": 1.0}]

    def test_corrupted_cache_returns_none(self, monkeypatch, tmpdir):
        cache_path = tmpdir / "cache.json"
        cache_path.write_text("{not valid")
        monkeypatch.setattr("freeride.v2compat.models.CACHE_FILE", cache_path)
        assert get_cached_models() is None

    def test_missing_cache_returns_none(self, monkeypatch, tmpdir):
        monkeypatch.setattr("freeride.v2compat.models.CACHE_FILE", tmpdir / "absent")
        assert get_cached_models() is None
