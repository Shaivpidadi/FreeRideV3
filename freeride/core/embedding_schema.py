"""OpenAI-compatible Pydantic schemas for /v1/embeddings traffic.

Embeddings have a much smaller schema than chat — input is a string or
list of strings, output is a flat list of vectors. We mirror the
public OpenAI shape so any client that already speaks OpenAI works.

Deliberately permissive: ``extra='allow'`` so provider-specific
fields (e.g. ``encoding_format``, ``dimensions``) pass through.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class EmbeddingRequest(BaseModel):
    """OpenAI-compatible /v1/embeddings request."""

    model: str
    input: str | list[str] | list[int] | list[list[int]]
    encoding_format: Literal["float", "base64"] | None = None
    dimensions: int | None = None
    user: str | None = None

    model_config = ConfigDict(extra="allow")


class EmbeddingObject(BaseModel):
    """One embedding row inside a /v1/embeddings response."""

    object: Literal["embedding"] = "embedding"
    index: int
    embedding: list[float] | str  # base64-encoded string when encoding_format=base64

    model_config = ConfigDict(extra="allow")


class EmbeddingUsage(BaseModel):
    prompt_tokens: int = 0
    total_tokens: int = 0

    model_config = ConfigDict(extra="allow")


class EmbeddingResponse(BaseModel):
    """OpenAI-compatible /v1/embeddings response."""

    object: Literal["list"] = "list"
    data: list[EmbeddingObject]
    model: str
    usage: EmbeddingUsage = Field(default_factory=EmbeddingUsage)

    model_config = ConfigDict(extra="allow")
