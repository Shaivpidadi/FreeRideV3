"""Tests for the auto-model resolver and the chat-route integration.

The resolver itself is pure-ish (reads env vars + KeyCooldown state),
so we can drive it directly. Integration tests stand up a FastAPI
TestClient with stub providers and assert on the X-FreeRide-Provider
header / response body.
"""

from __future__ import annotations

from typing import Any

import pytest

from freeride.core.auto_model import (
    is_auto_model,
    resolve_auto_model,
)


# ─── is_auto_model ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, True),
        ("", True),
        ("auto", True),
        ("AUTO", True),
        ("Auto", True),
        ("freeride/auto", True),
        ("default", True),
        ("  auto  ", True),
        ("llama-3.3-70b", False),
        ("openai/gpt-4o", False),
        ("automatic", False),  # close but not the sentinel
    ],
)
def test_is_auto_model(value: str | None, expected: bool) -> None:
    assert is_auto_model(value) is expected


# ─── resolve_auto_model ──────────────────────────────────────────


class _StubProvider:
    """Minimal provider lookalike — resolve_auto_model only reads .name."""

    def __init__(self, name: str) -> None:
        self.name = name


def test_resolve_returns_none_for_empty_catalog() -> None:
    model_id, provider = resolve_auto_model([_StubProvider("openrouter")], [])
    assert model_id is None
    assert provider is None


def test_resolve_returns_none_when_no_providers_have_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    catalog = [
        {
            "id": "meta-llama/llama-3.3-70b-instruct:free",
            "available_providers": ["openrouter", "groq"],
        }
    ]
    model_id, provider = resolve_auto_model(
        [_StubProvider("openrouter"), _StubProvider("groq")],
        catalog,
    )
    assert model_id is None
    assert provider is None


def test_resolve_picks_first_entry_whose_provider_has_a_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-stub")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    catalog = [
        {
            "id": "first-no-keys",
            "available_providers": ["groq"],
        },
        {
            "id": "second-has-keys",
            "available_providers": ["openrouter"],
        },
    ]
    model_id, provider = resolve_auto_model(
        [_StubProvider("openrouter"), _StubProvider("groq")],
        catalog,
    )
    assert model_id == "second-has-keys"
    assert provider == "openrouter"


def test_resolve_skips_entries_with_only_unconfigured_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CEREBRAS_API_KEY", "csk-stub")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    catalog = [
        {"id": "or-only", "available_providers": ["openrouter"]},
        {"id": "cerebras-only", "available_providers": ["cerebras"]},
    ]
    model_id, provider = resolve_auto_model(
        [_StubProvider("openrouter"), _StubProvider("cerebras")],
        catalog,
    )
    assert model_id == "cerebras-only"
    assert provider == "cerebras"


def test_resolve_handles_missing_available_providers_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A catalog row that for some reason lacks the field shouldn't crash
    the resolver — just skip it and try the next."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-stub")
    catalog: list[dict[str, Any]] = [
        {"id": "broken-row"},  # no available_providers
        {"id": "good-row", "available_providers": ["openrouter"]},
    ]
    model_id, provider = resolve_auto_model(
        [_StubProvider("openrouter")], catalog, leaderboard={}
    )
    assert model_id == "good-row"
    assert provider == "openrouter"


# ─── smart-routing integration ────────────────────────────────────


def test_resolve_prefers_higher_scored_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Demonstrates that the resolver actually consults the score —
    when the catalog order would pick A but the score sends B to the
    top, B wins. This is the regression test for the smart-routing
    upgrade: previously the first-listed entry always won."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-stub")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-stub")
    catalog: list[dict[str, Any]] = [
        # Listed first but only 1 provider, no popularity → score 10.
        {"id": "first-listed", "available_providers": ["openrouter"]},
        # Listed second but 2 providers + leaderboard hit → score 20 + bonus.
        {"id": "popular-and-redundant", "available_providers": ["openrouter", "groq"]},
    ]
    leaderboard = {"popular-and-redundant": 5_000_000}
    model_id, provider = resolve_auto_model(
        [_StubProvider("openrouter"), _StubProvider("groq")],
        catalog,
        leaderboard=leaderboard,
    )
    assert model_id == "popular-and-redundant"


def test_resolve_falls_back_to_first_when_no_popularity_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty leaderboard + tied provider counts → catalog order wins
    (stable sort), matching the pre-smart-routing behavior. Confirms
    no regression when the leaderboard is unreachable."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-stub")
    catalog: list[dict[str, Any]] = [
        {"id": "first", "available_providers": ["openrouter"]},
        {"id": "second", "available_providers": ["openrouter"]},
    ]
    model_id, _ = resolve_auto_model(
        [_StubProvider("openrouter")], catalog, leaderboard={}
    )
    assert model_id == "first"


# ─── invalidate_catalog ──────────────────────────────────────────


def test_invalidate_catalog_clears_both_grouped_and_ungrouped() -> None:
    from freeride.server.routes.models import _CACHE, _CACHE_KEY, invalidate_catalog

    _CACHE.set(f"{_CACHE_KEY}.grouped", [{"id": "stub"}])
    _CACHE.set(f"{_CACHE_KEY}.ungrouped", [{"id": "stub"}])

    assert _CACHE.get(f"{_CACHE_KEY}.grouped") is not None
    assert _CACHE.get(f"{_CACHE_KEY}.ungrouped") is not None

    invalidate_catalog()

    assert _CACHE.get(f"{_CACHE_KEY}.grouped") is None
    assert _CACHE.get(f"{_CACHE_KEY}.ungrouped") is None
