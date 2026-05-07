"""A trivial Provider implementation for the conformance suite.

NoopProvider exists to:

* Confirm the :class:`~freeride.core.provider.Provider` Protocol is
  implementable using only public types from ``freeride.core``.
* Serve as a test double for higher-level components (resolver, retry
  loop) that need *some* Provider but don't care which.

It returns minimal-but-valid values from every method. Real providers
land in ``freeride/providers/`` and must pass the same conformance
suite.
"""

from __future__ import annotations

import time
from typing import Any, AsyncIterator

from freeride.core.chat_schema import (
    ChatRequest,
    ChatResponse,
    ChatStreamEvent,
    Choice,
    ChoiceMessage,
    StreamChoice,
    StreamDelta,
    Usage,
)
from freeride.core.errors import ErrorKind
from freeride.core.provider import PROVIDER_API_VERSION
from freeride.core.types import Model, ProbeResult


class NoopProvider:
    """Stateless test double. Stable behavior: every call succeeds with empty/zero values."""

    name: str = "noop"
    api_version: int = PROVIDER_API_VERSION

    def list_free_models(self, key: str) -> list[Model]:
        return [
            Model(
                api_id="noop/echo",
                provider=self.name,
                context_length=4096,
                supported_parameters=("tools",),
            )
        ]

    def probe(self, model_id: str, key: str) -> ProbeResult:
        return ProbeResult(ok=True, latency_ms=0)

    async def forward_chat(
        self, request: ChatRequest, model_id: str, key: str
    ) -> ChatResponse:
        return ChatResponse(
            id=f"noop-{int(time.time())}",
            created=int(time.time()),
            model=model_id,
            choices=[
                Choice(
                    index=0,
                    message=ChoiceMessage(role="assistant", content=""),
                    finish_reason="stop",
                )
            ],
            usage=Usage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
        )

    async def forward_chat_stream(
        self, request: ChatRequest, model_id: str, key: str
    ) -> AsyncIterator[ChatStreamEvent]:
        yield ChatStreamEvent(
            id=f"noop-{int(time.time())}",
            created=int(time.time()),
            model=model_id,
            choices=[
                StreamChoice(
                    index=0,
                    delta=StreamDelta(role="assistant", content=""),
                    finish_reason="stop",
                )
            ],
        )

    def classify_error(self, response_or_exc: Any) -> ErrorKind:
        return ErrorKind.UNKNOWN

    def retry_after_hint(self, response: Any) -> int | None:
        return None

    def auth_header(self, key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {key}"}

    def attribution_headers(self) -> dict[str, str]:
        return {}
