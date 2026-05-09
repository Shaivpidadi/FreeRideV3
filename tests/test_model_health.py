"""Tests for the per-model runtime health cache."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from freeride.core import model_health
from freeride.core.errors import ErrorKind
from freeride.core.types import ProbeResult


# ─── cache I/O ───────────────────────────────────────────────────


def test_load_returns_empty_when_file_missing(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(model_health, "CACHE_PATH", tmp_path / "missing.json")
    assert model_health.load_cache() == {}


def test_save_and_load_roundtrip(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(model_health, "CACHE_PATH", tmp_path / "h.json")
    entries = {
        "groq::llama-3.3-70b-versatile": model_health.HealthEntry(
            status="ok", latency_ms=342, checked_at=int(time.time())
        ),
        "cerebras::ghost": model_health.HealthEntry(
            status="model_not_found", latency_ms=120, checked_at=int(time.time())
        ),
    }
    model_health.save_cache(entries)
    loaded = model_health.load_cache()
    assert set(loaded.keys()) == set(entries.keys())
    assert loaded["groq::llama-3.3-70b-versatile"].status == "ok"
    assert loaded["cerebras::ghost"].status == "model_not_found"


def test_load_returns_empty_when_stale(monkeypatch, tmp_path: Path) -> None:
    p = tmp_path / "stale.json"
    p.write_text(
        json.dumps(
            {
                "as_of": time.time() - 200_000,  # well past 24h
                "ttl_sec": 86_400,
                "results": {"any::model": {"status": "ok", "latency_ms": 0, "checked_at": 0}},
            }
        )
    )
    monkeypatch.setattr(model_health, "CACHE_PATH", p)
    assert model_health.load_cache() == {}


def test_load_handles_garbage_file(monkeypatch, tmp_path: Path) -> None:
    p = tmp_path / "garbage.json"
    p.write_text("not json at all")
    monkeypatch.setattr(model_health, "CACHE_PATH", p)
    assert model_health.load_cache() == {}


# ─── lookup helper ───────────────────────────────────────────────


def test_is_known_broken_for_unknown_returns_false() -> None:
    cache = {
        "groq::llama-3.3-70b-versatile": model_health.HealthEntry(
            status="ok", latency_ms=10, checked_at=0
        )
    }
    assert (
        model_health.is_model_known_broken("openrouter", "any-id", cache=cache) is False
    )


def test_is_known_broken_returns_true_for_broken_statuses() -> None:
    for status in (
        "model_not_found",
        "quota_exhausted",
        "auth",
        "rate_limit",
        "unavailable",
        "timeout",
        "unknown",
    ):
        cache = {
            "p::m": model_health.HealthEntry(status=status, latency_ms=0, checked_at=0)
        }
        assert (
            model_health.is_model_known_broken("p", "m", cache=cache) is True
        ), f"status {status} should mark the model as broken"


def test_is_known_broken_false_for_ok() -> None:
    cache = {"p::m": model_health.HealthEntry(status="ok", latency_ms=0, checked_at=0)}
    assert model_health.is_model_known_broken("p", "m", cache=cache) is False


# ─── audit_providers ─────────────────────────────────────────────


class _StubProvider:
    """Simulates Provider.list_free_models + Provider.probe with deterministic
    data, so audit_providers can be exercised without any HTTP."""

    def __init__(self, name: str, models: list[str], probe_map: dict[str, ProbeResult]) -> None:
        self.name = name
        self._models = models
        self._probe_map = probe_map

    def list_free_models(self, key: str):  # noqa: ANN001
        from freeride.core.types import Model

        return [
            Model(api_id=mid, provider=self.name, context_length=8192) for mid in self._models
        ]

    def probe(self, model_id: str, key: str) -> ProbeResult:  # noqa: ANN001
        return self._probe_map[model_id]


def test_audit_returns_one_entry_per_model() -> None:
    p = _StubProvider(
        name="stub",
        models=["good", "ghost"],
        probe_map={
            "good": ProbeResult(ok=True, latency_ms=42),
            "ghost": ProbeResult(ok=False, error=ErrorKind.MODEL_NOT_FOUND, latency_ms=10),
        },
    )
    out = model_health.audit_providers([p], {"stub": "key"})
    assert set(out.keys()) == {"stub::good", "stub::ghost"}
    assert out["stub::good"].status == "ok"
    assert out["stub::ghost"].status == "model_not_found"


def test_audit_skips_providers_without_keys() -> None:
    p = _StubProvider(name="stub", models=["m"], probe_map={"m": ProbeResult(ok=True)})
    # No key for "stub" → audit must skip it cleanly.
    out = model_health.audit_providers([p], keys_for={})
    assert out == {}


def test_audit_handles_provider_probe_exception() -> None:
    class _Exploding(_StubProvider):
        def probe(self, model_id, key):  # noqa: ANN001
            raise RuntimeError("boom")

    p = _Exploding(
        name="stub",
        models=["explodes"],
        probe_map={"explodes": ProbeResult(ok=True)},
    )
    out = model_health.audit_providers([p], {"stub": "key"})
    assert out["stub::explodes"].status == "unknown"


def test_audit_handles_list_free_models_exception() -> None:
    class _BadCatalog(_StubProvider):
        def list_free_models(self, key):  # noqa: ANN001
            raise RuntimeError("catalog API down")

    p = _BadCatalog(name="stub", models=[], probe_map={})
    # No models materialize, but audit doesn't crash.
    out = model_health.audit_providers([p], {"stub": "key"})
    assert out == {}


# ─── smart_routing integration ───────────────────────────────────


def test_score_zeros_out_known_broken_via_health_cache() -> None:
    from freeride.core.smart_routing import score_model

    cache = {
        "openrouter::dead-model": model_health.HealthEntry(
            status="model_not_found", latency_ms=0, checked_at=0
        )
    }
    score = score_model(
        {"id": "dead-model", "available_providers": ["openrouter"]},
        ["openrouter"],
        leaderboard={"dead-model": 1_000_000},  # would normally score high
        health_cache=cache,
    )
    assert score == 0.0


def test_score_uses_only_healthy_providers_when_cache_present() -> None:
    from freeride.core.smart_routing import score_model

    cache = {
        # OR is broken for this model, groq is fine (not in cache → healthy)
        "openrouter::shared-model": model_health.HealthEntry(
            status="quota_exhausted", latency_ms=0, checked_at=0
        ),
    }
    # 2 providers advertised, but only groq is healthy → headroom = 10
    score = score_model(
        {"id": "shared-model", "available_providers": ["openrouter", "groq"]},
        ["openrouter", "groq"],
        leaderboard={},
        health_cache=cache,
    )
    assert score == pytest.approx(10.0)
