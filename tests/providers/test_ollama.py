"""Tests for OllamaProvider — uses pytest-httpx to mock the local daemon."""

from __future__ import annotations

import pytest

from freeride.core.errors import ErrorKind
from freeride.providers.ollama import (
    DEFAULT_OLLAMA_BASE_URL,
    OllamaProvider,
)


@pytest.fixture
def provider() -> OllamaProvider:
    return OllamaProvider()


# ---- catalog -------------------------------------------------------------


class TestListFreeModels:
    def test_returns_local_catalog(self, provider, httpx_mock):
        catalog = {
            "data": [
                {"id": "llama3.1:8b"},
                {"id": "qwen2.5-coder:32b"},
                {"id": "nomic-embed-text"},
            ]
        }
        httpx_mock.add_response(url=f"{DEFAULT_OLLAMA_BASE_URL}/v1/models", json=catalog)
        models = provider.list_free_models("any")
        ids = {m.api_id for m in models}
        assert ids == {"llama3.1:8b", "qwen2.5-coder:32b", "nomic-embed-text"}
        # All marked as ollama-owned.
        assert all(m.provider == "ollama" for m in models)
        # Default context length when not exposed.
        assert all(m.context_length == 8_192 for m in models)

    def test_dedupes_repeated_ids(self, provider, httpx_mock):
        httpx_mock.add_response(
            url=f"{DEFAULT_OLLAMA_BASE_URL}/v1/models",
            json={"data": [{"id": "x"}, {"id": "x"}]},
        )
        assert len(provider.list_free_models("any")) == 1

    def test_resolves_base_url_from_key_argument(self, httpx_mock):
        """When the chain passes a non-default URL as the 'key', the
        provider should hit THAT URL instead of the constructor default.
        Lets one process target multiple Ollama hosts.
        """
        provider = OllamaProvider()
        alt_url = "http://other-host:11434"
        httpx_mock.add_response(url=f"{alt_url}/v1/models", json={"data": []})
        provider.list_free_models(alt_url)  # should hit alt_url, not localhost


# ---- error classification ------------------------------------------------


class TestClassifyError:
    def test_connect_error_is_unavailable(self, provider):
        import httpx

        # Ollama isn't running locally — the connect-refused case.
        assert (
            provider.classify_error(httpx.ConnectError("Connection refused"))
            is ErrorKind.UNAVAILABLE
        )

    def test_timeout_is_timeout(self, provider):
        import httpx

        assert provider.classify_error(httpx.ReadTimeout("slow")) is ErrorKind.TIMEOUT

    def test_404_with_pull_message_is_model_not_found(self, provider, httpx_mock):
        from httpx import Client

        httpx_mock.add_response(
            status_code=404,
            json={"error": {"message": "model 'foo' not found, try pulling it first"}},
        )
        with Client() as client:
            resp = client.get(f"{DEFAULT_OLLAMA_BASE_URL}/v1/models")
        assert provider.classify_error(resp) is ErrorKind.MODEL_NOT_FOUND

    def test_500_is_unavailable(self, provider, httpx_mock):
        from httpx import Client

        httpx_mock.add_response(status_code=502)
        with Client() as client:
            resp = client.get(f"{DEFAULT_OLLAMA_BASE_URL}/v1/models")
        assert provider.classify_error(resp) is ErrorKind.UNAVAILABLE


# ---- headers --------------------------------------------------------------


class TestHeaders:
    def test_auth_header_empty(self, provider):
        # Ollama is local and unauthenticated — auth_header is {}.
        assert provider.auth_header("anything") == {}

    def test_attribution_headers_empty(self, provider):
        assert provider.attribution_headers() == {}


# ---- retry-after ----------------------------------------------------------


class TestRetryAfterHint:
    def test_always_returns_none(self, provider, httpx_mock):
        from httpx import Client

        httpx_mock.add_response(status_code=429, headers={"retry-after": "60"})
        with Client() as client:
            resp = client.get(f"{DEFAULT_OLLAMA_BASE_URL}/v1/models")
        # Ollama doesn't issue retry-after; we ignore the header even
        # if present.
        assert provider.retry_after_hint(resp) is None
