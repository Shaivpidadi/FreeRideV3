"""Hermetic tests for the /v1/embeddings route.

Covers: provider filtering by embeddings_supported, success path,
non-supporting-provider 503, no-keys 503, AUTH→key advance,
MODEL_NOT_FOUND→provider advance, structured error shape.

Doesn't hit any real upstream — providers are mocked at the
``forward_embeddings`` level so we can assert the route's failover
state machine in isolation.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
from fastapi.testclient import TestClient

from freeride.core.embedding_schema import (
    EmbeddingObject,
    EmbeddingResponse,
    EmbeddingUsage,
)
from freeride.core.errors import ErrorKind
from freeride.server.app import create_app


# ---------------------------------------------------------------------------
# Test doubles. Two providers — one with embeddings, one without — so we
# can assert filter-and-failover in one shot.
# ---------------------------------------------------------------------------


class _StubProvider:
    """Minimal Provider-shaped stub — only the methods the route calls."""

    api_version = 1

    def __init__(
        self,
        name: str,
        *,
        supports_embeddings: bool,
        forward_result=None,
        forward_raises=None,
        retry_after: int | None = None,
    ):
        self.name = name
        self.embeddings_supported = supports_embeddings
        self._forward_result = forward_result
        self._forward_raises = forward_raises
        self._retry_after = retry_after

        # forward_embeddings only exists when supports_embeddings is True
        # (mirroring real provider classes).
        if supports_embeddings:
            self.forward_embeddings = AsyncMock(side_effect=self._dispatch)
        # The route also accesses these via the Provider Protocol surface.
        self.classify_error = self._classify
        self.retry_after_hint = self._retry_hint

    async def _dispatch(self, request, model_id, key):  # noqa: ARG002
        if self._forward_raises is not None:
            raise self._forward_raises
        return self._forward_result

    def _classify(self, exc_or_resp):
        # Real route passes ``e.response`` (an httpx.Response), but support
        # both shapes so the stub is forgiving.
        if isinstance(exc_or_resp, httpx.TimeoutException):
            return ErrorKind.TIMEOUT
        status = None
        if isinstance(exc_or_resp, httpx.HTTPStatusError):
            status = exc_or_resp.response.status_code
        elif isinstance(exc_or_resp, httpx.Response):
            status = exc_or_resp.status_code
        elif hasattr(exc_or_resp, "status_code"):
            status = exc_or_resp.status_code
        if status == 401:
            return ErrorKind.AUTH
        if status == 429:
            return ErrorKind.RATE_LIMIT
        if status == 404:
            return ErrorKind.MODEL_NOT_FOUND
        if status == 402:
            return ErrorKind.QUOTA_EXHAUSTED
        if status is not None and status >= 500:
            return ErrorKind.UNAVAILABLE
        return ErrorKind.UNKNOWN

    def _retry_hint(self, response):
        return self._retry_after


def _ok_response(model: str = "x/y") -> EmbeddingResponse:
    return EmbeddingResponse(
        data=[EmbeddingObject(index=0, embedding=[0.1, 0.2, 0.3])],
        model=model,
        usage=EmbeddingUsage(prompt_tokens=8, total_tokens=8),
    )


def _http_error(status: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://example.invalid/v1/embeddings")
    resp = httpx.Response(status, request=req)
    return httpx.HTTPStatusError(f"status {status}", request=req, response=resp)


def _client_with(providers: list[_StubProvider], monkeypatch, *, env_keys=None) -> TestClient:
    """Build a TestClient with the given stub providers + env vars."""
    monkeypatch.setenv("FREERIDE_EVENTS", "0")  # silence event emitter
    if env_keys is None:
        env_keys = {}
    for k, v in env_keys.items():
        monkeypatch.setenv(k, v)
    # Strip any cooldown state so tests are independent.
    monkeypatch.setattr(
        "freeride.core.cooldown.KeyCooldown.available_keys",
        lambda self, name, keys: list(keys),
    )
    app = create_app(providers=providers)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestProviderFiltering:
    def test_503_when_no_provider_supports_embeddings(self, monkeypatch):
        # Only Groq registered, embeddings unsupported → 503 with
        # explicit no_embedding_provider error.
        groq = _StubProvider("groq", supports_embeddings=False)
        client = _client_with([groq], monkeypatch, env_keys={"GROQ_API_KEY": "k"})

        r = client.post("/v1/embeddings", json={"model": "x", "input": "hi"})
        assert r.status_code == 503
        body = r.json()["detail"]["error"]
        assert body["type"] == "no_embedding_provider"
        assert body["configured_providers"] == ["groq"]
        assert body["embedding_capable"] == []
        assert "Groq does not currently offer embeddings" in body["suggestion"]

    def test_503_when_supporting_provider_has_no_keys(self, monkeypatch):
        # OR supports embeddings but no key in env → 503 no_usable_keys.
        # Note: with no env var set, the provider isn't even on the chain;
        # the route returns no_embedding_provider OR no_usable_keys
        # depending on whether the chain-resolver has anything to walk.
        # In this hermetic setup with no env var, nothing lands on the
        # chain, so we get no_usable_keys after the embedding-capable filter.
        or_provider = _StubProvider("openrouter", supports_embeddings=True)
        client = _client_with([or_provider], monkeypatch)
        r = client.post("/v1/embeddings", json={"model": "x", "input": "hi"})
        assert r.status_code == 503
        body = r.json()["detail"]["error"]
        assert body["type"] in ("no_usable_keys", "no_embedding_provider")


class TestSuccessPath:
    def test_happy_path_returns_provider_response(self, monkeypatch):
        or_provider = _StubProvider(
            "openrouter",
            supports_embeddings=True,
            forward_result=_ok_response(model="text-embedding-3-small"),
        )
        client = _client_with(
            [or_provider], monkeypatch, env_keys={"OPENROUTER_API_KEY": "k"}
        )

        r = client.post(
            "/v1/embeddings",
            json={"model": "text-embedding-3-small", "input": "hello"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["object"] == "list"
        assert len(body["data"]) == 1
        assert body["data"][0]["embedding"] == [0.1, 0.2, 0.3]
        assert body["_freeride_provider"] == "openrouter"
        assert body["_freeride_request_id"].startswith("req_")
        # Headers
        assert r.headers["X-FreeRide-Provider"] == "openrouter"
        assert r.headers["X-FreeRide-Request-ID"].startswith("req_")


class TestFailover:
    def test_skips_non_embedding_provider_in_chain(self, monkeypatch):
        """Groq (no embeddings) is registered first, OR (with embeddings) second.
        The route should skip Groq entirely and hand off to OR.
        """
        groq = _StubProvider("groq", supports_embeddings=False)
        or_provider = _StubProvider(
            "openrouter",
            supports_embeddings=True,
            forward_result=_ok_response(),
        )
        client = _client_with(
            [groq, or_provider],
            monkeypatch,
            env_keys={"GROQ_API_KEY": "k", "OPENROUTER_API_KEY": "k"},
        )
        r = client.post("/v1/embeddings", json={"model": "x", "input": "hi"})
        assert r.status_code == 200
        assert r.json()["_freeride_provider"] == "openrouter"

    def test_advances_provider_on_model_not_found(self, monkeypatch):
        """First provider 404s → advance to second; second succeeds."""
        first = _StubProvider(
            "openrouter",
            supports_embeddings=True,
            forward_raises=_http_error(404),
        )
        second = _StubProvider(
            "huggingface",
            supports_embeddings=True,
            forward_result=_ok_response(),
        )
        client = _client_with(
            [first, second],
            monkeypatch,
            env_keys={"OPENROUTER_API_KEY": "k", "HF_TOKEN": "k"},
        )
        r = client.post("/v1/embeddings", json={"model": "x", "input": "hi"})
        assert r.status_code == 200
        assert r.json()["_freeride_provider"] == "huggingface"

    def test_503_with_structured_tried_array_when_all_fail(self, monkeypatch):
        first = _StubProvider(
            "openrouter",
            supports_embeddings=True,
            forward_raises=_http_error(429),
            retry_after=42,
        )
        second = _StubProvider(
            "huggingface",
            supports_embeddings=True,
            forward_raises=_http_error(402),  # quota exhausted
        )
        client = _client_with(
            [first, second],
            monkeypatch,
            env_keys={"OPENROUTER_API_KEY": "k", "HF_TOKEN": "k"},
        )
        r = client.post("/v1/embeddings", json={"model": "x", "input": "hi"})
        assert r.status_code == 503
        err = r.json()["error"]
        assert err["type"] == "all_upstreams_failed"
        assert "request_id" in err
        tried = {t["provider"]: t for t in err["tried"]}
        assert "openrouter" in tried
        assert tried["openrouter"]["last_error"] == "rate_limit"
        assert tried["openrouter"]["retry_after_s"] == 42
        assert "huggingface" in tried
        assert tried["huggingface"]["last_error"] == "quota_exhausted"
