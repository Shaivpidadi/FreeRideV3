"""Tests for the single provider env-var registry."""

from __future__ import annotations

from freeride.core.provider_env import (
    all_keys_for,
    env_var_for,
    is_configured,
    parse_api_keys,
)


class TestParseApiKeys:
    def test_single_string(self):
        assert parse_api_keys("sk-or-v1-abc") == ["sk-or-v1-abc"]

    def test_json_array(self):
        assert parse_api_keys('["a", "b"]') == ["a", "b"]

    def test_empty(self):
        assert parse_api_keys("") == []
        assert parse_api_keys(None) == []


class TestAllKeysFor:
    def test_primary(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "g1")
        assert all_keys_for("groq") == ["g1"]

    def test_numbered_suffix(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "k1")
        monkeypatch.setenv("OPENROUTER_API_KEY_2", "k2")
        monkeypatch.setenv("OPENROUTER_API_KEY_3", "k3")
        assert all_keys_for("openrouter") == ["k1", "k2", "k3"]

    def test_hf_token_wins_over_alias(self, monkeypatch):
        monkeypatch.setenv("HF_TOKEN", "hf-primary")
        monkeypatch.setenv("HUGGINGFACE_API_KEY", "hf-alias")
        assert all_keys_for("huggingface") == ["hf-primary"]

    def test_hf_alias_when_primary_missing(self, monkeypatch):
        monkeypatch.delenv("HF_TOKEN", raising=False)
        monkeypatch.setenv("HUGGINGFACE_API_KEY", "hf-alias")
        assert all_keys_for("huggingface") == ["hf-alias"]

    def test_third_party_fallback(self, monkeypatch):
        monkeypatch.setenv("AWESOME_API_KEY", "ak")
        assert env_var_for("awesome") == "AWESOME_API_KEY"
        assert all_keys_for("awesome") == ["ak"]

    def test_nim_alias_when_primary_missing(self, monkeypatch):
        monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
        monkeypatch.setenv("NIM_API_KEY", "nv-alias")
        assert all_keys_for("nvidia_nim") == ["nv-alias"]
        assert is_configured("nvidia_nim")

    def test_nvidia_primary_wins_over_nim_alias(self, monkeypatch):
        monkeypatch.setenv("NVIDIA_API_KEY", "nv-primary")
        monkeypatch.setenv("NIM_API_KEY", "nv-alias")
        assert all_keys_for("nvidia_nim") == ["nv-primary"]

    def test_cloudflare_needs_account_id(self, monkeypatch):
        monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")
        monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
        assert all_keys_for("cloudflare_wai") == ["tok"]
        assert not is_configured("cloudflare_wai")
        monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
        assert is_configured("cloudflare_wai")
