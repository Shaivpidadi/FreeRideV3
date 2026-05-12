"""Anthropic Messages API ↔ OpenAI Chat Completions translator.

The shape of this is straightforward in the happy path (chat in,
chat out), and gnarly in the long tail (tool use, streaming, vision,
extended thinking, prompt caching).

**Phase 1 (shipped):** non-streaming chat, system prompt hoisting,
text-only content blocks, stop_reason + usage mapping, tool
definition request-side translation.

**Phase 2 (shipped):** streaming SSE — emits
message_start / content_block_start / content_block_delta /
content_block_stop / message_delta / message_stop. Text-only blocks
in that phase.

**Phase 3 (this commit):** tool use end-to-end.
  - ``tool_use`` blocks in assistant messages (input side) → OpenAI
    ``tool_calls`` on the message.
  - ``tool_result`` blocks in user messages (input side) → OpenAI
    ``role: tool`` messages with ``tool_call_id``.
  - Streaming tool calls — the gnarly part. OpenAI emits
    ``tool_calls[index].function.arguments`` as partial-JSON deltas
    across chunks; Anthropic expects an ``input_json_delta``
    sub-type carrying the same fragments inside its
    ``content_block_delta`` events, with one ``content_block_start``
    per tool_use block carrying the tool id+name. The state machine
    tracks block indices, transitions cleanly between text and
    tool_use blocks, and closes the last open block before
    ``message_delta``.

**Deferred:** vision (image blocks), extended thinking blocks,
``cache_control``.
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
    messages.

    Tool-use semantics differ subtly:

    - Anthropic: assistant message ``content`` is an array that can
      mix ``text`` and ``tool_use`` blocks; user message can mix
      ``text`` and ``tool_result`` blocks.

    - OpenAI: assistant message has separate ``content`` (string)
      and ``tool_calls`` (array) fields; tool results are
      ``role: tool`` messages, ONE PER tool_call_id, sent BEFORE
      the next user message.

    So a single Anthropic user message with two ``tool_result``
    blocks expands into two OpenAI ``role: tool`` messages (plus
    one user message if there's also text). The translation order
    matters — tool messages must precede any text in the user turn
    so the model sees the results before the follow-up.
    """
    from freeride.core.chat_schema import ToolCall, ToolCallFunction

    out: list[Message] = []
    for m in anthropic_messages:
        # AnthropicMessage Pydantic instance OR dict — be permissive.
        role = m.role if hasattr(m, "role") else m["role"]
        content = m.content if hasattr(m, "content") else m["content"]

        # Plain string content (no blocks) — simplest case.
        if isinstance(content, str):
            out.append(Message(role=role, content=content))
            continue

        # Block-array content. Separate by block type so we can build
        # the matching OpenAI message structure.
        text_parts: list[str] = []
        tool_use_blocks: list[dict[str, Any]] = []
        tool_result_blocks: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                t = block.get("text") or ""
                if t:
                    text_parts.append(t)
            elif btype == "tool_use":
                tool_use_blocks.append(block)
            elif btype == "tool_result":
                tool_result_blocks.append(block)
            elif btype == "image":
                raise UnsupportedContentBlock(
                    "image content blocks are deferred to a later phase"
                )
            elif btype in ("document", "search_result", "container_upload") or (
                isinstance(btype, str) and btype.endswith("_tool_result")
            ):
                raise UnsupportedContentBlock(
                    f"content block type {btype!r} is not supported"
                )
            elif btype in ("thinking", "redacted_thinking"):
                continue
            # Unknown types: forward-compat — drop silently.

        text = "\n".join(text_parts) if text_parts else None

        # Tool results: emit ONE OpenAI tool-role message per
        # tool_use_id, in the order they appear in the Anthropic block
        # array. Anthropic's `content` field of a tool_result can be a
        # string OR a list of dicts (text blocks); we flatten to a
        # string for OpenAI.
        for tr in tool_result_blocks:
            tr_content = tr.get("content", "")
            if isinstance(tr_content, list):
                # list of {type:"text",text:"..."} blocks
                pieces: list[str] = []
                for b in tr_content:
                    if isinstance(b, dict) and b.get("type") == "text":
                        pieces.append(b.get("text") or "")
                tr_content_str = "\n".join(pieces)
            else:
                tr_content_str = str(tr_content) if tr_content is not None else ""

            out.append(
                Message(
                    role="tool",
                    content=tr_content_str,
                    tool_call_id=tr.get("tool_use_id", ""),
                )
            )

        # The original assistant or user message. tool_use blocks (only
        # legal in assistant role) become OpenAI tool_calls. Text
        # becomes content.
        if role == "assistant" and tool_use_blocks:
            tool_calls_list = [
                ToolCall(
                    id=b.get("id", ""),
                    type="function",
                    function=ToolCallFunction(
                        name=b.get("name", ""),
                        arguments=json.dumps(b.get("input") or {}),
                    ),
                )
                for b in tool_use_blocks
            ]
            out.append(Message(role=role, content=text, tool_calls=tool_calls_list))
        elif text is not None:
            # Plain text message (user or assistant)
            out.append(Message(role=role, content=text))
        elif not tool_result_blocks:
            # Empty message with no tool_result either — emit empty
            # content so the message exists in the array (Llama models
            # accept empty messages; rejecting would over-validate).
            out.append(Message(role=role, content=""))

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

    reasoning = _extract_reasoning(msg)
    if reasoning:
        content_blocks.append(
            {"type": "thinking", "thinking": reasoning, "signature": ""}
        )

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


def _extract_reasoning(obj: Any) -> str | None:
    """Pull reasoning text out of an upstream message or stream delta.

    OpenRouter uses `reasoning`; vLLM / NIM use `reasoning_content`.
    Both arrive as model_extra because our schemas declare
    `extra="allow"`. Returns None when no usable text is present.
    """
    for attr in ("reasoning", "reasoning_content"):
        val: Any
        if hasattr(obj, attr):
            val = getattr(obj, attr)
        elif hasattr(obj, "model_extra") and obj.model_extra:
            val = obj.model_extra.get(attr)
        elif isinstance(obj, dict):
            val = obj.get(attr)
        else:
            val = None
        if isinstance(val, str) and val.strip():
            return val
    return None


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
    far. Streaming (Phase 2) and tool use (Phase 3) are now
    supported; images and documents still gate.

    (Function name preserved from Phase 1 to avoid an unnecessary
    rename touching every caller; semantically this is now
    "request_uses_unshipped_features".)
    """
    # Walk content blocks for image presence (still deferred). Tool
    # use and streaming are no longer gated.
    for m in req.messages:
        content = m.content
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    t = block.get("type")
                    if t == "image":
                        return "image content blocks are deferred to a later phase"
                    if t in ("document", "search_result", "container_upload"):
                        return (
                            f"content block type {t!r} requires Anthropic-side "
                            "infra not available through FreeRide"
                        )
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
      2. (per content block — text or tool_use:)
         content_block_start      — text block or tool_use shell
         content_block_delta * N  — text_delta or input_json_delta
         content_block_stop       — close the block
      3. message_delta            — stop_reason + cumulative usage
      4. message_stop

    Phase 3 supports text AND tool_use blocks, including the
    sub-flow when a model emits text BEFORE calling a tool. The
    state machine tracks one ``current_index`` (monotonic counter)
    and one ``current_kind`` (``text`` | ``tool_use`` | None);
    transitions close the previous block before opening the next.

    OpenAI tool_calls stream as partial JSON in
    ``delta.tool_calls[i].function.arguments`` — the first chunk
    for a given index carries the tool ``id`` and ``name`` and
    the args fragment, subsequent chunks carry only args. We map
    OpenAI's per-call ``index`` to our monotonic ``content_block``
    index, so two parallel tool_calls become two sequential
    Anthropic blocks (parallel-tool-use is rare in practice on
    free providers; sequential rendering matches what Anthropic's
    own API does today).
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

    # State machine -----------------------------------------------
    current_index = -1  # last-emitted block index; bumps on each open
    current_kind: str | None = None  # 'thinking' | 'text' | 'tool_use' | None
    # Map OpenAI tool_calls[i].index → our content_block index
    tool_call_to_block: dict[int, int] = {}

    finish_reason: str | None = None
    last_usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}

    async def _close_current() -> bytes | None:
        nonlocal current_kind
        if current_kind is None:
            return None
        out = _sse_event(
            "content_block_stop",
            {"type": "content_block_stop", "index": current_index},
        )
        current_kind = None
        return out

    async for chunk in chunks:
        choices = chunk.choices if hasattr(chunk, "choices") else chunk.get("choices") or []

        for choice in choices:
            delta = choice.delta if hasattr(choice, "delta") else choice.get("delta") or {}

            # ─── reasoning / thinking ───────────────────────
            # OpenRouter streams `delta.reasoning`; vLLM/NIM stream
            # `delta.reasoning_content`. Surface as Anthropic
            # thinking_delta events so Claude Code renders them in
            # the dimmed thinking block instead of as user-facing
            # text.
            reasoning_piece = _extract_reasoning(delta)
            if reasoning_piece:
                if current_kind != "thinking":
                    if current_kind is not None:
                        closed = await _close_current()
                        if closed:
                            yield closed
                    current_index += 1
                    current_kind = "thinking"
                    yield _sse_event(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": current_index,
                            "content_block": {
                                "type": "thinking",
                                "thinking": "",
                            },
                        },
                    )
                yield _sse_event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": current_index,
                        "delta": {
                            "type": "thinking_delta",
                            "thinking": reasoning_piece,
                        },
                    },
                )

            # ─── text content ───────────────────────────────
            content_piece = (
                delta.content if hasattr(delta, "content") else delta.get("content")
            )
            if content_piece:
                # Transition: open a text block if not already open.
                if current_kind != "text":
                    if current_kind is not None:
                        closed = await _close_current()
                        if closed:
                            yield closed
                    current_index += 1
                    current_kind = "text"
                    yield _sse_event(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": current_index,
                            "content_block": {"type": "text", "text": ""},
                        },
                    )
                yield _sse_event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": current_index,
                        "delta": {"type": "text_delta", "text": content_piece},
                    },
                )

            # ─── tool calls ─────────────────────────────────
            tool_calls = (
                delta.tool_calls
                if hasattr(delta, "tool_calls")
                else delta.get("tool_calls")
            )
            if tool_calls:
                for tc in tool_calls:
                    # tc is a dict per ChatStreamEvent's
                    # StreamDelta.tool_calls type; access defensively
                    if hasattr(tc, "model_dump"):
                        tc = tc.model_dump()
                    elif not isinstance(tc, dict):
                        continue

                    oai_index = tc.get("index", 0)
                    fn = tc.get("function") or {}
                    args_piece = fn.get("arguments")

                    if oai_index not in tool_call_to_block:
                        # New tool block — close any open block,
                        # open a tool_use shell with id+name (which
                        # the first chunk for this index should
                        # carry; if name is empty here it'll just be
                        # empty in the start event, the SDK still
                        # accepts that).
                        if current_kind is not None:
                            closed = await _close_current()
                            if closed:
                                yield closed
                        current_index += 1
                        current_kind = "tool_use"
                        tool_call_to_block[oai_index] = current_index
                        yield _sse_event(
                            "content_block_start",
                            {
                                "type": "content_block_start",
                                "index": current_index,
                                "content_block": {
                                    "type": "tool_use",
                                    "id": tc.get("id") or "",
                                    "name": fn.get("name") or "",
                                    "input": {},
                                },
                            },
                        )

                    block_index = tool_call_to_block[oai_index]
                    if args_piece:
                        yield _sse_event(
                            "content_block_delta",
                            {
                                "type": "content_block_delta",
                                "index": block_index,
                                "delta": {
                                    "type": "input_json_delta",
                                    "partial_json": args_piece,
                                },
                            },
                        )

            # finish_reason on this choice
            choice_finish = (
                choice.finish_reason
                if hasattr(choice, "finish_reason")
                else choice.get("finish_reason")
            )
            if choice_finish:
                finish_reason = choice_finish

        # Usage from the chunk (may arrive on a final choices=[] chunk)
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

    # End of stream: close any block we opened. If we opened NOTHING
    # (no text, no tool — e.g. provider returned a finish_reason
    # immediately), open and close a zero-length text block so the
    # Anthropic-shape message is always well-formed (content !=
    # empty-without-stop).
    if current_kind is None:
        current_index += 1
        yield _sse_event(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": current_index,
                "content_block": {"type": "text", "text": ""},
            },
        )
        yield _sse_event(
            "content_block_stop",
            {"type": "content_block_stop", "index": current_index},
        )
    else:
        closed = await _close_current()
        if closed:
            yield closed

    # Final message_delta + message_stop
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
