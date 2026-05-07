"""Core typed shapes used across the FreeRide gateway.

Kept deliberately small and Pydantic-free here — these are interchange
types between providers, the resolver, and the CLI. Pydantic models that
mirror the OpenAI wire schema live in `chat_schema.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from freeride.core.errors import ErrorKind


@dataclass(frozen=True, slots=True)
class Model:
    """A single inference model surfaced by some provider.

    `api_id` is the provider's native ID (e.g. ``meta/llama-3.1-8b-instruct``,
    ``qwen/qwen3-coder:free``). `provider` is the FreeRide ``Provider.name``
    that lists it. ``raw`` carries the unparsed catalog object so plugins
    can stash provider-specific fields without expanding this dataclass.
    """

    api_id: str
    provider: str
    context_length: int = 0
    output_modalities: tuple[str, ...] = ("text",)
    supported_parameters: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)

    def supports(self, capability: str) -> bool:
        """True if this model declares the given supported_parameters capability."""
        return capability in self.supported_parameters


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """Outcome of a single ``Provider.probe`` call.

    ``error`` is None on success and an :class:`ErrorKind` on failure.
    We deliberately avoid exposing raw HTTP responses here — that's the
    provider's job to classify.
    """

    ok: bool
    error: ErrorKind | None = None
    latency_ms: int = 0
