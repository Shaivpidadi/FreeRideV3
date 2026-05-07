"""Tests for X-FreeRide-Force-Provider header and the
/v1/_freeride/providers diagnostic endpoint.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from freeride.core.chat_schema import ChatResponse
from freeride.core.embedding_schema import (
    EmbeddingObject,
    EmbeddingResponse,
    EmbeddingUsage,
)
from freeride.core.errors import ErrorKind
from freeride.core.health import ProviderHealth
from freeride.server.app import create_app


@pytest.fixture(autouse=True)
def _reset_health(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FREERIDE_EVENTS", "0")
    monkeypatch.setattr(
        "freeride.core.cooldown.KeyCooldown.available_keys",
        lambda self, name, keys: list(keys),
    )
    ProviderHealth.reset()
    yield
    ProviderHealth.reset()


class _StubProvider:
    api_version = 1

    def __init__(
        self,
        name: str,
        *,
        chat_result=None,
        embed_result=None,
        supports_embeddings: bool = True,
    ):
        self.name = name
        self.embeddings_supported = supports_embeddings
        self._chat = chat_result
        self._embed = embed_result
        self.forward_chat = AsyncMock(side_effect=self._do_chat)
        if supports_embeddings:
            self.forward_embeddings = AsyncMock(side_effect=self._do_embed)

    async def _do_chat(self, request, model_id, key):  # noqa: ARG002
        return self._chat

    async def _do_embed(self, request, model_id, key):  # noqa: ARG002
        return self._embed

    def classify_error(self, x):
        return ErrorKind.UNKNOWN

    def retry_after_hint(self, response):
        return None


def _ok_chat() -> ChatResponse:
    return ChatResponse.model_validate(
        {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 0,
            "model": "x/y",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "hi"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    )


def _ok_embed() -> EmbeddingResponse:
    return EmbeddingResponse(
        data=[EmbeddingObject(index=0, embedding=[0.1, 0.2])],
        model="x/y",
        usage=EmbeddingUsage(prompt_tokens=1, total_tokens=1),
    )


def _client_with(providers, monkeypatch, env_keys=None):
    if env_keys:
        for k, v in env_keys.items():
            monkeypatch.setenv(k, v)
    return TestClient(create_app(providers=providers))


# ---------------------------------------------------------------------------
# X-FreeRide-Force-Provider — chat
# ---------------------------------------------------------------------------


class TestForceProviderChat:
    def test_pins_to_named_provider(self, monkeypatch):
        first = _StubProvider("openrouter", chat_result=_ok_chat())
        second = _StubProvider("groq", chat_result=_ok_chat())
        client = _client_with(
            [first, second],
            monkeypatch,
            env_keys={"OPENROUTER_API_KEY": "k", "GROQ_API_KEY": "k"},
        )
        # Without the header, OR (registration order) wins.
        # With the header forcing groq, groq must serve.
        r = client.post(
            "/v1/chat/completions",
            json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
            headers={"X-FreeRide-Force-Provider": "groq"},
        )
        assert r.status_code == 200
        assert r.headers["X-FreeRide-Provider"] == "groq"
        # OR's forward_chat should NOT have been called.
        first.forward_chat.assert_not_awaited()
        second.forward_chat.assert_awaited_once()

    def test_400_when_unknown_name(self, monkeypatch):
        first = _StubProvider("openrouter", chat_result=_ok_chat())
        client = _client_with(
            [first], monkeypatch, env_keys={"OPENROUTER_API_KEY": "k"}
        )
        r = client.post(
            "/v1/chat/completions",
            json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
            headers={"X-FreeRide-Force-Provider": "imaginary"},
        )
        assert r.status_code == 400
        body = r.json()["detail"]["error"]
        assert body["type"] == "force_provider_unknown"
        assert "openrouter" in body["registered"]

    def test_no_header_uses_normal_chain(self, monkeypatch):
        first = _StubProvider("openrouter", chat_result=_ok_chat())
        second = _StubProvider("groq", chat_result=_ok_chat())
        client = _client_with(
            [first, second],
            monkeypatch,
            env_keys={"OPENROUTER_API_KEY": "k", "GROQ_API_KEY": "k"},
        )
        r = client.post(
            "/v1/chat/completions",
            json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 200
        # Registration order, no header → OR serves.
        assert r.headers["X-FreeRide-Provider"] == "openrouter"


# ---------------------------------------------------------------------------
# X-FreeRide-Force-Provider — embeddings
# ---------------------------------------------------------------------------


class TestForceProviderEmbeddings:
    def test_pins_to_named_provider(self, monkeypatch):
        first = _StubProvider("openrouter", embed_result=_ok_embed())
        second = _StubProvider("huggingface", embed_result=_ok_embed())
        client = _client_with(
            [first, second],
            monkeypatch,
            env_keys={"OPENROUTER_API_KEY": "k", "HF_TOKEN": "k"},
        )
        r = client.post(
            "/v1/embeddings",
            json={"model": "x", "input": "hi"},
            headers={"X-FreeRide-Force-Provider": "huggingface"},
        )
        assert r.status_code == 200
        assert r.headers["X-FreeRide-Provider"] == "huggingface"


# ---------------------------------------------------------------------------
# /v1/_freeride/providers diagnostic endpoint
# ---------------------------------------------------------------------------


class TestProvidersEndpoint:
    def test_lists_registered_with_default_stats(self, monkeypatch):
        first = _StubProvider("openrouter", supports_embeddings=True)
        second = _StubProvider("groq", supports_embeddings=False)
        client = _client_with([first, second], monkeypatch)
        r = client.get("/v1/_freeride/providers")
        assert r.status_code == 200
        data = r.json()
        provs = {p["name"]: p for p in data["providers"]}
        assert provs["openrouter"]["embeddings_supported"] is True
        assert provs["groq"]["embeddings_supported"] is False
        # Default neutral stats for everyone (no recorded attempts).
        for p in data["providers"]:
            assert p["n"] == 0
            assert p["score"] == 100.0

    def test_reflects_recorded_health(self, monkeypatch):
        h = ProviderHealth.instance()
        for _ in range(10):
            h.record("openrouter", ok=False, duration_ms=999)
        for _ in range(10):
            h.record("groq", ok=True, duration_ms=80)
        first = _StubProvider("openrouter")
        second = _StubProvider("groq")
        client = _client_with([first, second], monkeypatch)
        r = client.get("/v1/_freeride/providers")
        data = r.json()
        provs = {p["name"]: p for p in data["providers"]}
        # Failing provider should have a low score; healthy provider high.
        assert provs["openrouter"]["score"] < provs["groq"]["score"]
        assert provs["openrouter"]["success_rate"] == 0.0
        assert provs["groq"]["success_rate"] == 1.0
