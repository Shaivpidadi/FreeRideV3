"""Bidirectional translation between OpenAI's Responses API and Chat
Completions.

The Codex CLI sends Responses-shape requests to whatever
``openai_base_url`` points at; we translate to Chat Completions so the
existing provider failover machinery can route them, then translate
back to Responses shape on the way out.

Translation surface:

* Request — ``input`` (string OR list of typed items) ↔ Chat
  ``messages``; flat Responses tool defs ↔ nested Chat tool defs;
  ``max_output_tokens`` ↔ ``max_tokens``; ``instructions`` prepended
  as a system message; ``function_call_output`` items become
  ``role=tool`` messages.
* Non-streaming response — Chat ``choices[0].message`` + ``tool_calls``
  fan out into a Responses ``output[]`` list of typed items;
  ``finish_reason`` maps to ``status`` + ``incomplete_details``;
  ``prompt_tokens``/``completion_tokens`` become
  ``input_tokens``/``output_tokens``.
* Streaming — Chat's delta-shaped stream gets re-framed into the
  Responses SSE event protocol (``response.created`` →
  ``response.output_item.added`` → per-item events →
  ``response.output_item.done`` → ``response.completed``), with a
  monotonically incrementing ``sequence_number`` on every event.

Out of scope this revision: reasoning items, ``previous_response_id``
stateful chaining, built-in tool types (``web_search``, ``file_search``,
``code_interpreter``), multimodal parts. Schema-side ``extra="allow"``
keeps them parseable; they just don't surface.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncIterator

from freeride.core.chat_schema import (
    ChatRequest,
    ChatResponse,
    Message,
    ToolCall,
    ToolCallFunction,
    ToolDef,
    ToolFunctionDef,
)
from freeride.core.codex_schema import (
    FunctionCallItem,
    FunctionCallOutputItem,
    MessageItem,
    ReasoningItem,
    ResponsesRequest,
    ResponsesResponse,
    ResponsesUsage,
)


# ─── request: Responses → Chat Completions ─────────────────────────


def _flatten_message_content(parts: list[dict[str, Any]]) -> str:
    """Pull text out of input_text / output_text parts. Other part
    types (input_image, input_file) are dropped — free-tier providers
    are largely text-only, and surfacing them upstream would 400."""
    pieces: list[str] = []
    for p in parts:
        if not isinstance(p, dict):
            continue
        if p.get("type") in ("input_text", "output_text"):
            t = p.get("text")
            if t:
                pieces.append(t)
        # text key directly (some clients flatten)
        elif "text" in p and isinstance(p["text"], str):
            pieces.append(p["text"])
    return "".join(pieces)


def _message_role(role: str | None) -> str:
    """Map Responses role to Chat Completions role. The ``developer``
    role is Responses-specific (it's a higher-priority system message);
    we collapse it to system since Chat Completions has only one
    system-tier slot."""
    if role in ("user", "system", "assistant"):
        return role
    if role == "developer":
        return "system"
    return "user"


def _item_to_chat_messages(item: Any) -> list[Message]:
    """One Responses input item → zero or more Chat Completions
    messages. The split is necessary because:

    * MessageItem maps 1→1 (with content flattening).
    * FunctionCallItem (model echoed back in a multi-turn flow)
      maps to an assistant message with tool_calls.
    * FunctionCallOutputItem (tool result) maps to a role=tool
      message — NOT a role=function message; OpenAI deprecated that.
    * ReasoningItem drops silently — free providers don't accept it.
    * Unknown dict items pass through best-effort: if they have a
      role/content pair we treat them as a message, else drop.
    """
    if isinstance(item, MessageItem):
        text = _flatten_message_content(item.content)
        if not text:
            return []
        return [Message(role=_message_role(item.role), content=text)]

    if isinstance(item, FunctionCallItem):
        return [
            Message(
                role="assistant",
                content=None,
                tool_calls=[
                    ToolCall(
                        id=item.call_id,
                        type="function",
                        function=ToolCallFunction(
                            name=item.name,
                            arguments=item.arguments,
                        ),
                    )
                ],
            )
        ]

    if isinstance(item, FunctionCallOutputItem):
        # Responses' ``output`` is a stringified payload; Chat's tool
        # message content is also a string, so this is a direct copy.
        return [
            Message(
                role="tool",
                content=item.output,
                tool_call_id=item.call_id,
            )
        ]

    if isinstance(item, ReasoningItem):
        return []  # silently drop

    if isinstance(item, dict):
        # Permissive fallback: dict items that look like a message
        # still pass through. Everything else (unknown types) drops.
        if item.get("type") == "message":
            text = _flatten_message_content(item.get("content") or [])
            if text:
                return [Message(role=_message_role(item.get("role")), content=text)]
        return []

    return []


def responses_to_chat_request(req: ResponsesRequest) -> ChatRequest:
    """Translate POST /v1/responses request → ChatRequest."""
    messages: list[Message] = []

    # ``instructions`` is system-prompt-like but sits outside ``input``.
    # Prepend as a system message so it carries through cleanly.
    if req.instructions:
        messages.append(Message(role="system", content=req.instructions))

    if isinstance(req.input, str):
        if req.input:
            messages.append(Message(role="user", content=req.input))
    else:
        for item in req.input:
            messages.extend(_item_to_chat_messages(item))

    # Tools: Responses is FLAT, Chat is NESTED. Wrap each function tool
    # def in the {type:"function", function:{...}} envelope.
    chat_tools: list[ToolDef] | None = None
    if req.tools:
        chat_tools = []
        for t in req.tools:
            chat_tools.append(
                ToolDef(
                    type="function",
                    function=ToolFunctionDef(
                        name=t.name,
                        description=t.description,
                        parameters=t.parameters or {},
                    ),
                )
            )
        if not chat_tools:
            chat_tools = None

    kwargs: dict[str, Any] = {
        "model": req.model,
        "messages": messages,
        "stream": bool(req.stream),
    }
    if chat_tools is not None:
        kwargs["tools"] = chat_tools
    if req.tool_choice is not None:
        kwargs["tool_choice"] = req.tool_choice
    if req.max_output_tokens is not None:
        kwargs["max_tokens"] = req.max_output_tokens
    if req.temperature is not None:
        kwargs["temperature"] = req.temperature
    if req.top_p is not None:
        kwargs["top_p"] = req.top_p
    if req.parallel_tool_calls is not None:
        kwargs["parallel_tool_calls"] = req.parallel_tool_calls
    return ChatRequest(**kwargs)


# ─── response: Chat Completions → Responses ────────────────────────


# Chat finish_reason → (Responses status, incomplete reason or None).
# tool_calls is still a "completed" status — the tool call lives in
# output[], not in any status flag.
_STATUS_MAP: dict[str, tuple[str, str | None]] = {
    "stop": ("completed", None),
    "tool_calls": ("completed", None),
    "function_call": ("completed", None),
    "length": ("incomplete", "max_output_tokens"),
    "content_filter": ("incomplete", "content_filter"),
}


def _new_response_id() -> str:
    return f"resp_{uuid.uuid4().hex}"


def _new_item_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _call_id(tc_id: str | None) -> str:
    """Tool calls in Responses use a ``call_id`` field. If the upstream
    provider returned its own id, reuse it. Otherwise synthesize one
    so downstream consumers can correlate function_call_output items
    on the next turn."""
    if tc_id:
        return tc_id
    return f"call_{uuid.uuid4().hex[:16]}"


def chat_to_responses_response(
    resp: ChatResponse, requested_model: str
) -> ResponsesResponse:
    """Translate ChatResponse → ResponsesResponse for non-streaming.

    Builds the ``output[]`` list by walking the assistant message:
    text content becomes one ``message`` item with an ``output_text``
    part; each tool_call becomes a separate ``function_call`` item
    AFTER the message item (Responses convention).
    """
    output: list[dict[str, Any]] = []
    status: str = "completed"
    incomplete_reason: str | None = None

    if resp.choices:
        choice = resp.choices[0]
        msg = choice.message
        # Map finish_reason to (status, incomplete_reason).
        status, incomplete_reason = _STATUS_MAP.get(
            choice.finish_reason or "stop", ("completed", None)
        )

        # Assistant text message item — only emit when there's text;
        # tool-only responses skip this.
        if msg.content:
            output.append(
                {
                    "type": "message",
                    "id": _new_item_id("msg"),
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": msg.content,
                            "annotations": [],
                        }
                    ],
                }
            )

        # Function call items — one per tool_call. Order matches the
        # upstream's tool_calls array (which is the order the model
        # emitted them).
        if msg.tool_calls:
            for tc in msg.tool_calls:
                output.append(
                    {
                        "type": "function_call",
                        "id": _new_item_id("fc"),
                        "call_id": _call_id(tc.id),
                        "name": tc.function.name,
                        "arguments": tc.function.arguments or "",
                        "status": "completed",
                    }
                )

    usage_obj: ResponsesUsage | None = None
    if resp.usage is not None:
        usage_obj = ResponsesUsage(
            input_tokens=int(resp.usage.prompt_tokens or 0),
            output_tokens=int(resp.usage.completion_tokens or 0),
            total_tokens=int(resp.usage.total_tokens or 0),
        )

    incomplete_details = None
    if incomplete_reason:
        from freeride.core.codex_schema import IncompleteDetails  # local import

        incomplete_details = IncompleteDetails(reason=incomplete_reason)

    return ResponsesResponse(
        id=_new_response_id(),
        created_at=int(time.time()),
        status=status,
        model=requested_model,
        output=output,
        usage=usage_obj,
        incomplete_details=incomplete_details,
    )


# ─── streaming: Chat deltas → Responses SSE events ─────────────────


def _sse(event_name: str, data: dict[str, Any]) -> bytes:
    """Build one SSE frame. Responses uses both ``event:`` and ``data:``
    lines — the event_name is significant (some clients dispatch on it
    rather than parsing the data payload). The ``type`` field inside
    data also matches the event name per OpenAI's spec, which is
    redundant but expected by some SDKs."""
    payload = {**data, "type": event_name}
    return (
        f"event: {event_name}\n"
        f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
    ).encode("utf-8")


class _StreamFramer:
    """Stateful tracker for re-framing OpenAI delta chunks into the
    Responses SSE event sequence.

    Responses streaming requires explicit ``output_item.added`` /
    ``content_part.added`` framing events around each piece of output
    — clients gate on these before consuming deltas, so the
    delta-only shape Chat emits is insufficient.

    State: ``current_item_kind`` is ``None``, ``"text"``, or
    ``"function_call:<oai_index>"``. Transitions close the previous
    item with its terminator events before opening the next.

    ``sequence_number`` increments on every event for SDK consumers
    that rely on it for idempotency / ordering checks.
    """

    def __init__(self, *, requested_model: str, response_id: str) -> None:
        self.requested_model = requested_model
        self.response_id = response_id
        self.sequence_number = 0
        self.output_index = -1  # bumps to 0 on first item
        self.current_kind: str | None = None
        self.current_item_id: str | None = None
        # Per-content-part state for text items (single part per
        # message in our streaming output).
        self.current_content_index = -1
        # Tool-call buffer for the partial-JSON args stream. Each entry:
        # {oai_index → {output_index, item_id, call_id, name, args_acc}}
        self.tool_state: dict[int, dict[str, Any]] = {}

    def _seq(self) -> int:
        self.sequence_number += 1
        return self.sequence_number

    def created_events(self) -> list[bytes]:
        """Frame events at stream start. The ``response`` skeleton
        contains everything needed for the client to render a blank
        in-progress state."""
        skeleton = {
            "id": self.response_id,
            "object": "response",
            "created_at": int(time.time()),
            "status": "in_progress",
            "model": self.requested_model,
            "output": [],
        }
        return [
            _sse(
                "response.created",
                {"response": skeleton, "sequence_number": self._seq()},
            ),
            _sse(
                "response.in_progress",
                {"response": skeleton, "sequence_number": self._seq()},
            ),
        ]

    def open_text_item(self) -> list[bytes]:
        """Start a new assistant message item carrying an output_text
        content part. Emits added events for both the item and the
        first content part inside it."""
        self.output_index += 1
        self.current_content_index = 0
        self.current_item_id = _new_item_id("msg")
        self.current_kind = "text"
        item = {
            "type": "message",
            "id": self.current_item_id,
            "status": "in_progress",
            "role": "assistant",
            "content": [],
        }
        part = {"type": "output_text", "text": "", "annotations": []}
        return [
            _sse(
                "response.output_item.added",
                {
                    "output_index": self.output_index,
                    "item": item,
                    "sequence_number": self._seq(),
                },
            ),
            _sse(
                "response.content_part.added",
                {
                    "item_id": self.current_item_id,
                    "output_index": self.output_index,
                    "content_index": self.current_content_index,
                    "part": part,
                    "sequence_number": self._seq(),
                },
            ),
        ]

    def text_delta(self, delta: str, *, accumulated: str) -> list[bytes]:
        return [
            _sse(
                "response.output_text.delta",
                {
                    "item_id": self.current_item_id,
                    "output_index": self.output_index,
                    "content_index": self.current_content_index,
                    "delta": delta,
                    "sequence_number": self._seq(),
                },
            )
        ]

    def close_text_item(self, *, accumulated: str) -> list[bytes]:
        """Emit done events for content part + item, in that order."""
        evs = [
            _sse(
                "response.output_text.done",
                {
                    "item_id": self.current_item_id,
                    "output_index": self.output_index,
                    "content_index": self.current_content_index,
                    "text": accumulated,
                    "sequence_number": self._seq(),
                },
            ),
            _sse(
                "response.content_part.done",
                {
                    "item_id": self.current_item_id,
                    "output_index": self.output_index,
                    "content_index": self.current_content_index,
                    "part": {
                        "type": "output_text",
                        "text": accumulated,
                        "annotations": [],
                    },
                    "sequence_number": self._seq(),
                },
            ),
            _sse(
                "response.output_item.done",
                {
                    "output_index": self.output_index,
                    "item": {
                        "type": "message",
                        "id": self.current_item_id,
                        "status": "completed",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": accumulated,
                                "annotations": [],
                            }
                        ],
                    },
                    "sequence_number": self._seq(),
                },
            ),
        ]
        self.current_kind = None
        self.current_item_id = None
        return evs

    def open_function_call_item(
        self, oai_index: int, *, name: str, call_id: str
    ) -> list[bytes]:
        self.output_index += 1
        item_id = _new_item_id("fc")
        self.tool_state[oai_index] = {
            "output_index": self.output_index,
            "item_id": item_id,
            "call_id": call_id,
            "name": name,
            "args_acc": "",
        }
        self.current_kind = f"function_call:{oai_index}"
        self.current_item_id = item_id
        return [
            _sse(
                "response.output_item.added",
                {
                    "output_index": self.output_index,
                    "item": {
                        "type": "function_call",
                        "id": item_id,
                        "call_id": call_id,
                        "name": name,
                        "arguments": "",
                        "status": "in_progress",
                    },
                    "sequence_number": self._seq(),
                },
            )
        ]

    def function_call_args_delta(self, oai_index: int, delta: str) -> list[bytes]:
        state = self.tool_state[oai_index]
        state["args_acc"] += delta
        return [
            _sse(
                "response.function_call_arguments.delta",
                {
                    "item_id": state["item_id"],
                    "output_index": state["output_index"],
                    "delta": delta,
                    "sequence_number": self._seq(),
                },
            )
        ]

    def close_function_call_item(self, oai_index: int) -> list[bytes]:
        state = self.tool_state[oai_index]
        args_acc = state["args_acc"]
        evs = [
            _sse(
                "response.function_call_arguments.done",
                {
                    "item_id": state["item_id"],
                    "output_index": state["output_index"],
                    "arguments": args_acc,
                    "sequence_number": self._seq(),
                },
            ),
            _sse(
                "response.output_item.done",
                {
                    "output_index": state["output_index"],
                    "item": {
                        "type": "function_call",
                        "id": state["item_id"],
                        "call_id": state["call_id"],
                        "name": state["name"],
                        "arguments": args_acc,
                        "status": "completed",
                    },
                    "sequence_number": self._seq(),
                },
            ),
        ]
        self.current_kind = None
        self.current_item_id = None
        return evs

    def completed_event(
        self, *, status: str, usage: ResponsesUsage | None
    ) -> list[bytes]:
        response = {
            "id": self.response_id,
            "object": "response",
            "created_at": int(time.time()),
            "status": status,
            "model": self.requested_model,
            "output": [],
        }
        if usage is not None:
            response["usage"] = usage.model_dump(exclude_none=True)
        return [
            _sse(
                "response.completed",
                {"response": response, "sequence_number": self._seq()},
            )
        ]


async def stream_chat_to_responses(
    chunks: AsyncIterator[Any],
    *,
    requested_model: str,
) -> AsyncIterator[bytes]:
    """Consume OpenAI Chat Completions streaming chunks and emit
    Responses-API SSE events with full framing
    (output_item.added/done, content_part.added/done, completed)
    so clients can render text + function calls correctly.

    Transitions: text deltas open a message item on first arrival;
    tool_call deltas open a function_call item per OpenAI tool index.
    A different-kind delta arriving while another item is open closes
    the previous item before opening the new one.
    """
    response_id = _new_response_id()
    framer = _StreamFramer(
        requested_model=requested_model, response_id=response_id
    )

    # Emit response.created + response.in_progress up front. Clients
    # need this to know they have a real response object before any
    # output_item events arrive.
    for ev in framer.created_events():
        yield ev

    text_acc: str = ""
    finish_reason: str | None = None
    last_usage: ResponsesUsage | None = None

    async for chunk in chunks:
        choices = chunk.choices if hasattr(chunk, "choices") else chunk.get("choices") or []

        for choice in choices:
            delta = choice.delta if hasattr(choice, "delta") else choice.get("delta") or {}

            # Text content path.
            content_piece = (
                delta.content if hasattr(delta, "content") else delta.get("content")
            )
            if content_piece:
                if framer.current_kind != "text":
                    if framer.current_kind is not None and framer.current_kind.startswith(
                        "function_call:"
                    ):
                        oai_idx = int(framer.current_kind.split(":", 1)[1])
                        for ev in framer.close_function_call_item(oai_idx):
                            yield ev
                    for ev in framer.open_text_item():
                        yield ev
                    text_acc = ""
                text_acc += content_piece
                for ev in framer.text_delta(content_piece, accumulated=text_acc):
                    yield ev

            # Tool call path.
            tool_calls = (
                delta.tool_calls
                if hasattr(delta, "tool_calls")
                else delta.get("tool_calls")
            )
            if tool_calls:
                for tc in tool_calls:
                    if hasattr(tc, "model_dump"):
                        tc = tc.model_dump()
                    elif not isinstance(tc, dict):
                        continue
                    oai_idx = tc.get("index", 0)
                    fn = tc.get("function") or {}
                    args_piece = fn.get("arguments")
                    name = fn.get("name") or ""
                    tc_id = tc.get("id") or ""

                    # Open new function call item if first time we see
                    # this index. Close any other item first.
                    if oai_idx not in framer.tool_state:
                        if framer.current_kind == "text":
                            for ev in framer.close_text_item(accumulated=text_acc):
                                yield ev
                            text_acc = ""
                        elif framer.current_kind and framer.current_kind.startswith(
                            "function_call:"
                        ):
                            prev_idx = int(framer.current_kind.split(":", 1)[1])
                            if prev_idx != oai_idx:
                                for ev in framer.close_function_call_item(prev_idx):
                                    yield ev
                        for ev in framer.open_function_call_item(
                            oai_idx, name=name, call_id=_call_id(tc_id)
                        ):
                            yield ev
                    elif framer.current_kind != f"function_call:{oai_idx}":
                        # Switching back to this item after another —
                        # rare, but we recover by closing whatever's
                        # open and re-opening this one's frame is
                        # already emitted (item still has its initial
                        # added event). The delta event itself is
                        # safe.
                        if framer.current_kind == "text":
                            for ev in framer.close_text_item(accumulated=text_acc):
                                yield ev
                            text_acc = ""
                        framer.current_kind = f"function_call:{oai_idx}"
                        framer.current_item_id = framer.tool_state[oai_idx]["item_id"]
                    if args_piece:
                        for ev in framer.function_call_args_delta(
                            oai_idx, args_piece
                        ):
                            yield ev

            choice_finish = (
                choice.finish_reason
                if hasattr(choice, "finish_reason")
                else choice.get("finish_reason")
            )
            if choice_finish:
                finish_reason = choice_finish

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
                else usage.get("completion_tokens", 0)
                if isinstance(usage, dict)
                else 0
            )
            total_t = (
                usage.total_tokens
                if hasattr(usage, "total_tokens")
                else usage.get("total_tokens", 0) if isinstance(usage, dict) else 0
            )
            last_usage = ResponsesUsage(
                input_tokens=int(input_t or 0),
                output_tokens=int(output_t or 0),
                total_tokens=int(total_t or (input_t or 0) + (output_t or 0)),
            )

    # Close any item still open at end of stream.
    if framer.current_kind == "text":
        for ev in framer.close_text_item(accumulated=text_acc):
            yield ev
    elif framer.current_kind and framer.current_kind.startswith("function_call:"):
        prev_idx = int(framer.current_kind.split(":", 1)[1])
        for ev in framer.close_function_call_item(prev_idx):
            yield ev

    status, _incomplete = _STATUS_MAP.get(finish_reason or "stop", ("completed", None))
    for ev in framer.completed_event(status=status, usage=last_usage):
        yield ev
