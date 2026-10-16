"""Keyless opt-in through provider_env and model-scoped cooldowns."""

from __future__ import annotations

import pytest

from freeride.core.errors import ErrorKind
from freeride.core.provider_env import all_keys_for, env_var_for, is_configured
from freeride.core.provider_specs import KEYLESS_ENV, KEYLESS_SENTINEL, PROVIDER_SPECS


class TestKeylessEnv:
    def test_off_by_default(self, monkeypatch):
        monkeypatch.delenv(KEYLESS_ENV, raising=False)
        assert all_keys_for("ovh") == []
        assert not is_configured("ovh")

    def test_named_opt_in(self, monkeypatch):
        monkeypatch.setenv(KEYLESS_ENV, "OVH, blockrun")
        assert all_keys_for("ovh") == [KEYLESS_SENTINEL]
        assert is_configured("ovh") and is_configured("blockrun") and not is_configured("kilo")

    def test_all(self, monkeypatch):
        monkeypatch.setenv(KEYLESS_ENV, "all")
        for s in PROVIDER_SPECS:
            if s.keyless:
                assert is_configured(s.name)

    def test_keyed_specs_need_their_env_var(self, monkeypatch):
        monkeypatch.setenv(KEYLESS_ENV, "all")
        monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
        assert not is_configured("mistral")
        monkeypatch.setenv("MISTRAL_API_KEY", "m1")
        assert all_keys_for("mistral") == ["m1"]
        assert env_var_for("mistral") == "MISTRAL_API_KEY"
        assert env_var_for("ovh") == KEYLESS_ENV

    def test_gemini_alias(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.setenv("GOOGLE_API_KEY", "g")
        assert all_keys_for("gemini") == ["g"]


class TestServeRegistry:
    def test_spec_providers_load_when_configured(self, monkeypatch):
        from freeride.cli.cmd_serve import build_provider_registry

        monkeypatch.setattr("freeride.core.dotenv.load_dotenv_into_environ", lambda *a, **k: None)
        for s in PROVIDER_SPECS:
            for v in s.env_vars:
                monkeypatch.delenv(v, raising=False)
        monkeypatch.setenv(KEYLESS_ENV, "ovh")
        monkeypatch.setenv("SAMBANOVA_API_KEY", "s")
        names = [p.name for p in build_provider_registry()]
        assert "ovh" in names and "sambanova" in names
        assert "kilo" not in names and "mistral" not in names


class TestModelScopedCooldown:
    def test_model_scope_marks_model_not_key(self, monkeypatch, tmp_path):
        from freeride.core import failover
        from freeride.core.cooldown import KeyCooldown
        from freeride.core.provider_specs import spec_by_name
        from freeride.providers.openai_compat import make_provider

        marked: list[tuple[str, str]] = []
        monkeypatch.setattr("freeride.core.model_health.mark_recent_failure", lambda p, m: marked.append((p, m)))
        cd = KeyCooldown(tmp_path / "cd.json")
        samba = make_provider(spec_by_name("sambanova"))
        assert samba.cooldown_scope == "model"
        failover._cool(cd, samba, "k", ErrorKind.RATE_LIMIT, None, model="DeepSeek-V3.1")
        assert marked == [("sambanova", "DeepSeek-V3.1")]
        assert not cd.is_in_cooldown("sambanova", "k")

        # AUTH still benches the key regardless of scope.
        failover._cool(cd, samba, "k", ErrorKind.AUTH, None, model="DeepSeek-V3.1")
        assert cd.is_in_cooldown("sambanova", "k")

    def test_key_scope_unchanged(self, monkeypatch, tmp_path):
        from freeride.core import failover
        from freeride.core.cooldown import KeyCooldown
        from freeride.core.provider_specs import spec_by_name
        from freeride.providers.openai_compat import make_provider

        cd = KeyCooldown(tmp_path / "cd.json")
        mistral = make_provider(spec_by_name("mistral"))
        failover._cool(cd, mistral, "k", ErrorKind.RATE_LIMIT, None, model="codestral-latest")
        assert cd.is_in_cooldown("mistral", "k")
