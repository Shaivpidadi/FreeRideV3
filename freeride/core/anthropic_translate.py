"""Anthropic Messages API ↔ OpenAI Chat Completions translator.

The shape of this is straightforward in the happy path (chat in,
chat out), and gnarly in the long tail (tool use, streaming, vision,
extended thinking, prompt caching).

**Phase 1 (shipped):**
  - Non-streaming chat
  - System prompt hoisting (Anthropic top-level → OpenAI first
    ``system`` message)
  - Text-only content blocks
  - stop_reason mapping
  - usage mapping
  - Tool definitions translate (request side); tool_use in responses
    surfaces correctly when present.

**Phase 2 (this commit):**
  - Streaming SSE: consume OpenAI streaming chunks, emit Anthropic
    SSE events (message_start / content_block_start /
    content_block_delta / content_block_stop / message_delta /
    message_stop). Text-only blocks for now; tool_use streaming
    lands in Phase 3.

**Phase 3 (deferred):**
  - tool_use blocks in messages, tool_result handling, the
    ``input_json_delta`` partial-JSON streaming state machine.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, AsyncIterator

from freeride.core.anthropic_schema import (
    AnthropicMessagesRequest,
    AnthropicMessagesResponse,
    AnthropicStopReason,
    AnthropicUsage,
)
from freeride.core.chat_schema import (
    ChatRequest,
    ChatResponse,
    Message,
    ToolDef,
    ToolFunctionDef,
)


# ─── stop reason mapping ────────────────────────────────────────────


_OPENAI_TO_ANTHROPIC_STOP: dict[str, AnthropicStopReason] = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "refusal",
    "function_call": "tool_use",  # legacy
}


def map_stop_reason(openai_finish: str | None) -> AnthropicStopReason:
    """OpenAI ``finish_reason`` → Anthropic ``stop_reason``. Unknown
    or missing values fall back to ``end_turn`` (the safe default —
    means "model finished naturally").
    """
    if not openai_finish:
        return "end_turn"
    return _OPENAI_TO_ANTHROPIC_STOP.get(openai_finish, "end_turn")


# ─── content / messages translation ─────────────────────────────────


class UnsupportedContentBlock(Exception):
    """Raised when an Anthropic content block can't be represented in
    OpenAI Chat Completions and we'd rather 400 the caller than
    silently drop data. Document blocks, server-side tool results,
    etc. fall here.
    """


def _flatten_anthropic_content(content: str | list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Walk a single Anthropic ``MessageParam.content`` (string or list
    of blocks) and return:

        (text_content, tool_use_or_result_blocks)

    Phase 1 only routes the text content; tool blocks are returned but
    the caller currently rejects messages that contain them.
    """
    if isinstance(content, str):
        return content, []

    text_parts: list[str] = []
    tool_blocks: list[dict[str, Any]] = []
    for block in content:
        btype = block.get("type")
        if btype == "text":
            t = block.get("text") or ""
            if t:
                text_parts.append(t)
        elif btype in ("tool_use", "tool_result"):
            tool_blocks.append(block)
        elif btype == "image":
            raise UnsupportedContentBlock(
                "image content blocks are deferred to a later phase"
            )
        elif btype in ("document", "search_result", "container_upload") or (
            isinstance(btype, str) and btype.endswith("_tool_result")
        ):
            raise UnsupportedContentBlock(
                f"content block type {btype!r} is not supported by FreeRide "
                "(requires Anthropic-side infra we can't replicate)"
            )
        elif btype in ("thinking", "redacted_thinking"):
            # Drop — free models can't reason about extended thinking
            # blocks, and emitting them in input would just confuse the
            # underlying model. Logged at the route level.
            continue
        else:
            # Unknown block type — be permissive, just skip. New
            # Anthropic features shouldn't break us until we've
            # implemented them.
            continue
    return "\n".join(text_parts), tool_blocks


def _hoist_system(
    system: str | list[dict[str, Any]] | None,
) -> Message | None:
    """Anthropic's top-level ``system`` (string OR list of TextBlocks)
    becomes a leading OpenAI ``role: system`` message. Empty / null
    returns None (caller skips).
    """
    if system is None:
        return None
    if isinstance(system, str):
        text = system.strip()
        return Message(role="system", content=text) if text else None
    # list of dicts — concatenate the text fields
    parts: list[str] = []
    for block in system:
        if isinstance(block, dict) and block.get("type") == "text":
            t = block.get("text") or ""
            if t:
                parts.append(t)
    joined = "\n".join(parts).strip()
    return Message(role="system", content=joined) if joined else None


def _translate_messages(
    anthropic_messages: list[Any],
) -> list[Message]:
    """Translate Anthropic message array → OpenAI Chat Completions
    messages. Phase 1 handles text only; tool use is a Phase 3 concern.
    """
    out: list[Message] = []
    for m in anthropic_messages:
        # AnthropicMessage Pydantic instance OR dict — be permissive.
        role = m.role if hasattr(m, "role") else m["role"]
        content = m.content if hasattr(m, "content") else m["content"]
        text, tool_blocks = _flatten_anthropic_content(content)
        if tool_blocks:
            raise UnsupportedContentBlock(
                "tool_use / tool_result blocks are deferred to Phase 3"
            )
        # OpenAI roles for a normal chat: 'user' or 'assistant' (we
        # don't translate to 'tool' until Phase 3).
        out.append(Message(role=role, content=text))
    return out


# ─── tool definitions ───────────────────────────────────────────────


def _translate_tool_definitions(tools: list[Any] | None) -> list[ToolDef] | None:
    """Anthropic tool definitions → OpenAI function definitions.

    Phase 1 supports the request-side mapping (so a model that picks a
    function gets the right schema). The response-side mapping
    (tool_use blocks ↔ tool_calls) is Phase 3.
    """
    if not tools:
        return None
    out: list[ToolDef] = []
    for t in tools:
        name = t.name if hasattr(t, "name") else t.get("name", "")
        description = (
            t.description if hasattr(t, "description") else t.get("description")
        )
        input_schema = (
            t.input_schema if hasattr(t, "input_schema") else t.get("input_schema") or {}
        )
        out.append(
            ToolDef(
                type="function",
                function=ToolFunctionDef(
                    name=name,
                    description=description,
                    parameters=input_schema,
                ),
            )
        )
    return out


# ─── request translation ────────────────────────────────────────────


def anthropic_to_openai_request(
    req: AnthropicMessagesRequest,
) -> ChatRequest:
    """Build an OpenAI Chat Completions request from an Anthropic
    Messages request. Drops Anthropic-only fields that have no OpenAI
    equivalent (thinking, cache_control, service_tier, etc.) per the
    feasibility doc.
    """
    messages: list[Message] = []
    sys_msg = _hoist_system(req.system)
    if sys_msg is not None:
        messages.append(sys_msg)
    messages.extend(_translate_messages(req.messages))

    return ChatRequest(
        model=req.model,
        messages=messages,
        max_tokens=req.max_tokens,
        temperature=req.temperature,
        top_p=req.top_p,
        stop=req.stop_sequences,
        stream=req.stream,
        tools=_translate_tool_definitions(req.tools),
        tool_choice=_translate_tool_choice(req.tool_choice),
    )


def _translate_tool_choice(tc: Any | None) -> str | dict[str, Any] | None:
    """Anthropic ``tool_choice`` → OpenAI ``tool_choice``.

      auto                    → "auto"
      any                     → "required"
      tool + name             → {"type":"function","function":{"name":...}}
      none                    → "none"
    """
    if tc is None:
        return None
    type_ = tc.type if hasattr(tc, "type") else tc.get("type")
    name = tc.name if hasattr(tc, "name") else tc.get("name")
    if type_ == "auto":
        return "auto"
    if type_ == "any":
        return "required"
    if type_ == "none":
        return "none"
    if type_ == "tool" and name:
        return {"type": "function", "function": {"name": name}}
    return None


# ─── response translation ───────────────────────────────────────────


def _new_message_id() -> str:
    return f"msg_{uuid.uuid4().hex}"


def openai_to_anthropic_response(
    resp: ChatResponse,
    request_model: str,
) -> AnthropicMessagesResponse:
    """OpenAI Chat Completions response → Anthropic Messages response.

    ``request_model`` is what the caller asked for (e.g.
    ``claude-sonnet-4-6``); we echo that in the response so Anthropic
    SDK clients see the model id they expect, NOT the resolved
    free-tier id we actually routed to. (Use the
    ``X-FreeRide-Provider`` header to discover the real provider.)
    """
    if not resp.choices:
        return AnthropicMessagesResponse(
            id=_new_message_id(),
            model=request_model,
            content=[],
            stop_reason="end_turn",
            stop_sequence=None,
            usage=AnthropicUsage(input_tokens=0, output_tokens=0),
        )

    choice = resp.choices[0]
    finish = choice.finish_reason
    msg = choice.message

    content_blocks: list[dict[str, Any]] = []
    text = msg.content or ""
    if text:
        content_blocks.append({"type": "text", "text": text})

    if msg.tool_calls:
        # Phase 3 will translate tool_calls into tool_use blocks here.
        # In Phase 1 we don't generate tool_calls (the route rejects
        # tool_use input), so this branch shouldn't fire during
        # normal use. If it does — provider returned an unrequested
        # tool call — surface it with id+name only so Claude Code
        # doesn't crash on KeyError later.
        for tc in msg.tool_calls:
            content_blocks.append(
                {
                    "type": "tool_use",
                    "id": tc.id,
                    "name": tc.function.name,
                    "input": _safe_parse_tool_args(tc.function.arguments),
                }
            )

    stop_reason = map_stop_reason(finish)

    usage_obj: AnthropicUsage
    if resp.usage is not None:
        usage_obj = AnthropicUsage(
            input_tokens=int(resp.usage.prompt_tokens or 0),
            output_tokens=int(resp.usage.completion_tokens or 0),
        )
    else:
        usage_obj = AnthropicUsage(input_tokens=0, output_tokens=0)

    return AnthropicMessagesResponse(
        id=_new_message_id(),
        model=request_model,
        content=content_blocks,
        stop_reason=stop_reason,
        stop_sequence=None,
        usage=usage_obj,
    )


def _safe_parse_tool_args(arguments: str | None) -> dict[str, Any]:
    """OpenAI tool call ``function.arguments`` is a JSON string.
    Llama-class models occasionally emit malformed JSON; per the
    feasibility doc we'd rather emit ``{}`` than 500 the request.
    """
    if not arguments:
        return {}
    import json

    try:
        parsed = json.loads(arguments)
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


# ─── small helpers used by the route ────────────────────────────────


def request_unsupported_for_phase_1(req: AnthropicMessagesRequest) -> str | None:
    """Return a human-readable reason if this request needs features
    we haven't shipped yet. The route uses this to fail fast with a
    clean 501 instead of producing partial / wrong results.

    Returns None if the request is serviceable by phases shipped so
    far. Streaming is now (Phase 2) supported; tool_use and images
    still gate.
    """
    if req.tools:
        # Tool definitions translate, but tool_use blocks in input
        # don't yet — and a model that picks a tool_call confuses the
        # response translator. Reject all tool requests for now.
        return "tool_use is deferred to Phase 3"
    # Walk content blocks for tool_use / tool_result presence
    for m in req.messages:
        content = m.content
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    t = block.get("type")
                    if t in ("tool_use", "tool_result"):
                        return "tool_use is deferred to Phase 3"
                    if t == "image":
                        return "image content blocks are deferred to a later phase"
    return None


# ─── streaming SSE translation (Phase 2) ────────────────────────


def _sse_event(event_name: str, data: dict[str, Any]) -> bytes:
    """Format one Anthropic-style SSE event.

    Anthropic's SSE format includes BOTH an explicit ``event:`` line
    (the event name) AND a ``data:`` line (JSON payload), separated
    from the next event by a blank line. This is distinct from
    OpenAI's bare ``data:`` lines.
    """
    payload = json.dumps(data, separators=(",", ":"))
    return f"event: {event_name}\ndata: {payload}\n\n".encode("utf-8")


async def stream_openai_to_anthropic(
    chunks: AsyncIterator[Any],
    *,
    request_model: str,
) -> AsyncIterator[bytes]:
    """Consume OpenAI streaming chunks (``ChatStreamEvent``) and emit
    Anthropic-format SSE events.

    Event flow per
    https://platform.claude.com/docs/en/api/messages-streaming :

      1. message_start
      2. content_block_start              (text block, index 0)
      3. content_block_delta * N          (one per chunk with content)
      4. content_block_stop
      5. message_delta                    (final stop_reason + usage)
      6. message_stop

    Phase 2 only emits a single text content block. Phase 3 will add
    tool_use blocks (each with its own content_block_start /
    input_json_delta * N / content_block_stop sub-flow).

    The OpenAI side may emit:
      - role-only first delta (delta.role="assistant")  → swallowed
      - delta.content chunks                            → text_delta
      - finish_reason on the last choice                → captured for
                                                          message_delta
      - a final chunk with empty choices and usage      → captured for
                                                          message_delta
      - [DONE] sentinel                                 → handled at
                                                          provider layer
    """
    msg_id = _new_message_id()

    yield _sse_event(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": request_model,
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        },
    )

    yield _sse_event(
        "content_block_start",
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
    )

    finish_reason: str | None = None
    last_usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}

    async for chunk in chunks:
        # chunk is a ChatStreamEvent (or dict; tolerate both for tests)
        choices = chunk.choices if hasattr(chunk, "choices") else chunk.get("choices") or []

        for choice in choices:
            delta = choice.delta if hasattr(choice, "delta") else choice.get("delta") or {}
            content_piece = (
                delta.content if hasattr(delta, "content") else delta.get("content")
            )
            if content_piece:
                yield _sse_event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": content_piece},
                    },
                )

            choice_finish = (
                choice.finish_reason
                if hasattr(choice, "finish_reason")
                else choice.get("finish_reason")
            )
            if choice_finish:
                finish_reason = choice_finish

        # Usage may arrive on a final choices=[] chunk (NIM does this;
        # OpenAI does it when stream_options.include_usage=true). We
        # forward whatever's most recent.
        usage = chunk.usage if hasattr(chunk, "usage") else chunk.get("usage")
        if usage is not None:
            input_t = (
                usage.prompt_tokens
                if hasattr(usage, "prompt_tokens")
                else usage.get("prompt_tokens", 0) if isinstance(usage, dict) else 0
            )
            output_t = (
                usage.completion_tokens
                if hasattr(usage, "completion_tokens")
                else usage.get("completion_tokens", 0) if isinstance(usage, dict) else 0
            )
            last_usage = {
                "input_tokens": int(input_t or 0),
                "output_tokens": int(output_t or 0),
            }

    # End the (single) content block
    yield _sse_event(
        "content_block_stop",
        {"type": "content_block_stop", "index": 0},
    )

    # Final usage + stop_reason
    yield _sse_event(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {
                "stop_reason": map_stop_reason(finish_reason),
                "stop_sequence": None,
            },
            "usage": last_usage,
        },
    )

    yield _sse_event("message_stop", {"type": "message_stop"})
