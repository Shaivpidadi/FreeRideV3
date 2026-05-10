"""Anthropic Messages API ↔ OpenAI Chat Completions translator.

The shape of this is straightforward in the happy path (chat in,
chat out), and gnarly in the long tail (tool use, streaming, vision,
extended thinking, prompt caching).

**Phase 1 scope (this commit):**
  - Non-streaming chat
  - System prompt hoisting (Anthropic top-level → OpenAI first
    ``system`` message)
  - Text-only content blocks
  - stop_reason mapping
  - usage mapping
  - Stub tool support: tool definitions translate, but tool_use blocks
    in responses raise ``NotImplementedError`` so the route can return
    a clean 501 if Claude Code asks for tools before Phase 3 lands.

Phase 2 (streaming) and Phase 3 (tool use) are tracked separately;
this module's public surface is designed to grow into them without
breaking callers.
"""

from __future__ import annotations

import uuid
from typing import Any

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
    """Return a human-readable reason if this request needs Phase 2/3
    features we haven't shipped yet. The route uses this to fail fast
    with a clean 501 instead of producing partial / wrong results.

    Returns None if the request is fully Phase-1-serviceable.
    """
    if req.stream:
        return "streaming responses are deferred to Phase 2"
    if req.tools:
        # Tool definitions translate, but tool_use blocks in input
        # don't yet — and a model that picks a tool_call confuses the
        # response translator. Reject all tool requests for Phase 1.
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
