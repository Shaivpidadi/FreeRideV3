"""Provider Protocol conformance suite.

Every concrete :class:`~freeride.core.provider.Provider` implementation
that lands in this repo must pass these tests. The suite is parameterized
over a registry list — when a new provider lands, it adds itself to
``CONFORMANT_PROVIDERS`` and the same checks apply.

The checks are deliberately minimal — Protocol shape and basic invariants
only. Behavior tests (real API round-trips, error mapping accuracy) live
in per-provider test modules under ``tests/providers/``.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from freeride.core.provider import PROVIDER_API_VERSION, Provider
from freeride.providers.cerebras import CerebrasProvider
from freeride.providers.cloudflare_wai import CloudflareWAIProvider
from freeride.providers.groq import GroqProvider
from freeride.providers.huggingface import HuggingFaceProvider
from freeride.providers.nvidia_nim import NVIDIANIMProvider
from freeride.providers.ollama import OllamaProvider
from freeride.providers.openrouter import OpenRouterProvider
from tests.fixtures.noop_provider import NoopProvider


# Registry: every Provider class that lands ships an entry here.
CONFORMANT_PROVIDERS: list[type] = [
    NoopProvider,
    OpenRouterProvider,
    NVIDIANIMProvider,
    GroqProvider,
    CloudflareWAIProvider,
    HuggingFaceProvider,
    OllamaProvider,
    CerebrasProvider,
]


# Some providers need extra constructor args. Register them here so the
# generic fixture can build them without per-class branching.
PROVIDER_KWARGS: dict[type, dict[str, Any]] = {
    CloudflareWAIProvider: {"account_id": "00000000000000000000000000000000"},
}


@pytest.fixture(params=CONFORMANT_PROVIDERS, ids=lambda cls: cls.__name__)
def provider_class(request: pytest.FixtureRequest) -> type:
    return request.param


@pytest.fixture
def provider(provider_class: type) -> Any:
    return provider_class(**PROVIDER_KWARGS.get(provider_class, {}))


@pytest.fixture(autouse=True)
def _stub_outbound_http(provider_class: type, httpx_mock):
    """For non-Noop providers, stub all outbound HTTP to canned 200s so the
    conformance suite can exercise list_free_models/probe without real keys
    or network. NoopProvider doesn't make network calls, so it's a no-op.
    """
    if provider_class is NoopProvider:
        return
    httpx_mock.add_response(
        json={
            "data": [],
            "id": "stub",
            "object": "chat.completion",
            "created": 0,
            "model": "stub",
            "choices": [],
        },
        is_reusable=True,
    )


@pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
class TestProviderConformance:
    def test_runtime_isinstance(self, provider: Any):
        assert isinstance(provider, Provider), (
            f"{type(provider).__name__} does not satisfy the Provider Protocol"
        )

    def test_has_name(self, provider: Any):
        assert isinstance(provider.name, str)
        assert provider.name  # non-empty

    def test_api_version_matches_core(self, provider: Any):
        assert provider.api_version == PROVIDER_API_VERSION, (
            f"{type(provider).__name__}.api_version={provider.api_version} "
            f"!= PROVIDER_API_VERSION={PROVIDER_API_VERSION}"
        )

    def test_attribution_headers_returns_dict(self, provider: Any):
        headers = provider.attribution_headers()
        assert isinstance(headers, dict)
        # Keys and values must be strings (HTTP header constraint)
        for k, v in headers.items():
            assert isinstance(k, str)
            assert isinstance(v, str)

    def test_auth_header_returns_dict_with_authorization(self, provider: Any):
        headers = provider.auth_header("dummy-key")
        assert isinstance(headers, dict)
        # Conventionally Authorization, but the Protocol doesn't mandate
        # which header — it just mandates a dict[str, str].
        for k, v in headers.items():
            assert isinstance(k, str)
            assert isinstance(v, str)

    def test_classify_error_returns_errorkind(self, provider: Any):
        from freeride.core.errors import ErrorKind

        kind = provider.classify_error(Exception("synthetic"))
        assert isinstance(kind, ErrorKind)

    def test_retry_after_hint_returns_int_or_none(self, provider: Any):
        out = provider.retry_after_hint(None)
        assert out is None or isinstance(out, int)

    def test_list_free_models_returns_list(self, provider: Any):
        models = provider.list_free_models("dummy-key")
        assert isinstance(models, list)
        # Each entry must be a Model — checked at the type level, here we
        # verify duck-typed attrs since dataclasses don't pass isinstance
        # for Protocols cleanly.
        for m in models:
            assert hasattr(m, "api_id")
            assert hasattr(m, "provider")

    def test_probe_returns_proberesult(self, provider: Any):
        from freeride.core.types import ProbeResult

        result = provider.probe("noop/echo", "dummy-key")
        assert isinstance(result, ProbeResult)

    def test_forward_chat_is_async(self, provider_class: type):
        # Inspect the unbound method on the class so we don't have to
        # actually call it to verify it's a coroutine.
        method = getattr(provider_class, "forward_chat")
        assert inspect.iscoroutinefunction(method), (
            f"{provider_class.__name__}.forward_chat must be `async def`"
        )

    def test_forward_chat_stream_is_async_generator(self, provider_class: type):
        method = getattr(provider_class, "forward_chat_stream")
        assert inspect.isasyncgenfunction(method), (
            f"{provider_class.__name__}.forward_chat_stream must be an async generator"
        )

    def test_embeddings_optin_is_consistent(self, provider_class: type):
        """If a provider declares ``embeddings_supported = True``, it must
        also implement ``forward_embeddings`` as an async coroutine.
        ``False`` (or the missing-attribute default) means no embedding
        support and ``forward_embeddings`` may be absent.
        """
        if getattr(provider_class, "embeddings_supported", False):
            method = getattr(provider_class, "forward_embeddings", None)
            assert method is not None, (
                f"{provider_class.__name__}.embeddings_supported = True but "
                "no forward_embeddings method defined"
            )
            assert inspect.iscoroutinefunction(method), (
                f"{provider_class.__name__}.forward_embeddings must be `async def`"
            )
