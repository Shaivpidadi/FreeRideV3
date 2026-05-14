"""Bidirectional translation between Google's Generative Language API
shape and OpenAI's Chat Completions shape.

The ``gemini`` CLI sends Google-format requests to whatever
``GOOGLE_GEMINI_BASE_URL`` points at; we accept those, translate them
to OpenAI shape so the existing provider failover machinery can route
them, then translate the OpenAI response back to Google shape so the
CLI parses it natively.

Translation tables in this module are intentionally narrow — we only
map fields we understand. Anything Google adds that we don't recognize
passes through schema-side (``extra="allow"``) but is not surfaced to
upstream OpenAI providers (they'd 400 on unknown args). On the way
back, OpenAI extensions we don't surface explicitly are dropped — the
CLI would 400 on them anyway.

Multimodal parts (inlineData, fileData) and safetySettings are not
translated in this revision. Text + function_call + function_response
cover the agentic CLI use case.
"""

from __future__ import annotations

import json
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
from freeride.core.gemini_schema import (
    Candidate,
    Content,
    FunctionCall,
    GeminiGenerateRequest,
    GeminiGenerateResponse,
    Part,
    UsageMetadata,
)


# ─── request: Gemini → OpenAI ──────────────────────────────────────


def _join_text_parts(parts: list[Part]) -> str:
    """Concatenate consecutive text parts into one string. Google
    allows multiple text parts in a single Content; OpenAI expects one
    string per message."""
    return "".join(p.text for p in parts if p.text)


def _content_to_openai_messages(content: Content) -> list[Message]:
    """Convert one Google Content (a single turn from a single role)
    into one or more OpenAI Messages.

    Multi-part Content is normal in Google's format — a model turn can
    contain text *and* function calls in the same Content. OpenAI splits
    these: a text message AND an assistant message with tool_calls.
    Per OpenAI's rules an assistant message can carry both text content
    AND tool_calls in the same message, so we keep them together when
    they share a turn.

    For role="function" (legacy Google role for tool results) we emit
    one OpenAI ``role: tool`` message per FunctionResponse part.
    """
    role = content.role or "user"
    parts = content.parts

    if role == "user":
        text = _join_text_parts(parts)
        # User turns can also carry FunctionResponse parts (newer
        # Google convention puts tool results under role=user instead
        # of role=function). Split those out into tool messages.
        tool_results: list[Message] = []
        for p in parts:
            if p.function_response is not None:
                tool_results.append(
                    Message(
                        role="tool",
                        # OpenAI tool messages stringify the response
                        # body — Google's structured response gets
                        # JSON-encoded for transport.
                        content=json.dumps(p.function_response.response),
                        tool_call_id=p.function_response.name,
                    )
                )
        msgs: list[Message] = []
        if text:
            msgs.append(Message(role="user", content=text))
        msgs.extend(tool_results)
        return msgs

    if role == "model":
        # Assistant turn: text + optional function_calls in one message.
        text = _join_text_parts(parts)
        tool_calls: list[ToolCall] = []
        for p in parts:
            if p.function_call is not None:
                tool_calls.append(
                    ToolCall(
                        id=p.function_call.name + "_" + uuid.uuid4().hex[:8],
                        type="function",
                        function=ToolCallFunction(
                            name=p.function_call.name,
                            arguments=json.dumps(p.function_call.args),
                        ),
                    )
                )
        return [
            Message(
                role="assistant",
                content=text if text else None,
                tool_calls=tool_calls if tool_calls else None,
            )
        ]

    if role == "function":
        # Legacy: role=function with FunctionResponse parts. Each one
        # becomes an OpenAI tool message.
        return [
            Message(
                role="tool",
                content=json.dumps(p.function_response.response),
                tool_call_id=p.function_response.name,
            )
            for p in parts
            if p.function_response is not None
        ]

    # Unknown role — pass through as user text.
    return [Message(role="user", content=_join_text_parts(parts))]


def _map_function_calling_mode(mode: str | None) -> Any:
    """Google's modes → OpenAI tool_choice values.

    AUTO → "auto" (default — model decides)
    NONE → "none" (model can't call tools)
    ANY  → "required" (model MUST call some tool)
    """
    if mode is None:
        return None
    m = mode.upper()
    if m == "AUTO":
        return "auto"
    if m == "NONE":
        return "none"
    if m == "ANY":
        return "required"
    return None  # unknown modes drop


def gemini_to_openai_request(req: GeminiGenerateRequest, model_id: str) -> ChatRequest:
    """Translate a Gemini /generateContent request body into the OpenAI
    Chat Completions shape that our provider chain expects.

    ``model_id`` comes from the URL path
    (``/v1beta/models/<model>:generateContent``) and is set on the
    OpenAI request so the auto-resolver / claude-cli pin path sees it.
    """
    messages: list[Message] = []

    # System instruction lands as a role="system" prefix on the
    # OpenAI message list.
    if req.system_instruction is not None:
        text = _join_text_parts(req.system_instruction.parts)
        if text:
            messages.append(Message(role="system", content=text))

    for content in req.contents:
        messages.extend(_content_to_openai_messages(content))

    # Tools: flatten Google's nested functionDeclarations into OpenAI's
    # top-level tools[] list of {type:function, function:{...}}.
    tools: list[ToolDef] | None = None
    if req.tools:
        tools = []
        for tool in req.tools:
            for fd in tool.function_declarations:
                tools.append(
                    ToolDef(
                        type="function",
                        function=ToolFunctionDef(
                            name=fd.name,
                            description=fd.description,
                            parameters=fd.parameters or {},
                        ),
                    )
                )
        if not tools:
            tools = None  # empty list → omit

    tool_choice = None
    if req.tool_config and req.tool_config.function_calling_config:
        tool_choice = _map_function_calling_mode(
            req.tool_config.function_calling_config.mode
        )

    gc = req.generation_config
    chat_kwargs: dict[str, Any] = {
        "model": model_id,
        "messages": messages,
    }
    if tools is not None:
        chat_kwargs["tools"] = tools
    if tool_choice is not None:
        chat_kwargs["tool_choice"] = tool_choice
    if gc is not None:
        if gc.max_output_tokens is not None:
            chat_kwargs["max_tokens"] = gc.max_output_tokens
        if gc.temperature is not None:
            chat_kwargs["temperature"] = gc.temperature
        if gc.top_p is not None:
            chat_kwargs["top_p"] = gc.top_p
        if gc.stop_sequences:
            chat_kwargs["stop"] = list(gc.stop_sequences)
        # top_k has no OpenAI equivalent — dropped.
        # response_mime_type: only "application/json" maps cleanly to
        # OpenAI's response_format={"type":"json_object"}. Other values
        # are dropped.
        if gc.response_mime_type == "application/json":
            chat_kwargs["response_format"] = {"type": "json_object"}

    return ChatRequest(**chat_kwargs)


# ─── response: OpenAI → Gemini ─────────────────────────────────────


# OpenAI finish_reason → Google finishReason. OpenAI's set is narrower;
# anything we don't know maps to OTHER per Google's spec.
_FINISH_REASON_MAP = {
    "stop": "STOP",
    "length": "MAX_TOKENS",
    "tool_calls": "TOOL_CALL",
    "function_call": "TOOL_CALL",  # legacy openai field
    "content_filter": "SAFETY",
}


def _map_finish_reason(openai_reason: str | None) -> str | None:
    if openai_reason is None:
        return None
    return _FINISH_REASON_MAP.get(openai_reason, "OTHER")


def _openai_message_to_parts(
    text: str | None, tool_calls: list[ToolCall] | None
) -> list[Part]:
    """Build a Google parts[] list from an OpenAI assistant message's
    text + tool_calls. Text goes first (Google convention), then one
    Part per function_call."""
    parts: list[Part] = []
    if text:
        parts.append(Part(text=text))
    if tool_calls:
        for tc in tool_calls:
            # OpenAI's arguments is a JSON-encoded string; Google's
            # args is a structured object. Tolerate malformed JSON the
            # same way anthropic_translate does — empty dict beats 500.
            try:
                args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                if not isinstance(args, dict):
                    args = {}
            except (ValueError, TypeError):
                args = {}
            parts.append(
                Part(
                    function_call=FunctionCall(
                        name=tc.function.name,
                        args=args,
                    )
                )
            )
    return parts


def openai_to_gemini_response(
    resp: ChatResponse, request_model: str
) -> GeminiGenerateResponse:
    """Translate an OpenAI ChatResponse back to Gemini's response shape.

    ``request_model`` is echoed on ``modelVersion`` so SDK clients see
    the id they asked for, not the resolved free-tier model.
    """
    candidates: list[Candidate] = []
    if resp.choices:
        choice = resp.choices[0]
        msg = choice.message
        parts = _openai_message_to_parts(msg.content, msg.tool_calls)
        # Google requires content.parts to be non-empty when a candidate
        # is present; emit a zero-length text part if there's literally
        # nothing (rare but happens when finish_reason fires immediately).
        if not parts:
            parts = [Part(text="")]
        candidates.append(
            Candidate(
                content=Content(role="model", parts=parts),
                finish_reason=_map_finish_reason(choice.finish_reason),
                index=0,
            )
        )

    usage = None
    if resp.usage is not None:
        usage = UsageMetadata(
            prompt_token_count=int(resp.usage.prompt_tokens or 0),
            candidates_token_count=int(resp.usage.completion_tokens or 0),
            total_token_count=int(resp.usage.total_tokens or 0),
        )

    return GeminiGenerateResponse(
        candidates=candidates,
        usage_metadata=usage,
        model_version=request_model,
    )


# ─── streaming: OpenAI chunks → Gemini SSE events ──────────────────


def _sse(payload: dict[str, Any]) -> bytes:
    """Emit one SSE event in Google's format: a single `data:` line
    with JSON, separated by a blank line. No `event:` line — Google
    uses unnamed events."""
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n".encode("utf-8")


async def stream_openai_to_gemini(
    chunks: AsyncIterator[Any],
    *,
    request_model: str,
) -> AsyncIterator[bytes]:
    """Consume OpenAI streaming chunks and emit Gemini SSE events.

    Google's wire format is "incremental complete responses": each SSE
    chunk is a full GeminiGenerateResponse-shaped object, but with the
    *partial* piece in candidates[0].content.parts. The client
    concatenates ``.text`` across chunks and accumulates each
    ``functionCall`` as a single complete unit.

    Behavior:
    * Each OpenAI text delta → one Gemini chunk with that delta in
      parts[0].text.
    * OpenAI tool_call deltas arrive as partial JSON in
      ``function.arguments``. We buffer per tool_call index and emit
      ONE Gemini chunk with the assembled functionCall when its JSON
      parses cleanly. (Google doesn't natively stream partial
      function calls — clients expect them whole.)
    * The final chunk (when finish_reason arrives) carries an empty
      candidate plus finishReason and aggregated usageMetadata.
    """
    # Per-tool-call state. OpenAI's tool_calls[i].index is a per-call
    # counter; we buffer name + JSON args until the JSON parses, then
    # emit a Gemini chunk and reset.
    tool_buffers: dict[int, dict[str, str]] = {}
    tool_emitted: dict[int, bool] = {}

    finish_reason: str | None = None
    last_usage: UsageMetadata | None = None

    async for chunk in chunks:
        choices = chunk.choices if hasattr(chunk, "choices") else chunk.get("choices") or []

        for choice in choices:
            delta = choice.delta if hasattr(choice, "delta") else choice.get("delta") or {}

            # Text delta → one Gemini chunk.
            text_piece = (
                delta.content if hasattr(delta, "content") else delta.get("content")
            )
            if text_piece:
                yield _sse(
                    {
                        "candidates": [
                            {
                                "content": {
                                    "role": "model",
                                    "parts": [{"text": text_piece}],
                                },
                                "index": 0,
                            }
                        ],
                        "modelVersion": request_model,
                    }
                )

            # Tool-call deltas: buffer until JSON args complete.
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
                    idx = tc.get("index", 0)
                    if idx not in tool_buffers:
                        tool_buffers[idx] = {"name": "", "args": ""}
                        tool_emitted[idx] = False
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        tool_buffers[idx]["name"] = fn["name"]
                    args_piece = fn.get("arguments")
                    if args_piece:
                        tool_buffers[idx]["args"] += args_piece
                    # Try to emit if we haven't yet and the args parse.
                    if not tool_emitted[idx] and tool_buffers[idx]["name"]:
                        raw = tool_buffers[idx]["args"]
                        try:
                            parsed_args = json.loads(raw) if raw else {}
                            if not isinstance(parsed_args, dict):
                                parsed_args = {}
                        except (ValueError, TypeError):
                            parsed_args = None
                        if parsed_args is not None:
                            tool_emitted[idx] = True
                            yield _sse(
                                {
                                    "candidates": [
                                        {
                                            "content": {
                                                "role": "model",
                                                "parts": [
                                                    {
                                                        "functionCall": {
                                                            "name": tool_buffers[idx][
                                                                "name"
                                                            ],
                                                            "args": parsed_args,
                                                        }
                                                    }
                                                ],
                                            },
                                            "index": 0,
                                        }
                                    ],
                                    "modelVersion": request_model,
                                }
                            )

            choice_finish = (
                choice.finish_reason
                if hasattr(choice, "finish_reason")
                else choice.get("finish_reason")
            )
            if choice_finish:
                finish_reason = choice_finish

        # Track usage from any chunk that carries it (typically the
        # final empty-choices chunk).
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
            last_usage = UsageMetadata(
                prompt_token_count=int(input_t or 0),
                candidates_token_count=int(output_t or 0),
                total_token_count=int(total_t or (input_t or 0) + (output_t or 0)),
            )

    # Emit any unflushed tool buffers (model finished but JSON was
    # malformed end-to-end — emit with empty args so the client at
    # least sees the function name).
    for idx, buf in tool_buffers.items():
        if tool_emitted.get(idx) or not buf["name"]:
            continue
        yield _sse(
            {
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [
                                {"functionCall": {"name": buf["name"], "args": {}}}
                            ],
                        },
                        "index": 0,
                    }
                ],
                "modelVersion": request_model,
            }
        )

    # Final chunk: finishReason + usage. Google ships these on a
    # candidates entry with no new parts.
    final: dict[str, Any] = {
        "candidates": [
            {
                "content": {"role": "model", "parts": []},
                "finishReason": _map_finish_reason(finish_reason) or "STOP",
                "index": 0,
            }
        ],
        "modelVersion": request_model,
    }
    if last_usage is not None:
        final["usageMetadata"] = last_usage.model_dump(by_alias=True, exclude_none=True)
    yield _sse(final)
