"""Tests for the smart-routing scorer + leaderboard cache.

The on-disk cache and HTTP fetch are exercised through monkeypatch
so the suite never touches the network or the user's real
``~/.freeride/cache``.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any

import pytest

from freeride.core import smart_routing


# ─── score_model ──────────────────────────────────────────────────


def test_score_zero_when_no_available_providers() -> None:
    assert (
        smart_routing.score_model(
            {"id": "anything", "available_providers": ["openrouter"]},
            available_providers=[],
            leaderboard={"anything": 5_000_000},
        )
        == 0.0
    )


def test_score_grows_with_provider_count() -> None:
    entry = {"id": "x"}
    s1 = smart_routing.score_model(entry, ["openrouter"], {})
    s3 = smart_routing.score_model(entry, ["openrouter", "groq", "cerebras"], {})
    # 10 points per provider; popularity 0 so they only differ in headroom.
    assert s1 == pytest.approx(10.0)
    assert s3 == pytest.approx(30.0)
    assert s3 > s1


def test_score_popularity_bonus_uses_log_scale() -> None:
    entry = {"id": "popular"}
    # 1M tokens vs 10 tokens: log10 difference = 5; bonus difference = 25.
    s_high = smart_routing.score_model(entry, ["openrouter"], {"popular": 1_000_000})
    s_low = smart_routing.score_model(entry, ["openrouter"], {"popular": 10})
    assert s_high - s_low == pytest.approx(5.0 * (math.log10(1_000_001) - math.log10(11)))


def test_score_unknown_model_treats_popularity_as_zero() -> None:
    # An entry whose id doesn't appear in the leaderboard scores like
    # popularity = 0 — falls through to provider-count-only.
    s = smart_routing.score_model({"id": "obscure"}, ["openrouter"], {"popular": 999})
    assert s == pytest.approx(10.0)


# ─── rank_catalog ────────────────────────────────────────────────


def test_rank_drops_entries_with_no_overlap() -> None:
    catalog = [
        {"id": "a", "available_providers": ["groq"]},
        {"id": "b", "available_providers": ["openrouter"]},
    ]
    ranked = smart_routing.rank_catalog(catalog, {"openrouter"}, {})
    assert [r[0]["id"] for r in ranked] == ["b"]


def test_rank_orders_by_score_desc() -> None:
    catalog = [
        {"id": "low", "available_providers": ["groq"]},
        {"id": "high", "available_providers": ["openrouter", "groq", "cerebras"]},
    ]
    ranked = smart_routing.rank_catalog(catalog, {"openrouter", "groq", "cerebras"}, {})
    # 'high' has 3 providers (score 30), 'low' has 1 (score 10) — 'high' first.
    assert [r[0]["id"] for r in ranked] == ["high", "low"]


def test_rank_popularity_can_outweigh_one_extra_provider() -> None:
    catalog = [
        {"id": "boring", "available_providers": ["openrouter", "groq"]},        # 20
        {"id": "popular", "available_providers": ["openrouter"]},               # 10 + log10(5e6+1)*5 ≈ 43
    ]
    ranked = smart_routing.rank_catalog(
        catalog,
        {"openrouter", "groq"},
        leaderboard={"popular": 5_000_000},
    )
    assert ranked[0][0]["id"] == "popular"


def test_rank_stable_on_ties() -> None:
    # Both entries have the same provider count (1), no popularity —
    # registration order should win (stable sort).
    catalog = [
        {"id": "first", "available_providers": ["openrouter"]},
        {"id": "second", "available_providers": ["openrouter"]},
    ]
    ranked = smart_routing.rank_catalog(catalog, {"openrouter"}, {})
    assert [r[0]["id"] for r in ranked] == ["first", "second"]


# ─── leaderboard cache ──────────────────────────────────────────


def test_read_cache_returns_none_when_file_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(smart_routing, "_CACHE_PATH", tmp_path / "missing.json")
    assert smart_routing._read_cache() is None


def test_read_cache_returns_none_when_stale(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    p = tmp_path / "stale.json"
    p.write_text(json.dumps({"as_of": time.time() - 10_000, "models": {"x": 1}}))
    monkeypatch.setattr(smart_routing, "_CACHE_PATH", p)
    monkeypatch.setattr(smart_routing, "_CACHE_TTL_SEC", 3600)
    assert smart_routing._read_cache() is None


def test_read_cache_returns_models_when_fresh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    p = tmp_path / "fresh.json"
    p.write_text(json.dumps({"as_of": time.time(), "models": {"a": 100, "b": 200}}))
    monkeypatch.setattr(smart_routing, "_CACHE_PATH", p)
    monkeypatch.setattr(smart_routing, "_CACHE_TTL_SEC", 3600)
    out = smart_routing._read_cache()
    assert out == {"a": 100, "b": 200}


def test_read_cache_returns_none_on_garbage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    p = tmp_path / "garbage.json"
    p.write_text("not json")
    monkeypatch.setattr(smart_routing, "_CACHE_PATH", p)
    assert smart_routing._read_cache() is None


def test_fetch_leaderboard_falls_back_empty_on_remote_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If both cache miss AND remote fetch fail, fetch_leaderboard
    returns {} — never raises and never blocks the resolver."""
    monkeypatch.setattr(smart_routing, "_CACHE_PATH", tmp_path / "no.json")
    monkeypatch.setattr(smart_routing, "_fetch_remote", lambda: None)
    assert smart_routing.fetch_leaderboard() == {}


def test_fetch_leaderboard_writes_cache_on_remote_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cache_path = tmp_path / "cache.json"
    monkeypatch.setattr(smart_routing, "_CACHE_PATH", cache_path)
    monkeypatch.setattr(smart_routing, "_fetch_remote", lambda: {"foo": 999})
    out = smart_routing.fetch_leaderboard()
    assert out == {"foo": 999}
    assert cache_path.exists()
    cached = json.loads(cache_path.read_text())
    assert cached["models"] == {"foo": 999}


def test_fetch_leaderboard_uses_cache_before_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    p = tmp_path / "cache.json"
    p.write_text(json.dumps({"as_of": time.time(), "models": {"a": 1}}))
    monkeypatch.setattr(smart_routing, "_CACHE_PATH", p)

    called = {"n": 0}

    def boom() -> dict[str, int] | None:  # pragma: no cover — should NEVER run
        called["n"] += 1
        return {"b": 2}

    monkeypatch.setattr(smart_routing, "_fetch_remote", boom)
    assert smart_routing.fetch_leaderboard() == {"a": 1}
    assert called["n"] == 0
