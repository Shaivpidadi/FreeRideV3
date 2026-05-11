"""Tests for the ``/v1/messages`` routing decision module.

The resolver is pure — given a model id and a header dict, it returns
a ``RoutingDecision``. These tests pin the four buckets:

- ``freeride/*`` → free mode (even with auth)
- ``claude-*`` + auth → passthrough
- ``claude-*`` + no auth → free fallback
- anything else → free

…plus the small auth-detection helper that backs the decision.
"""

from __future__ import annotations

import pytest

from freeride.core.model_router import (
    PRESET_CODING,
    PRESET_FAST,
    PRESET_FREE,
    PRESET_QUALITY,
    decide,
    has_inbound_auth,
    is_anthropic_model,
    parse_freeride_model,
)


# ─── is_anthropic_model ─────────────────────────────────────────


@pytest.mark.parametrize(
    "model_id, expected",
    [
        ("claude-opus-4-5", True),
        ("claude-sonnet-4-6", True),
        ("claude-haiku-4-5", True),
        ("claude-3-5-sonnet-20241022", True),
        # Case + whitespace
        ("CLAUDE-OPUS-4-5", True),
        ("  claude-sonnet-4-6  ", True),
        # Hypothetical future ids — permissive prefix, no allowlist
        ("claude-5-opus", True),
        ("claude-banana-99", True),
        # Not Anthropic
        ("gpt-4o-mini", False),
        ("llama-3.3-70b", False),
        ("freeride/free", False),
        ("openrouter/free", False),
        ("", False),
    ],
)
def test_is_anthropic_model(model_id: str, expected: bool) -> None:
    assert is_anthropic_model(model_id) is expected


# ─── parse_freeride_model ───────────────────────────────────────


@pytest.mark.parametrize(
    "model_id, expected",
    [
        ("freeride/free", PRESET_FREE),
        ("freeride/fast", PRESET_FAST),
        ("freeride/quality", PRESET_QUALITY),
        ("freeride/coding", PRESET_CODING),
        # Case-insensitive prefix
        ("Freeride/Coding", PRESET_CODING),
        ("FREERIDE/FAST", PRESET_FAST),
        # Bare prefix → default to "free" (user clearly meant *something*
        # free)
        ("freeride/", PRESET_FREE),
        # Unknown preset → fall back to free, don't 400
        ("freeride/banana", PRESET_FREE),
        # Not a freeride id at all
        ("claude-opus-4-5", None),
        ("gpt-4o", None),
        ("", None),
    ],
)
def test_parse_freeride_model(model_id: str, expected: str | None) -> None:
    assert parse_freeride_model(model_id) == expected


# ─── has_inbound_auth ───────────────────────────────────────────


def test_has_inbound_auth_with_bearer_token() -> None:
    assert has_inbound_auth({"Authorization": "Bearer sk-ant-oat01-xxx"}) is True


def test_has_inbound_auth_with_x_api_key() -> None:
    assert has_inbound_auth({"x-api-key": "sk-ant-api03-yyy"}) is True


def test_has_inbound_auth_case_insensitive() -> None:
    """FastAPI lowercases headers; we still accept caller-side casing
    that wasn't normalized yet (e.g. unit tests passing raw dicts)."""
    assert has_inbound_auth({"AUTHORIZATION": "Bearer x"}) is True
    assert has_inbound_auth({"X-API-KEY": "abc"}) is True


def test_has_inbound_auth_empty_value_doesnt_count() -> None:
    """Whitespace or empty auth headers are as-good-as-absent."""
    assert has_inbound_auth({"Authorization": ""}) is False
    assert has_inbound_auth({"Authorization": "   "}) is False
    assert has_inbound_auth({"x-api-key": ""}) is False


def test_has_inbound_auth_no_headers() -> None:
    assert has_inbound_auth({}) is False
    assert has_inbound_auth(None) is False


def test_has_inbound_auth_other_headers_dont_count() -> None:
    """Anthropic-version, content-type, etc. mustn't be mistaken for
    auth."""
    headers = {
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
        "user-agent": "anthropic-sdk-python/0.18",
    }
    assert has_inbound_auth(headers) is False


# ─── decide() — the four buckets ────────────────────────────────


def test_decide_freeride_free_with_auth_still_routes_free() -> None:
    """If the user explicitly asks for freeride/*, respect that even
    when they DO have an Anthropic subscription — they're opting out
    for this request."""
    d = decide("freeride/free", {"Authorization": "Bearer sk-ant-oat01-x"})
    assert d.mode == "free"
    assert d.preset == PRESET_FREE


def test_decide_freeride_preset_passes_through_preset() -> None:
    for preset_id, expected_preset in [
        ("freeride/fast", PRESET_FAST),
        ("freeride/quality", PRESET_QUALITY),
        ("freeride/coding", PRESET_CODING),
    ]:
        d = decide(preset_id, headers={})
        assert d.mode == "free"
        assert d.preset == expected_preset


def test_decide_claude_with_oauth_bearer_passes_through() -> None:
    """Subscription user — relay to Anthropic untouched."""
    d = decide(
        "claude-sonnet-4-6",
        {"Authorization": "Bearer sk-ant-oat01-mock-subscription-token"},
    )
    assert d.mode == "passthrough"
    assert d.preset is None
    assert "Anthropic" in d.reason


def test_decide_claude_with_api_key_passes_through() -> None:
    """Direct API key — also passthrough."""
    d = decide("claude-opus-4-5", {"x-api-key": "sk-ant-api03-mock"})
    assert d.mode == "passthrough"
    assert d.preset is None


def test_decide_claude_without_auth_falls_back_to_free() -> None:
    """User has the gateway wired up but never ran `claude login`.
    Don't 401 — give them a free response."""
    d = decide("claude-opus-4-5", headers={"content-type": "application/json"})
    assert d.mode == "free"
    assert d.preset == PRESET_FREE
    assert "no Authorization" in d.reason or "no authorization" in d.reason.lower()


def test_decide_claude_with_empty_auth_falls_back_to_free() -> None:
    """An empty Authorization header is as-good-as-absent — we won't
    relay a blank credential and let Anthropic 401 us."""
    d = decide("claude-haiku-4-5", {"Authorization": ""})
    assert d.mode == "free"


def test_decide_unknown_model_routes_free() -> None:
    """Random model ids (OpenAI's, custom strings) still flow through
    /v1/messages — they translate to OpenAI shape internally and route
    free. This keeps /v1/messages a drop-in for callers that don't
    speak Anthropic ids."""
    d = decide("gpt-4o-mini", headers={})
    assert d.mode == "free"
    assert d.preset == PRESET_FREE


def test_decide_decision_carries_reason_string() -> None:
    """The reason field is the audit trail — telemetry stamps it, the
    doctor probe reads it back. Every decision must populate it."""
    for case in [
        ("claude-sonnet-4-6", {"Authorization": "Bearer x"}),
        ("claude-sonnet-4-6", {}),
        ("freeride/free", {}),
        ("gpt-4o", {}),
    ]:
        d = decide(*case)
        assert d.reason
        assert isinstance(d.reason, str)
