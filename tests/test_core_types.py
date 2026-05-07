"""Tests for freeride.core: Model, ProbeResult, ErrorKind, chat schemas."""

from __future__ import annotations

import json

import pytest

from freeride.core.chat_schema import (
    ChatRequest,
    ChatResponse,
    ChatStreamEvent,
)
from freeride.core.errors import ErrorKind, is_retryable
from freeride.core.types import Model, ProbeResult


# ---- Model ----------------------------------------------------------------


class TestModel:
    def test_basic_construction(self):
        m = Model(api_id="qwen/qwen3-coder:free", provider="openrouter")
        assert m.api_id == "qwen/qwen3-coder:free"
        assert m.provider == "openrouter"
        assert m.context_length == 0
        assert m.output_modalities == ("text",)
        assert m.supported_parameters == ()
        assert m.raw == {}

    def test_supports_helper(self):
        m = Model(
            api_id="m",
            provider="p",
            supported_parameters=("tools", "vision", "response_format"),
        )
        assert m.supports("tools")
        assert m.supports("vision")
        assert not m.supports("logprobs")

    def test_frozen(self):
        m = Model(api_id="m", provider="p")
        with pytest.raises(Exception):
            m.api_id = "changed"  # type: ignore[misc]


# ---- ProbeResult ----------------------------------------------------------


class TestProbeResult:
    def test_success(self):
        p = ProbeResult(ok=True, latency_ms=42)
        assert p.ok is True
        assert p.error is None
        assert p.latency_ms == 42

    def test_failure_with_kind(self):
        p = ProbeResult(ok=False, error=ErrorKind.RATE_LIMIT, latency_ms=10)
        assert p.ok is False
        assert p.error is ErrorKind.RATE_LIMIT
        assert is_retryable(p.error)


# ---- ErrorKind ------------------------------------------------------------


class TestErrorKind:
    def test_all_values_present(self):
        expected = {
            "ok",
            "rate_limit",
            "quota_exhausted",
            "model_not_found",
            "unavailable",
            "timeout",
            "auth",
            "unknown",
        }
        assert {k.value for k in ErrorKind} == expected

    def test_is_retryable_narrow_set(self):
        retryable = {k for k in ErrorKind if is_retryable(k)}
        assert retryable == {
            ErrorKind.RATE_LIMIT,
            ErrorKind.UNAVAILABLE,
            ErrorKind.TIMEOUT,
        }

    def test_is_retryable_excludes_terminal_and_advance_kinds(self):
        # OK is not retryable (it's success); UNKNOWN is not retryable (we
        # don't double-bill on unclassified errors); the others advance the
        # tuple at the resolver layer, not retry the same tuple.
        for kind in (
            ErrorKind.OK,
            ErrorKind.UNKNOWN,
            ErrorKind.AUTH,
            ErrorKind.QUOTA_EXHAUSTED,
            ErrorKind.MODEL_NOT_FOUND,
        ):
            assert not is_retryable(kind), f"{kind} should not be retryable"


# ---- ChatRequest ----------------------------------------------------------


class TestChatRequest:
    def test_minimal_round_trip(self):
        payload = {
            "model": "qwen/qwen3-coder:free",
            "messages": [{"role": "user", "content": "hi"}],
        }
        req = ChatRequest.model_validate(payload)
        assert req.model == payload["model"]
        assert req.messages[0].role == "user"
        assert req.messages[0].content == "hi"
        assert req.is_streaming() is False

        # Round-trip JSON without losing fields
        assert json.loads(req.model_dump_json())["model"] == payload["model"]

    def test_streaming_flag(self):
        req = ChatRequest.model_validate(
            {
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            }
        )
        assert req.is_streaming() is True

    def test_provider_extras_preserved(self):
        req = ChatRequest.model_validate(
            {
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "nvext": {"some_provider_setting": True},
                "future_field": 42,
            }
        )
        dumped = req.model_dump()
        assert dumped["nvext"] == {"some_provider_setting": True}
        assert dumped["future_field"] == 42

    def test_tools_round_trip(self):
        req = ChatRequest.model_validate(
            {
                "model": "m",
                "messages": [{"role": "user", "content": "what time is it?"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "get_time",
                            "description": "current UTC time",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                ],
            }
        )
        assert req.tools is not None
        assert req.tools[0].function.name == "get_time"

    def test_multimodal_content(self):
        req = ChatRequest.model_validate(
            {
                "model": "m",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "describe:"},
                            {
                                "type": "image_url",
                                "image_url": {"url": "https://example.com/x.png"},
                            },
                        ],
                    }
                ],
            }
        )
        # Permissive parsing — content is a list of mixed parts
        msg_content = req.messages[0].content
        assert isinstance(msg_content, list)
        assert len(msg_content) == 2

    def test_response_format_json_schema(self):
        req = ChatRequest.model_validate(
            {
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "person",
                        "schema": {"type": "object", "properties": {"name": {"type": "string"}}},
                    },
                },
            }
        )
        assert req.response_format is not None
        assert req.response_format.type == "json_schema"
        assert req.response_format.json_schema is not None


# ---- ChatResponse ---------------------------------------------------------


class TestChatResponse:
    def test_basic_round_trip(self):
        payload = {
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "created": 1778127786,
            "model": "qwen/qwen3-coder:free",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "hi"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
        }
        resp = ChatResponse.model_validate(payload)
        assert resp.id == "chatcmpl-1"
        assert resp.choices[0].message.content == "hi"
        assert resp.usage is not None
        assert resp.usage.total_tokens == 6

    def test_vllm_extensions_preserved(self):
        # NIM 70b-instruct returns a vLLM-extended response shape; we
        # round-trip it without dropping the extra fields.
        resp = ChatResponse.model_validate(
            {
                "id": "x",
                "object": "chat.completion",
                "created": 1,
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "ok",
                            "reasoning_content": "thinking step 1",
                            "refusal": None,
                        },
                        "finish_reason": "stop",
                    }
                ],
            }
        )
        msg_dump = resp.choices[0].message.model_dump()
        assert msg_dump.get("reasoning_content") == "thinking step 1"

    def test_nvext_top_level_preserved(self):
        resp = ChatResponse.model_validate(
            {
                "id": "x",
                "object": "chat.completion",
                "created": 1,
                "model": "m",
                "choices": [],
                "nvext": {"request_id": "abc"},
            }
        )
        assert resp.model_dump().get("nvext") == {"request_id": "abc"}


# ---- ChatStreamEvent ------------------------------------------------------


class TestChatStreamEvent:
    def test_delta_round_trip(self):
        evt = ChatStreamEvent.model_validate(
            {
                "id": "x",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "m",
                "choices": [
                    {"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}
                ],
            }
        )
        assert evt.choices[0].delta.role == "assistant"

    def test_nim_penultimate_usage_event(self):
        # NIM streams its final usage on the penultimate event with empty choices.
        evt = ChatStreamEvent.model_validate(
            {
                "id": "x",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "m",
                "choices": [],
                "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
            }
        )
        assert evt.choices == []
        assert evt.usage is not None
        assert evt.usage.total_tokens == 10
