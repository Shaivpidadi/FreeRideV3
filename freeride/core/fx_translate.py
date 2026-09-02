"""fx gateway dialect ⇄ OpenAI Chat Completions translation.

Request side: :func:`fx_to_chat_request` maps the AI-SDK-shaped
``{prompt: [...]}`` body (see :mod:`freeride.core.fx_schema`) into the
gateway's internal :class:`~freeride.core.chat_schema.ChatRequest` so
the existing provider failover chain can route it.

Response side, streaming: :func:`stream_chat_to_fx` re-frames OpenAI
Chat Completions SSE chunks into the AI SDK stream-part events fx's
client consumes (``src/gateway/client.zig`` — ``consumeSseStream``):

    data: {"type":"response-metadata","modelId":"..."}
    data: {"type":"text-delta","id":"answer_1","delta":"..."}
    data: {"type":"tool-input-start","id":"c1","toolName":"Write"}
    data: {"type":"tool-input-delta","id":"c1","delta":"{\\"pa"}
    data: {"type":"tool-input-end","id":"c1"}
    data: {"type":"tool-call","toolCallId":"c1","toolName":"Write","input":{...}}
    data: {"type":"finish","finishReason":{"unified":"tool-calls","raw":"tool_calls"},
           "usage":{"inputTokens":{"total":3},"outputTokens":{"total":5}}}
    data: [DONE]

Hard requirements from fx's parser:

- ``finishReason`` MUST be an object with a ``unified`` string that is
  one of ``stop | length | content-filter | tool-calls | error | other``.
  A bare string or an unknown value kills the whole stream on fx's side
  (``error.InvalidProviderFinishReason``).
- Usage token counts are nested: ``{"inputTokens": {"total": N}}``.
  Flat integers are silently ignored.
- ``tool-call`` needs a non-empty ``toolCallId``; without one fx marks
  the call invalid and the agent can't execute it. We synthesize
  ``call_<index>`` when a provider omits ids.
- Billing metadata (``providerMetadata.gateway.*``) is optional —
  fx logs "billing ignored" and moves on, so we don't fabricate it.

Response side, non-streaming (``ai-language-model-streaming: false``):
fx's ``parseGatewayCompletion`` reads the plain OpenAI
``choices[0].message`` shape, which is exactly what providers hand us —
the route returns the upstream body untouched, no translator needed.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

from freeride.core.chat_schema import ChatRequest, Message, ToolCall, ToolCallFunction
from freeride.core.fx_schema import FxMessage, FxRequest

logger = logging.getLogger(__name__)


# ─── request translation ────────────────────────────────────────────


class UnsupportedFxPrompt(ValueError):
    """Raised when a prompt entry can't be mapped to Chat Completions."""


def _user_message(msg: FxMessage) -> Message:
    """User content is a list of ``text`` and ``file`` (image) parts.
    Text-only collapses to a plain string; images become OpenAI
    ``image_url`` parts with a data URI."""
    parts = msg.content if isinstance(msg.content, list) else []
    if isinstance(msg.content, str):
        return Message(role="user", content=msg.content)

    texts: list[str] = []
    images: list[dict[str, Any]] = []
    for part in parts:
        kind = part.get("type")
        if kind == "text" and isinstance(part.get("text"), str):
            texts.append(part["text"])
        elif kind == "file" and isinstance(part.get("data"), str):
            media = part.get("mediaType") or "application/octet-stream"
            images.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{media};base64,{part['data']}"},
                }
            )
        # Unknown part types are dropped rather than 400ing — fx adds
        # part kinds faster than we track them.

    if not images:
        return Message(role="user", content="".join(texts))
    content: list[dict[str, Any]] = []
    if texts:
        content.append({"type": "text", "text": "".join(texts)})
    content.extend(images)
    return Message(role="user", content=content)


def _assistant_message(msg: FxMessage) -> Message:
    """Assistant content mixes ``text`` and ``tool-call`` parts."""
    if isinstance(msg.content, str):
        return Message(role="assistant", content=msg.content)

    texts: list[str] = []
    tool_calls: list[ToolCall] = []
    for part in msg.content or []:
        kind = part.get("type")
        if kind == "text" and isinstance(part.get("text"), str):
            texts.append(part["text"])
        elif kind == "tool-call":
            raw_input = part.get("input")
            arguments = (
                raw_input if isinstance(raw_input, str) else json.dumps(raw_input or {})
            )
            tool_calls.append(
                ToolCall(
                    id=str(part.get("toolCallId") or ""),
                    function=ToolCallFunction(
                        name=str(part.get("toolName") or ""),
                        arguments=arguments,
                    ),
                )
            )
    return Message(
        role="assistant",
        content="".join(texts) or None,
        tool_calls=tool_calls or None,
    )


def _tool_messages(msg: FxMessage) -> list[Message]:
    """A tool message carries ``tool-result`` parts. fx writes exactly
    one per message but we map each part defensively. The typed
    ``output`` envelope (``text`` / ``error-text`` / ``execution-denied``)
    flattens to a string — OpenAI's tool role has no error channel, and
    the value/reason text already says what happened."""
    out: list[Message] = []
    for part in msg.content or [] if isinstance(msg.content, list) else []:
        if part.get("type") != "tool-result":
            continue
        output = part.get("output") or {}
        value = output.get("value", output.get("reason", ""))
        if not isinstance(value, str):
            value = json.dumps(value)
        out.append(
            Message(
                role="tool",
                content=value,
                tool_call_id=str(part.get("toolCallId") or ""),
            )
        )
    return out


def _translate_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """fx tool defs are flat (``{"type":"function","name","description",
    "inputSchema"}`` — its tests assert there is NO nested ``function``
    key). OpenAI nests everything under ``function`` and calls the
    schema ``parameters``."""
    translated: list[dict[str, Any]] = []
    for tool in tools:
        if tool.get("type") not in (None, "function"):
            continue  # provider-executed tool kinds (web_search etc.) don't map
        name = tool.get("name")
        if not name:
            continue
        fn: dict[str, Any] = {
            "name": name,
            "parameters": tool.get("inputSchema")
            or {"type": "object", "properties": {}},
        }
        if tool.get("description"):
            fn["description"] = tool["description"]
        translated.append({"type": "function", "function": fn})
    return translated or None


def _translate_tool_choice(choice: dict[str, Any] | str | None) -> str | None:
    """fx serializes ``{"toolChoice":{"type":"auto|required|none"}}``."""
    if choice is None:
        return None
    kind = choice if isinstance(choice, str) else choice.get("type")
    if kind in ("auto", "required", "none"):
        return kind
    return None


def fx_to_chat_request(body: FxRequest, model: str) -> ChatRequest:
    """Map an fx gateway request (+ the model id from the
    ``ai-language-model-id`` header) to a ChatRequest."""
    messages: list[Message] = []
    for msg in body.prompt:
        if msg.role == "system":
            content = msg.content if isinstance(msg.content, str) else ""
            messages.append(Message(role="system", content=content))
        elif msg.role == "user":
            messages.append(_user_message(msg))
        elif msg.role == "assistant":
            messages.append(_assistant_message(msg))
        elif msg.role == "tool":
            messages.extend(_tool_messages(msg))

    # fx's structured output (responseFormat type "json") carries a
    # JSON schema; json_object is the widest-supported downgrade
    # across free providers.
    wants_json = bool(body.responseFormat) and body.responseFormat.get("type") == "json"
    return ChatRequest(
        model=model,
        messages=messages,
        tools=_translate_tools(body.tools),
        tool_choice=_translate_tool_choice(body.toolChoice),
        max_tokens=body.maxOutputTokens or None,
        response_format={"type": "json_object"} if wants_json else None,
    )


# ─── finish reason mapping ──────────────────────────────────────────

_UNIFIED_FINISH = {
    "stop": "stop",
    "length": "length",
    "max_tokens": "length",
    "content_filter": "content-filter",
    "content-filter": "content-filter",
    "tool_calls": "tool-calls",
    "tool-calls": "tool-calls",
    "function_call": "tool-calls",
}


def finish_reason_to_unified(reason: str | None, *, has_tool_calls: bool) -> str:
    """Map an OpenAI finish_reason onto fx's unified enum. Unknown
    values become ``other`` — anything outside the enum is a hard
    stream error on fx's side, never pass one through. A completion
    that produced tool calls is reported as ``tool-calls`` regardless
    of what the provider said, so the agent loop executes them."""
    if has_tool_calls:
        return "tool-calls"
    if reason is None:
        return "stop"
    return _UNIFIED_FINISH.get(reason, "other")


# ─── stream translation ─────────────────────────────────────────────


def _sse(event: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(event, separators=(',', ':'))}\n\n".encode()


class _ToolCallAccumulator:
    """Collects OpenAI tool_call delta fragments for one tool index.

    ``tool-input-start`` is only emitted once both id and name are
    known (fx requires the pair); argument fragments that arrive
    earlier are buffered and flushed as the first ``tool-input-delta``.
    """

    def __init__(self, index: int) -> None:
        self.index = index
        self.id = ""
        self.name = ""
        self.arguments = ""
        self.started = False  # tool-input-start emitted

    def feed(self, fragment: dict[str, Any]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        if fragment.get("id"):
            self.id = self.id or str(fragment["id"])
        fn = fragment.get("function") or {}
        if fn.get("name"):
            self.name += fn["name"]
        pending = fn.get("arguments") or ""

        if not self.started and self.name:
            self.id = self.id or f"call_{self.index}"
            self.started = True
            events.append(
                {"type": "tool-input-start", "id": self.id, "toolName": self.name}
            )
            if self.arguments:  # flush fragments that arrived pre-start
                events.append(
                    {"type": "tool-input-delta", "id": self.id, "delta": self.arguments}
                )

        if pending:
            self.arguments += pending
            if self.started:
                events.append(
                    {"type": "tool-input-delta", "id": self.id, "delta": pending}
                )
        return events

    def finalize(self) -> list[dict[str, Any]]:
        """``tool-input-end`` + the final ``tool-call`` with full input.
        fx accepts the input as a parsed object or a serialized string;
        a malformed-JSON string is still delivered (fx surfaces it to
        the model as a tool error rather than dying)."""
        if not self.name:
            return []  # fragment stream never produced a callable id
        self.id = self.id or f"call_{self.index}"
        try:
            input_value: Any = json.loads(self.arguments) if self.arguments else {}
        except ValueError:
            input_value = self.arguments
        events: list[dict[str, Any]] = []
        if self.started:
            events.append({"type": "tool-input-end", "id": self.id})
        events.append(
            {
                "type": "tool-call",
                "toolCallId": self.id,
                "toolName": self.name,
                "input": input_value,
            }
        )
        return events


async def stream_chat_to_fx(
    chunks: AsyncIterator[Any],
    *,
    resolved_model: str,
) -> AsyncIterator[bytes]:
    """Consume OpenAI Chat Completions stream chunks and emit fx
    AI-SDK stream-part SSE frames.

    Text deltas pass through live; tool-call fragments accumulate per
    OpenAI tool index and materialize as ``tool-call`` events once the
    provider signals completion (finish chunk or end of stream) —
    mirroring the accumulator fx itself runs on the other end.
    """
    yield _sse({"type": "response-metadata", "modelId": resolved_model})

    accumulators: dict[int, _ToolCallAccumulator] = {}
    finish_reason: str | None = None
    input_tokens = 0
    output_tokens = 0

    async for chunk in chunks:
        choices = (
            chunk.choices if hasattr(chunk, "choices") else chunk.get("choices") or []
        )
        usage = chunk.usage if hasattr(chunk, "usage") else chunk.get("usage")
        if usage is not None:
            input_tokens = getattr(usage, "prompt_tokens", 0) or (
                usage.get("prompt_tokens", 0) if isinstance(usage, dict) else 0
            )
            output_tokens = getattr(usage, "completion_tokens", 0) or (
                usage.get("completion_tokens", 0) if isinstance(usage, dict) else 0
            )

        for choice in choices:
            delta = (
                choice.delta if hasattr(choice, "delta") else choice.get("delta") or {}
            )
            content = (
                delta.content if hasattr(delta, "content") else delta.get("content")
            )
            if content:
                yield _sse({"type": "text-delta", "id": "answer_1", "delta": content})

            tool_calls = (
                delta.tool_calls
                if hasattr(delta, "tool_calls")
                else delta.get("tool_calls")
            )
            for fragment in tool_calls or []:
                index = int(fragment.get("index", 0) or 0)
                acc = accumulators.setdefault(index, _ToolCallAccumulator(index))
                for event in acc.feed(fragment):
                    yield _sse(event)

            reason = (
                choice.finish_reason
                if hasattr(choice, "finish_reason")
                else choice.get("finish_reason")
            )
            if reason:
                finish_reason = reason

    for index in sorted(accumulators):
        for event in accumulators[index].finalize():
            yield _sse(event)

    has_tool_calls = any(acc.name for acc in accumulators.values())
    unified = finish_reason_to_unified(finish_reason, has_tool_calls=has_tool_calls)
    yield _sse(
        {
            "type": "finish",
            "finishReason": {"unified": unified, "raw": finish_reason or unified},
            "usage": {
                "inputTokens": {"total": int(input_tokens)},
                "outputTokens": {"total": int(output_tokens)},
            },
        }
    )
    yield b"data: [DONE]\n\n"
