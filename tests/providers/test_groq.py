"""Tests for GroqProvider — uses pytest-httpx to mock outbound traffic."""

from __future__ import annotations


import pytest

from freeride.core.errors import ErrorKind
from freeride.providers.groq import (
    GROQ_CHAT_URL,
    GROQ_MODELS_URL,
    GroqProvider,
)


@pytest.fixture
def provider() -> GroqProvider:
    return GroqProvider()


# ---- catalog + free-model intersection -----------------------------------


class TestListFreeModels:
    def test_intersects_allowlist(self, provider, httpx_mock):
        catalog = {
            "data": [
                {"id": "llama-3.1-8b-instant", "owned_by": "meta", "context_window": 131072},
                {"id": "llama-3.3-70b-versatile", "owned_by": "meta", "context_window": 131072},
                {"id": "mystery-paid-model", "owned_by": "groq", "context_window": 8192},
                {"id": "gemma2-9b-it", "owned_by": "google", "context_window": 8192},
            ]
        }
        httpx_mock.add_response(url=GROQ_MODELS_URL, json=catalog)
        models = provider.list_free_models("dummy")
        ids = {m.api_id for m in models}
        assert "llama-3.1-8b-instant" in ids
        assert "llama-3.3-70b-versatile" in ids
        assert "gemma2-9b-it" in ids
        assert "mystery-paid-model" not in ids

    def test_uses_catalog_context_when_larger_than_metadata(self, provider, httpx_mock):
        # If groq publishes a larger context_window than our sidecar knows
        # about, the catalog wins.
        catalog = {"data": [{"id": "llama-3.1-8b-instant", "context_window": 200_000}]}
        httpx_mock.add_response(url=GROQ_MODELS_URL, json=catalog)
        models = provider.list_free_models("k")
        assert models[0].context_length == 200_000

    def test_env_override_replaces_default_set(self, provider, httpx_mock, monkeypatch):
        monkeypatch.setenv("GROQ_FREE_MODELS_OVERRIDE", "only-this-model,and-this-one")
        catalog = {
            "data": [
                {"id": "llama-3.1-8b-instant"},
                {"id": "only-this-model"},
                {"id": "and-this-one"},
            ]
        }
        httpx_mock.add_response(url=GROQ_MODELS_URL, json=catalog)
        models = provider.list_free_models("k")
        assert {m.api_id for m in models} == {"only-this-model", "and-this-one"}

    def test_attribution_returns_empty_dict(self, provider):
        assert provider.attribution_headers() == {}

    def test_auth_header_bearer(self, provider):
        assert provider.auth_header("k") == {"Authorization": "Bearer k"}


# ---- error classification -------------------------------------------------


class TestClassifyError:
    def _resp(self, status, body=None, headers=None):
        from unittest.mock import MagicMock
        import httpx

        r = MagicMock(spec=httpx.Response)
        r.status_code = status
        if body is None:
            r.json.side_effect = ValueError("not json")
        else:
            r.json.return_value = body
        r.headers = headers or {}
        return r

    def test_200_ok(self, provider):
        assert provider.classify_error(self._resp(200)) is ErrorKind.OK

    def test_401_auth(self, provider):
        assert provider.classify_error(self._resp(401)) is ErrorKind.AUTH

    def test_429_rate_limit(self, provider):
        assert provider.classify_error(self._resp(429)) is ErrorKind.RATE_LIMIT

    def test_503_unavailable(self, provider):
        assert provider.classify_error(self._resp(503)) is ErrorKind.UNAVAILABLE

    def test_400_with_model_decommissioned_message(self, provider):
        # Groq decommissions models periodically; the error message
        # contains "decommissioned" — we map that to MODEL_NOT_FOUND so
        # the resolver advances rather than retrying.
        body = {"error": {"message": "The model `mixtral-8x7b-32768` has been decommissioned."}}
        assert provider.classify_error(self._resp(400, body=body)) is ErrorKind.MODEL_NOT_FOUND

    def test_404_with_model_not_found_code(self, provider):
        body = {"error": {"code": "model_not_found", "message": "..."}}
        assert provider.classify_error(self._resp(404, body=body)) is ErrorKind.MODEL_NOT_FOUND

    def test_400_with_unrelated_message_classifies_unknown(self, provider):
        body = {"error": {"message": "Bad request payload"}}
        assert provider.classify_error(self._resp(400, body=body)) is ErrorKind.UNKNOWN

    def test_httpx_timeout(self, provider):
        import httpx

        assert provider.classify_error(httpx.TimeoutException("t")) is ErrorKind.TIMEOUT

    def test_other_exception(self, provider):
        assert provider.classify_error(RuntimeError("?")) is ErrorKind.UNKNOWN


# ---- retry_after_hint -----------------------------------------------------


class TestRetryAfter:
    def test_parses_int_seconds(self, provider):
        class R:
            headers = {"retry-after": "12"}

        assert provider.retry_after_hint(R()) == 12

    def test_none_when_absent(self, provider):
        class R:
            headers = {}

        assert provider.retry_after_hint(R()) is None

    def test_unparseable_returns_none(self, provider):
        class R:
            headers = {"retry-after": "soon"}

        assert provider.retry_after_hint(R()) is None


# ---- probe ----------------------------------------------------------------


class TestProbe:
    def test_200_ok(self, provider, httpx_mock):
        httpx_mock.add_response(
            url=GROQ_CHAT_URL,
            json={
                "id": "x",
                "object": "chat.completion",
                "created": 1,
                "model": "llama-3.1-8b-instant",
                "choices": [],
            },
        )
        r = provider.probe("llama-3.1-8b-instant", "k")
        assert r.ok is True

    def test_429_classifies(self, provider, httpx_mock):
        httpx_mock.add_response(url=GROQ_CHAT_URL, status_code=429, json={"error": {}})
        r = provider.probe("llama-3.1-8b-instant", "k")
        assert r.error is ErrorKind.RATE_LIMIT


# ---- forward_chat scrubs x_groq ------------------------------------------


class TestForwardChat:
    @pytest.mark.asyncio
    async def test_strips_x_groq_extension(self, provider, httpx_mock):
        from freeride.core.chat_schema import ChatRequest

        # Groq sometimes returns x_groq alongside the OpenAI-shape body.
        httpx_mock.add_response(
            url=GROQ_CHAT_URL,
            json={
                "id": "chatcmpl-x",
                "object": "chat.completion",
                "created": 1,
                "model": "llama-3.1-8b-instant",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "x_groq": {"request_id": "abc"},  # Groq private extension
            },
        )
        req = ChatRequest(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": "hi"}],
        )
        resp = await provider.forward_chat(req, "llama-3.1-8b-instant", "k")
        # Body returned to gateway core must NOT carry the Groq extension
        # (would leak through to clients).
        dumped = resp.model_dump()
        assert "x_groq" not in dumped
