"""Tests for the Anthropic Messages ↔ OpenAI translation layer.

Phase 1 scope: non-streaming chat. Schema parsing (lenient on input,
strict on output), system-prompt hoisting, content-block flattening,
stop-reason and usage mapping, request-validity gating for features
that aren't shipped yet (streaming / tool_use / images all
expected to gate cleanly).
"""

from __future__ import annotations

import pytest

from freeride.core.anthropic_schema import (
    AnthropicMessagesRequest,
    AnthropicMessagesResponse,
)
from freeride.core.anthropic_translate import (
    UnsupportedContentBlock,
    anthropic_to_openai_request,
    map_stop_reason,
    openai_to_anthropic_response,
    request_unsupported_for_phase_1,
)
from freeride.core.chat_schema import (
    ChatResponse,
    Choice,
    ChoiceMessage,
    ToolCall,
    ToolCallFunction,
    Usage,
)


# ─── stop reason mapping ─────────────────────────────────────────


@pytest.mark.parametrize(
    "openai_finish, expected",
    [
        ("stop", "end_turn"),
        ("length", "max_tokens"),
        ("tool_calls", "tool_use"),
        ("content_filter", "refusal"),
        ("function_call", "tool_use"),
        (None, "end_turn"),  # missing → safe default
        ("", "end_turn"),  # empty string → safe default
        ("weird_unknown", "end_turn"),  # forward-compat fallback
    ],
)
def test_map_stop_reason(openai_finish: str | None, expected: str) -> None:
    assert map_stop_reason(openai_finish) == expected


# ─── system prompt hoisting ──────────────────────────────────────


def test_system_string_becomes_first_message() -> None:
    req = AnthropicMessagesRequest(
        model="claude-sonnet-4-6",
        max_tokens=100,
        system="You are a helpful pirate.",
        messages=[{"role": "user", "content": "ahoy"}],
    )
    out = anthropic_to_openai_request(req)
    assert len(out.messages) == 2
    assert out.messages[0].role == "system"
    assert out.messages[0].content == "You are a helpful pirate."
    assert out.messages[1].role == "user"


def test_system_list_of_text_blocks_concatenates() -> None:
    req = AnthropicMessagesRequest(
        model="claude-sonnet-4-6",
        max_tokens=100,
        system=[
            {"type": "text", "text": "Persona A."},
            {"type": "text", "text": "Persona B."},
        ],
        messages=[{"role": "user", "content": "hi"}],
    )
    out = anthropic_to_openai_request(req)
    assert out.messages[0].role == "system"
    assert out.messages[0].content == "Persona A.\nPersona B."


def test_no_system_means_no_leading_system_message() -> None:
    req = AnthropicMessagesRequest(
        model="claude-sonnet-4-6",
        max_tokens=100,
        messages=[{"role": "user", "content": "hi"}],
    )
    out = anthropic_to_openai_request(req)
    assert out.messages[0].role == "user"
    # First (and only) message should be the user's
    assert len(out.messages) == 1


def test_empty_system_dropped() -> None:
    req = AnthropicMessagesRequest(
        model="claude-sonnet-4-6",
        max_tokens=100,
        system="",
        messages=[{"role": "user", "content": "hi"}],
    )
    out = anthropic_to_openai_request(req)
    assert len(out.messages) == 1  # no leading system


# ─── content block flattening ────────────────────────────────────


def test_text_blocks_flatten_to_string() -> None:
    req = AnthropicMessagesRequest(
        model="claude-sonnet-4-6",
        max_tokens=100,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "First."},
                    {"type": "text", "text": "Second."},
                ],
            }
        ],
    )
    out = anthropic_to_openai_request(req)
    assert out.messages[0].content == "First.\nSecond."


def test_unknown_block_types_dropped_quietly() -> None:
    """Forward-compat: Anthropic adding a new block type shouldn't 500.
    We don't *use* the new block (we have no idea what it means), but
    we also don't crash on it."""
    req = AnthropicMessagesRequest(
        model="claude-sonnet-4-6",
        max_tokens=100,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hi"},
                    {"type": "future_unicorn_block", "data": {"foo": "bar"}},
                ],
            }
        ],
    )
    out = anthropic_to_openai_request(req)
    assert out.messages[0].content == "hi"


def test_thinking_blocks_dropped_silently() -> None:
    req = AnthropicMessagesRequest(
        model="claude-sonnet-4-6",
        max_tokens=100,
        messages=[
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "Let me think...", "signature": "x"},
                    {"type": "text", "text": "Here's the answer."},
                ],
            }
        ],
    )
    out = anthropic_to_openai_request(req)
    assert out.messages[0].content == "Here's the answer."


def test_image_blocks_raise_unsupported() -> None:
    req = AnthropicMessagesRequest(
        model="claude-sonnet-4-6",
        max_tokens=100,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": "iVBOR...",
                        },
                    }
                ],
            }
        ],
    )
    with pytest.raises(UnsupportedContentBlock):
        anthropic_to_openai_request(req)


def test_document_blocks_raise_unsupported() -> None:
    req = AnthropicMessagesRequest(
        model="claude-sonnet-4-6",
        max_tokens=100,
        messages=[
            {
                "role": "user",
                "content": [{"type": "document", "source": {"type": "url", "url": "x"}}],
            }
        ],
    )
    with pytest.raises(UnsupportedContentBlock):
        anthropic_to_openai_request(req)


# ─── tool definitions translate (request side only in Phase 1) ───


def test_tool_definitions_translate() -> None:
    req = AnthropicMessagesRequest(
        model="claude-sonnet-4-6",
        max_tokens=100,
        messages=[{"role": "user", "content": "hi"}],
        tools=[
            {
                "name": "get_weather",
                "description": "Look up weather.",
                "input_schema": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            }
        ],
    )
    out = anthropic_to_openai_request(req)
    assert out.tools is not None
    assert len(out.tools) == 1
    assert out.tools[0].type == "function"
    assert out.tools[0].function.name == "get_weather"
    assert out.tools[0].function.parameters["properties"]["city"] == {"type": "string"}


@pytest.mark.parametrize(
    "anthropic_choice, expected",
    [
        ({"type": "auto"}, "auto"),
        ({"type": "any"}, "required"),
        ({"type": "none"}, "none"),
        (
            {"type": "tool", "name": "get_weather"},
            {"type": "function", "function": {"name": "get_weather"}},
        ),
        ({"type": "tool"}, None),  # missing name → caller error, drop
    ],
)
def test_tool_choice_translates(anthropic_choice, expected) -> None:
    req = AnthropicMessagesRequest(
        model="claude-sonnet-4-6",
        max_tokens=100,
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"name": "get_weather", "input_schema": {}}],
        tool_choice=anthropic_choice,
    )
    out = anthropic_to_openai_request(req)
    assert out.tool_choice == expected


# ─── pass-through fields ─────────────────────────────────────────


def test_temperature_top_p_stop_pass_through() -> None:
    req = AnthropicMessagesRequest(
        model="claude-sonnet-4-6",
        max_tokens=42,
        temperature=0.7,
        top_p=0.95,
        stop_sequences=["\n\n", "END"],
        messages=[{"role": "user", "content": "hi"}],
    )
    out = anthropic_to_openai_request(req)
    assert out.temperature == 0.7
    assert out.top_p == 0.95
    assert out.stop == ["\n\n", "END"]
    assert out.max_tokens == 42


def test_anthropic_only_fields_dropped() -> None:
    """thinking, cache_control, service_tier, metadata, etc. should be
    accepted (so the request parses) but never reach the OpenAI body."""
    req = AnthropicMessagesRequest(
        model="claude-sonnet-4-6",
        max_tokens=100,
        messages=[{"role": "user", "content": "hi"}],
        thinking={"type": "enabled", "budget_tokens": 5000},
        cache_control={"type": "ephemeral"},
        service_tier="priority",
        metadata={"user_id": "u123"},
    )
    out = anthropic_to_openai_request(req)
    # Just check none of these dropped fields ended up in extras as
    # something an OpenAI provider might reject.
    dumped = out.model_dump(exclude_none=True)
    assert "thinking" not in dumped
    assert "cache_control" not in dumped
    assert "service_tier" not in dumped


# ─── response translation ────────────────────────────────────────


def _stub_openai_response(
    *,
    content: str | None = None,
    finish_reason: str = "stop",
    tool_calls: list[ToolCall] | None = None,
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
) -> ChatResponse:
    return ChatResponse(
        id="chatcmpl-stub",
        created=1234567890,
        model="actual-resolved",
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


def test_response_text_only() -> None:
    resp = _stub_openai_response(content="Hello!")
    out = openai_to_anthropic_response(resp, "claude-sonnet-4-6")

    assert isinstance(out, AnthropicMessagesResponse)
    assert out.id.startswith("msg_")
    assert out.model == "claude-sonnet-4-6"  # echoes the requested model
    assert out.content == [{"type": "text", "text": "Hello!"}]
    assert out.stop_reason == "end_turn"
    assert out.usage.input_tokens == 10
    assert out.usage.output_tokens == 5


def test_response_max_tokens_finish() -> None:
    resp = _stub_openai_response(content="truncated", finish_reason="length")
    out = openai_to_anthropic_response(resp, "claude-sonnet-4-6")
    assert out.stop_reason == "max_tokens"


def test_response_no_choices_yields_empty_content_not_crash() -> None:
    resp = ChatResponse(id="x", created=0, model="m", choices=[])
    out = openai_to_anthropic_response(resp, "claude-sonnet-4-6")
    assert out.content == []
    assert out.stop_reason == "end_turn"
    assert out.usage.input_tokens == 0


def test_response_with_tool_calls_emits_tool_use_block() -> None:
    """Phase-1 doesn't ROUTE tool requests, but if a provider
    spontaneously emits tool_calls anyway, the translator must not
    crash and must surface them in Anthropic shape."""
    resp = _stub_openai_response(
        content=None,
        finish_reason="tool_calls",
        tool_calls=[
            ToolCall(
                id="call_xyz",
                type="function",
                function=ToolCallFunction(
                    name="get_weather",
                    arguments='{"city": "NYC"}',
                ),
            )
        ],
    )
    out = openai_to_anthropic_response(resp, "claude-sonnet-4-6")
    assert out.stop_reason == "tool_use"
    assert out.content == [
        {"type": "tool_use", "id": "call_xyz", "name": "get_weather", "input": {"city": "NYC"}}
    ]


def test_response_with_malformed_tool_args_yields_empty_input() -> None:
    """Llama models occasionally emit malformed JSON for tool calls.
    Per the feasibility doc we'd rather emit input={} than 500."""
    resp = _stub_openai_response(
        content=None,
        finish_reason="tool_calls",
        tool_calls=[
            ToolCall(
                id="call_bad",
                type="function",
                function=ToolCallFunction(
                    name="get_weather",
                    arguments="this is not json {",
                ),
            )
        ],
    )
    out = openai_to_anthropic_response(resp, "claude-sonnet-4-6")
    assert out.content[0]["input"] == {}


# ─── Phase-1 unsupported-feature gate ────────────────────────────


def test_tool_request_blocked_in_phase_1() -> None:
    req = AnthropicMessagesRequest(
        model="claude-sonnet-4-6",
        max_tokens=100,
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"name": "x", "input_schema": {}}],
    )
    reason = request_unsupported_for_phase_1(req)
    assert reason is not None
    assert "tool" in reason.lower()


def test_tool_use_in_message_blocked_in_phase_1() -> None:
    req = AnthropicMessagesRequest(
        model="claude-sonnet-4-6",
        max_tokens=100,
        messages=[
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "x", "name": "f", "input": {}}
                ],
            }
        ],
    )
    reason = request_unsupported_for_phase_1(req)
    assert reason is not None
    assert "tool" in reason.lower()


def test_image_in_message_blocked_in_phase_1() -> None:
    req = AnthropicMessagesRequest(
        model="claude-sonnet-4-6",
        max_tokens=100,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "x"}}
                ],
            }
        ],
    )
    reason = request_unsupported_for_phase_1(req)
    assert reason is not None
    assert "image" in reason.lower()


def test_plain_chat_passes_phase_1_gate() -> None:
    req = AnthropicMessagesRequest(
        model="claude-sonnet-4-6",
        max_tokens=100,
        system="You are helpful.",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert request_unsupported_for_phase_1(req) is None


def test_streaming_request_now_passes_phase_2_gate() -> None:
    """Phase 2 ships streaming. The gate should now allow stream=true."""
    req = AnthropicMessagesRequest(
        model="claude-sonnet-4-6",
        max_tokens=100,
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
    )
    assert request_unsupported_for_phase_1(req) is None


# ─── Phase 2: streaming SSE translation ──────────────────────────


import json as _json  # noqa: E402

from freeride.core.anthropic_translate import stream_openai_to_anthropic  # noqa: E402
from freeride.core.chat_schema import (  # noqa: E402
    ChatStreamEvent,
    StreamChoice,
    StreamDelta,
)


def _chunk(
    *,
    content: str | None = None,
    finish_reason: str | None = None,
    usage: Usage | None = None,
    role_only: bool = False,
) -> ChatStreamEvent:
    """Build a synthetic OpenAI streaming chunk for the translator."""
    delta = StreamDelta(role="assistant" if role_only else None, content=content)
    return ChatStreamEvent(
        id="chatcmpl-stub",
        created=1234567890,
        model="actual-resolved",
        choices=[StreamChoice(index=0, delta=delta, finish_reason=finish_reason)],
        usage=usage,
    )


async def _collect_sse_events(chunks_list: list[ChatStreamEvent], request_model: str = "claude-sonnet-4-6") -> list[tuple[str, dict]]:
    """Run the translator over a list of synthetic chunks and return
    [(event_name, payload_dict), ...] for assertion. Bytes are parsed
    back so assertions are on semantic content, not wire bytes."""

    async def chunks_iter():
        for c in chunks_list:
            yield c

    parsed: list[tuple[str, dict]] = []
    async for raw in stream_openai_to_anthropic(
        chunks_iter(), request_model=request_model
    ):
        text = raw.decode("utf-8")
        # Each emitted block is "event: <name>\ndata: <json>\n\n"
        # so we pull both lines deterministically.
        lines = text.strip().split("\n")
        assert lines[0].startswith("event: "), f"bad SSE block: {text!r}"
        assert lines[1].startswith("data: "), f"bad SSE block: {text!r}"
        event_name = lines[0][len("event: "):]
        data = _json.loads(lines[1][len("data: "):])
        parsed.append((event_name, data))
    return parsed


@pytest.mark.asyncio
async def test_stream_emits_correct_event_sequence() -> None:
    """The minimum viable stream: role-only opener, two content
    chunks, finish_reason on the third. Should emit:
      message_start → content_block_start → content_block_delta(s)
      → content_block_stop → message_delta → message_stop
    """
    chunks = [
        _chunk(role_only=True),
        _chunk(content="Hello"),
        _chunk(content=" world"),
        _chunk(content=None, finish_reason="stop"),
    ]
    events = await _collect_sse_events(chunks)
    names = [e[0] for e in events]
    assert names == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]


@pytest.mark.asyncio
async def test_stream_message_start_carries_request_model() -> None:
    """The model echoed in message_start.message.model must be the
    REQUESTED model (e.g. claude-sonnet-4-6), NOT the actual routed
    free-tier model. SDK clients see a familiar string."""
    chunks = [
        _chunk(content="hi"),
        _chunk(content=None, finish_reason="stop"),
    ]
    events = await _collect_sse_events(chunks, request_model="claude-sonnet-4-6")
    name, payload = events[0]
    assert name == "message_start"
    assert payload["message"]["model"] == "claude-sonnet-4-6"
    assert payload["message"]["id"].startswith("msg_")
    assert payload["message"]["role"] == "assistant"
    assert payload["message"]["type"] == "message"
    assert payload["message"]["stop_reason"] is None


@pytest.mark.asyncio
async def test_stream_content_block_start_is_text_block_at_index_zero() -> None:
    chunks = [
        _chunk(content="x"),
        _chunk(content=None, finish_reason="stop"),
    ]
    events = await _collect_sse_events(chunks)
    name, payload = events[1]
    assert name == "content_block_start"
    assert payload == {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "text", "text": ""},
    }


@pytest.mark.asyncio
async def test_stream_content_deltas_are_text_delta_subtype() -> None:
    chunks = [
        _chunk(content="alpha"),
        _chunk(content="beta"),
        _chunk(content=None, finish_reason="stop"),
    ]
    events = await _collect_sse_events(chunks)
    deltas = [e for e in events if e[0] == "content_block_delta"]
    assert len(deltas) == 2
    for _, payload in deltas:
        assert payload["index"] == 0
        assert payload["delta"]["type"] == "text_delta"
    assert deltas[0][1]["delta"]["text"] == "alpha"
    assert deltas[1][1]["delta"]["text"] == "beta"


@pytest.mark.asyncio
async def test_stream_message_delta_carries_stop_reason_and_usage() -> None:
    chunks = [
        _chunk(content="x"),
        _chunk(content=None, finish_reason="stop"),
        # NIM-style: usage on a final empty-choices chunk.
        ChatStreamEvent(
            id="x",
            created=0,
            model="m",
            choices=[],
            usage=Usage(prompt_tokens=12, completion_tokens=3, total_tokens=15),
        ),
    ]
    events = await _collect_sse_events(chunks)
    md = next(p for n, p in events if n == "message_delta")
    assert md["delta"]["stop_reason"] == "end_turn"  # mapped from "stop"
    assert md["usage"]["input_tokens"] == 12
    assert md["usage"]["output_tokens"] == 3


@pytest.mark.asyncio
async def test_stream_max_tokens_finish_maps_correctly() -> None:
    chunks = [
        _chunk(content="truncated"),
        _chunk(content=None, finish_reason="length"),
    ]
    events = await _collect_sse_events(chunks)
    md = next(p for n, p in events if n == "message_delta")
    assert md["delta"]["stop_reason"] == "max_tokens"


@pytest.mark.asyncio
async def test_stream_skips_role_only_chunks_without_emitting_empty_delta() -> None:
    """A role-only opener (delta.role='assistant', no content) must
    NOT emit a content_block_delta — that would be a zero-length
    text_delta which Anthropic clients accept but is wasted bytes."""
    chunks = [
        _chunk(role_only=True),  # role only, no content
        _chunk(content="real text"),
        _chunk(content=None, finish_reason="stop"),
    ]
    events = await _collect_sse_events(chunks)
    deltas = [e for e in events if e[0] == "content_block_delta"]
    assert len(deltas) == 1
    assert deltas[0][1]["delta"]["text"] == "real text"


@pytest.mark.asyncio
async def test_stream_handles_no_chunks_at_all() -> None:
    """Edge case: provider returns zero useful chunks. We must still
    emit the framing events so the client sees a complete (empty)
    Anthropic message."""
    events = await _collect_sse_events([])
    names = [e[0] for e in events]
    assert names == [
        "message_start",
        "content_block_start",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    md = next(p for n, p in events if n == "message_delta")
    assert md["delta"]["stop_reason"] == "end_turn"  # safe default


@pytest.mark.asyncio
async def test_stream_message_stop_is_terminal() -> None:
    """No further events after message_stop. Anthropic's SSE format
    relies on the client treating message_stop as the terminator
    (no [DONE] sentinel like OpenAI)."""
    chunks = [
        _chunk(content="x"),
        _chunk(content=None, finish_reason="stop"),
    ]
    events = await _collect_sse_events(chunks)
    assert events[-1][0] == "message_stop"
    assert events[-1][1] == {"type": "message_stop"}


@pytest.mark.asyncio
async def test_stream_event_format_has_event_and_data_lines() -> None:
    """Verify wire bytes specifically — Anthropic's SSE format requires
    BOTH 'event: <name>' AND 'data: <json>' on each event, separated
    by '\\n\\n'. OpenAI's bare 'data:' lines are NOT what we emit."""

    async def chunks_iter():
        yield _chunk(content="hi")
        yield _chunk(content=None, finish_reason="stop")

    raw = b""
    async for piece in stream_openai_to_anthropic(
        chunks_iter(), request_model="claude-sonnet-4-6"
    ):
        raw += piece

    text = raw.decode("utf-8")
    # First event in the stream is message_start
    assert text.startswith("event: message_start\n")
    # Each event ends with two newlines
    assert "\n\ndata:" not in text  # ensures every data: follows an event:
    # The terminator is message_stop
    assert "event: message_stop\ndata:" in text
    assert text.rstrip().endswith('"type":"message_stop"}')
