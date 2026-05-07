"""Tests for HuggingFaceProvider — uses pytest-httpx to mock outbound traffic."""

from __future__ import annotations

import pytest

from freeride.core.errors import ErrorKind
from freeride.providers.huggingface import (
    HF_CHAT_URL,
    HF_MODELS_URL,
    HuggingFaceProvider,
)


@pytest.fixture
def provider() -> HuggingFaceProvider:
    return HuggingFaceProvider()


# ---- catalog -------------------------------------------------------------


class TestListFreeModels:
    def test_returns_full_catalog(self, provider, httpx_mock):
        # HF doesn't have a per-model free flag — budget governs access.
        # We pass through everything the catalog returns.
        catalog = {
            "data": [
                {"id": "deepseek-ai/DeepSeek-R1", "context_length": 64_000},
                {"id": "meta-llama/Llama-3.3-70B-Instruct", "max_model_len": 8_192},
                {"id": "Qwen/Qwen2.5-Coder-32B-Instruct"},
            ]
        }
        httpx_mock.add_response(url=HF_MODELS_URL, json=catalog)
        models = provider.list_free_models("dummy")
        ids = {m.api_id for m in models}
        assert ids == {
            "deepseek-ai/DeepSeek-R1",
            "meta-llama/Llama-3.3-70B-Instruct",
            "Qwen/Qwen2.5-Coder-32B-Instruct",
        }

    def test_picks_up_context_length_from_catalog(self, provider, httpx_mock):
        catalog = {"data": [{"id": "x/y", "context_length": 32_000}]}
        httpx_mock.add_response(url=HF_MODELS_URL, json=catalog)
        models = provider.list_free_models("dummy")
        assert models[0].context_length == 32_000

    def test_default_context_length_when_missing(self, provider, httpx_mock):
        catalog = {"data": [{"id": "x/y"}]}
        httpx_mock.add_response(url=HF_MODELS_URL, json=catalog)
        models = provider.list_free_models("dummy")
        assert models[0].context_length == 8_192

    def test_dedupes_repeated_ids(self, provider, httpx_mock):
        catalog = {"data": [{"id": "dup"}, {"id": "dup"}]}
        httpx_mock.add_response(url=HF_MODELS_URL, json=catalog)
        models = provider.list_free_models("dummy")
        assert len(models) == 1


# ---- error classification ------------------------------------------------


class TestClassifyError:
    def test_402_is_quota_exhausted(self, provider, httpx_mock):
        # HF returns 402 Payment Required when the monthly free credit runs out.
        from httpx import Client

        httpx_mock.add_response(status_code=402, json={})
        with Client() as client:
            resp = client.get(HF_MODELS_URL)
        assert provider.classify_error(resp) is ErrorKind.QUOTA_EXHAUSTED

    def test_401_is_auth(self, provider, httpx_mock):
        from httpx import Client

        httpx_mock.add_response(status_code=401)
        with Client() as client:
            resp = client.get(HF_MODELS_URL)
        assert provider.classify_error(resp) is ErrorKind.AUTH

    def test_429_is_rate_limit(self, provider, httpx_mock):
        from httpx import Client

        httpx_mock.add_response(status_code=429)
        with Client() as client:
            resp = client.get(HF_MODELS_URL)
        assert provider.classify_error(resp) is ErrorKind.RATE_LIMIT

    def test_503_is_unavailable(self, provider, httpx_mock):
        from httpx import Client

        httpx_mock.add_response(status_code=503)
        with Client() as client:
            resp = client.get(HF_MODELS_URL)
        assert provider.classify_error(resp) is ErrorKind.UNAVAILABLE

    def test_credit_exhausted_message_in_400(self, provider, httpx_mock):
        from httpx import Client

        httpx_mock.add_response(
            status_code=400,
            json={"error": {"message": "Monthly credit exhausted"}},
        )
        with Client() as client:
            resp = client.get(HF_MODELS_URL)
        assert provider.classify_error(resp) is ErrorKind.QUOTA_EXHAUSTED

    def test_model_not_found_in_400(self, provider, httpx_mock):
        from httpx import Client

        httpx_mock.add_response(
            status_code=400,
            json={"error": {"message": "Model does not exist on HF"}},
        )
        with Client() as client:
            resp = client.get(HF_MODELS_URL)
        assert provider.classify_error(resp) is ErrorKind.MODEL_NOT_FOUND

    def test_timeout(self, provider):
        import httpx

        assert provider.classify_error(httpx.ReadTimeout("x")) is ErrorKind.TIMEOUT


# ---- retry-after --------------------------------------------------------


class TestRetryAfterHint:
    def test_parses_integer(self, provider, httpx_mock):
        from httpx import Client

        httpx_mock.add_response(status_code=429, headers={"retry-after": "12"})
        with Client() as client:
            resp = client.get(HF_MODELS_URL)
        assert provider.retry_after_hint(resp) == 12

    def test_returns_none_when_absent(self, provider, httpx_mock):
        from httpx import Client

        httpx_mock.add_response(status_code=429)
        with Client() as client:
            resp = client.get(HF_MODELS_URL)
        assert provider.retry_after_hint(resp) is None


# ---- headers -------------------------------------------------------------


class TestHeaders:
    def test_auth_header(self, provider):
        assert provider.auth_header("hf_xxx") == {"Authorization": "Bearer hf_xxx"}

    def test_attribution_headers_empty_when_no_bill_to(self, provider, monkeypatch):
        monkeypatch.delenv("HUGGINGFACE_BILL_TO", raising=False)
        assert provider.attribution_headers() == {}

    def test_bill_to_when_env_set(self, provider, monkeypatch):
        monkeypatch.setenv("HUGGINGFACE_BILL_TO", "myorg")
        assert provider.attribution_headers() == {"X-HF-Bill-To": "myorg"}
