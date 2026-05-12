"""Tests for the Anthropic Messages ↔ OpenAI translation layer.

Phase 1 scope: non-streaming chat. Schema parsing (lenient on input,
strict on output), system-prompt hoisting, content-block flattening,
stop-reason and usage mapping, request-validity gating for features
that aren't shipped yet (streaming / tool_use / images all
expected to gate cleanly).
"""

from __future__ import annotations

from typing import Any

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


def test_response_reasoning_field_becomes_thinking_block() -> None:
    """OpenRouter returns model's chain-of-thought in
    `choices[0].message.reasoning` (separate from `content`). Surface
    it as an Anthropic thinking block so Claude Code renders it
    dimmed/collapsed rather than as a user-facing text bullet."""
    resp = ChatResponse(
        id="chatcmpl-r",
        created=0,
        model="poolside/laguna-xs.2-20260421:free",
        choices=[
            Choice(
                index=0,
                message=ChoiceMessage(
                    role="assistant",
                    content="The file has been created.",
                    reasoning="Let me confirm this to the user.",  # type: ignore[call-arg]
                ),
                finish_reason="stop",
            )
        ],
    )
    out = openai_to_anthropic_response(resp, "claude-sonnet-4-6")
    assert out.content == [
        {
            "type": "thinking",
            "thinking": "Let me confirm this to the user.",
            "signature": "",
        },
        {"type": "text", "text": "The file has been created."},
    ]


def test_response_reasoning_content_alias_becomes_thinking_block() -> None:
    """vLLM and NIM use `reasoning_content` instead of `reasoning`."""
    resp = ChatResponse(
        id="chatcmpl-r",
        created=0,
        model="m",
        choices=[
            Choice(
                index=0,
                message=ChoiceMessage(
                    role="assistant",
                    content="Done.",
                    reasoning_content="I figured out X.",  # type: ignore[call-arg]
                ),
                finish_reason="stop",
            )
        ],
    )
    out = openai_to_anthropic_response(resp, "claude-sonnet-4-6")
    assert out.content[0]["type"] == "thinking"
    assert out.content[0]["thinking"] == "I figured out X."


def test_response_empty_reasoning_omitted() -> None:
    """Empty or whitespace-only reasoning must not produce an empty
    thinking block (would render as a blank dimmed bullet)."""
    resp = ChatResponse(
        id="chatcmpl-r",
        created=0,
        model="m",
        choices=[
            Choice(
                index=0,
                message=ChoiceMessage(
                    role="assistant",
                    content="Hello",
                    reasoning="   ",  # type: ignore[call-arg]
                ),
                finish_reason="stop",
            )
        ],
    )
    out = openai_to_anthropic_response(resp, "claude-sonnet-4-6")
    assert out.content == [{"type": "text", "text": "Hello"}]


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


def test_tool_request_passes_phase_3_gate() -> None:
    """Phase 3 ships tool use — requests carrying a ``tools`` array
    must no longer be rejected at the gate."""
    req = AnthropicMessagesRequest(
        model="claude-sonnet-4-6",
        max_tokens=100,
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"name": "x", "input_schema": {}}],
    )
    assert request_unsupported_for_phase_1(req) is None


def test_tool_use_in_message_passes_phase_3_gate() -> None:
    """Phase 3: assistant messages with ``tool_use`` blocks must pass
    the gate so they can be translated into OpenAI ``tool_calls``."""
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
    assert request_unsupported_for_phase_1(req) is None


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
    reasoning: str | None = None,
) -> ChatStreamEvent:
    """Build a synthetic OpenAI streaming chunk for the translator."""
    extra: dict[str, Any] = {}
    if reasoning is not None:
        extra["reasoning"] = reasoning
    delta = StreamDelta(
        role="assistant" if role_only else None,
        content=content,
        **extra,
    )
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
async def test_stream_reasoning_opens_thinking_block_before_text() -> None:
    """When `delta.reasoning` arrives before `delta.content`, the
    translator opens a thinking content block at index 0, streams
    thinking_delta events into it, closes it, then opens a separate
    text block at index 1. Claude Code uses this to render reasoning
    dimmed and the answer normally."""
    chunks = [
        _chunk(role_only=True),
        _chunk(reasoning="Let me think..."),
        _chunk(reasoning=" about this."),
        _chunk(content="Here's the answer."),
        _chunk(content=None, finish_reason="stop"),
    ]
    events = await _collect_sse_events(chunks)
    names = [e[0] for e in events]
    assert names == [
        "message_start",
        "content_block_start",   # thinking block opens at index 0
        "content_block_delta",
        "content_block_delta",
        "content_block_stop",    # thinking closes before text opens
        "content_block_start",   # text block opens at index 1
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    # Thinking block shape
    start_thinking = events[1][1]
    assert start_thinking["index"] == 0
    assert start_thinking["content_block"] == {"type": "thinking", "thinking": ""}
    # Thinking deltas use thinking_delta subtype, not text_delta
    assert events[2][1]["delta"] == {"type": "thinking_delta", "thinking": "Let me think..."}
    assert events[3][1]["delta"] == {"type": "thinking_delta", "thinking": " about this."}
    # Text block opens at index 1
    start_text = events[5][1]
    assert start_text["index"] == 1
    assert start_text["content_block"] == {"type": "text", "text": ""}
    assert events[6][1]["delta"] == {"type": "text_delta", "text": "Here's the answer."}


@pytest.mark.asyncio
async def test_stream_reasoning_only_no_text() -> None:
    """Some models stream only reasoning then finish without content
    (e.g. forced empty answer). The thinking block must still close
    cleanly and we must not synthesize an empty text block."""
    chunks = [
        _chunk(reasoning="Thinking out loud."),
        _chunk(content=None, finish_reason="stop"),
    ]
    events = await _collect_sse_events(chunks)
    names = [e[0] for e in events]
    assert names == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    assert events[1][1]["content_block"]["type"] == "thinking"


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


# ─── Phase 3: tool use translation ──────────────────────────────


def test_translate_assistant_tool_use_becomes_openai_tool_calls() -> None:
    """A single Anthropic assistant message carrying one ``tool_use``
    block must translate to one OpenAI assistant message with one
    ``tool_calls`` entry whose ``function.arguments`` is a JSON string
    of the original ``input`` object."""
    req = AnthropicMessagesRequest(
        model="claude-sonnet-4-6",
        max_tokens=100,
        messages=[
            {"role": "user", "content": "what's the weather in Berlin?"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "get_weather",
                        "input": {"city": "Berlin", "units": "metric"},
                    }
                ],
            },
        ],
    )
    out = anthropic_to_openai_request(req)
    assert len(out.messages) == 2
    asst = out.messages[1]
    assert asst.role == "assistant"
    assert asst.tool_calls is not None
    assert len(asst.tool_calls) == 1
    tc = asst.tool_calls[0]
    assert tc.id == "toolu_1"
    assert tc.type == "function"
    assert tc.function.name == "get_weather"
    parsed = _json.loads(tc.function.arguments)
    assert parsed == {"city": "Berlin", "units": "metric"}


def test_translate_assistant_text_plus_tool_use_keeps_both() -> None:
    """Assistant message that mixes a leading ``text`` block with a
    trailing ``tool_use`` must become a single OpenAI message with both
    ``content`` set and ``tool_calls`` populated — the OpenAI shape that
    most providers expect for ``finish_reason=tool_calls``."""
    req = AnthropicMessagesRequest(
        model="claude-sonnet-4-6",
        max_tokens=100,
        messages=[
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Let me look that up."},
                    {
                        "type": "tool_use",
                        "id": "toolu_a",
                        "name": "search",
                        "input": {"q": "freeride"},
                    },
                ],
            }
        ],
    )
    out = anthropic_to_openai_request(req)
    assert len(out.messages) == 1
    asst = out.messages[0]
    assert asst.role == "assistant"
    assert asst.content == "Let me look that up."
    assert asst.tool_calls is not None and len(asst.tool_calls) == 1


def test_translate_user_tool_result_becomes_role_tool_message() -> None:
    """A user message with a single ``tool_result`` block must produce
    one OpenAI ``role: tool`` message carrying the ``tool_use_id`` as
    ``tool_call_id`` and the inner content as a string."""
    req = AnthropicMessagesRequest(
        model="claude-sonnet-4-6",
        max_tokens=100,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": "22°C, sunny",
                    }
                ],
            }
        ],
    )
    out = anthropic_to_openai_request(req)
    assert len(out.messages) == 1
    tool_msg = out.messages[0]
    assert tool_msg.role == "tool"
    assert tool_msg.tool_call_id == "toolu_1"
    assert tool_msg.content == "22°C, sunny"


def test_translate_user_tool_result_list_content_flattens_to_string() -> None:
    """Anthropic accepts ``tool_result.content`` as a list of
    ``{type:'text',text:...}`` blocks. The translator must flatten that
    to a single string for OpenAI."""
    req = AnthropicMessagesRequest(
        model="claude-sonnet-4-6",
        max_tokens=100,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_2",
                        "content": [
                            {"type": "text", "text": "line one"},
                            {"type": "text", "text": "line two"},
                        ],
                    }
                ],
            }
        ],
    )
    out = anthropic_to_openai_request(req)
    assert out.messages[0].role == "tool"
    assert out.messages[0].content == "line one\nline two"


def test_translate_user_text_plus_tool_result_emits_two_messages_in_order() -> None:
    """A user turn that carries BOTH a ``tool_result`` and follow-up
    ``text`` must emit the tool message FIRST, then the user text —
    OpenAI's chat history convention is that tool results precede the
    next user turn so the model sees results before the question."""
    req = AnthropicMessagesRequest(
        model="claude-sonnet-4-6",
        max_tokens=100,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_x",
                        "content": "42",
                    },
                    {"type": "text", "text": "anything else?"},
                ],
            }
        ],
    )
    out = anthropic_to_openai_request(req)
    assert len(out.messages) == 2
    assert out.messages[0].role == "tool"
    assert out.messages[0].tool_call_id == "toolu_x"
    assert out.messages[0].content == "42"
    assert out.messages[1].role == "user"
    assert out.messages[1].content == "anything else?"


def test_translate_multiple_tool_results_one_message_per_id() -> None:
    """Two ``tool_result`` blocks in one user message must expand to
    two ``role:tool`` messages, one per ``tool_use_id``, in source
    order."""
    req = AnthropicMessagesRequest(
        model="claude-sonnet-4-6",
        max_tokens=100,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "id_a",
                        "content": "result_a",
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "id_b",
                        "content": "result_b",
                    },
                ],
            }
        ],
    )
    out = anthropic_to_openai_request(req)
    assert len(out.messages) == 2
    assert out.messages[0].tool_call_id == "id_a"
    assert out.messages[0].content == "result_a"
    assert out.messages[1].tool_call_id == "id_b"
    assert out.messages[1].content == "result_b"


# ─── Phase 3 streaming: tool_use block flow ─────────────────────


def _tool_chunk(
    *,
    index: int = 0,
    tc_id: str | None = None,
    name: str | None = None,
    args_piece: str | None = None,
    finish_reason: str | None = None,
) -> ChatStreamEvent:
    """Build a synthetic OpenAI streaming chunk carrying a partial
    tool_call delta (the way Groq/OR/NIM stream tool args)."""
    tc: dict = {"index": index}
    if tc_id is not None:
        tc["id"] = tc_id
    if name is not None or args_piece is not None:
        tc["function"] = {}
        if name is not None:
            tc["function"]["name"] = name
        if args_piece is not None:
            tc["function"]["arguments"] = args_piece
    delta = StreamDelta(tool_calls=[tc])
    return ChatStreamEvent(
        id="chatcmpl-stub",
        created=0,
        model="resolved-model",
        choices=[StreamChoice(index=0, delta=delta, finish_reason=finish_reason)],
    )


@pytest.mark.asyncio
async def test_stream_tool_call_emits_tool_use_block_and_input_json_delta() -> None:
    """A streamed tool call must emit, in order:
      content_block_start  (tool_use shell with id+name, input={})
      content_block_delta  (input_json_delta, partial_json=<piece>) * N
      content_block_stop
    """
    chunks = [
        _tool_chunk(index=0, tc_id="toolu_1", name="get_weather", args_piece='{"city":'),
        _tool_chunk(index=0, args_piece='"Berlin"}'),
        _chunk(content=None, finish_reason="tool_calls"),
    ]
    events = await _collect_sse_events(chunks)
    names = [n for n, _ in events]
    assert names == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    start = events[1][1]
    assert start["index"] == 0
    assert start["content_block"]["type"] == "tool_use"
    assert start["content_block"]["id"] == "toolu_1"
    assert start["content_block"]["name"] == "get_weather"
    assert start["content_block"]["input"] == {}
    d0 = events[2][1]
    d1 = events[3][1]
    assert d0["delta"]["type"] == "input_json_delta"
    assert d0["delta"]["partial_json"] == '{"city":'
    assert d1["delta"]["partial_json"] == '"Berlin"}'


@pytest.mark.asyncio
async def test_stream_tool_call_stop_reason_maps_to_tool_use() -> None:
    """``finish_reason='tool_calls'`` must surface as
    ``stop_reason='tool_use'`` in the message_delta."""
    chunks = [
        _tool_chunk(index=0, tc_id="t1", name="f", args_piece="{}"),
        _chunk(content=None, finish_reason="tool_calls"),
    ]
    events = await _collect_sse_events(chunks)
    md = next(p for n, p in events if n == "message_delta")
    assert md["delta"]["stop_reason"] == "tool_use"


@pytest.mark.asyncio
async def test_stream_text_then_tool_use_transitions_block() -> None:
    """Common shape: model emits a sentence, THEN a tool call. The
    state machine must close the text block before opening the
    tool_use block, and indices must advance monotonically."""
    chunks = [
        _chunk(content="Let me check."),
        _tool_chunk(index=0, tc_id="t1", name="search", args_piece='{"q":"x"}'),
        _chunk(content=None, finish_reason="tool_calls"),
    ]
    events = await _collect_sse_events(chunks)
    names = [n for n, _ in events]
    assert names == [
        "message_start",
        "content_block_start",   # text block, index 0
        "content_block_delta",   # text_delta "Let me check."
        "content_block_stop",    # close text
        "content_block_start",   # tool_use block, index 1
        "content_block_delta",   # input_json_delta
        "content_block_stop",    # close tool_use
        "message_delta",
        "message_stop",
    ]
    text_start = events[1][1]
    tool_start = events[4][1]
    assert text_start["index"] == 0
    assert text_start["content_block"]["type"] == "text"
    assert tool_start["index"] == 1
    assert tool_start["content_block"]["type"] == "tool_use"
    assert tool_start["content_block"]["name"] == "search"


@pytest.mark.asyncio
async def test_stream_two_parallel_tool_calls_each_get_own_block() -> None:
    """If the provider streams two parallel tool calls (indices 0 and
    1), each must map to its own monotonic content-block index."""
    chunks = [
        _tool_chunk(index=0, tc_id="t0", name="a", args_piece='{"x":1}'),
        _tool_chunk(index=1, tc_id="t1", name="b", args_piece='{"y":2}'),
        _chunk(content=None, finish_reason="tool_calls"),
    ]
    events = await _collect_sse_events(chunks)
    starts = [p for n, p in events if n == "content_block_start"]
    assert len(starts) == 2
    assert starts[0]["index"] == 0
    assert starts[0]["content_block"]["name"] == "a"
    assert starts[1]["index"] == 1
    assert starts[1]["content_block"]["name"] == "b"
    # Each tool block gets exactly one stop event
    stops = [p for n, p in events if n == "content_block_stop"]
    assert [s["index"] for s in stops] == [0, 1]


@pytest.mark.asyncio
async def test_stream_tool_call_with_no_args_piece_emits_no_input_delta() -> None:
    """First chunk may carry id+name only (no arguments yet). The
    translator must open the tool_use block but NOT emit a
    zero-length input_json_delta — only emit deltas when there's
    an actual argument fragment."""
    chunks = [
        _tool_chunk(index=0, tc_id="t1", name="f"),  # no args_piece
        _tool_chunk(index=0, args_piece="{}"),
        _chunk(content=None, finish_reason="tool_calls"),
    ]
    events = await _collect_sse_events(chunks)
    deltas = [p for n, p in events if n == "content_block_delta"]
    assert len(deltas) == 1
    assert deltas[0]["delta"]["type"] == "input_json_delta"
    assert deltas[0]["delta"]["partial_json"] == "{}"
