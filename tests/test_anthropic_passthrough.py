"""Tests for the Anthropic Messages passthrough handler.

Two layers covered:

1. ``relay_to_anthropic`` unit-tested against a mocked Anthropic
   endpoint (httpx_mock fixture). Verifies header forwarding, body
   relay, status mirroring, streaming pipe-through, and transport-
   error handling.

2. The ``/v1/messages`` route integration — given a claude-* model id
   + auth, the route hits the passthrough; given freeride/* or
   no-auth, it hits the free path.

We never use a real api.anthropic.com URL in CI.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from freeride.core.anthropic_passthrough import (
    ANTHROPIC_API_URL,
    _auth_fingerprint,
    _peek_streaming,
    _select_forwarded_headers,
    _select_response_headers,
    relay_to_anthropic,
)
from freeride.server.app import create_app


# ─── unit: helpers ───────────────────────────────────────────────


def test_auth_fingerprint_is_short_and_deterministic() -> None:
    fp1 = _auth_fingerprint("Bearer sk-ant-oat01-abc")
    fp2 = _auth_fingerprint("Bearer sk-ant-oat01-abc")
    assert fp1 == fp2
    assert len(fp1) == 8
    # Different tokens produce different fingerprints
    assert _auth_fingerprint("Bearer different") != fp1


def test_auth_fingerprint_empty_token_is_empty_string() -> None:
    assert _auth_fingerprint("") == ""


def test_select_forwarded_headers_keeps_auth_and_anthropic_specific() -> None:
    inbound = {
        "authorization": "Bearer sk-ant-oat01-xxx",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "messages-2023-12-15",
        "content-type": "application/json",
        "user-agent": "anthropic-sdk-python/0.18",
        # The following must be dropped
        "host": "localhost:11343",
        "x-freeride-force-provider": "openrouter",
        "x-forwarded-for": "10.0.0.1",
        "cookie": "session=evil",
    }
    out = _select_forwarded_headers(inbound)
    assert out["authorization"] == "Bearer sk-ant-oat01-xxx"
    assert out["anthropic-version"] == "2023-06-01"
    assert out["anthropic-beta"] == "messages-2023-12-15"
    assert out["content-type"] == "application/json"
    assert "host" not in out
    assert "x-freeride-force-provider" not in out
    assert "cookie" not in out


def test_select_forwarded_headers_drops_empty_values() -> None:
    """Headers with empty values are dropped — relaying an empty
    Authorization would give Anthropic a worse error than just
    omitting it."""
    out = _select_forwarded_headers({"authorization": "", "anthropic-version": "2023-06-01"})
    assert "authorization" not in out
    assert out["anthropic-version"] == "2023-06-01"


def test_select_forwarded_headers_accepts_x_api_key() -> None:
    out = _select_forwarded_headers({"x-api-key": "sk-ant-api03-yyy"})
    assert out["x-api-key"] == "sk-ant-api03-yyy"


def test_select_response_headers_drops_hop_by_hop_and_content_length() -> None:
    """Anthropic's response will carry hop-by-hop headers that don't
    travel through proxies — drop them. Also content-length, which
    Starlette will recompute."""
    upstream = httpx.Headers(
        {
            "content-type": "application/json",
            "content-length": "123",
            "transfer-encoding": "chunked",
            "connection": "keep-alive",
            "anthropic-ratelimit-requests-remaining": "99",
            "request-id": "req_abc",
        }
    )
    out = _select_response_headers(upstream)
    assert out.get("content-type") == "application/json"
    assert "content-length" not in {k.lower() for k in out}
    assert "transfer-encoding" not in {k.lower() for k in out}
    assert "connection" not in {k.lower() for k in out}
    # Rate-limit + request-id headers are useful to the client
    assert "anthropic-ratelimit-requests-remaining" in {k.lower() for k in out}
    assert "request-id" in {k.lower() for k in out}


def test_peek_streaming_detects_stream_true() -> None:
    body = json.dumps({"model": "claude-sonnet-4-6", "stream": True}).encode()
    assert _peek_streaming(body) is True


def test_peek_streaming_returns_false_when_absent() -> None:
    body = json.dumps({"model": "claude-sonnet-4-6"}).encode()
    assert _peek_streaming(body) is False


def test_peek_streaming_returns_false_for_malformed_json() -> None:
    """We don't want to pre-empt Anthropic's validation. Malformed
    JSON gets relayed (and Anthropic returns its 400)."""
    assert _peek_streaming(b"not json at all") is False
    assert _peek_streaming(b"") is False


# ─── unit: relay_to_anthropic — non-streaming ────────────────────


@pytest.mark.asyncio
async def test_relay_buffered_forwards_body_and_auth(httpx_mock) -> None:
    """The exact request bytes must arrive at Anthropic, and the
    Authorization header must come along."""
    body = b'{"model":"claude-sonnet-4-6","max_tokens":10,"messages":[{"role":"user","content":"hi"}]}'
    httpx_mock.add_response(
        url=ANTHROPIC_API_URL,
        method="POST",
        json={
            "id": "msg_real",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-6",
            "content": [{"type": "text", "text": "Hi back."}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 4, "output_tokens": 3},
        },
    )

    resp = await relay_to_anthropic(
        body_bytes=body,
        inbound_headers={
            "authorization": "Bearer sk-ant-oat01-test",
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        request_id="req_test_123",
        model_id="claude-sonnet-4-6",
    )
    assert resp.status_code == 200
    assert resp.headers["X-FreeRide-Provider"] == "anthropic-passthrough"
    assert resp.headers["X-FreeRide-Request-Id"] == "req_test_123"

    # Verify the outbound request to Anthropic carried what we expect
    outbound = httpx_mock.get_request()
    assert outbound is not None
    assert outbound.content == body
    assert outbound.headers["authorization"] == "Bearer sk-ant-oat01-test"
    assert outbound.headers["anthropic-version"] == "2023-06-01"


@pytest.mark.asyncio
async def test_relay_buffered_mirrors_upstream_4xx(httpx_mock) -> None:
    """If Anthropic returns 401, we mirror that — the caller MUST
    see the original error so they know to re-auth, not get a 500
    from our wrapper."""
    httpx_mock.add_response(
        url=ANTHROPIC_API_URL,
        method="POST",
        status_code=401,
        json={
            "type": "error",
            "error": {"type": "authentication_error", "message": "invalid x-api-key"},
        },
    )
    resp = await relay_to_anthropic(
        body_bytes=b'{"model":"claude-opus-4-5","max_tokens":1,"messages":[]}',
        inbound_headers={"x-api-key": "bad-key"},
        request_id="req_x",
        model_id="claude-opus-4-5",
    )
    assert resp.status_code == 401
    body = json.loads(resp.body)
    assert body["error"]["type"] == "authentication_error"


@pytest.mark.asyncio
async def test_relay_buffered_transport_error_becomes_502(httpx_mock) -> None:
    """If we can't reach api.anthropic.com at all, surface a 502
    with a clear message — distinct from a 5xx from Anthropic itself."""
    httpx_mock.add_exception(httpx.ConnectError("dns failure"))
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await relay_to_anthropic(
            body_bytes=b'{"model":"claude-sonnet-4-6","max_tokens":1,"messages":[]}',
            inbound_headers={"authorization": "Bearer x"},
            request_id="req_y",
            model_id="claude-sonnet-4-6",
        )
    assert exc.value.status_code == 502
    detail = exc.value.detail
    assert "api.anthropic.com" in detail["error"]["message"]
    assert detail["error"]["request_id"] == "req_y"


# ─── unit: relay_to_anthropic — streaming ────────────────────────


@pytest.mark.asyncio
async def test_relay_streaming_pipes_bytes_through(httpx_mock) -> None:
    """For stream:true bodies, Anthropic's SSE bytes flow through
    unchanged. We don't translate or buffer."""
    sse_payload = (
        b"event: message_start\n"
        b'data: {"type":"message_start"}\n\n'
        b"event: content_block_delta\n"
        b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Hi"}}\n\n'
        b"event: message_stop\n"
        b'data: {"type":"message_stop"}\n\n'
    )
    httpx_mock.add_response(
        url=ANTHROPIC_API_URL,
        method="POST",
        status_code=200,
        content=sse_payload,
        headers={"content-type": "text/event-stream"},
    )
    resp = await relay_to_anthropic(
        body_bytes=b'{"model":"claude-sonnet-4-6","max_tokens":10,"stream":true,"messages":[]}',
        inbound_headers={"authorization": "Bearer x"},
        request_id="req_stream",
        model_id="claude-sonnet-4-6",
    )

    from fastapi.responses import StreamingResponse

    assert isinstance(resp, StreamingResponse)
    assert resp.media_type == "text/event-stream"
    assert resp.headers["X-FreeRide-Provider"] == "anthropic-passthrough"

    # Drain the body iterator and confirm bytes match.
    chunks: list[bytes] = []
    async for chunk in resp.body_iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode())
    received = b"".join(chunks)
    assert b"message_start" in received
    assert b"message_stop" in received
    assert b'"text":"Hi"' in received


# ─── route integration: ``/v1/messages`` ─────────────────────────


def _app_with_no_providers():
    """Build a real FastAPI app with NO providers. Lets us verify the
    routing decision branches without any real provider plugin being
    available — the free path would 503 anyway."""
    return create_app(providers=[])


def test_route_claude_with_auth_hits_passthrough(httpx_mock) -> None:
    """End-to-end: a request with claude-* + Authorization goes to
    Anthropic, NOT the free-route's provider chain."""
    httpx_mock.add_response(
        url=ANTHROPIC_API_URL,
        method="POST",
        json={
            "id": "msg_relayed",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-6",
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )
    client = TestClient(_app_with_no_providers())
    r = client.post(
        "/v1/messages",
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 10,
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers={"Authorization": "Bearer sk-ant-oat01-test"},
    )
    assert r.status_code == 200
    assert r.headers["X-FreeRide-Provider"] == "anthropic-passthrough"
    assert r.json()["id"] == "msg_relayed"


def test_route_freeride_model_skips_passthrough(httpx_mock) -> None:
    """freeride/* model ids must go to the free route even with auth
    present. We don't mock Anthropic here — if the route mistakenly
    routes to passthrough, httpx_mock will fail because no mock is set."""
    client = TestClient(_app_with_no_providers())
    r = client.post(
        "/v1/messages",
        json={
            "model": "freeride/free",
            "max_tokens": 10,
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers={"Authorization": "Bearer sk-ant-oat01-test"},
    )
    # Free path with no providers registered → 503 (NOT 200 from
    # passthrough). The status alone proves we took the free branch.
    assert r.status_code == 503
    body = r.json()
    assert body["detail"]["error"]["type"] == "api_error"


def test_route_claude_no_auth_falls_back_to_free() -> None:
    """claude-* without an auth header falls back to free providers.
    With zero providers registered, that surfaces as a 503 — but
    crucially NOT a passthrough attempt."""
    client = TestClient(_app_with_no_providers())
    r = client.post(
        "/v1/messages",
        json={
            "model": "claude-opus-4-5",
            "max_tokens": 10,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.status_code == 503


def test_route_malformed_json_returns_400() -> None:
    client = TestClient(_app_with_no_providers())
    r = client.post(
        "/v1/messages",
        content=b"this is not json at all",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error"]["type"] == "invalid_request_error"


# ─── preset → provider re-ordering at the route layer ───────────


class _PresetStubProvider:
    """Minimal Provider stub: records that forward_chat was called and
    returns a canned response. Used to verify which provider was
    picked first by the route."""

    api_version = 1

    def __init__(self, name: str):
        from unittest.mock import AsyncMock

        from freeride.core.chat_schema import ChatResponse

        self.name = name
        self.embeddings_supported = False
        self._response = ChatResponse.model_validate(
            {
                "id": "chatcmpl-stub",
                "object": "chat.completion",
                "created": 0,
                "model": "stub-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }
        )
        self.forward_chat = AsyncMock(side_effect=self._do_chat)

    async def _do_chat(self, request, model_id, key):  # noqa: ARG002
        return self._response

    def classify_error(self, x):  # noqa: ARG002
        from freeride.core.errors import ErrorKind

        return ErrorKind.UNKNOWN

    def retry_after_hint(self, response):  # noqa: ARG002
        return None


def _make_preset_test_client(monkeypatch, providers):
    """Build an app with stub providers + bypass the smart-router
    catalog requirement (we're testing chain order, not catalog
    ranking)."""
    from freeride.core.health import ProviderHealth

    monkeypatch.setenv("FREERIDE_EVENTS", "0")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("GROQ_API_KEY", "k")
    monkeypatch.setenv("CEREBRAS_API_KEY", "k")
    monkeypatch.setenv("NVIDIA_API_KEY", "k")
    # Bypass cooldown so all keys are always available
    monkeypatch.setattr(
        "freeride.core.cooldown.KeyCooldown.available_keys",
        lambda self, name, keys: list(keys),
    )
    # Bypass auto-model catalog: pretend we always resolve fine
    async def fake_catalog(*a, **kw):  # noqa: ARG001
        return []

    monkeypatch.setattr(
        "freeride.server.routes.messages.get_or_fetch_catalog", fake_catalog
    )
    monkeypatch.setattr(
        "freeride.server.routes.messages.resolve_auto_model",
        lambda providers, catalog: ("stub-model", providers[0].name if providers else None),
    )
    ProviderHealth.reset()
    return TestClient(create_app(providers=providers))


def test_route_preset_fast_pulls_groq_to_front_of_chain(monkeypatch) -> None:
    """When the request is freeride/fast and both openrouter and groq
    are registered (openrouter first by registration order), the
    preset must re-order so groq serves the request."""
    openrouter = _PresetStubProvider("openrouter")
    groq = _PresetStubProvider("groq")
    client = _make_preset_test_client(monkeypatch, [openrouter, groq])
    r = client.post(
        "/v1/messages",
        json={
            "model": "freeride/fast",
            "max_tokens": 10,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.status_code == 200
    assert r.headers["X-FreeRide-Provider"] == "groq"
    groq.forward_chat.assert_awaited_once()
    openrouter.forward_chat.assert_not_awaited()


def test_route_preset_quality_pulls_openrouter_first(monkeypatch) -> None:
    """freeride/quality prefers OpenRouter. Register groq first
    (would normally win by registration order) and confirm
    openrouter gets the call instead."""
    groq = _PresetStubProvider("groq")
    openrouter = _PresetStubProvider("openrouter")
    client = _make_preset_test_client(monkeypatch, [groq, openrouter])
    r = client.post(
        "/v1/messages",
        json={
            "model": "freeride/quality",
            "max_tokens": 10,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.status_code == 200
    assert r.headers["X-FreeRide-Provider"] == "openrouter"
    openrouter.forward_chat.assert_awaited_once()


def test_route_preset_free_uses_registration_order(monkeypatch) -> None:
    """freeride/free has empty preset preference — falls through to
    the existing health-ranked/registration order. Whoever is first
    in the registered chain wins."""
    openrouter = _PresetStubProvider("openrouter")
    groq = _PresetStubProvider("groq")
    client = _make_preset_test_client(monkeypatch, [openrouter, groq])
    r = client.post(
        "/v1/messages",
        json={
            "model": "freeride/free",
            "max_tokens": 10,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.status_code == 200
    assert r.headers["X-FreeRide-Provider"] == "openrouter"


def test_route_claude_cli_pins_to_code_tools_model(monkeypatch) -> None:
    """When User-Agent is claude-cli AND the user picked the bare
    freeride/free default, pin to a specific code+tools-capable
    model on OpenRouter (openrouter/free). Smart-router's
    auto-resolution roulette is bypassed — it tends to return
    openai/gpt-oss-120b which doesn't reliably trigger tool_calls,
    or groq's llama-3.3-70b-versatile which 413s on large payloads."""
    captured = {}
    groq = _PresetStubProvider("groq")
    openrouter = _PresetStubProvider("openrouter")
    original = openrouter._do_chat

    async def capture(request, model_id, key):
        captured["model_id"] = model_id
        captured["request_model"] = request.model
        return await original(request, model_id, key)

    openrouter.forward_chat.side_effect = capture
    # Register groq FIRST so registration order doesn't trick us
    client = _make_preset_test_client(monkeypatch, [groq, openrouter])
    r = client.post(
        "/v1/messages",
        json={
            "model": "freeride/free",
            "max_tokens": 10,
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers={"User-Agent": "claude-cli/2.1.139 (external, cli)"},
    )
    assert r.status_code == 200
    # MUST land on openrouter because we pinned the provider, even though
    # groq is registered first.
    assert r.headers["X-FreeRide-Provider"] == "openrouter"
    # MUST use openrouter/free (the pinned model), not the
    # smart-router's auto-resolution pick.
    assert captured["request_model"] == "openrouter/free"


def test_route_non_claude_cli_freeride_free_no_pin(monkeypatch) -> None:
    """Same request but from a different User-Agent (e.g., curl or
    a python script) — freeride/free stays as free, no pin. The
    smart-router auto-resolves as normal."""
    captured = {}
    groq = _PresetStubProvider("groq")
    original = groq._do_chat

    async def cap(request, model_id, key):
        captured["request_model"] = request.model
        return await original(request, model_id, key)

    groq.forward_chat.side_effect = cap
    openrouter = _PresetStubProvider("openrouter")
    client = _make_preset_test_client(monkeypatch, [groq, openrouter])
    r = client.post(
        "/v1/messages",
        json={
            "model": "freeride/free",
            "max_tokens": 10,
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers={"User-Agent": "curl/8.4.0"},
    )
    assert r.status_code == 200
    # No pin → model is "auto" (rewritten from freeride/free by route)
    # and registration order picks groq first.
    assert r.headers["X-FreeRide-Provider"] == "groq"
    # The pinned model id should NOT have been applied.
    assert captured["request_model"] != "openrouter/free"


def test_route_claude_cli_all_presets_pinned(monkeypatch) -> None:
    """Updated 0.4.0a10: claude-cli pins for ALL freeride/* presets,
    not just /free. /fast, /quality, /coding all route through the
    tools-capable model because Claude Code is useless without
    tool calling — preset is a HINT, tool support is non-negotiable.
    """
    captured = {}
    groq = _PresetStubProvider("groq")
    openrouter = _PresetStubProvider("openrouter")
    original = openrouter._do_chat

    async def cap(request, model_id, key):
        captured["request_model"] = request.model
        return await original(request, model_id, key)

    openrouter.forward_chat.side_effect = cap
    # groq registered first so default order would prefer it
    client = _make_preset_test_client(monkeypatch, [groq, openrouter])
    r = client.post(
        "/v1/messages",
        json={
            "model": "freeride/coding",  # NOT free — proves pin fires for any preset
            "max_tokens": 10,
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers={"User-Agent": "claude-cli/2.1.139"},
    )
    assert r.status_code == 200
    # Pin MUST fire even though preset is "coding" — claude-cli always
    # gets the tools-capable pin.
    assert r.headers["X-FreeRide-Provider"] == "openrouter"
    assert captured["request_model"] == "openrouter/free"


def test_route_claude_cli_pin_env_override(monkeypatch) -> None:
    """FREERIDE_CLAUDE_CODE_MODEL env var overrides the default pin."""
    monkeypatch.setenv("FREERIDE_CLAUDE_CODE_MODEL", "qwen-3-coder")
    monkeypatch.setenv("FREERIDE_CLAUDE_CODE_PROVIDER", "openrouter")
    captured = {}
    openrouter = _PresetStubProvider("openrouter")
    original = openrouter._do_chat

    async def cap(request, model_id, key):
        captured["request_model"] = request.model
        return await original(request, model_id, key)

    openrouter.forward_chat.side_effect = cap
    groq = _PresetStubProvider("groq")
    client = _make_preset_test_client(monkeypatch, [openrouter, groq])
    r = client.post(
        "/v1/messages",
        json={
            "model": "freeride/free",
            "max_tokens": 10,
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers={"User-Agent": "claude-cli/2.1.139"},
    )
    assert r.status_code == 200
    assert r.headers["X-FreeRide-Provider"] == "openrouter"
    assert captured["request_model"] == "qwen-3-coder"


def test_route_free_strips_tools_from_request(monkeypatch) -> None:
    """Claude Code 2.x sends ~70 tools in every request. Free
    providers can't handle that. When routing to free we MUST drop
    the tools array so the upstream request fits within provider
    constraints. The user opted into free; they get a text answer.

    We assert by checking what gets passed to the stub provider's
    forward_chat — the ChatRequest seen there must have tools=None.
    """
    captured = {}
    openrouter = _PresetStubProvider("openrouter")
    original = openrouter._do_chat

    async def capturing_do_chat(request, model_id, key):
        captured["request"] = request
        return await original(request, model_id, key)

    openrouter.forward_chat.side_effect = capturing_do_chat
    client = _make_preset_test_client(monkeypatch, [openrouter])
    r = client.post(
        "/v1/messages",
        json={
            "model": "freeride/free",
            "max_tokens": 10,
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {"name": f"tool_{i}", "input_schema": {"type": "object"}}
                for i in range(70)
            ],
            "tool_choice": {"type": "auto"},
        },
    )
    assert r.status_code == 200
    # The request that reached the provider must have NO tools.
    assert captured["request"].tools is None
    assert captured["request"].tool_choice is None


def test_route_passthrough_preserves_tools(monkeypatch, httpx_mock) -> None:
    """Counterpart of the above: when routing to PASSTHROUGH (claude-*
    + auth), the raw body is relayed untouched. Tools MUST survive —
    that's the entire point of the passthrough path, the user wants
    their agentic Claude Code session to work end-to-end."""
    httpx_mock.add_response(
        url=ANTHROPIC_API_URL,
        method="POST",
        json={
            "id": "msg_x",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-6",
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )
    client = TestClient(_app_with_no_providers())
    payload = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 10,
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [
            {"name": "Agent", "description": "x", "input_schema": {"type": "object"}}
        ],
    }
    r = client.post(
        "/v1/messages",
        json=payload,
        headers={"Authorization": "Bearer sk-ant-oat01-test"},
    )
    assert r.status_code == 200
    # The OUTBOUND body to Anthropic must still contain the tools
    out = httpx_mock.get_request()
    relayed_body = json.loads(out.content)
    assert relayed_body["tools"][0]["name"] == "Agent"


def test_route_preset_preference_falls_back_when_first_pick_absent(monkeypatch) -> None:
    """freeride/fast prefers [groq, cerebras, nvidia_nim]. If groq
    isn't registered but cerebras is, cerebras should win — the
    preference walks the list in order, not just the first item."""
    cerebras = _PresetStubProvider("cerebras")
    openrouter = _PresetStubProvider("openrouter")
    client = _make_preset_test_client(monkeypatch, [openrouter, cerebras])
    r = client.post(
        "/v1/messages",
        json={
            "model": "freeride/fast",
            "max_tokens": 10,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.status_code == 200
    assert r.headers["X-FreeRide-Provider"] == "cerebras"


def test_route_passthrough_relays_x_api_key(httpx_mock) -> None:
    """x-api-key flow (raw API key, not OAuth) must also passthrough.
    Confirms we don't accidentally hard-code on Authorization."""
    httpx_mock.add_response(
        url=ANTHROPIC_API_URL,
        method="POST",
        json={
            "id": "msg_ok",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-6",
            "content": [],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    )
    client = TestClient(_app_with_no_providers())
    r = client.post(
        "/v1/messages",
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 10,
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers={"x-api-key": "sk-ant-api03-direct"},
    )
    assert r.status_code == 200
    assert r.headers["X-FreeRide-Provider"] == "anthropic-passthrough"
    # Check the outbound request carried x-api-key (not Authorization)
    out = httpx_mock.get_request()
    assert out.headers.get("x-api-key") == "sk-ant-api03-direct"
    assert "authorization" not in {k.lower() for k in out.headers.keys()}
