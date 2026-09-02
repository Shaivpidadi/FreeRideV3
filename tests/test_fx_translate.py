"""Unit tests for the fx gateway dialect translator.

Request fixtures mirror what fx's ``buildGatewayRequestBodyValidated``
(src/gateway/vercel_protocol.zig) actually serializes; stream-output
assertions mirror what fx's ``consumeSseStream`` (src/gateway/client.zig)
and the fake gateway in fx's tests/e2e/tmux-helpers.ts require:

- ``finishReason`` is an OBJECT with a ``unified`` member from the
  closed enum — a bare string is a hard stream error on fx's side.
- usage totals are nested (``{"inputTokens": {"total": N}}``).
- tool calls need non-empty ids; input may be an object or a string.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from freeride.core.chat_schema import ChatStreamEvent
from freeride.core.fx_schema import FxRequest
from freeride.core.fx_translate import (
    finish_reason_to_unified,
    fx_to_chat_request,
    stream_chat_to_fx,
)

# ─── request translation ────────────────────────────────────────────


def _fx_body(**overrides: Any) -> FxRequest:
    base: dict[str, Any] = {
        "prompt": [
            {"role": "system", "content": "you are ridex"},
            {"role": "user", "content": [{"type": "text", "text": "create test.py"}]},
        ],
        "tools": [
            {
                "type": "function",
                "name": "write_file",
                "description": "write a file",
                "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}},
            }
        ],
        "toolChoice": {"type": "auto"},
        "maxOutputTokens": 4096,
    }
    base.update(overrides)
    return FxRequest.model_validate(base)


def test_basic_prompt_maps_roles_and_text() -> None:
    req = fx_to_chat_request(_fx_body(), "freeride/coding")
    assert req.model == "freeride/coding"
    assert [m.role for m in req.messages] == ["system", "user"]
    assert req.messages[0].content == "you are ridex"
    assert req.messages[1].content == "create test.py"
    assert req.max_tokens == 4096
    assert req.tool_choice == "auto"


def test_tools_translate_from_flat_to_openai_nested() -> None:
    req = fx_to_chat_request(_fx_body(), "auto")
    assert req.tools is not None and len(req.tools) == 1
    tool = req.tools[0]
    assert tool.type == "function"
    assert tool.function.name == "write_file"
    assert tool.function.description == "write a file"
    assert tool.function.parameters == {
        "type": "object",
        "properties": {"path": {"type": "string"}},
    }


def test_assistant_tool_call_part_becomes_openai_tool_call() -> None:
    body = _fx_body(
        prompt=[
            {"role": "user", "content": [{"type": "text", "text": "write it"}]},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "on it"},
                    {
                        "type": "tool-call",
                        "toolCallId": "call_1",
                        "toolName": "write_file",
                        "input": {"path": "test.py"},
                    },
                ],
            },
            {
                "role": "tool",
                "content": [
                    {
                        "type": "tool-result",
                        "toolCallId": "call_1",
                        "toolName": "write_file",
                        "output": {"type": "text", "value": "wrote 1 line"},
                    }
                ],
            },
        ]
    )
    req = fx_to_chat_request(body, "auto")
    assistant = req.messages[1]
    assert assistant.content == "on it"
    assert assistant.tool_calls is not None
    call = assistant.tool_calls[0]
    assert call.id == "call_1"
    assert call.function.name == "write_file"
    assert json.loads(call.function.arguments) == {"path": "test.py"}

    tool_msg = req.messages[2]
    assert tool_msg.role == "tool"
    assert tool_msg.tool_call_id == "call_1"
    assert tool_msg.content == "wrote 1 line"


def test_tool_result_error_and_denied_outputs_flatten_to_text() -> None:
    body = _fx_body(
        prompt=[
            {
                "role": "tool",
                "content": [
                    {
                        "type": "tool-result",
                        "toolCallId": "c1",
                        "toolName": "run",
                        "output": {"type": "error-text", "value": "exit 1"},
                    },
                    {
                        "type": "tool-result",
                        "toolCallId": "c2",
                        "toolName": "run",
                        "output": {"type": "execution-denied", "reason": "user said no"},
                    },
                ],
            }
        ]
    )
    req = fx_to_chat_request(body, "auto")
    assert [m.content for m in req.messages] == ["exit 1", "user said no"]
    assert [m.tool_call_id for m in req.messages] == ["c1", "c2"]


def test_user_image_file_part_becomes_data_uri() -> None:
    body = _fx_body(
        prompt=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this"},
                    {"type": "file", "mediaType": "image/png", "data": "aGk="},
                ],
            }
        ]
    )
    req = fx_to_chat_request(body, "auto")
    content = req.messages[0].content
    assert isinstance(content, list)
    # pydantic coerces the raw dict parts into typed content models —
    # compare the serialized shape, which is what providers receive.
    parts = [
        p.model_dump(exclude_none=True) if hasattr(p, "model_dump") else p
        for p in content
    ]
    assert parts[0] == {"type": "text", "text": "what is this"}
    assert parts[1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,aGk="},
    }


def test_serialized_tool_call_input_passes_through_as_string() -> None:
    """fx re-sends prior assistant turns with input already serialized
    (arguments_json). The translator must not double-encode."""
    body = _fx_body(
        prompt=[
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool-call",
                        "toolCallId": "c1",
                        "toolName": "run",
                        "input": '{"cmd":"ls"}',
                    }
                ],
            }
        ]
    )
    req = fx_to_chat_request(body, "auto")
    assert req.messages[0].tool_calls[0].function.arguments == '{"cmd":"ls"}'


def test_response_format_json_downgrades_to_json_object() -> None:
    body = _fx_body(
        responseFormat={"type": "json", "name": "decision", "schema": {"type": "object"}}
    )
    req = fx_to_chat_request(body, "auto")
    assert req.response_format is not None
    assert req.response_format.type == "json_object"


def test_unknown_part_types_and_extra_fields_do_not_400() -> None:
    body = FxRequest.model_validate(
        {
            "prompt": [
                {
                    "role": "user",
                    "content": [{"type": "future-part", "payload": 1}],
                    "providerOptions": {"anthropic": {"cacheControl": {"type": "ephemeral"}}},
                }
            ],
            "headers": {"user-agent": "fx/1.0"},
            "futureField": True,
        }
    )
    req = fx_to_chat_request(body, "auto")
    assert req.messages[0].content == ""


# ─── finish reason mapping ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("reason", "unified"),
    [
        ("stop", "stop"),
        ("length", "length"),
        ("content_filter", "content-filter"),
        ("tool_calls", "tool-calls"),
        (None, "stop"),
        ("weird_new_reason", "other"),
    ],
)
def test_finish_reason_unified_mapping(reason: str | None, unified: str) -> None:
    assert finish_reason_to_unified(reason, has_tool_calls=False) == unified


def test_finish_reason_forced_to_tool_calls_when_calls_present() -> None:
    """Some free models say finish_reason=stop while still emitting
    tool_calls; fx must see tool-calls or the agent won't execute."""
    assert finish_reason_to_unified("stop", has_tool_calls=True) == "tool-calls"


# ─── stream translation ─────────────────────────────────────────────


def _chunk(
    *,
    content: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str | None = None,
    usage: dict[str, int] | None = None,
) -> ChatStreamEvent:
    delta: dict[str, Any] = {}
    if content is not None:
        delta["content"] = content
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    choices = []
    if delta or finish_reason:
        choices.append({"index": 0, "delta": delta, "finish_reason": finish_reason})
    return ChatStreamEvent.model_validate(
        {
            "id": "chatcmpl-1",
            "created": 1,
            "model": "test/model",
            "choices": choices,
            "usage": usage,
        }
    )


async def _collect(chunks: list[ChatStreamEvent]) -> list[Any]:
    """Run the translator and parse SSE frames back into event dicts.
    The [DONE] sentinel is returned as the literal string 'DONE'."""

    async def gen():
        for c in chunks:
            yield c

    out: list[Any] = []
    async for raw in stream_chat_to_fx(gen(), resolved_model="test/model"):
        text = raw.decode()
        assert text.startswith("data: ") and text.endswith("\n\n"), repr(text)
        payload = text[len("data: ") : -2]
        out.append("DONE" if payload == "[DONE]" else json.loads(payload))
    return out


@pytest.mark.asyncio
async def test_stream_text_only() -> None:
    events = await _collect(
        [
            _chunk(content="po"),
            _chunk(content="ng"),
            _chunk(finish_reason="stop", usage={"prompt_tokens": 3, "completion_tokens": 5}),
        ]
    )
    assert events[0] == {"type": "response-metadata", "modelId": "test/model"}
    assert events[1] == {"type": "text-delta", "id": "answer_1", "delta": "po"}
    assert events[2] == {"type": "text-delta", "id": "answer_1", "delta": "ng"}
    finish = events[3]
    assert finish["type"] == "finish"
    assert finish["finishReason"] == {"unified": "stop", "raw": "stop"}
    assert finish["usage"] == {
        "inputTokens": {"total": 3},
        "outputTokens": {"total": 5},
    }
    assert events[4] == "DONE"


@pytest.mark.asyncio
async def test_stream_tool_call_fragments_accumulate() -> None:
    events = await _collect(
        [
            _chunk(
                tool_calls=[
                    {
                        "index": 0,
                        "id": "call_9",
                        "function": {"name": "write_file", "arguments": ""},
                    }
                ]
            ),
            _chunk(
                tool_calls=[{"index": 0, "function": {"arguments": '{"path":'}}]
            ),
            _chunk(
                tool_calls=[{"index": 0, "function": {"arguments": '"test.py"}'}}]
            ),
            _chunk(finish_reason="tool_calls"),
        ]
    )
    kinds = [e["type"] for e in events if isinstance(e, dict)]
    assert kinds == [
        "response-metadata",
        "tool-input-start",
        "tool-input-delta",
        "tool-input-delta",
        "tool-input-end",
        "tool-call",
        "finish",
    ]
    start = events[1]
    assert start == {"type": "tool-input-start", "id": "call_9", "toolName": "write_file"}
    call = events[5]
    assert call["toolCallId"] == "call_9"
    assert call["toolName"] == "write_file"
    assert call["input"] == {"path": "test.py"}
    assert events[6]["finishReason"]["unified"] == "tool-calls"


@pytest.mark.asyncio
async def test_stream_tool_call_without_id_gets_synthesized_id() -> None:
    """fx rejects tool calls with empty ids — some free models omit
    them, so the translator must synthesize."""
    events = await _collect(
        [
            _chunk(
                tool_calls=[
                    {"index": 0, "function": {"name": "run", "arguments": "{}"}}
                ]
            ),
            _chunk(finish_reason="tool_calls"),
        ]
    )
    call = next(e for e in events if isinstance(e, dict) and e["type"] == "tool-call")
    assert call["toolCallId"] == "call_0"


@pytest.mark.asyncio
async def test_stream_malformed_tool_arguments_ship_as_string() -> None:
    """Malformed JSON args are delivered as a raw string — fx accepts
    string input and surfaces the integrity failure to the model
    itself instead of killing the stream."""
    events = await _collect(
        [
            _chunk(
                tool_calls=[
                    {
                        "index": 0,
                        "id": "c1",
                        "function": {"name": "run", "arguments": '{"broken":'},
                    }
                ]
            ),
            _chunk(finish_reason="tool_calls"),
        ]
    )
    call = next(e for e in events if isinstance(e, dict) and e["type"] == "tool-call")
    assert call["input"] == '{"broken":'


@pytest.mark.asyncio
async def test_stream_parallel_tool_calls_keep_distinct_indices() -> None:
    events = await _collect(
        [
            _chunk(
                tool_calls=[
                    {"index": 0, "id": "a", "function": {"name": "read", "arguments": "{}"}},
                    {"index": 1, "id": "b", "function": {"name": "write", "arguments": "{}"}},
                ]
            ),
            _chunk(finish_reason="tool_calls"),
        ]
    )
    calls = [e for e in events if isinstance(e, dict) and e["type"] == "tool-call"]
    assert [(c["toolCallId"], c["toolName"]) for c in calls] == [
        ("a", "read"),
        ("b", "write"),
    ]


@pytest.mark.asyncio
async def test_stream_finish_reason_never_leaves_the_unified_enum() -> None:
    events = await _collect([_chunk(finish_reason="bizarre")])
    finish = next(e for e in events if isinstance(e, dict) and e["type"] == "finish")
    assert finish["finishReason"]["unified"] == "other"
    assert finish["finishReason"]["raw"] == "bizarre"


@pytest.mark.asyncio
async def test_stream_always_terminates_with_finish_then_done() -> None:
    events = await _collect([])  # upstream produced nothing at all
    assert [e["type"] for e in events if isinstance(e, dict)] == [
        "response-metadata",
        "finish",
    ]
    assert events[-1] == "DONE"
