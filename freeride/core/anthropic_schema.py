"""Pydantic schemas for Anthropic's Messages API.

Mirrors the spec at
https://platform.claude.com/docs/en/api/messages closely enough to
parse what Claude Code, the official ``@anthropic-ai/sdk``, and
hand-rolled Anthropic clients send. Permissive on input
(``extra='ignore'`` so Anthropic adding new fields doesn't break us),
explicit on output (so consumers see exactly the shape they expect).

Phase 1 deliberately drops these on input even if present:

- ``thinking`` (extended thinking — free models can't reason about it)
- ``cache_control`` (no OpenAI equivalent)
- ``service_tier``, ``inference_geo``, ``container``, ``output_config``
- ``metadata.user_id`` (no per-user tracking on free providers)

And rejects (with a 400) on input:

- ``document`` content blocks (free models don't support PDF/text doc input)
- ``search_result``, ``*_tool_result`` server-side blocks
  (require Anthropic-side infra we can't replicate)
"""

from __future__ import annotations

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


# ─── content blocks ─────────────────────────────────────────────────


class _AnthropicBlockBase(BaseModel):
    model_config = ConfigDict(extra="ignore")


class AnthropicTextBlock(_AnthropicBlockBase):
    type: Literal["text"]
    text: str


class AnthropicImageSource(_AnthropicBlockBase):
    """Two flavors per the spec: base64-encoded data, or remote URL."""

    type: Literal["base64", "url"]
    media_type: Optional[str] = None  # required on base64; ignored on url
    data: Optional[str] = None  # required on base64; ignored on url
    url: Optional[str] = None  # required on url; ignored on base64


class AnthropicImageBlock(_AnthropicBlockBase):
    type: Literal["image"]
    source: AnthropicImageSource


class AnthropicToolUseBlock(_AnthropicBlockBase):
    """Emitted by the assistant when it decides to call a tool."""

    type: Literal["tool_use"]
    id: str
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class AnthropicToolResultBlock(_AnthropicBlockBase):
    """Sent BACK by the user (caller) carrying the tool's output for a
    prior tool_use. Anthropic accepts both string and structured content
    for the inner value."""

    type: Literal["tool_result"]
    tool_use_id: str
    content: Union[str, list[dict[str, Any]]] = ""
    is_error: bool = False


AnthropicContentBlock = Union[
    AnthropicTextBlock,
    AnthropicImageBlock,
    AnthropicToolUseBlock,
    AnthropicToolResultBlock,
]


# ─── messages ───────────────────────────────────────────────────────


class AnthropicMessage(BaseModel):
    """One element of the request's ``messages`` array.

    ``content`` can be a plain string OR an array of content blocks per
    the spec. We accept both shapes; the translator normalizes to a
    single representation downstream.
    """

    model_config = ConfigDict(extra="ignore")

    role: Literal["user", "assistant"]
    content: Union[str, list[dict[str, Any]]]


class AnthropicSystemBlock(_AnthropicBlockBase):
    """Anthropic accepts ``system`` as either a top-level string OR a
    list of TextBlockParam-shaped dicts. We support both via
    ``Union[str, list[...]]`` on the request model directly."""

    type: Literal["text"]
    text: str


class AnthropicToolDefinition(BaseModel):
    """One entry in the request's ``tools`` array."""

    model_config = ConfigDict(extra="ignore")

    name: str
    description: Optional[str] = None
    input_schema: dict[str, Any] = Field(default_factory=dict)


class AnthropicToolChoice(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: Literal["auto", "any", "tool", "none"]
    name: Optional[str] = None
    disable_parallel_tool_use: Optional[bool] = None


# ─── request ────────────────────────────────────────────────────────


class AnthropicMessagesRequest(BaseModel):
    """``POST /v1/messages`` request body."""

    model_config = ConfigDict(extra="ignore")

    model: str
    messages: list[AnthropicMessage]
    max_tokens: int

    system: Optional[Union[str, list[dict[str, Any]]]] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None  # dropped in translation — no OpenAI equivalent
    stop_sequences: Optional[list[str]] = None
    stream: bool = False
    tools: Optional[list[AnthropicToolDefinition]] = None
    tool_choice: Optional[AnthropicToolChoice] = None
    metadata: Optional[dict[str, Any]] = None  # mostly dropped

    # Phase-1-dropped fields (accepted to avoid 400s, ignored on translation):
    thinking: Optional[dict[str, Any]] = None
    cache_control: Optional[dict[str, Any]] = None
    service_tier: Optional[str] = None
    inference_geo: Optional[str] = None
    container: Optional[Any] = None
    output_config: Optional[Any] = None


# ─── response ───────────────────────────────────────────────────────


AnthropicStopReason = Literal[
    "end_turn",
    "max_tokens",
    "stop_sequence",
    "tool_use",
    "pause_turn",
    "refusal",
]


class AnthropicUsage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: Optional[int] = None
    cache_read_input_tokens: Optional[int] = None


class AnthropicMessagesResponse(BaseModel):
    """``POST /v1/messages`` non-streaming response body."""

    model_config = ConfigDict(extra="ignore")

    id: str
    type: Literal["message"] = "message"
    role: Literal["assistant"] = "assistant"
    model: str
    content: list[dict[str, Any]]  # mix of text / tool_use blocks
    stop_reason: AnthropicStopReason
    stop_sequence: Optional[str] = None
    usage: AnthropicUsage
