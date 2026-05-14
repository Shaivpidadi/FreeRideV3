"""Tests for the Codex/Responses ↔ ChatCompletions translator."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from freeride.core.chat_schema import (
    ChatResponse,
    ChatStreamEvent,
    Choice,
    ChoiceMessage,
    StreamChoice,
    StreamDelta,
    ToolCall,
    ToolCallFunction,
    Usage,
)
from freeride.core.codex_schema import ResponsesRequest
from freeride.core.codex_translate import (
    chat_to_responses_response,
    responses_to_chat_request,
    stream_chat_to_responses,
)


# ─── request: Responses → Chat ────────────────────────────────────


def test_string_input_becomes_single_user_message() -> None:
    """Responses lets you pass a bare string as input shorthand."""
    req = ResponsesRequest.model_validate({"model": "gpt-5-codex", "input": "hello"})
    out = responses_to_chat_request(req)
    assert out.model == "gpt-5-codex"
    assert len(out.messages) == 1
    assert out.messages[0].role == "user"
    assert out.messages[0].content == "hello"


def test_instructions_become_leading_system_message() -> None:
    req = ResponsesRequest.model_validate(
        {
            "model": "m",
            "instructions": "You are terse.",
            "input": "hi",
        }
    )
    out = responses_to_chat_request(req)
    assert out.messages[0].role == "system"
    assert out.messages[0].content == "You are terse."
    assert out.messages[1].role == "user"


def test_message_item_with_input_text_part_translates() -> None:
    req = ResponsesRequest.model_validate(
        {
            "model": "m",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hello"}],
                }
            ],
        }
    )
    out = responses_to_chat_request(req)
    assert out.messages[0].role == "user"
    assert out.messages[0].content == "hello"


def test_function_call_item_becomes_assistant_with_tool_calls() -> None:
    """A prior-turn function_call item echoed back in the request must
    re-materialize as an OpenAI assistant message with tool_calls so
    the upstream provider sees the full multi-turn context."""
    req = ResponsesRequest.model_validate(
        {
            "model": "m",
            "input": [
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "Write",
                    "arguments": '{"path":"test.py"}',
                }
            ],
        }
    )
    out = responses_to_chat_request(req)
    assert out.messages[0].role == "assistant"
    assert out.messages[0].content is None
    assert len(out.messages[0].tool_calls) == 1
    tc = out.messages[0].tool_calls[0]
    assert tc.id == "call_1"
    assert tc.function.name == "Write"
    assert tc.function.arguments == '{"path":"test.py"}'


def test_function_call_output_item_becomes_tool_message() -> None:
    """How tool RESULTS travel back to the model in Responses-shape.
    Must map to OpenAI's role=tool message with matching tool_call_id."""
    req = ResponsesRequest.model_validate(
        {
            "model": "m",
            "input": [
                {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": "Wrote 1 line.",
                }
            ],
        }
    )
    out = responses_to_chat_request(req)
    assert out.messages[0].role == "tool"
    assert out.messages[0].tool_call_id == "call_1"
    assert out.messages[0].content == "Wrote 1 line."


def test_developer_role_maps_to_system() -> None:
    """The Responses-specific 'developer' role (a higher-priority
    system message) collapses to 'system' since Chat Completions
    has no equivalent."""
    req = ResponsesRequest.model_validate(
        {
            "model": "m",
            "input": [
                {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "be terse"}],
                }
            ],
        }
    )
    out = responses_to_chat_request(req)
    assert out.messages[0].role == "system"


def test_reasoning_items_drop_silently() -> None:
    """Free providers don't accept reasoning items — they'd 400.
    The translator drops them so the upstream call doesn't fail."""
    req = ResponsesRequest.model_validate(
        {
            "model": "m",
            "input": [
                {"type": "reasoning", "id": "rs_1", "summary": []},
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "go"}],
                },
            ],
        }
    )
    out = responses_to_chat_request(req)
    # Only the user message lands — reasoning dropped.
    assert len(out.messages) == 1
    assert out.messages[0].role == "user"


def test_tools_unwrap_to_chat_completion_shape() -> None:
    """Responses uses FLAT tool defs ({type:function, name, parameters}).
    Chat Completions wraps that under a 'function' key. Translation
    must re-wrap so upstream providers parse it correctly."""
    req = ResponsesRequest.model_validate(
        {
            "model": "m",
            "input": "hi",
            "tools": [
                {
                    "type": "function",
                    "name": "Write",
                    "description": "write a file",
                    "parameters": {"type": "object"},
                }
            ],
        }
    )
    out = responses_to_chat_request(req)
    assert out.tools is not None
    assert len(out.tools) == 1
    t = out.tools[0]
    assert t.type == "function"
    assert t.function.name == "Write"
    assert t.function.description == "write a file"
    assert t.function.parameters == {"type": "object"}


def test_max_output_tokens_renames_to_max_tokens() -> None:
    req = ResponsesRequest.model_validate(
        {"model": "m", "input": "hi", "max_output_tokens": 1234}
    )
    out = responses_to_chat_request(req)
    assert out.max_tokens == 1234


# ─── response: Chat → Responses ───────────────────────────────────


def _chat_response(
    *,
    content: str | None = None,
    tool_calls: list[ToolCall] | None = None,
    finish_reason: str = "stop",
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
) -> ChatResponse:
    return ChatResponse(
        id="chatcmpl-stub",
        created=1234567890,
        model="resolved",
        choices=[
            Choice(
                index=0,
                message=ChoiceMessage(
                    role="assistant",
                    content=content,
                    tool_calls=tool_calls,
                ),
                finish_reason=finish_reason,
            )
        ],
        usage=Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
    )


def test_text_response_produces_one_message_output_item() -> None:
    resp = _chat_response(content="Hello.")
    out = chat_to_responses_response(resp, "gpt-5-codex")
    assert out.model == "gpt-5-codex"
    assert out.status == "completed"
    assert len(out.output) == 1
    item = out.output[0]
    assert item["type"] == "message"
    assert item["role"] == "assistant"
    assert item["content"][0]["type"] == "output_text"
    assert item["content"][0]["text"] == "Hello."


def test_tool_call_response_produces_function_call_item() -> None:
    resp = _chat_response(
        content=None,
        tool_calls=[
            ToolCall(
                id="call_1",
                type="function",
                function=ToolCallFunction(name="Write", arguments='{"path":"x"}'),
            )
        ],
        finish_reason="tool_calls",
    )
    out = chat_to_responses_response(resp, "gpt-5-codex")
    # No message item (no text), just the function_call.
    assert len(out.output) == 1
    item = out.output[0]
    assert item["type"] == "function_call"
    assert item["call_id"] == "call_1"
    assert item["name"] == "Write"
    assert item["arguments"] == '{"path":"x"}'


def test_text_and_tool_call_produces_two_output_items() -> None:
    """Order: message first, then function_call. Mirrors Responses
    convention as documented in OpenAI's API reference."""
    resp = _chat_response(
        content="Sure, doing it.",
        tool_calls=[
            ToolCall(
                id="c",
                type="function",
                function=ToolCallFunction(name="X", arguments="{}"),
            )
        ],
        finish_reason="tool_calls",
    )
    out = chat_to_responses_response(resp, "m")
    assert len(out.output) == 2
    assert out.output[0]["type"] == "message"
    assert out.output[1]["type"] == "function_call"


def test_length_finish_reason_maps_to_incomplete_with_reason() -> None:
    resp = _chat_response(content="trunc", finish_reason="length")
    out = chat_to_responses_response(resp, "m")
    assert out.status == "incomplete"
    assert out.incomplete_details is not None
    assert out.incomplete_details.reason == "max_output_tokens"


def test_tool_call_finish_reason_is_still_completed() -> None:
    """tool_calls is NOT a degraded status — the tool call lives in
    output[], not in a finish reason. Status stays 'completed'."""
    resp = _chat_response(
        content=None,
        tool_calls=[
            ToolCall(
                id="c",
                type="function",
                function=ToolCallFunction(name="X", arguments="{}"),
            )
        ],
        finish_reason="tool_calls",
    )
    out = chat_to_responses_response(resp, "m")
    assert out.status == "completed"
    assert out.incomplete_details is None


def test_usage_renames_token_keys() -> None:
    resp = _chat_response(content="x", prompt_tokens=42, completion_tokens=7)
    out = chat_to_responses_response(resp, "m")
    assert out.usage is not None
    assert out.usage.input_tokens == 42
    assert out.usage.output_tokens == 7
    assert out.usage.total_tokens == 49


# ─── streaming ────────────────────────────────────────────────────


def _stream_chunk(
    *,
    content: str | None = None,
    finish_reason: str | None = None,
    usage: Usage | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
) -> ChatStreamEvent:
    delta_kwargs: dict[str, Any] = {}
    if content is not None:
        delta_kwargs["content"] = content
    if tool_calls is not None:
        delta_kwargs["tool_calls"] = tool_calls
    return ChatStreamEvent(
        id="stub",
        created=0,
        model="m",
        choices=[StreamChoice(index=0, delta=StreamDelta(**delta_kwargs), finish_reason=finish_reason)],
        usage=usage,
    )


async def _collect_events(chunks: list[ChatStreamEvent]) -> list[tuple[str, dict[str, Any]]]:
    """Parse the SSE byte stream back into (event_name, data) tuples."""

    async def gen():
        for c in chunks:
            yield c

    out: list[tuple[str, dict[str, Any]]] = []
    async for raw in stream_chat_to_responses(gen(), requested_model="gpt-5-codex"):
        text = raw.decode("utf-8")
        # SSE frames are "event: <name>\ndata: <json>\n\n"
        lines = text.rstrip("\n").split("\n")
        assert lines[0].startswith("event: "), repr(text)
        assert lines[1].startswith("data: "), repr(text)
        event_name = lines[0][len("event: ") :]
        data = json.loads(lines[1][len("data: ") :])
        out.append((event_name, data))
    return out


@pytest.mark.asyncio
async def test_stream_text_emits_full_framed_event_sequence() -> None:
    """Codex CLI gates on output_item.added + content_part.added BEFORE
    consuming text deltas. The translator must emit the full frame."""
    events = await _collect_events(
        [
            _stream_chunk(content="Hello"),
            _stream_chunk(content=" world"),
            _stream_chunk(content=None, finish_reason="stop"),
        ]
    )
    names = [e[0] for e in events]
    assert names == [
        "response.created",
        "response.in_progress",
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.delta",
        "response.output_text.delta",
        "response.output_text.done",
        "response.content_part.done",
        "response.output_item.done",
        "response.completed",
    ]


@pytest.mark.asyncio
async def test_stream_text_deltas_carry_individual_pieces() -> None:
    events = await _collect_events(
        [
            _stream_chunk(content="Hello"),
            _stream_chunk(content=" world"),
            _stream_chunk(content=None, finish_reason="stop"),
        ]
    )
    deltas = [e[1] for e in events if e[0] == "response.output_text.delta"]
    assert [d["delta"] for d in deltas] == ["Hello", " world"]
    # Done event carries the accumulated text.
    done = next(e[1] for e in events if e[0] == "response.output_text.done")
    assert done["text"] == "Hello world"


@pytest.mark.asyncio
async def test_stream_sequence_numbers_are_monotonic() -> None:
    """Some SDK consumers gate on sequence_number for ordering."""
    events = await _collect_events(
        [_stream_chunk(content="hi"), _stream_chunk(content=None, finish_reason="stop")]
    )
    seqs = [d.get("sequence_number") for _, d in events]
    assert all(s is not None for s in seqs)
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)  # all unique


@pytest.mark.asyncio
async def test_stream_tool_call_uses_function_call_arguments_events() -> None:
    """Tool call args stream as function_call_arguments.delta events,
    not output_text.delta. Framing still applies — output_item.added
    before any delta, output_item.done after."""
    events = await _collect_events(
        [
            _stream_chunk(
                tool_calls=[
                    {
                        "index": 0,
                        "id": "call_x",
                        "type": "function",
                        "function": {"name": "Write", "arguments": '{"path"'},
                    }
                ]
            ),
            _stream_chunk(
                tool_calls=[{"index": 0, "function": {"arguments": ':"x"}'}}]
            ),
            _stream_chunk(content=None, finish_reason="tool_calls"),
        ]
    )
    names = [e[0] for e in events]
    # Framing: created, in_progress, item.added, args.delta+, args.done, item.done, completed
    assert names[0] == "response.created"
    assert "response.output_item.added" in names
    arg_deltas = [e[1] for e in events if e[0] == "response.function_call_arguments.delta"]
    assert [d["delta"] for d in arg_deltas] == ['{"path"', ':"x"}']
    args_done = next(e[1] for e in events if e[0] == "response.function_call_arguments.done")
    assert args_done["arguments"] == '{"path":"x"}'
    # The output_item.added event names the function and carries the call_id.
    added = next(e[1] for e in events if e[0] == "response.output_item.added")
    assert added["item"]["type"] == "function_call"
    assert added["item"]["name"] == "Write"
    assert added["item"]["call_id"] == "call_x"
    assert names[-1] == "response.completed"


@pytest.mark.asyncio
async def test_stream_completed_event_carries_usage_and_status() -> None:
    events = await _collect_events(
        [
            _stream_chunk(content="hi"),
            _stream_chunk(
                content=None,
                finish_reason="stop",
                usage=Usage(prompt_tokens=12, completion_tokens=3, total_tokens=15),
            ),
        ]
    )
    completed = next(e[1] for e in events if e[0] == "response.completed")
    assert completed["response"]["status"] == "completed"
    assert completed["response"]["usage"]["input_tokens"] == 12
    assert completed["response"]["usage"]["output_tokens"] == 3


@pytest.mark.asyncio
async def test_stream_response_id_consistent_across_created_and_completed() -> None:
    """Clients use the response.id from response.created to track the
    stream — the completed event MUST carry the same id."""
    events = await _collect_events(
        [_stream_chunk(content="hi"), _stream_chunk(content=None, finish_reason="stop")]
    )
    created_id = events[0][1]["response"]["id"]
    completed_id = events[-1][1]["response"]["id"]
    assert created_id == completed_id
    assert re.match(r"^resp_[0-9a-f]{32}$", created_id)
