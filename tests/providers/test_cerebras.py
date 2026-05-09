"""Tests for CerebrasProvider — uses pytest-httpx to mock outbound traffic."""

from __future__ import annotations

import pytest

from freeride.core.errors import ErrorKind
from freeride.providers.cerebras import (
    CEREBRAS_MODELS_URL,
    CerebrasProvider,
)


@pytest.fixture
def provider() -> CerebrasProvider:
    return CerebrasProvider()


# ---- catalog -------------------------------------------------------------


class TestListFreeModels:
    def test_returns_full_catalog_by_default(self, provider, httpx_mock):
        catalog = {
            "data": [
                {"id": "llama3.1-8b", "context_length": 128_000},
                {"id": "llama3.1-70b", "context_length": 128_000},
                {"id": "qwen-3-coder-480b", "context_length": 32_768},
            ]
        }
        httpx_mock.add_response(url=CEREBRAS_MODELS_URL, json=catalog)
        models = provider.list_free_models("dummy")
        ids = {m.api_id for m in models}
        assert ids == {"llama3.1-8b", "llama3.1-70b", "qwen-3-coder-480b"}

    def test_env_override_restricts_to_allowlist(self, provider, httpx_mock, monkeypatch):
        monkeypatch.setenv("CEREBRAS_FREE_MODELS_OVERRIDE", "llama3.1-8b,llama3.1-70b")
        catalog = {
            "data": [
                {"id": "llama3.1-8b"},
                {"id": "llama3.1-70b"},
                {"id": "expensive-flagship-model"},
            ]
        }
        httpx_mock.add_response(url=CEREBRAS_MODELS_URL, json=catalog)
        models = provider.list_free_models("dummy")
        ids = {m.api_id for m in models}
        assert ids == {"llama3.1-8b", "llama3.1-70b"}

    def test_dedupes_repeated_ids(self, provider, httpx_mock):
        httpx_mock.add_response(
            url=CEREBRAS_MODELS_URL,
            json={"data": [{"id": "llama3.1-8b"}, {"id": "llama3.1-8b"}]},
        )
        assert len(provider.list_free_models("dummy")) == 1

    def test_drops_known_broken_ids(self, provider, httpx_mock):
        # Cerebras's /models endpoint advertises ids that the inference
        # API itself rejects with model_not_found. These two were
        # confirmed via the 2026-05-09 audit and are pinned in
        # _CEREBRAS_KNOWN_BROKEN_IDS — make sure they never reach the
        # /v1/models response no matter how Cerebras lists them.
        catalog = {
            "data": [
                {"id": "llama3.1-8b", "context_length": 128_000},
                {"id": "qwen-3-235b-a22b-instruct-2507", "context_length": 64_000},
                {"id": "zai-glm-4.7", "context_length": 128_000},   # ghost
                {"id": "gpt-oss-120b", "context_length": 128_000},  # ghost
            ]
        }
        httpx_mock.add_response(url=CEREBRAS_MODELS_URL, json=catalog)
        ids = {m.api_id for m in provider.list_free_models("dummy")}
        assert ids == {"llama3.1-8b", "qwen-3-235b-a22b-instruct-2507"}
        assert "zai-glm-4.7" not in ids
        assert "gpt-oss-120b" not in ids


# ---- error classification ------------------------------------------------


class TestClassifyError:
    def test_401_is_auth(self, provider, httpx_mock):
        from httpx import Client

        httpx_mock.add_response(status_code=401)
        with Client() as client:
            resp = client.get(CEREBRAS_MODELS_URL)
        assert provider.classify_error(resp) is ErrorKind.AUTH

    def test_429_is_rate_limit(self, provider, httpx_mock):
        from httpx import Client

        httpx_mock.add_response(status_code=429)
        with Client() as client:
            resp = client.get(CEREBRAS_MODELS_URL)
        assert provider.classify_error(resp) is ErrorKind.RATE_LIMIT

    def test_500_is_unavailable(self, provider, httpx_mock):
        from httpx import Client

        httpx_mock.add_response(status_code=502)
        with Client() as client:
            resp = client.get(CEREBRAS_MODELS_URL)
        assert provider.classify_error(resp) is ErrorKind.UNAVAILABLE

    def test_model_not_found_via_code_field(self, provider, httpx_mock):
        from httpx import Client

        httpx_mock.add_response(
            status_code=400,
            json={"error": {"message": "no such model 'foo'", "code": "model_not_found"}},
        )
        with Client() as client:
            resp = client.get(CEREBRAS_MODELS_URL)
        assert provider.classify_error(resp) is ErrorKind.MODEL_NOT_FOUND

    def test_model_not_found_via_message(self, provider, httpx_mock):
        from httpx import Client

        httpx_mock.add_response(
            status_code=400,
            json={"error": {"message": "Model not found in catalog"}},
        )
        with Client() as client:
            resp = client.get(CEREBRAS_MODELS_URL)
        assert provider.classify_error(resp) is ErrorKind.MODEL_NOT_FOUND

    def test_quota_exhausted_pattern(self, provider, httpx_mock):
        from httpx import Client

        httpx_mock.add_response(
            status_code=400,
            json={"error": {"message": "monthly quota exceeded for free plan"}},
        )
        with Client() as client:
            resp = client.get(CEREBRAS_MODELS_URL)
        assert provider.classify_error(resp) is ErrorKind.QUOTA_EXHAUSTED

    def test_timeout(self, provider):
        import httpx

        assert provider.classify_error(httpx.ReadTimeout("x")) is ErrorKind.TIMEOUT


# ---- retry-after ----------------------------------------------------------


class TestRetryAfterHint:
    def test_parses_integer(self, provider, httpx_mock):
        from httpx import Client

        httpx_mock.add_response(status_code=429, headers={"retry-after": "12"})
        with Client() as client:
            resp = client.get(CEREBRAS_MODELS_URL)
        assert provider.retry_after_hint(resp) == 12

    def test_returns_none_when_absent(self, provider, httpx_mock):
        from httpx import Client

        httpx_mock.add_response(status_code=429)
        with Client() as client:
            resp = client.get(CEREBRAS_MODELS_URL)
        assert provider.retry_after_hint(resp) is None


# ---- headers --------------------------------------------------------------


class TestHeaders:
    def test_auth_header(self, provider):
        assert provider.auth_header("cb-test") == {"Authorization": "Bearer cb-test"}

    def test_attribution_headers_empty(self, provider):
        # Cerebras has no documented app-attribution header.
        assert provider.attribution_headers() == {}

    def test_embeddings_unsupported(self, provider):
        # Cerebras is chat-only; the embedding-capable filter must skip it.
        assert provider.embeddings_supported is False
