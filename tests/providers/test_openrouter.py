"""Tests for OpenRouterProvider — uses pytest-httpx to mock outbound traffic.

These tests are hermetic: no real OpenRouter calls. Live-API checks live in
``tests/parity/`` (added in Phase 1.8) and require ``OPENROUTER_API_KEY``.
"""

from __future__ import annotations

import json

import pytest

from freeride.core.errors import ErrorKind
from freeride.core.types import ProbeResult
from freeride.providers.openrouter import (
    OPENROUTER_CHAT_URL,
    OPENROUTER_MODELS_URL,
    OpenRouterProvider,
    filter_free_chat_models,
    is_chat_model,
    is_free_model,
)


@pytest.fixture
def provider() -> OpenRouterProvider:
    return OpenRouterProvider()


# ---- pure helpers ----------------------------------------------------------


class TestFreeDetection:
    def test_pricing_zero_signal(self):
        assert is_free_model({"id": "x/m", "pricing": {"prompt": "0"}})
        assert is_free_model({"id": "x/m", "pricing": {"prompt": 0}})
        assert not is_free_model({"id": "x/m", "pricing": {"prompt": "0.000005"}})

    def test_free_suffix_signal(self):
        assert is_free_model({"id": "x/m:free", "pricing": {}})
        assert not is_free_model({"id": "x/m", "pricing": {}})

    def test_dual_signal_either_wins(self):
        # pricing missing but suffix present
        assert is_free_model({"id": "x/m:free"})
        # pricing zero, no suffix
        assert is_free_model({"id": "x/m", "pricing": {"prompt": 0}})

    def test_pricing_garbage_falls_through_to_suffix(self):
        # Bad pricing string shouldn't crash; falls through to suffix check
        assert is_free_model({"id": "x/m:free", "pricing": {"prompt": "n/a"}})
        assert not is_free_model({"id": "x/m", "pricing": {"prompt": "n/a"}})


class TestChatShape:
    def test_explicit_text_only(self):
        assert is_chat_model({"architecture": {"output_modalities": ["text"]}})

    def test_explicit_image_output_filtered(self):
        assert not is_chat_model({"architecture": {"output_modalities": ["image"]}})
        assert not is_chat_model({"architecture": {"output_modalities": ["text", "image"]}})

    def test_modality_string_parsed(self):
        # "text+image->text" — text-only output, keep
        assert is_chat_model({"architecture": {"modality": "text+image->text"}})
        # "text->text+audio" — multimodal output, filter out
        assert not is_chat_model({"architecture": {"modality": "text->text+audio"}})

    def test_unknown_shape_kept(self):
        # Empty architecture means we don't know; let probe sort it
        assert is_chat_model({})
        assert is_chat_model({"architecture": {}})


class TestFilterFreeChatModels:
    def test_filters_and_dedupes(self):
        m_free = {"id": "qwen/qwen3:free", "architecture": {"output_modalities": ["text"]}}
        m_paid = {"id": "anthropic/opus", "architecture": {"output_modalities": ["text"]},
                  "pricing": {"prompt": "0.000015"}}
        m_image = {"id": "lyria:free", "architecture": {"modality": "text->image"}}
        m_dup = dict(m_free)
        out = filter_free_chat_models([m_free, m_paid, m_image, m_dup])
        assert [m["id"] for m in out] == ["qwen/qwen3:free"]


# ---- list_free_models ------------------------------------------------------


class TestListFreeModels:
    def test_parses_and_filters(self, provider, httpx_mock):
        catalog = {
            "data": [
                {
                    "id": "qwen/qwen3:free",
                    "architecture": {"output_modalities": ["text"]},
                    "context_length": 131072,
                    "pricing": {"prompt": "0"},
                    "supported_parameters": ["tools", "vision"],
                },
                {
                    "id": "anthropic/opus",
                    "architecture": {"output_modalities": ["text"]},
                    "context_length": 200000,
                    "pricing": {"prompt": "0.000015"},
                    "supported_parameters": ["tools"],
                },
                {
                    "id": "openrouter/free",
                    "architecture": {"output_modalities": ["text"]},
                    "context_length": 65536,
                    "pricing": {"prompt": "0"},
                    "supported_parameters": [],
                },
            ]
        }
        httpx_mock.add_response(url=OPENROUTER_MODELS_URL, json=catalog)
        models = provider.list_free_models("dummy-key")
        ids = [m.api_id for m in models]
        assert ids == ["qwen/qwen3:free", "openrouter/free"]
        m = models[0]
        assert m.provider == "openrouter"
        assert m.context_length == 131072
        assert m.supported_parameters == ("tools", "vision")
        assert m.output_modalities == ("text",)
        assert m.raw["id"] == "qwen/qwen3:free"

    def test_raises_on_unauthorized(self, provider, httpx_mock):
        httpx_mock.add_response(url=OPENROUTER_MODELS_URL, status_code=401, json={"error": "no"})
        with pytest.raises(Exception):
            provider.list_free_models("bad-key")

    def test_attribution_headers_sent(self, provider, httpx_mock):
        httpx_mock.add_response(url=OPENROUTER_MODELS_URL, json={"data": []})
        provider.list_free_models("k")
        req = httpx_mock.get_request()
        assert req is not None
        assert req.headers.get("authorization") == "Bearer k"
        assert req.headers.get("http-referer") == "https://github.com/Shaivpidadi/FreeRideV3"
        assert req.headers.get("x-title") == "FreeRide Gateway"
        # Marketplace categories so we surface in OR's category leaderboards
        # (/apps/category/cli-agent, etc.). See OPENROUTER_CATEGORIES.
        assert req.headers.get("x-openrouter-categories") == (
            "cli-agent,personal-agent,programming-app"
        )


# ---- probe -----------------------------------------------------------------


class TestProbe:
    def test_200_is_ok(self, provider, httpx_mock):
        httpx_mock.add_response(
            url=OPENROUTER_CHAT_URL,
            json={"id": "x", "object": "chat.completion", "created": 1, "model": "m",
                  "choices": [], "usage": {}},
        )
        r: ProbeResult = provider.probe("openrouter/free", "k")
        assert r.ok is True
        assert r.error is None
        assert r.latency_ms >= 0

    def test_401_classifies_as_auth(self, provider, httpx_mock):
        httpx_mock.add_response(url=OPENROUTER_CHAT_URL, status_code=401, json={"error": {}})
        r = provider.probe("m", "bad")
        assert r.ok is False
        assert r.error is ErrorKind.AUTH

    def test_429_classifies_as_rate_limit(self, provider, httpx_mock):
        httpx_mock.add_response(url=OPENROUTER_CHAT_URL, status_code=429, json={"error": {}})
        r = provider.probe("m", "k")
        assert r.error is ErrorKind.RATE_LIMIT

    def test_503_classifies_as_unavailable(self, provider, httpx_mock):
        httpx_mock.add_response(url=OPENROUTER_CHAT_URL, status_code=503, json={"error": {}})
        r = provider.probe("m", "k")
        assert r.error is ErrorKind.UNAVAILABLE

    def test_400_unknown_model_id_classifies_as_model_not_found(
        self, provider, httpx_mock
    ):
        # Regression test for the bug discovered live: openrouter returns 400
        # with message "<id> is not a valid model ID" for typos.
        httpx_mock.add_response(
            url=OPENROUTER_CHAT_URL,
            status_code=400,
            json={"error": {"message": "nonexistent/model:free is not a valid model ID",
                            "code": 400}},
        )
        r = provider.probe("nonexistent/model:free", "k")
        assert r.error is ErrorKind.MODEL_NOT_FOUND

    def test_404_unknown_model_legacy_message(self, provider, httpx_mock):
        # Older shape: "Unknown model: <id>"
        httpx_mock.add_response(
            url=OPENROUTER_CHAT_URL,
            status_code=404,
            json={"error": {"message": "Unknown model: foo", "code": 404}},
        )
        r = provider.probe("foo", "k")
        assert r.error is ErrorKind.MODEL_NOT_FOUND

    def test_400_with_explicit_model_not_found_code(self, provider, httpx_mock):
        httpx_mock.add_response(
            url=OPENROUTER_CHAT_URL,
            status_code=400,
            json={"error": {"code": "model_not_found", "message": "..."}},
        )
        r = provider.probe("m", "k")
        assert r.error is ErrorKind.MODEL_NOT_FOUND

    def test_400_other_classifies_as_unknown(self, provider, httpx_mock):
        httpx_mock.add_response(
            url=OPENROUTER_CHAT_URL,
            status_code=400,
            json={"error": {"message": "Bad request", "code": 400}},
        )
        r = provider.probe("m", "k")
        assert r.error is ErrorKind.UNKNOWN

    def test_500_classifies_as_unavailable(self, provider, httpx_mock):
        httpx_mock.add_response(url=OPENROUTER_CHAT_URL, status_code=500, json={"error": {}})
        r = provider.probe("m", "k")
        assert r.error is ErrorKind.UNAVAILABLE

    def test_probe_payload_shape(self, provider, httpx_mock):
        httpx_mock.add_response(
            url=OPENROUTER_CHAT_URL,
            json={"id": "x", "object": "chat.completion", "created": 1, "model": "m",
                  "choices": []},
        )
        provider.probe("foo:free", "k")
        req = httpx_mock.get_request()
        body = json.loads(req.read())
        assert body["model"] == "foo:free"
        assert body["max_tokens"] == 5
        assert body["stream"] is False
        assert body["messages"] == [{"role": "user", "content": "Hi"}]


# ---- retry_after_hint ------------------------------------------------------


class TestRetryAfterHint:
    def test_parses_int(self, provider, httpx_mock):
        httpx_mock.add_response(url=OPENROUTER_CHAT_URL, status_code=429, headers={"retry-after": "12"})
        r = provider.probe("m", "k")
        # We don't expose the response from probe; this exercises classify
        # and the helper indirectly. The unit-style test below covers the
        # helper directly.
        assert r.error is ErrorKind.RATE_LIMIT

    def test_helper_with_none(self, provider):
        assert provider.retry_after_hint(None) is None

    def test_helper_with_no_header(self, provider):
        class FakeResp:
            headers = {}

        assert provider.retry_after_hint(FakeResp()) is None

    def test_helper_with_unparseable(self, provider):
        class FakeResp:
            headers = {"retry-after": "tomorrow"}

        assert provider.retry_after_hint(FakeResp()) is None

    def test_helper_with_int_string(self, provider):
        class FakeResp:
            headers = {"retry-after": "30"}

        assert provider.retry_after_hint(FakeResp()) == 30


# ---- forward_chat / forward_chat_stream (mocked round-trips) -------------


class TestForwardChat:
    @pytest.mark.asyncio
    async def test_forward_chat_passes_through(self, provider, httpx_mock):
        from freeride.core.chat_schema import ChatRequest

        httpx_mock.add_response(
            url=OPENROUTER_CHAT_URL,
            json={
                "id": "chatcmpl-x",
                "object": "chat.completion",
                "created": 1,
                "model": "qwen/qwen3:free",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )
        req = ChatRequest(model="qwen/qwen3:free", messages=[{"role": "user", "content": "hi"}])
        resp = await provider.forward_chat(req, "qwen/qwen3:free", "k")
        assert resp.choices[0].message.content == "ok"
        assert resp.usage is not None and resp.usage.total_tokens == 2

    @pytest.mark.asyncio
    async def test_forward_chat_stream_yields_events(self, provider, httpx_mock):
        from freeride.core.chat_schema import ChatRequest

        sse_body = (
            'data: {"id":"x","object":"chat.completion.chunk","created":1,"model":"m",'
            '"choices":[{"index":0,"delta":{"role":"assistant","content":"hi"}}]}\n\n'
            'data: {"id":"x","object":"chat.completion.chunk","created":1,"model":"m",'
            '"choices":[{"index":0,"delta":{"content":""},"finish_reason":"stop"}]}\n\n'
            "data: [DONE]\n\n"
        ).encode()
        httpx_mock.add_response(
            url=OPENROUTER_CHAT_URL,
            content=sse_body,
            headers={"content-type": "text/event-stream"},
        )
        req = ChatRequest(model="m", messages=[{"role": "user", "content": "hi"}], stream=True)
        events = []
        async for evt in provider.forward_chat_stream(req, "m", "k"):
            events.append(evt)
        # Two SSE events — [DONE] is swallowed
        assert len(events) == 2
        assert events[0].choices[0].delta.content == "hi"
        assert events[1].choices[0].finish_reason == "stop"
