"""Integration tests for the fx gateway dialect routes
(``POST /v3/ai/language-model`` + ``GET /coding-agent/v1/models``)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from freeride.server.app import create_app


def _client() -> TestClient:
    return TestClient(create_app(providers=[]))


_MINIMAL_BODY = {
    "prompt": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
    "tools": [],
    "toolChoice": {"type": "auto"},
}


def test_models_endpoint_serves_presets_with_no_providers() -> None:
    """fx validates its API key by GETting this path — any 200 means
    "key accepted", which is what lets ridex run on a dummy Bearer.
    The preset ids must be present even before any provider key is
    configured so the fx model picker isn't empty."""
    r = _client().get(
        "/coding-agent/v1/models", headers={"authorization": "Bearer dummy"}
    )
    assert r.status_code == 200
    data = r.json()["data"]
    ids = [m["id"] for m in data]
    assert "freeride/coding" in ids
    assert "auto" in ids
    coding = next(m for m in data if m["id"] == "freeride/coding")
    assert coding["type"] == "language"
    assert "tool-use" in coding["tags"]


def test_malformed_json_returns_400() -> None:
    r = _client().post(
        "/v3/ai/language-model",
        content=b"not json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 400
    assert "JSON" in r.json()["detail"]["error"]["message"]


def test_missing_prompt_field_returns_400() -> None:
    r = _client().post("/v3/ai/language-model", json={"tools": []})
    assert r.status_code == 400
    assert "validation" in r.json()["detail"]["error"]["message"].lower()


def test_valid_request_with_no_providers_returns_503() -> None:
    r = _client().post(
        "/v3/ai/language-model",
        json=_MINIMAL_BODY,
        headers={
            "ai-language-model-id": "freeride/coding",
            "ai-language-model-streaming": "true",
        },
    )
    assert r.status_code == 503
    assert r.json()["detail"]["error"]["type"] == "service_unavailable"


def test_non_streaming_request_with_no_providers_returns_503() -> None:
    r = _client().post(
        "/v3/ai/language-model",
        json=_MINIMAL_BODY,
        headers={
            "ai-language-model-id": "auto",
            "ai-language-model-streaming": "false",
        },
    )
    assert r.status_code == 503


def test_missing_model_header_defaults_to_auto_and_validates() -> None:
    """fx always sends ai-language-model-id, but the route must not
    KeyError without it."""
    r = _client().post("/v3/ai/language-model", json=_MINIMAL_BODY)
    assert r.status_code == 503  # validated, then no providers


def test_full_agent_turn_shape_validates() -> None:
    """A realistic mid-session fx request: system + user + assistant
    tool-call + tool-result + follow-up. Must clear validation and
    reach the failover logic (503 with no providers)."""
    body = {
        "prompt": [
            {"role": "system", "content": "you are ridex"},
            {"role": "user", "content": [{"type": "text", "text": "create test.py"}]},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool-call",
                        "toolCallId": "call_1",
                        "toolName": "write_file",
                        "input": {"path": "test.py", "content": "print('hi')"},
                    }
                ],
            },
            {
                "role": "tool",
                "content": [
                    {
                        "type": "tool-result",
                        "toolCallId": "call_1",
                        "toolName": "write_file",
                        "output": {"type": "text", "value": "ok"},
                    }
                ],
            },
            {"role": "user", "content": [{"type": "text", "text": "now run it"}]},
        ],
        "tools": [
            {
                "type": "function",
                "name": "write_file",
                "description": "write a file",
                "inputSchema": {"type": "object"},
            }
        ],
        "toolChoice": {"type": "auto"},
        "maxOutputTokens": 4096,
    }
    r = _client().post(
        "/v3/ai/language-model",
        json=body,
        headers={"ai-language-model-id": "freeride/coding"},
    )
    assert r.status_code == 503


def test_sse_error_events_shape() -> None:
    """In-stream terminal failures must stay inside fx's closed
    finishReason enum ('error') — an unknown value kills the stream
    parser on the agent side."""
    from freeride.server.routes.fx import _sse_error_events

    frames = _sse_error_events("all providers exhausted").decode().split("\n\n")
    assert frames[0].startswith('data: {"type":"error"')
    assert '"unified":"error"' in frames[1]
    assert frames[2] == "data: [DONE]"


class _FakeProvider:
    def __init__(self, name: str) -> None:
        self.name = name


def _catalog_entry(provider: str, model_id: str, *, tools: bool = True) -> dict:
    return {
        "id": model_id,
        "available_providers": [provider],
        "supported_parameters": ["tools"] if tools else [],
    }


def test_agent_candidates_pin_first_then_tools_capable_fallbacks(monkeypatch) -> None:
    from freeride.server.routes.fx import _agent_candidates

    monkeypatch.delenv("FREERIDE_FX_MODEL", raising=False)
    monkeypatch.delenv("FREERIDE_FX_PROVIDER", raising=False)
    monkeypatch.delenv("FREERIDE_CLAUDE_CODE_MODEL", raising=False)
    monkeypatch.delenv("FREERIDE_CLAUDE_CODE_PROVIDER", raising=False)
    providers = [_FakeProvider("openrouter"), _FakeProvider("groq"), _FakeProvider("huggingface")]
    catalog = [
        _catalog_entry("openrouter", "some/or-model:free"),
        _catalog_entry("groq", "groq-chat-model", tools=False),
        _catalog_entry("groq", "groq-tools-model"),
        _catalog_entry("huggingface", "hf-text-model", tools=False),
    ]
    ladder = _agent_candidates(providers, catalog)
    # Pin first; groq falls back to its first TOOLS-capable model; HF has
    # none and is skipped — a tool-less fallback would silently break the
    # agent loop.
    assert ladder == [
        ("openrouter", "openrouter/free"),
        ("groq", "groq-tools-model"),
    ]


def test_agent_candidates_skip_unregistered_pin_and_respect_cap(monkeypatch) -> None:
    from freeride.server.routes import fx as fx_module

    monkeypatch.setenv("FREERIDE_FX_PROVIDER", "openrouter")
    providers = [_FakeProvider(f"p{i}") for i in range(6)]  # pin provider absent
    catalog = [_catalog_entry(f"p{i}", f"m{i}") for i in range(6)]
    ladder = fx_module._agent_candidates(providers, catalog)
    assert ladder == [(f"p{i}", f"m{i}") for i in range(fx_module._MAX_AGENT_CANDIDATES)]


def test_agent_candidates_skips_health_broken_models(monkeypatch) -> None:
    from freeride.server.routes import fx as fx_module

    monkeypatch.delenv("FREERIDE_FX_MODEL", raising=False)
    monkeypatch.delenv("FREERIDE_FX_PROVIDER", raising=False)
    import freeride.core.model_health as mh

    monkeypatch.setattr(mh, "load_cache", lambda: {"sentinel": True})
    monkeypatch.setattr(
        mh,
        "is_model_known_broken",
        lambda prov, mid, cache=None: mid == "groq-broken",
    )
    providers = [_FakeProvider("groq")]
    catalog = [
        _catalog_entry("groq", "groq-broken"),
        _catalog_entry("groq", "groq-good"),
    ]
    assert fx_module._agent_candidates(providers, catalog) == [("groq", "groq-good")]


def test_materialize_attempts_concrete_model_gets_ladder_backstop() -> None:
    """The req_8533b4a7 failure: a /models pick of an OpenRouter-only id
    while OpenRouter cooled had nowhere to go. Concrete models now walk
    the full chain first, then fall through to the tools ladder."""
    from freeride.server.routes.fx import _materialize_attempts

    chain = [(_FakeProvider("groq"), ["k"]), (_FakeProvider("nvidia_nim"), ["k"])]
    ladder = [("openrouter", "openrouter/free"), ("groq", "groq-tools-model")]
    attempts = _materialize_attempts(
        chain, ladder, primary_model="minimax/minimax-m3:free", is_agent_default=False
    )
    # Primary: requested model over the whole chain. Backstop: groq's
    # tools model (openrouter has no usable keys → no scoped chain).
    assert [(len(c), m) for c, m in attempts] == [
        (2, "minimax/minimax-m3:free"),
        (1, "groq-tools-model"),
    ]


def test_materialize_attempts_agent_default_is_ladder_only() -> None:
    from freeride.server.routes.fx import _materialize_attempts

    chain = [(_FakeProvider("openrouter"), ["k"]), (_FakeProvider("groq"), ["k"])]
    ladder = [("openrouter", "openrouter/free"), ("groq", "groq-tools-model")]
    attempts = _materialize_attempts(
        chain, ladder, primary_model="openrouter/free", is_agent_default=True
    )
    assert [(c[0][0].name, m) for c, m in attempts] == [
        ("openrouter", "openrouter/free"),
        ("groq", "groq-tools-model"),
    ]


def _stream_event(**kw):
    from freeride.core.chat_schema import ChatStreamEvent

    delta = {}
    if "content" in kw:
        delta["content"] = kw["content"]
    choices = [{"index": 0, "delta": delta, "finish_reason": kw.get("finish_reason")}]
    return ChatStreamEvent.model_validate(
        {"id": "c", "created": 1, "model": "m", "choices": choices}
    )


async def _collect_stream(monkeypatch, outcomes):
    """Drive _build_fx_stream with scripted try_stream_with_failover
    outcomes; return the decoded SSE payloads ([DONE] as 'DONE')."""
    import freeride.server.routes.fx as fx_module
    from freeride.core.chat_schema import ChatRequest
    from freeride.core.cooldown import KeyCooldown
    from freeride.core.failover import FailoverContext

    calls = iter(outcomes)

    async def fake_failover(chain, body, cooldown, ctx, **kwargs):
        outcome = next(calls)
        if outcome is None:
            return None, None, None
        first, rest_events, die = outcome

        async def rest():
            for evt in rest_events:
                yield evt
            if die:
                raise RuntimeError("connection reset by upstream")

        return _FakeProvider("fake"), first, rest()

    monkeypatch.setattr(fx_module, "try_stream_with_failover", fake_failover)
    monkeypatch.setattr(
        "freeride.core.telemetry.record_request", lambda **kw: None
    )
    request = ChatRequest(model="m", messages=[])
    resp = await fx_module._build_fx_stream(
        attempts=[([("chain1", ["k"])], "model-a"), ([("chain2", ["k"])], "model-b")],
        openai_request=request,
        cooldown=KeyCooldown(),
        ctx=FailoverContext(request_id="req_test"),
    )
    out = []
    async for raw in resp.body_iterator:
        for frame in raw.decode().split("\n\n"):
            if not frame.startswith("data: "):
                continue
            payload = frame[len("data: ") :]
            out.append("DONE" if payload == "[DONE]" else json.loads(payload))
    return out


@pytest.mark.asyncio
async def test_stream_mid_death_after_content_ends_turn_as_error(monkeypatch) -> None:
    """Once real content shipped, a dying upstream must NOT be dressed
    up as a clean finish — fx retries an error, but trusts a finish."""
    events = await _collect_stream(
        monkeypatch,
        [(_stream_event(content="partial answer"), [], True)],
    )
    kinds = [e["type"] for e in events if isinstance(e, dict)]
    assert "error" in kinds
    finish = next(e for e in events if isinstance(e, dict) and e["type"] == "finish")
    assert finish["finishReason"]["unified"] == "error"


@pytest.mark.asyncio
async def test_stream_mid_death_before_content_switches_candidate(monkeypatch) -> None:
    """An upstream that dies before producing anything semantic is
    absorbed: the walk moves to the next (provider, model) candidate
    inside the same response."""
    events = await _collect_stream(
        monkeypatch,
        [
            (_stream_event(), [], True),  # role-only first chunk, then dies
            (
                _stream_event(content="pong"),
                [_stream_event(finish_reason="stop")],
                False,
            ),
        ],
    )
    deltas = [e for e in events if isinstance(e, dict) and e["type"] == "text-delta"]
    assert [d["delta"] for d in deltas] == ["pong"]
    finish = next(e for e in events if isinstance(e, dict) and e["type"] == "finish")
    assert finish["finishReason"]["unified"] == "stop"
    assert events[-1] == "DONE"


@pytest.mark.asyncio
async def test_stream_preflight_failure_walks_to_next_candidate(monkeypatch) -> None:
    events = await _collect_stream(
        monkeypatch,
        [
            None,  # candidate 1: all keys failed pre-first-chunk
            (
                _stream_event(content="pong"),
                [_stream_event(finish_reason="stop")],
                False,
            ),
        ],
    )
    finish = next(e for e in events if isinstance(e, dict) and e["type"] == "finish")
    assert finish["finishReason"]["unified"] == "stop"
