"""Tests for CloudflareWAIProvider — uses pytest-httpx to mock outbound traffic."""

from __future__ import annotations

import pytest

from freeride.core.errors import ErrorKind
from freeride.providers.cloudflare_wai import (
    CF_API_BASE_TEMPLATE,
    CloudflareWAIProvider,
    _free_model_set,
)


ACCOUNT_ID = "11111111111111111111111111111111"


def _base() -> str:
    return CF_API_BASE_TEMPLATE.format(account_id=ACCOUNT_ID)


@pytest.fixture
def provider() -> CloudflareWAIProvider:
    return CloudflareWAIProvider(account_id=ACCOUNT_ID)


# ---- construction --------------------------------------------------------


class TestConstruction:
    def test_requires_account_id(self, monkeypatch):
        monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
        with pytest.raises(ValueError, match="account_id"):
            CloudflareWAIProvider()

    def test_reads_account_id_from_env(self, monkeypatch):
        monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "from-env")
        p = CloudflareWAIProvider()
        assert "from-env" in p._chat_url


# ---- catalog -------------------------------------------------------------


class TestListFreeModels:
    def test_intersects_allowlist_and_attaches_metadata(self, provider, httpx_mock):
        # Mix of allowlisted + non-allowlisted ids. Only the allowlisted
        # ones should come back, with sidecar metadata applied.
        catalog = {
            "data": [
                {"id": "@cf/meta/llama-3.1-8b-instruct-fp8"},
                {"id": "@cf/some/paid-flagship-model"},
                {"id": "@cf/qwen/qwen2.5-coder-32b-instruct"},
            ]
        }
        httpx_mock.add_response(url=f"{_base()}/models", json=catalog)
        models = provider.list_free_models("dummy")
        ids = {m.api_id for m in models}
        assert "@cf/meta/llama-3.1-8b-instruct-fp8" in ids
        assert "@cf/qwen/qwen2.5-coder-32b-instruct" in ids
        assert "@cf/some/paid-flagship-model" not in ids
        # Llama 3.1 8b should pick up its 131K context from the sidecar.
        llama = next(m for m in models if m.api_id == "@cf/meta/llama-3.1-8b-instruct-fp8")
        assert llama.context_length == 131_072
        assert "tools" in llama.supported_parameters

    def test_handles_cf_envelope_shape(self, provider, httpx_mock):
        # CF sometimes wraps OpenAI-shape responses in a `result` envelope.
        body = {
            "result": {"data": [{"id": "@cf/meta/llama-3.2-1b-instruct"}]},
            "success": True,
        }
        httpx_mock.add_response(url=f"{_base()}/models", json=body)
        models = provider.list_free_models("dummy")
        assert len(models) == 1
        assert models[0].api_id == "@cf/meta/llama-3.2-1b-instruct"

    def test_env_override_replaces_default_set(self, provider, httpx_mock, monkeypatch):
        monkeypatch.setenv("CF_WAI_FREE_MODELS_OVERRIDE", "@cf/only-this,@cf/and-this")
        catalog = {
            "data": [
                {"id": "@cf/meta/llama-3.1-8b-instruct-fp8"},  # default-allowlisted, now excluded
                {"id": "@cf/only-this"},
                {"id": "@cf/and-this"},
            ]
        }
        httpx_mock.add_response(url=f"{_base()}/models", json=catalog)
        models = provider.list_free_models("dummy")
        ids = {m.api_id for m in models}
        assert ids == {"@cf/only-this", "@cf/and-this"}

    def test_free_model_set_default_is_nonempty(self):
        s = _free_model_set()
        assert len(s) > 0


# ---- error classification ------------------------------------------------


class TestClassifyError:
    def test_403_is_auth(self, provider, httpx_mock):
        # CF returns 403 for AI-permission missing; map to AUTH not RATE_LIMIT.
        from httpx import Client

        httpx_mock.add_response(status_code=403, json={"success": False})
        with Client() as client:
            resp = client.get(f"{_base()}/models")
        assert provider.classify_error(resp) is ErrorKind.AUTH

    def test_401_is_auth(self, provider, httpx_mock):
        from httpx import Client

        httpx_mock.add_response(status_code=401)
        with Client() as client:
            resp = client.get(f"{_base()}/models")
        assert provider.classify_error(resp) is ErrorKind.AUTH

    def test_429_is_rate_limit(self, provider, httpx_mock):
        from httpx import Client

        httpx_mock.add_response(status_code=429)
        with Client() as client:
            resp = client.get(f"{_base()}/models")
        assert provider.classify_error(resp) is ErrorKind.RATE_LIMIT

    def test_500_is_unavailable(self, provider, httpx_mock):
        from httpx import Client

        httpx_mock.add_response(status_code=502)
        with Client() as client:
            resp = client.get(f"{_base()}/models")
        assert provider.classify_error(resp) is ErrorKind.UNAVAILABLE

    def test_neuron_quota_message_in_envelope_is_quota_exhausted(self, provider, httpx_mock):
        from httpx import Client

        httpx_mock.add_response(
            status_code=400,
            json={
                "success": False,
                "errors": [{"message": "Daily neuron quota exhausted for free plan"}],
            },
        )
        with Client() as client:
            resp = client.get(f"{_base()}/models")
        assert provider.classify_error(resp) is ErrorKind.QUOTA_EXHAUSTED

    def test_model_not_found_in_envelope(self, provider, httpx_mock):
        from httpx import Client

        httpx_mock.add_response(
            status_code=400,
            json={"success": False, "errors": [{"message": "Model not found"}]},
        )
        with Client() as client:
            resp = client.get(f"{_base()}/models")
        assert provider.classify_error(resp) is ErrorKind.MODEL_NOT_FOUND

    def test_timeout_exception(self, provider):
        import httpx

        assert provider.classify_error(httpx.ReadTimeout("slow")) is ErrorKind.TIMEOUT

    def test_request_error_is_unavailable(self, provider):
        import httpx

        assert (
            provider.classify_error(httpx.ConnectError("boom")) is ErrorKind.UNAVAILABLE
        )


# ---- retry-after --------------------------------------------------------


class TestRetryAfterHint:
    def test_returns_none_when_absent(self, provider, httpx_mock):
        from httpx import Client

        httpx_mock.add_response(status_code=429)
        with Client() as client:
            resp = client.get(f"{_base()}/models")
        assert provider.retry_after_hint(resp) is None

    def test_parses_integer_seconds(self, provider, httpx_mock):
        from httpx import Client

        httpx_mock.add_response(status_code=429, headers={"retry-after": "37"})
        with Client() as client:
            resp = client.get(f"{_base()}/models")
        assert provider.retry_after_hint(resp) == 37

    def test_invalid_returns_none(self, provider, httpx_mock):
        from httpx import Client

        httpx_mock.add_response(status_code=429, headers={"retry-after": "soon"})
        with Client() as client:
            resp = client.get(f"{_base()}/models")
        assert provider.retry_after_hint(resp) is None


# ---- headers -------------------------------------------------------------


class TestHeaders:
    def test_auth_header(self, provider):
        h = provider.auth_header("test-key")
        assert h == {"Authorization": "Bearer test-key"}

    def test_attribution_headers_empty(self, provider):
        # CF doesn't provide an app-attribution slot.
        assert provider.attribution_headers() == {}
