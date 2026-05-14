"""Google Generative Language API (Gemini) Pydantic schemas.

Models the wire shape of POST ``/v1beta/models/<model>:generateContent``
and ``:streamGenerateContent``. This is what the official ``gemini`` CLI
(github.com/google-gemini/gemini-cli, backed by ``@google/genai``)
sends when it's pointed at a base URL via ``GOOGLE_GEMINI_BASE_URL``.

Permissive (``extra="allow"``) on every model — Google adds fields
quietly (safetySettings, cachedContent, etc.) and we'd rather pass them
through untouched than 400 on unknown keys. We translate the fields we
*understand* and forward the rest verbatim.

Reference: https://ai.google.dev/api/generate-content
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


# Google's REST API speaks camelCase ("systemInstruction",
# "functionCall", "maxOutputTokens", ...). Python convention is
# snake_case. Pydantic's to_camel alias generator handles the bridge:
# fields are declared snake_case but accepted/emitted as camelCase.
# populate_by_name=True keeps snake_case names valid for direct
# instantiation in our own code/tests.
_GOOGLE_MODEL_CONFIG: ConfigDict = ConfigDict(
    extra="allow",
    alias_generator=to_camel,
    populate_by_name=True,
)


# ─── request ────────────────────────────────────────────────────────


class FunctionCall(BaseModel):
    """A model-emitted tool invocation. ``args`` is a structured object
    (already JSON-decoded), NOT a string — different from OpenAI's
    ``arguments`` which is a JSON-encoded string."""

    name: str
    args: dict[str, Any] = {}
    model_config = _GOOGLE_MODEL_CONFIG


class FunctionResponse(BaseModel):
    """Caller-supplied tool result, sent back to the model after it
    emitted a FunctionCall."""

    name: str
    response: dict[str, Any] = {}
    model_config = _GOOGLE_MODEL_CONFIG


class InlineData(BaseModel):
    """Base64-encoded media (images, audio). We pass through but the
    free-tier providers we route to mostly only handle text — the
    request will likely fail downstream if a non-text part hits a
    text-only model. Caller should pick a multimodal model id."""

    mime_type: str
    data: str  # base64
    model_config = _GOOGLE_MODEL_CONFIG


class FileData(BaseModel):
    """Reference to a file uploaded via the File API."""

    mime_type: str | None = None
    file_uri: str
    model_config = _GOOGLE_MODEL_CONFIG


class Part(BaseModel):
    """One slot in a Content.parts array. Exactly one of these fields
    should be populated per Google's spec, but we don't enforce that —
    permissive parsing keeps quirky clients working."""

    text: str | None = None
    function_call: FunctionCall | None = None
    function_response: FunctionResponse | None = None
    inline_data: InlineData | None = None
    file_data: FileData | None = None
    # Some clients send a "thought" boolean alongside text for thinking
    # traces; we treat as a regular text part for routing purposes.
    thought: bool | None = None
    model_config = _GOOGLE_MODEL_CONFIG


# Google's role enum: "user", "model", "function" (deprecated, now uses
# "user" with FunctionResponse parts). We accept all three for input
# compatibility but emit only "user"/"model" on the way back.
ContentRole = Literal["user", "model", "function"]


class Content(BaseModel):
    role: ContentRole | None = None
    parts: list[Part] = []
    model_config = _GOOGLE_MODEL_CONFIG


class FunctionDeclaration(BaseModel):
    """A tool the model can call. ``parameters`` is a JSON Schema object
    (same shape OpenAI uses for ``function.parameters``)."""

    name: str
    description: str | None = None
    parameters: dict[str, Any] | None = None
    model_config = _GOOGLE_MODEL_CONFIG


class Tool(BaseModel):
    """Google nests tool defs under ``functionDeclarations[]`` instead
    of being flat at the top level like OpenAI does."""

    function_declarations: list[FunctionDeclaration] = []
    model_config = _GOOGLE_MODEL_CONFIG


class FunctionCallingConfig(BaseModel):
    # "AUTO" / "ANY" / "NONE". "ANY" means must call SOME tool —
    # closest OpenAI mapping is tool_choice="required".
    mode: str | None = None
    allowed_function_names: list[str] | None = None
    model_config = _GOOGLE_MODEL_CONFIG


class ToolConfig(BaseModel):
    function_calling_config: FunctionCallingConfig | None = None
    model_config = _GOOGLE_MODEL_CONFIG


class GenerationConfig(BaseModel):
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None  # no OpenAI equivalent — dropped in translation
    max_output_tokens: int | None = None
    stop_sequences: list[str] | None = None
    response_mime_type: str | None = None  # "application/json" → openai response_format
    candidate_count: int | None = None  # we only support 1 candidate today
    model_config = _GOOGLE_MODEL_CONFIG


class GeminiGenerateRequest(BaseModel):
    """Top-level request body for :generateContent and
    :streamGenerateContent. The model id comes from the URL path, not
    this body — the route handler injects it before translation."""

    contents: list[Content] = []
    system_instruction: Content | None = None
    tools: list[Tool] | None = None
    tool_config: ToolConfig | None = None
    generation_config: GenerationConfig | None = None
    model_config = _GOOGLE_MODEL_CONFIG


# ─── response ───────────────────────────────────────────────────────


class UsageMetadata(BaseModel):
    prompt_token_count: int = 0
    candidates_token_count: int = 0
    total_token_count: int = 0
    model_config = _GOOGLE_MODEL_CONFIG


# Google's finish reasons. The most common: STOP (natural end),
# MAX_TOKENS, SAFETY, TOOL_CALL (renamed from FUNCTION_CALL in some
# SDK versions). Less common: RECITATION, OTHER. We map OpenAI's
# narrower set to these in the translator.
GeminiFinishReason = Literal[
    "STOP",
    "MAX_TOKENS",
    "SAFETY",
    "RECITATION",
    "TOOL_CALL",
    "FUNCTION_CALL",
    "OTHER",
    "FINISH_REASON_UNSPECIFIED",
]


class Candidate(BaseModel):
    content: Content
    finish_reason: GeminiFinishReason | None = None
    index: int = 0
    safety_ratings: list[dict[str, Any]] | None = None
    model_config = _GOOGLE_MODEL_CONFIG


class GeminiGenerateResponse(BaseModel):
    candidates: list[Candidate] = []
    usage_metadata: UsageMetadata | None = None
    model_version: str | None = None
    model_config = _GOOGLE_MODEL_CONFIG
