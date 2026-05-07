"""OpenAI-compatible Pydantic schemas for /v1/chat/completions traffic.

Deliberately permissive: ``extra='allow'`` so provider-specific fields
(NIM's ``nvext``, vLLM's ``reasoning_content``, etc.) pass through.
Provider plugins are responsible for scrubbing their own extensions
before forwarding upstream traffic to clients.

We mirror the request and response shapes only loosely — the goal is
type-safety on the fields *FreeRide itself* reads (model, messages,
stream, tools, response_format), not full OpenAI fidelity.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ----- request side ---------------------------------------------------------


class ContentPartText(BaseModel):
    type: Literal["text"]
    text: str
    model_config = ConfigDict(extra="allow")


class ContentPartImageUrl(BaseModel):
    url: str
    detail: Literal["auto", "low", "high"] | None = None
    model_config = ConfigDict(extra="allow")


class ContentPartImage(BaseModel):
    type: Literal["image_url"]
    image_url: ContentPartImageUrl
    model_config = ConfigDict(extra="allow")


# A message's content is either a plain string or a list of content parts.
MessageContent = str | list[ContentPartText | ContentPartImage | dict[str, Any]]


class ToolCallFunction(BaseModel):
    name: str
    arguments: str  # JSON-encoded string per OpenAI spec
    model_config = ConfigDict(extra="allow")


class ToolCall(BaseModel):
    id: str
    type: Literal["function"] = "function"
    function: ToolCallFunction
    model_config = ConfigDict(extra="allow")


class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: MessageContent | None = None
    name: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    model_config = ConfigDict(extra="allow")


class ToolFunctionDef(BaseModel):
    name: str
    description: str | None = None
    parameters: dict[str, Any] | None = None
    model_config = ConfigDict(extra="allow")


class ToolDef(BaseModel):
    type: Literal["function"] = "function"
    function: ToolFunctionDef
    model_config = ConfigDict(extra="allow")


class ResponseFormat(BaseModel):
    """OpenAI's response_format. Type can be ``text``, ``json_object`` or
    ``json_schema``; the json_schema variant carries an additional ``json_schema``
    object (nested), and providers without structured-output support need to be
    skipped by the resolver.
    """

    type: Literal["text", "json_object", "json_schema"]
    json_schema: dict[str, Any] | None = None
    model_config = ConfigDict(extra="allow")


class ChatRequest(BaseModel):
    """Inbound /v1/chat/completions request. Permissive on extras."""

    model: str
    messages: list[Message]
    max_tokens: int | None = None
    max_completion_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    n: int | None = None
    stream: bool = False
    stop: str | list[str] | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    logit_bias: dict[str, float] | None = None
    user: str | None = None
    tools: list[ToolDef] | None = None
    tool_choice: str | dict[str, Any] | None = None
    response_format: ResponseFormat | None = None
    seed: int | None = None
    logprobs: bool | None = None
    top_logprobs: int | None = None
    model_config = ConfigDict(extra="allow")

    def is_streaming(self) -> bool:
        return bool(self.stream)


# ----- response side --------------------------------------------------------


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    model_config = ConfigDict(extra="allow")


class ChoiceMessage(BaseModel):
    role: str | None = None
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    refusal: str | None = None
    model_config = ConfigDict(extra="allow")


class Choice(BaseModel):
    index: int
    message: ChoiceMessage
    finish_reason: str | None = None
    logprobs: dict[str, Any] | None = None
    model_config = ConfigDict(extra="allow")


class ChatResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[Choice]
    usage: Usage | None = None
    system_fingerprint: str | None = None
    model_config = ConfigDict(extra="allow")


# ----- streaming ------------------------------------------------------------


class StreamDelta(BaseModel):
    role: str | None = None
    content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    refusal: str | None = None
    model_config = ConfigDict(extra="allow")


class StreamChoice(BaseModel):
    index: int
    delta: StreamDelta
    finish_reason: str | None = None
    logprobs: dict[str, Any] | None = None
    model_config = ConfigDict(extra="allow")


class ChatStreamEvent(BaseModel):
    """A single SSE event from /v1/chat/completions when stream=True.

    NIM emits its final ``usage`` on the **penultimate** event with empty
    ``choices: []``; the ``[DONE]`` sentinel that follows is not represented
    by this model — providers handle it as a stream termination.
    """

    id: str
    object: str = "chat.completion.chunk"
    created: int
    model: str
    choices: list[StreamChoice] = Field(default_factory=list)
    usage: Usage | None = None
    model_config = ConfigDict(extra="allow")
