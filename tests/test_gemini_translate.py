"""Tests for the Gemini ↔ OpenAI translator.

Schema parsing is permissive (`extra="allow"`) and uses Pydantic's
to_camel alias so real Google clients hit it untouched. Each test
exercises one of the translation seams: messages flattening, tool
defs, generation config, finish reasons, usage rollups, streaming
deltas.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from freeride.core.chat_schema import (
    ChatResponse,
    Choice,
    ChoiceMessage,
    ChatStreamEvent,
    StreamChoice,
    StreamDelta,
    ToolCall,
    ToolCallFunction,
    Usage,
)
from freeride.core.gemini_schema import GeminiGenerateRequest
from freeride.core.gemini_translate import (
    gemini_to_openai_request,
    openai_to_gemini_response,
    stream_openai_to_gemini,
)


# ─── request: Gemini → OpenAI ──────────────────────────────────────


def test_simple_user_text_becomes_user_message() -> None:
    req = GeminiGenerateRequest.model_validate(
        {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]}
    )
    out = gemini_to_openai_request(req, "gemini-2.0-flash")
    assert out.model == "gemini-2.0-flash"
    assert len(out.messages) == 1
    assert out.messages[0].role == "user"
    assert out.messages[0].content == "hello"


def test_system_instruction_becomes_leading_system_message() -> None:
    req = GeminiGenerateRequest.model_validate(
        {
            "systemInstruction": {"parts": [{"text": "You are terse."}]},
            "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
        }
    )
    out = gemini_to_openai_request(req, "m")
    assert out.messages[0].role == "system"
    assert out.messages[0].content == "You are terse."
    assert out.messages[1].role == "user"


def test_model_turn_with_text_and_function_call_becomes_assistant_with_tool_calls() -> None:
    """A single model Content can carry both text AND function calls.
    OpenAI's assistant message also supports both — keep them in one
    message so the upstream provider sees the full turn."""
    req = GeminiGenerateRequest.model_validate(
        {
            "contents": [
                {"role": "user", "parts": [{"text": "create test.py"}]},
                {
                    "role": "model",
                    "parts": [
                        {"text": "I'll do that."},
                        {
                            "functionCall": {
                                "name": "Write",
                                "args": {"path": "test.py", "content": "print('hi')"},
                            }
                        },
                    ],
                },
            ]
        }
    )
    out = gemini_to_openai_request(req, "m")
    assistant = out.messages[1]
    assert assistant.role == "assistant"
    assert assistant.content == "I'll do that."
    assert assistant.tool_calls is not None
    assert len(assistant.tool_calls) == 1
    tc = assistant.tool_calls[0]
    assert tc.function.name == "Write"
    # args round-trips through JSON encoding — OpenAI's arguments field
    # is a JSON-encoded STRING, not a dict.
    assert json.loads(tc.function.arguments) == {"path": "test.py", "content": "print('hi')"}


def test_function_response_part_under_user_role_becomes_tool_message() -> None:
    """Newer Google convention: tool results land under role=user with
    a FunctionResponse part. OpenAI represents this as role=tool."""
    req = GeminiGenerateRequest.model_validate(
        {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"functionResponse": {"name": "Write", "response": {"ok": True}}}
                    ],
                }
            ]
        }
    )
    out = gemini_to_openai_request(req, "m")
    assert len(out.messages) == 1
    assert out.messages[0].role == "tool"
    assert out.messages[0].tool_call_id == "Write"
    assert json.loads(out.messages[0].content) == {"ok": True}


def test_legacy_function_role_each_part_becomes_tool_message() -> None:
    """Older Google clients used role=function for tool results.
    Each FunctionResponse part splits into its own tool message."""
    req = GeminiGenerateRequest.model_validate(
        {
            "contents": [
                {
                    "role": "function",
                    "parts": [
                        {"functionResponse": {"name": "A", "response": {"x": 1}}},
                        {"functionResponse": {"name": "B", "response": {"y": 2}}},
                    ],
                }
            ]
        }
    )
    out = gemini_to_openai_request(req, "m")
    assert len(out.messages) == 2
    assert {m.tool_call_id for m in out.messages} == {"A", "B"}


def test_tools_flatten_function_declarations() -> None:
    """Google nests tool defs under functionDeclarations[]; OpenAI is
    flat. Multiple Tools each with multiple declarations all merge."""
    req = GeminiGenerateRequest.model_validate(
        {
            "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
            "tools": [
                {
                    "functionDeclarations": [
                        {"name": "A", "parameters": {"type": "object"}},
                        {"name": "B", "parameters": {"type": "object"}},
                    ]
                },
                {"functionDeclarations": [{"name": "C", "parameters": {"type": "object"}}]},
            ],
        }
    )
    out = gemini_to_openai_request(req, "m")
    assert out.tools is not None
    assert [t.function.name for t in out.tools] == ["A", "B", "C"]
    assert all(t.type == "function" for t in out.tools)


@pytest.mark.parametrize(
    "google_mode,openai_choice",
    [
        ("AUTO", "auto"),
        ("auto", "auto"),  # case-insensitive
        ("NONE", "none"),
        ("ANY", "required"),  # ANY = "must call a tool"
    ],
)
def test_function_calling_mode_translates(google_mode: str, openai_choice: str) -> None:
    req = GeminiGenerateRequest.model_validate(
        {
            "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
            "toolConfig": {"functionCallingConfig": {"mode": google_mode}},
        }
    )
    out = gemini_to_openai_request(req, "m")
    assert out.tool_choice == openai_choice


def test_generation_config_fields_translate() -> None:
    req = GeminiGenerateRequest.model_validate(
        {
            "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
            "generationConfig": {
                "maxOutputTokens": 1024,
                "temperature": 0.5,
                "topP": 0.9,
                "topK": 40,  # no OpenAI equivalent — dropped
                "stopSequences": ["END"],
                "responseMimeType": "application/json",
            },
        }
    )
    out = gemini_to_openai_request(req, "m")
    assert out.max_tokens == 1024
    assert out.temperature == 0.5
    assert out.top_p == 0.9
    assert out.stop == ["END"]
    # response_format is parsed into a Pydantic ResponseFormat model;
    # we care that the type is json_object.
    assert out.response_format is not None
    assert out.response_format.type == "json_object"
    # top_k must be absent (dropped) — OpenAI 400s on unknown args.
    assert not hasattr(out, "top_k") or out.model_extra is None or "top_k" not in (out.model_extra or {})


def test_empty_generation_config_omits_optional_fields() -> None:
    req = GeminiGenerateRequest.model_validate(
        {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}
    )
    out = gemini_to_openai_request(req, "m")
    # Defaults: ChatRequest doesn't carry these unless set
    assert out.max_tokens is None
    assert out.temperature is None
    assert out.tools is None


# ─── response: OpenAI → Gemini ─────────────────────────────────────


def _make_openai_response(
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
        model="resolved-model",
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


def test_text_response_becomes_one_candidate_with_text_part() -> None:
    resp = _make_openai_response(content="Hello.")
    out = openai_to_gemini_response(resp, "gemini-2.0-flash")
    assert len(out.candidates) == 1
    cand = out.candidates[0]
    assert cand.content.role == "model"
    assert len(cand.content.parts) == 1
    assert cand.content.parts[0].text == "Hello."
    assert cand.finish_reason == "STOP"


def test_response_echoes_requested_model_not_resolved_one() -> None:
    """Like /v1/messages, gemini callers expect to see the model id
    they asked for, not whatever free-tier we routed to."""
    resp = _make_openai_response(content="hi")
    out = openai_to_gemini_response(resp, "gemini-2.0-flash")
    assert out.model_version == "gemini-2.0-flash"


def test_tool_call_response_becomes_function_call_part() -> None:
    """OpenAI's JSON-encoded arguments string becomes Google's
    structured args object."""
    resp = _make_openai_response(
        content=None,
        tool_calls=[
            ToolCall(
                id="call_1",
                type="function",
                function=ToolCallFunction(name="Write", arguments='{"path": "x.py"}'),
            )
        ],
        finish_reason="tool_calls",
    )
    out = openai_to_gemini_response(resp, "m")
    cand = out.candidates[0]
    assert len(cand.content.parts) == 1
    fc = cand.content.parts[0].function_call
    assert fc is not None
    assert fc.name == "Write"
    assert fc.args == {"path": "x.py"}
    assert cand.finish_reason == "TOOL_CALL"


def test_text_and_tool_call_emit_both_parts_in_order() -> None:
    """OpenAI assistant message can have content AND tool_calls.
    Google convention puts text first, function calls after."""
    resp = _make_openai_response(
        content="Let me do that.",
        tool_calls=[
            ToolCall(
                id="c",
                type="function",
                function=ToolCallFunction(name="Write", arguments='{}'),
            )
        ],
        finish_reason="tool_calls",
    )
    out = openai_to_gemini_response(resp, "m")
    parts = out.candidates[0].content.parts
    assert len(parts) == 2
    assert parts[0].text == "Let me do that."
    assert parts[1].function_call is not None


def test_malformed_tool_args_yields_empty_dict() -> None:
    """Some Llama-class models emit invalid JSON in arguments. We'd
    rather emit empty args than 500 the response."""
    resp = _make_openai_response(
        content=None,
        tool_calls=[
            ToolCall(
                id="c",
                type="function",
                function=ToolCallFunction(name="X", arguments="not json"),
            )
        ],
        finish_reason="tool_calls",
    )
    out = openai_to_gemini_response(resp, "m")
    assert out.candidates[0].content.parts[0].function_call.args == {}


def test_empty_response_synthesizes_zero_length_text_part() -> None:
    """Google requires non-empty parts[]. If OpenAI returns no content
    and no tool_calls (rare — finish_reason fires immediately) we emit
    a zero-length text part."""
    resp = _make_openai_response(content=None, finish_reason="stop")
    out = openai_to_gemini_response(resp, "m")
    assert out.candidates[0].content.parts == [
        out.candidates[0].content.parts[0]  # exactly one zero-length text part
    ]
    assert out.candidates[0].content.parts[0].text == ""


def test_usage_translates() -> None:
    resp = _make_openai_response(content="hi", prompt_tokens=42, completion_tokens=7)
    out = openai_to_gemini_response(resp, "m")
    assert out.usage_metadata is not None
    assert out.usage_metadata.prompt_token_count == 42
    assert out.usage_metadata.candidates_token_count == 7
    assert out.usage_metadata.total_token_count == 49


@pytest.mark.parametrize(
    "openai_reason,gemini_reason",
    [
        ("stop", "STOP"),
        ("length", "MAX_TOKENS"),
        ("tool_calls", "TOOL_CALL"),
        ("content_filter", "SAFETY"),
        ("unexpected_thing", "OTHER"),
    ],
)
def test_finish_reason_mapping(openai_reason: str, gemini_reason: str) -> None:
    resp = _make_openai_response(content="x", finish_reason=openai_reason)
    out = openai_to_gemini_response(resp, "m")
    assert out.candidates[0].finish_reason == gemini_reason


def test_no_choices_yields_empty_candidates() -> None:
    """Edge case: provider returned no choices at all. Empty list is
    valid in Google's spec (a candidate with no parts is NOT)."""
    resp = ChatResponse(id="x", created=0, model="m", choices=[])
    out = openai_to_gemini_response(resp, "m")
    assert out.candidates == []


# ─── streaming ─────────────────────────────────────────────────────


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


async def _collect_stream(chunks_list: list[ChatStreamEvent]) -> list[dict[str, Any]]:
    """Run the translator and parse each SSE event back into a dict."""

    async def gen():
        for c in chunks_list:
            yield c

    out: list[dict[str, Any]] = []
    async for raw in stream_openai_to_gemini(gen(), request_model="m"):
        text = raw.decode("utf-8")
        # "data: <json>\n\n"
        assert text.startswith("data: "), repr(text)
        out.append(json.loads(text[len("data: ") :].strip()))
    return out


@pytest.mark.asyncio
async def test_stream_text_deltas_become_separate_sse_events() -> None:
    """Each OpenAI text delta produces one Gemini SSE event with that
    text in candidates[0].content.parts[0].text. The final event
    carries finishReason and a candidate with no new parts."""
    events = await _collect_stream(
        [
            _stream_chunk(content="Hello"),
            _stream_chunk(content=" world"),
            _stream_chunk(content=None, finish_reason="stop"),
        ]
    )
    # 2 text events + 1 final event
    assert len(events) == 3
    assert events[0]["candidates"][0]["content"]["parts"] == [{"text": "Hello"}]
    assert events[1]["candidates"][0]["content"]["parts"] == [{"text": " world"}]
    final = events[-1]
    assert final["candidates"][0]["finishReason"] == "STOP"
    assert final["candidates"][0]["content"]["parts"] == []  # no new content on terminator


@pytest.mark.asyncio
async def test_stream_tool_call_buffered_until_args_parse() -> None:
    """OpenAI streams tool_call args as partial JSON. We buffer until
    json.loads succeeds, then emit ONE Gemini functionCall event."""
    events = await _collect_stream(
        [
            _stream_chunk(
                tool_calls=[
                    {
                        "index": 0,
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "Write", "arguments": '{"path": "'},
                    }
                ]
            ),
            _stream_chunk(
                tool_calls=[{"index": 0, "function": {"arguments": 'x.py"}'}}]
            ),
            _stream_chunk(content=None, finish_reason="tool_calls"),
        ]
    )
    # Find the one event that carries the functionCall
    fc_events = [
        e
        for e in events
        if e["candidates"][0]["content"]["parts"]
        and "functionCall" in e["candidates"][0]["content"]["parts"][0]
    ]
    assert len(fc_events) == 1
    fc = fc_events[0]["candidates"][0]["content"]["parts"][0]["functionCall"]
    assert fc["name"] == "Write"
    assert fc["args"] == {"path": "x.py"}
    # Final event still includes finishReason
    final = events[-1]
    assert final["candidates"][0]["finishReason"] == "TOOL_CALL"


@pytest.mark.asyncio
async def test_stream_usage_lands_on_final_event() -> None:
    events = await _collect_stream(
        [
            _stream_chunk(content="x"),
            _stream_chunk(
                content=None,
                finish_reason="stop",
                usage=Usage(prompt_tokens=12, completion_tokens=3, total_tokens=15),
            ),
        ]
    )
    final = events[-1]
    assert final["usageMetadata"] == {
        "promptTokenCount": 12,
        "candidatesTokenCount": 3,
        "totalTokenCount": 15,
    }


@pytest.mark.asyncio
async def test_stream_request_model_echoed_on_every_event() -> None:
    """The CLI parses each chunk individually; modelVersion must be on
    every event so it doesn't get a "model: undefined" UI later."""
    events = await _collect_stream(
        [_stream_chunk(content="hi"), _stream_chunk(content=None, finish_reason="stop")]
    )
    assert all(e["modelVersion"] == "m" for e in events)
