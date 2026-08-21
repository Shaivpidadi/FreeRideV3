"""Tests for `freeride keys`."""

from __future__ import annotations

import json

import pytest

from freeride.cli import cmd_keys as keys_module
from freeride.cli.cmd_keys import (
    _COOLDOWN_TTL,
    _key_status,
    collect_status,
    format_summary,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    """Strip every provider env var and point cooldown.json at a tmp file."""
    for var in (
        "OPENROUTER_API_KEY",
        "GROQ_API_KEY",
        "NVIDIA_API_KEY",
        "NIM_API_KEY",
        "CLOUDFLARE_API_TOKEN",
        "CLOUDFLARE_ACCOUNT_ID",
        "HF_TOKEN",
        "HUGGINGFACE_API_KEY",
        "CEREBRAS_API_KEY",
        "OLLAMA_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    cd_path = tmp_path / "cooldown.json"
    monkeypatch.setattr(keys_module, "_COOLDOWN_PATH", cd_path)
    yield


def _write_cooldown(monkeypatch, state: dict):
    p = keys_module._COOLDOWN_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state), encoding="utf-8")


# ---------------------------------------------------------------------------


class TestKeyStatus:
    def test_no_cooldown_is_available(self):
        status, remaining = _key_status(cooldown_ts=None, now=1000.0)
        assert status == "available"
        assert remaining is None

    def test_recent_cooldown_is_cooling(self):
        # Cooldown started 30s ago, TTL is 120s → 90s remaining.
        status, remaining = _key_status(cooldown_ts=970.0, now=1000.0)
        assert status == "cooling"
        assert remaining == _COOLDOWN_TTL - 30

    def test_expired_cooldown_is_available(self):
        status, remaining = _key_status(cooldown_ts=500.0, now=1000.0)
        assert status == "available"
        assert remaining is None


class TestCollectStatus:
    def test_no_env_vars_returns_empty(self):
        assert collect_status(now=1000.0) == []

    def test_single_key_provider_listed(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        out = collect_status(now=1000.0)
        assert len(out) == 1
        assert out[0]["provider"] == "openrouter"
        assert out[0]["n_keys"] == 1
        assert out[0]["n_available"] == 1
        assert out[0]["n_cooling"] == 0

    def test_multi_key_json_array(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", '["k1","k2","k3"]')
        out = collect_status(now=1000.0)
        assert out[0]["n_keys"] == 3
        assert out[0]["n_available"] == 3

    def test_cooling_key_reflected_in_status(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", '["k1","k2","k3"]')
        # Mark k2 as cooling 30s ago.
        _write_cooldown(monkeypatch, {"openrouter": {"k2": 970.0}})
        out = collect_status(now=1000.0)
        assert out[0]["n_cooling"] == 1
        assert out[0]["n_available"] == 2
        # The cooling key is k1 in display index (k2 is the second key,
        # 0-indexed → "k1"). Actually keys list is ["k1", "k2", "k3"]
        # which means index 0 is "k1", index 1 is "k2". The cooling key
        # in cooldown.json was "k2" → display index 1 → "k1" in our
        # k0/k1/k2 numbering. Verify.
        cooling_keys = [k for k in out[0]["per_key"] if k["status"] == "cooling"]
        assert len(cooling_keys) == 1
        assert cooling_keys[0]["index"] == 1  # k1 in display

    def test_huggingface_accepts_either_env_var(self, monkeypatch):
        monkeypatch.setenv("HUGGINGFACE_API_KEY", "hf-x")
        out = collect_status(now=1000.0)
        assert any(s["provider"] == "huggingface" and s["n_keys"] == 1 for s in out)

    def test_soonest_back_is_minimum_remaining(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", '["a","b","c"]')
        # a cooling 100s ago (20s remaining), c cooling 30s ago (90s).
        _write_cooldown(monkeypatch, {"openrouter": {"a": 900.0, "c": 970.0}})
        out = collect_status(now=1000.0)
        soonest = out[0]["soonest_back"]
        assert soonest is not None
        assert soonest["index"] == 0  # 'a' is index 0, 20s remaining < 90s
        assert soonest["remaining_s"] == 20


class TestFormatSummary:
    def test_no_providers_helpful_hint(self):
        out = format_summary([], no_color=True, verbose=False)
        assert "freeride init" in out

    def test_table_includes_each_provider(self):
        snapshot = [
            {
                "provider": "openrouter",
                "n_keys": 3, "n_available": 2, "n_cooling": 1,
                "per_key": [], "soonest_back": {"index": 1, "remaining_s": 47},
            },
            {
                "provider": "groq",
                "n_keys": 1, "n_available": 1, "n_cooling": 0,
                "per_key": [], "soonest_back": None,
            },
        ]
        out = format_summary(snapshot, no_color=True, verbose=False)
        assert "openrouter" in out
        assert "groq" in out
        assert "k1 in 47s" in out
        # Footer.
        assert "1/4 keys cooling" in out

    def test_verbose_shows_per_key_lines(self):
        snapshot = [
            {
                "provider": "openrouter",
                "n_keys": 2, "n_available": 1, "n_cooling": 1,
                "per_key": [
                    {"index": 0, "hash": "a1b2c3d4", "status": "cooling", "remaining_s": 47},
                    {"index": 1, "hash": "e5f6g7h8", "status": "available", "remaining_s": None},
                ],
                "soonest_back": {"index": 0, "remaining_s": 47},
            },
        ]
        out = format_summary(snapshot, no_color=True, verbose=True)
        assert "k0 (a1b2c3d4)" in out
        assert "k1 (e5f6g7h8)" in out
        assert "47s remaining" in out

    def test_all_available_footer(self):
        snapshot = [
            {
                "provider": "openrouter",
                "n_keys": 1, "n_available": 1, "n_cooling": 0,
                "per_key": [], "soonest_back": None,
            },
        ]
        out = format_summary(snapshot, no_color=True, verbose=False)
        assert "all 1 keys available" in out
