"""Pydantic schemas for the fx gateway wire dialect.

The ridex agent (our fork of vercel-labs/fx) talks the Vercel AI
Gateway "language model" protocol, not OpenAI Chat Completions. The
authoritative spec is fx's own source — ``src/gateway/vercel_protocol.zig``
builds the request, ``src/gateway/client.zig`` parses the response —
plus the executable fixtures in fx's ``tests/e2e/tmux-helpers.ts``
fake gateway.

Request body shape (built by ``buildGatewayRequestBodyValidated``):

.. code-block:: json

    {
      "prompt": [
        {"role": "system", "content": "<string>"},
        {"role": "user", "content": [{"type": "text", "text": "..."}]},
        {"role": "assistant", "content": [
            {"type": "text", "text": "..."},
            {"type": "tool-call", "toolCallId": "c1", "toolName": "Write",
             "input": {"path": "..."}}]},
        {"role": "tool", "content": [
            {"type": "tool-result", "toolCallId": "c1", "toolName": "Write",
             "output": {"type": "text", "value": "ok"}}]}
      ],
      "tools": [{"type": "function", "name": "Write",
                 "description": "...", "inputSchema": {...}}],
      "toolChoice": {"type": "auto"},
      "maxOutputTokens": 4096,
      "reasoning": "medium",
      "responseFormat": {"type": "json", "name": "...", "schema": {...}},
      "providerOptions": {...},
      "headers": {"user-agent": "..."}
    }

Model id and streaming mode arrive as HTTP headers
(``ai-language-model-id``, ``ai-language-model-streaming``), NOT in
the body.

Deliberately permissive (``extra="allow"``) like the other wire
schemas — fx evolves fast and unknown fields must not 400.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Header names fx sends on every chat request (see gatewayExtraHeaders
# in fx's src/gateway/client.zig).
FX_MODEL_HEADER = "ai-language-model-id"
FX_STREAMING_HEADER = "ai-language-model-streaming"
FX_SPEC_VERSION_HEADER = "ai-language-model-specification-version"


class FxMessage(BaseModel):
    """One entry of the ``prompt`` array.

    ``content`` is a plain string for system messages and a list of
    typed parts (``text`` / ``tool-call`` / ``tool-result`` / ``file``)
    for user/assistant/tool messages. Parts stay raw dicts — the
    translator picks out the types it understands and skips the rest.
    """

    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[dict[str, Any]] | None = None
    providerOptions: dict[str, Any] | None = None  # e.g. anthropic cacheControl
    model_config = ConfigDict(extra="allow")


class FxRequest(BaseModel):
    """Inbound ``POST /v3/ai/language-model`` request body."""

    prompt: list[FxMessage]
    tools: list[dict[str, Any]] = Field(default_factory=list)
    toolChoice: dict[str, Any] | str | None = None
    maxOutputTokens: int | None = None
    reasoning: str | None = None
    responseFormat: dict[str, Any] | None = None
    providerOptions: dict[str, Any] | None = None
    headers: dict[str, Any] | None = None
    model_config = ConfigDict(extra="allow")
