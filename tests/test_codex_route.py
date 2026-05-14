"""Integration tests for POST /v1/responses (Codex CLI shim)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from freeride.server.app import create_app


def _app_with_no_providers():
    return create_app(providers=[])


def test_malformed_json_returns_400_with_openai_envelope() -> None:
    """The Codex CLI parses OpenAI's error envelope shape from this
    endpoint, so error responses must match that shape (not the Google
    or Anthropic variants used elsewhere in the gateway)."""
    client = TestClient(_app_with_no_providers())
    r = client.post(
        "/v1/responses",
        content=b"not json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 400
    err = r.json()["detail"]["error"]
    assert err["type"] == "invalid_request_error"
    assert "JSON" in err["message"]


def test_non_object_body_returns_400() -> None:
    client = TestClient(_app_with_no_providers())
    r = client.post("/v1/responses", json=["not", "an", "object"])
    assert r.status_code == 400


def test_missing_model_field_returns_400() -> None:
    """model is the only required field on the Responses request body.
    Without it Pydantic validation fails."""
    client = TestClient(_app_with_no_providers())
    r = client.post("/v1/responses", json={"input": "hi"})
    assert r.status_code == 400
    err = r.json()["detail"]["error"]
    assert "validation" in err["message"].lower()


def test_valid_request_with_no_providers_returns_503() -> None:
    """Happy-path schema, no providers registered → 503 in OpenAI
    envelope. Status code proves we got past validation; envelope
    proves we hit the responses route (not a FastAPI default)."""
    client = TestClient(_app_with_no_providers())
    r = client.post(
        "/v1/responses",
        json={"model": "gpt-5-codex", "input": "hello"},
    )
    assert r.status_code == 503
    err = r.json()["detail"]["error"]
    assert err["type"] == "service_unavailable"


def test_streaming_request_with_no_providers_returns_503() -> None:
    client = TestClient(_app_with_no_providers())
    r = client.post(
        "/v1/responses",
        json={"model": "gpt-5-codex", "input": "hello", "stream": True},
    )
    assert r.status_code == 503


def test_request_with_string_input_and_tools_validates() -> None:
    """The CLI sends the flat Responses-shape tool defs. Make sure
    validation passes (the schema accepts them) — without this, the
    route would 400 before ever reaching the failover logic."""
    client = TestClient(_app_with_no_providers())
    r = client.post(
        "/v1/responses",
        json={
            "model": "gpt-5-codex",
            "input": "create test.py",
            "tools": [
                {
                    "type": "function",
                    "name": "Write",
                    "description": "write a file",
                    "parameters": {"type": "object"},
                }
            ],
        },
    )
    # No providers → 503, but the request itself validated cleanly.
    assert r.status_code == 503


def test_multi_turn_input_with_function_call_output_validates() -> None:
    """A real multi-turn codex request: prior user msg + function_call
    + function_call_output + new user msg. Must parse cleanly through
    the typed-item schema."""
    client = TestClient(_app_with_no_providers())
    r = client.post(
        "/v1/responses",
        json={
            "model": "gpt-5-codex",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "create test.py"}],
                },
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "Write",
                    "arguments": '{"path":"test.py"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": "wrote 1 line",
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "run it"}],
                },
            ],
        },
    )
    assert r.status_code == 503  # validated, then no providers
