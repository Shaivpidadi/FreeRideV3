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
    preset_provider_order,
    reorder_providers_for_preset,
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


def test_has_inbound_auth_freeride_sentinel_doesnt_count_as_auth() -> None:
    """The `freeride run claude` wrapper injects
    ANTHROPIC_API_KEY=sk-freeride-no-auth when the user has no real
    credential, to satisfy claude-cli 2.1.140+'s pre-flight gate. The
    gateway must NOT treat that as real auth — it has to fall through
    to free routing instead of attempting a passthrough that would
    401 against api.anthropic.com."""
    assert has_inbound_auth({"x-api-key": "sk-freeride-no-auth"}) is False
    assert has_inbound_auth({"Authorization": "Bearer sk-freeride-no-auth"}) is False
    # Real keys still count.
    assert has_inbound_auth({"x-api-key": "sk-ant-api03-real"}) is True


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


# ─── preset provider preference ──────────────────────────────────


def test_preset_provider_order_free_has_no_preference() -> None:
    """freeride/free routes through pure health-ranked smart-routing —
    no preset preference, returns empty tuple."""
    assert preset_provider_order(PRESET_FREE) == ()


def test_preset_provider_order_fast_prefers_low_latency_providers() -> None:
    """freeride/fast → Groq, Cerebras, NVIDIA NIM are the fastest free
    providers today (LPU/dedicated silicon)."""
    order = preset_provider_order(PRESET_FAST)
    assert order[0] == "groq"
    assert "cerebras" in order
    assert "nvidia_nim" in order


def test_preset_provider_order_quality_prefers_wide_model_selection() -> None:
    """freeride/quality → OpenRouter has the widest free model
    catalog; HuggingFace Inference has the classic Mixtral/Llama
    endpoints."""
    order = preset_provider_order(PRESET_QUALITY)
    assert order[0] == "openrouter"
    assert "huggingface" in order


def test_preset_provider_order_coding_prefers_code_tuned_providers() -> None:
    order = preset_provider_order(PRESET_CODING)
    assert "openrouter" in order  # Qwen-Coder, DeepSeek
    assert "groq" in order  # DeepSeek-R1 distill at speed


def test_preset_provider_order_none_returns_empty() -> None:
    assert preset_provider_order(None) == ()
    assert preset_provider_order("") == ()


def test_preset_provider_order_unknown_preset_returns_empty() -> None:
    """Unknown presets don't crash — just no preference. Caller falls
    through to standard health-ranked order."""
    assert preset_provider_order("banana") == ()


# ─── reorder_providers_for_preset ────────────────────────────────


def test_reorder_for_fast_puts_groq_first() -> None:
    """A chain with several providers gets re-ordered so groq comes
    first when preset is fast."""
    chain = ["openrouter", "huggingface", "groq", "nvidia_nim"]
    out = reorder_providers_for_preset(chain, PRESET_FAST)
    # First element is the highest-priority provider in the preference
    # list that's ALSO present in the chain.
    assert out[0] == "groq"
    # nvidia_nim is preferred too — should also move forward
    assert "nvidia_nim" in out[:3]
    # All original providers are still present
    assert set(out) == set(chain)


def test_reorder_preserves_unspecified_providers_at_tail() -> None:
    """Providers not mentioned by the preset preference stay in the
    chain (just at the end). We don't drop anyone — failover still
    works as a last resort."""
    chain = ["mystery_provider", "groq", "openrouter"]
    out = reorder_providers_for_preset(chain, PRESET_FAST)
    # groq comes first (preferred for fast); mystery_provider stays
    # but ends up at the back.
    assert out[0] == "groq"
    assert "mystery_provider" in out
    assert set(out) == set(chain)


def test_reorder_empty_preset_returns_original_order() -> None:
    chain = ["openrouter", "groq", "huggingface"]
    out = reorder_providers_for_preset(chain, PRESET_FREE)
    assert out == chain


def test_reorder_with_no_chain_returns_empty() -> None:
    assert reorder_providers_for_preset([], PRESET_FAST) == []


def test_reorder_only_keeps_providers_actually_in_chain() -> None:
    """If the preset prefers a provider that's not in the chain (not
    registered or no key), it can't be added — re-ordering only
    operates on what's there."""
    chain = ["openrouter"]  # groq + cerebras + nvidia_nim absent
    out = reorder_providers_for_preset(chain, PRESET_FAST)
    assert out == ["openrouter"]
