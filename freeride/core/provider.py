"""The provider plugin contract.

A :class:`Provider` is anything that exposes free-tier inference: OpenRouter,
NVIDIA NIM, Groq, Cloudflare Workers AI, etc. Plugins ship as separate pip
packages and register via the ``freeride.providers`` entry point group;
core has no per-provider knowledge beyond what this Protocol declares.

The contract is **versioned from day one** as ``api_version = 1``. Future
breaking changes to the Protocol bump this number; older plugins keep
working because the loader filters on ``api_version`` at registration time.
This is decision D5 in the design plan
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Protocol, runtime_checkable

from freeride.core.chat_schema import ChatRequest, ChatResponse, ChatStreamEvent
from freeride.core.errors import ErrorKind
from freeride.core.types import Model, ProbeResult


PROVIDER_API_VERSION = 1
"""The current Provider Protocol version. Plugins declare matching
``api_version`` to be loaded. Bumped only on breaking changes."""


@runtime_checkable
class Provider(Protocol):
    """Free-tier inference provider plugin contract.

    Implementations are stateless w.r.t. credentials — every method takes
    a ``key`` so the gateway can rotate across multiple keys without
    reinstantiating the plugin. The plugin holds only configuration that
    doesn't vary per request (base URL, attribution metadata).

    Async surface
    -------------
    ``forward_chat`` and ``forward_chat_stream`` are async because the
    server is async end-to-end. ``list_free_models`` and ``probe`` come
    in sync flavors because they're called from the CLI as well; provider
    implementations can wrap an internal async impl with ``asyncio.run``
    where convenient.

    Error classification
    --------------------
    ``classify_error`` accepts whatever the provider got back — an
    ``httpx.Response``, a raised exception, etc. — and returns an
    :class:`~freeride.core.errors.ErrorKind`. The retry loop branches
    on this enum, never on raw responses.
    """

    # ----- identity -------------------------------------------------------
    name: str
    """Stable plugin identifier (e.g. "openrouter", "nvidia_nim"). Used
    for namespacing keys in the cooldown pool, in stats, and in the
    Telegram beacon's ``providers_active`` list."""

    api_version: int
    """Set to :data:`PROVIDER_API_VERSION` on the implementing class.
    The loader skips plugins whose ``api_version`` doesn't match."""

    # ----- discovery & probing -------------------------------------------
    def list_free_models(self, key: str) -> list[Model]:
        """Return the provider's free-tier-eligible chat models.

        Implementations decide what "free" means per provider — see
        ``docs/providers/SURVEY.md`` for the three patterns
        (per-model flag, global free budget, per-model RPM/TPM caps).
        """
        ...

    def probe(self, model_id: str, key: str) -> ProbeResult:
        """Live-test ``model_id`` with the cheapest possible call
        (canonically a chat completion with ``max_tokens=5``). Returns
        ``ok=True`` on a 2xx, otherwise an :class:`ErrorKind` in the
        result and ``ok=False``.
        """
        ...

    # ----- request forwarding (the gateway's hot path) -------------------
    async def forward_chat(
        self, request: ChatRequest, model_id: str, key: str
    ) -> ChatResponse:
        """Non-streaming chat completion. Pass ``request`` upstream verbatim
        modulo provider-specific scrubbing (e.g., NIM's ``nvext``).
        """
        ...

    def forward_chat_stream(
        self, request: ChatRequest, model_id: str, key: str
    ) -> AsyncIterator[ChatStreamEvent]:
        """Streaming chat completion. Returns an async iterator over SSE
        events. Implementations are async generators; the Protocol
        declares the call shape only. Provider plugins swallow the
        ``[DONE]`` sentinel and stop iteration.
        """
        ...

    # ----- error classification ------------------------------------------
    def classify_error(self, response_or_exc: Any) -> ErrorKind:
        """Classify a raw response or raised exception into an :class:`ErrorKind`.

        Provider-specific quirks live here — e.g., NIM uses HTTP 403 (not
        401) for invalid keys, and serves a ``text/plain`` 404 for unknown
        models (not a JSON error envelope). See per-provider docs in
        ``docs/providers/``.
        """
        ...

    def retry_after_hint(self, response: Any) -> int | None:
        """Extract a ``Retry-After`` hint in seconds, if the provider
        exposes one. Most don't; the gateway falls back to its own
        cooldown when this returns ``None``.
        """
        ...

    # ----- request stamping ----------------------------------------------
    def auth_header(self, key: str) -> dict[str, str]:
        """The authentication header(s) for an outbound request. Almost
        always ``{"Authorization": f"Bearer {key}"}`` but providers are
        free to override.
        """
        ...

    def attribution_headers(self) -> dict[str, str]:
        """Headers that identify FreeRide as the calling application.

        Optional — providers without an app-attribution mechanism return
        ``{}``. OpenRouter's ``HTTP-Referer`` + ``X-Title`` is the
        canonical example. NIM accepts but ignores these headers; it has
        no equivalent.
        """
        ...
