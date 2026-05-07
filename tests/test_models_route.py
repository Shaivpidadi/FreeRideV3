"""Hermetic tests for the /v1/models route.

Covers the grouped / ungrouped modes — provider-stamped catalogs
collapse via canonicalize() into one logical entry per model when
``group=true`` (default). ``group=false`` returns the raw matrix.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from freeride.core.types import Model
from freeride.server.app import create_app


class _StubProvider:
    """Minimal provider stub that returns a hardcoded list of free models."""

    api_version = 1

    def __init__(self, name: str, models: list[Model]):
        self.name = name
        self._models = models

    def list_free_models(self, key: str):  # noqa: ARG002
        return self._models


def _make_model(api_id: str, provider: str, ctx: int = 8192) -> Model:
    return Model(
        api_id=api_id,
        provider=provider,
        context_length=ctx,
        output_modalities=("text",),
        supported_parameters=(),
        raw={"id": api_id, "created": 0},
    )


def _client_with(providers, monkeypatch, env_keys=None) -> TestClient:
    monkeypatch.setenv("FREERIDE_EVENTS", "0")
    if env_keys:
        for k, v in env_keys.items():
            monkeypatch.setenv(k, v)
    monkeypatch.setattr(
        "freeride.core.cooldown.KeyCooldown.available_keys",
        lambda self, name, keys: list(keys),
    )
    # Bypass the 6h TTL cache so each test starts clean.
    from freeride.server.routes import models as models_route

    models_route._CACHE._store.clear()  # type: ignore[attr-defined]
    return TestClient(create_app(providers=providers))


# ---------------------------------------------------------------------------


class TestUngrouped:
    def test_returns_one_entry_per_provider_model_pair(self, monkeypatch):
        or_p = _StubProvider("openrouter", [_make_model("meta-llama/llama-3.1-8b-instruct", "openrouter")])
        groq_p = _StubProvider("groq", [_make_model("llama-3.1-8b-instant", "groq")])
        client = _client_with(
            [or_p, groq_p],
            monkeypatch,
            env_keys={"OPENROUTER_API_KEY": "k", "GROQ_API_KEY": "k"},
        )
        r = client.get("/v1/models?group=false")
        assert r.status_code == 200
        data = r.json()["data"]
        ids = [m["id"] for m in data]
        assert "meta-llama/llama-3.1-8b-instruct" in ids
        assert "llama-3.1-8b-instant" in ids
        # Every entry should carry canonical_id, even ungrouped.
        for m in data:
            assert "canonical_id" in m
            assert "aliases" in m
            assert "available_providers" in m


class TestGrouped:
    def test_collapses_same_logical_model_across_providers(self, monkeypatch):
        # OR + Groq + HF all have Llama 3.1 8B Instruct under different ids.
        or_p = _StubProvider("openrouter", [_make_model("meta-llama/llama-3.1-8b-instruct", "openrouter")])
        groq_p = _StubProvider("groq", [_make_model("llama-3.1-8b-instant", "groq")])
        hf_p = _StubProvider("huggingface", [_make_model("meta-llama/Llama-3.1-8B-Instruct", "huggingface")])
        client = _client_with(
            [or_p, groq_p, hf_p],
            monkeypatch,
            env_keys={"OPENROUTER_API_KEY": "k", "GROQ_API_KEY": "k", "HF_TOKEN": "k"},
        )
        r = client.get("/v1/models")  # group=true by default
        assert r.status_code == 200
        data = r.json()["data"]
        assert len(data) == 1, f"expected 1 grouped entry, got {len(data)}: {[m['id'] for m in data]}"
        entry = data[0]
        # First-seen provider's id wins as the canonical surface.
        assert entry["id"] == "meta-llama/llama-3.1-8b-instruct"
        assert entry["canonical_id"] == "llama-3.1-8b-instruct"
        # Other providers' ids land in aliases.
        assert "llama-3.1-8b-instant" in entry["aliases"]
        assert "meta-llama/Llama-3.1-8B-Instruct" in entry["aliases"]
        # All three providers listed.
        assert set(entry["available_providers"]) == {"openrouter", "groq", "huggingface"}

    def test_distinct_models_stay_distinct(self, monkeypatch):
        or_p = _StubProvider(
            "openrouter",
            [
                _make_model("meta-llama/llama-3.1-8b-instruct", "openrouter"),
                _make_model("meta-llama/llama-3.3-70b-instruct", "openrouter"),
            ],
        )
        client = _client_with([or_p], monkeypatch, env_keys={"OPENROUTER_API_KEY": "k"})
        r = client.get("/v1/models")
        assert r.status_code == 200
        data = r.json()["data"]
        canon = sorted(m["canonical_id"] for m in data)
        assert canon == ["llama-3.1-8b-instruct", "llama-3.3-70b-instruct"]

    def test_quantization_variants_collapse(self, monkeypatch):
        # CF's fp8 variant + OR's standard form should group.
        or_p = _StubProvider("openrouter", [_make_model("meta-llama/llama-3.1-8b-instruct", "openrouter")])
        cf_p = _StubProvider(
            "cloudflare_wai",
            [_make_model("@cf/meta/llama-3.1-8b-instruct-fp8", "cloudflare_wai")],
        )
        client = _client_with(
            [or_p, cf_p],
            monkeypatch,
            env_keys={"OPENROUTER_API_KEY": "k", "CLOUDFLARE_API_TOKEN": "k", "CLOUDFLARE_ACCOUNT_ID": "x"},
        )
        r = client.get("/v1/models")
        assert r.status_code == 200
        data = r.json()["data"]
        assert len(data) == 1
        assert data[0]["canonical_id"] == "llama-3.1-8b-instruct"
        assert "@cf/meta/llama-3.1-8b-instruct-fp8" in data[0]["aliases"]


class TestCacheBehavior:
    def test_grouped_and_ungrouped_have_separate_cache_keys(self, monkeypatch):
        or_p = _StubProvider(
            "openrouter",
            [
                _make_model("meta-llama/llama-3.1-8b-instruct", "openrouter"),
                _make_model("llama-3.1-8b-instant", "openrouter"),
            ],
        )
        client = _client_with([or_p], monkeypatch, env_keys={"OPENROUTER_API_KEY": "k"})

        grouped = client.get("/v1/models").json()["data"]
        ungrouped = client.get("/v1/models?group=false").json()["data"]

        assert len(grouped) == 1, f"grouped should collapse: {grouped}"
        assert len(ungrouped) == 2, f"ungrouped should expose both: {ungrouped}"
