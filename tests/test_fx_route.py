"""Integration tests for the fx gateway dialect routes
(``POST /v3/ai/language-model`` + ``GET /coding-agent/v1/models``)."""

from __future__ import annotations

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
