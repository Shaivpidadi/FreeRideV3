"""Integration tests for the Gemini-format route.

POST /v1beta/models/<model>:generateContent — non-streaming
POST /v1beta/models/<model>:streamGenerateContent — SSE

We exercise the URL path parsing, error envelopes (Google shape, not
the Anthropic-shape used by /v1/messages), and verify the no-providers
case returns the expected 503 with Google's error envelope. The deeper
translation + routing logic is covered by the unit tests in
test_gemini_translate.py.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from freeride.server.app import create_app


def _app_with_no_providers():
    return create_app(providers=[])


def test_unknown_action_returns_404_with_google_error_envelope() -> None:
    """Path missing the ':<action>' suffix is a routing error. The
    error envelope shape matches Google's REST API so SDK clients
    parse it natively (.error.message / .error.status)."""
    client = TestClient(_app_with_no_providers())
    r = client.post(
        "/v1beta/models/gemini-2.0-flash",  # missing :generateContent
        json={"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
    )
    assert r.status_code == 404
    body = r.json()
    err = body["detail"]["error"]
    assert err["status"] == "NOT_FOUND"
    assert "generateContent" in err["message"]


def test_unsupported_action_returns_404() -> None:
    client = TestClient(_app_with_no_providers())
    r = client.post(
        "/v1beta/models/gemini-2.0-flash:countTokens",  # we don't support this
        json={"contents": []},
    )
    assert r.status_code == 404
    body = r.json()
    assert body["detail"]["error"]["status"] == "NOT_FOUND"
    assert "countTokens" in body["detail"]["error"]["message"]


def test_malformed_json_returns_400_with_google_envelope() -> None:
    client = TestClient(_app_with_no_providers())
    r = client.post(
        "/v1beta/models/gemini-2.0-flash:generateContent",
        content=b"this is not json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 400
    body = r.json()
    assert body["detail"]["error"]["status"] == "INVALID_ARGUMENT"


def test_non_object_body_returns_400() -> None:
    """Google's API rejects array / scalar bodies. Match that contract
    so SDK error handlers don't see a different shape from us."""
    client = TestClient(_app_with_no_providers())
    r = client.post(
        "/v1beta/models/gemini-2.0-flash:generateContent",
        json=["not", "an", "object"],
    )
    assert r.status_code == 400
    body = r.json()
    assert body["detail"]["error"]["status"] == "INVALID_ARGUMENT"


def test_valid_request_with_no_providers_returns_503() -> None:
    """Happy-path schema, no providers registered → 503 in Google
    envelope shape. The status code alone proves we got past parsing
    and validation; the envelope shape proves we built the error from
    the gemini route (not a FastAPI default 503)."""
    client = TestClient(_app_with_no_providers())
    r = client.post(
        "/v1beta/models/gemini-2.0-flash:generateContent",
        json={
            "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
        },
    )
    assert r.status_code == 503
    body = r.json()
    assert body["detail"]["error"]["status"] == "UNAVAILABLE"


def test_streaming_endpoint_routed_to_streaming_path() -> None:
    """The streamGenerateContent variant must take the streaming code
    path. With no providers registered we still get 503, but again the
    Google envelope confirms we hit the gemini route specifically."""
    client = TestClient(_app_with_no_providers())
    r = client.post(
        "/v1beta/models/gemini-2.0-flash:streamGenerateContent",
        json={
            "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
        },
    )
    # 503 expected (no providers); the assertion that matters is the
    # envelope shape — proves we didn't get a 404 from path mismatch.
    assert r.status_code == 503
    assert r.json()["detail"]["error"]["status"] == "UNAVAILABLE"


def test_validation_error_returns_400() -> None:
    """A request that parses as JSON but fails Pydantic validation
    surfaces a 400 with Google's envelope. (Hard to actually trigger
    given our schema is permissive everywhere; we send a value of the
    wrong TYPE for a known field to force it.)"""
    client = TestClient(_app_with_no_providers())
    r = client.post(
        "/v1beta/models/gemini-2.0-flash:generateContent",
        json={
            "contents": "not a list",  # type error: should be list
        },
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error"]["status"] == "INVALID_ARGUMENT"
