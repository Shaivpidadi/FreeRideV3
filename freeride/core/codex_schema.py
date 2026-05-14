"""OpenAI Responses API (``POST /v1/responses``) Pydantic schemas.

What the OpenAI Codex CLI sends when it's pointed at a custom base URL
via the ``openai_base_url`` config key. The Responses API is *not* a
superset of Chat Completions — it has a flat tool-def shape, a unified
``input`` list of typed items, and no ``choices[]`` / ``finish_reason``.

Reference: https://platform.openai.com/docs/api-reference/responses

Scope of this revision: text + function calls. Reasoning items,
``previous_response_id`` stateful chaining, built-in tool types
(``web_search``, ``file_search``, ``code_interpreter``, etc.), and
multimodal parts (``input_image``, ``input_file``) pass through
schema-side via ``extra="allow"`` but aren't surfaced in translation.
"""

from __future__ import annotations

from typing import Any, Literal, Union

from pydantic import BaseModel, ConfigDict


_PERMISSIVE: ConfigDict = ConfigDict(extra="allow")


# ─── request ────────────────────────────────────────────────────────


class InputTextPart(BaseModel):
    type: Literal["input_text"] = "input_text"
    text: str
    model_config = _PERMISSIVE


class OutputTextPart(BaseModel):
    """Part shape used inside prior-assistant message items echoed back
    on subsequent turns. Codex emits these in transcripts."""

    type: Literal["output_text"] = "output_text"
    text: str
    annotations: list[dict[str, Any]] | None = None
    model_config = _PERMISSIVE


# Other part types (input_image, input_file) pass through unmapped.
# We accept them as opaque dicts via the BaseModel's extra="allow" on
# the parent Message item.
ContentPart = Union[InputTextPart, OutputTextPart, dict[str, Any]]


class MessageItem(BaseModel):
    """An input/output ``message`` item — analogous to a chat
    Message but with typed content parts rather than a string."""

    type: Literal["message"] = "message"
    role: Literal["user", "system", "assistant", "developer"] = "user"
    content: list[dict[str, Any]] = []
    id: str | None = None
    status: str | None = None
    model_config = _PERMISSIVE


class FunctionCallItem(BaseModel):
    """A tool invocation. In input it's an echo of a prior turn; in
    output it's what the model just produced. ``call_id`` is the
    cross-reference key — function_call_output items reference it."""

    type: Literal["function_call"] = "function_call"
    id: str | None = None
    call_id: str
    name: str
    arguments: str  # JSON-encoded string, same as Chat Completions
    status: str | None = None
    model_config = _PERMISSIVE


class FunctionCallOutputItem(BaseModel):
    """How tool results travel BACK to the model. Chat Completions
    uses ``role: "tool"`` messages; Responses uses these typed items
    in ``input``."""

    type: Literal["function_call_output"] = "function_call_output"
    call_id: str
    output: str  # stringified payload
    id: str | None = None
    model_config = _PERMISSIVE


class ReasoningItem(BaseModel):
    """o-series / gpt-5-codex reasoning blocks. We accept and pass
    through but don't translate to upstream free providers — they
    don't emit reasoning items themselves."""

    type: Literal["reasoning"] = "reasoning"
    id: str | None = None
    summary: list[dict[str, Any]] = []
    encrypted_content: str | None = None
    model_config = _PERMISSIVE


# Union of every input/output item type. Pydantic v2 picks the right
# variant via the ``type`` discriminator (since each model fixes it
# to a Literal).
InputItem = Union[
    MessageItem,
    FunctionCallItem,
    FunctionCallOutputItem,
    ReasoningItem,
    dict[str, Any],  # opaque escape hatch for item types we don't know
]


class FunctionTool(BaseModel):
    """Responses-API tool def — FLAT (no nested ``function`` key like
    Chat Completions has). The translator unwraps Chat-Completions-shape
    tools[] to this when going one direction and re-wraps the other."""

    type: Literal["function"] = "function"
    name: str
    description: str | None = None
    parameters: dict[str, Any] | None = None
    strict: bool | None = None
    model_config = _PERMISSIVE


class ReasoningConfig(BaseModel):
    effort: str | None = None  # "low" | "medium" | "high"
    summary: str | None = None  # "auto" | "concise" | "detailed"
    model_config = _PERMISSIVE


class ResponsesRequest(BaseModel):
    """Top-level POST /v1/responses body. ``input`` is either a plain
    string (shorthand for a single user message) or a list of typed
    items. ``instructions`` is a system-prompt-like prefix that's
    *not* an item but lives separately."""

    model: str
    input: Union[str, list[InputItem]] = ""
    instructions: str | None = None
    tools: list[FunctionTool] | None = None
    tool_choice: Any = None  # "auto" | "none" | "required" | {"type":"function","name":"..."}
    max_output_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    stream: bool = False
    parallel_tool_calls: bool | None = None
    previous_response_id: str | None = None
    reasoning: ReasoningConfig | None = None
    truncation: str | None = None
    include: list[str] | None = None
    metadata: dict[str, Any] | None = None
    user: str | None = None
    store: bool | None = None
    model_config = _PERMISSIVE


# ─── response ───────────────────────────────────────────────────────


class ResponsesUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    input_tokens_details: dict[str, Any] | None = None
    output_tokens_details: dict[str, Any] | None = None
    model_config = _PERMISSIVE


class IncompleteDetails(BaseModel):
    reason: str | None = None  # "max_output_tokens" | "content_filter" | ...
    model_config = _PERMISSIVE


class ResponsesResponse(BaseModel):
    """Top-level response body. No ``choices[]`` — ``output`` is a
    flat list of items in emission order. Terminal state lives on
    ``status`` (+ ``incomplete_details`` when degraded)."""

    id: str
    object: Literal["response"] = "response"
    created_at: int
    status: Literal["completed", "in_progress", "failed", "incomplete"] = "completed"
    model: str
    output: list[dict[str, Any]] = []
    usage: ResponsesUsage | None = None
    incomplete_details: IncompleteDetails | None = None
    error: dict[str, Any] | None = None
    tools: list[FunctionTool] | None = None
    tool_choice: Any = None
    parallel_tool_calls: bool | None = None
    previous_response_id: str | None = None
    reasoning: ReasoningConfig | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_output_tokens: int | None = None
    metadata: dict[str, Any] | None = None
    model_config = _PERMISSIVE
