"""Hermetic tests for the v2compat OpenClaw writer."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from freeride.v2compat.openclaw import (
    ensure_config_structure,
    format_model_for_openclaw,
    load_openclaw_config,
    save_openclaw_config,
    setup_openrouter_auth,
    stored_to_api_id,
    update_model_config,
)


@pytest.fixture
def cfg_path() -> Path:
    with tempfile.TemporaryDirectory() as d:
        yield Path(d) / "openclaw.json"


class TestFormatModelForOpenclaw:
    """Verbatim from v2 docstring — these examples are the parity gate."""

    def test_with_free_suffix(self):
        assert format_model_for_openclaw("qwen/qwen3-coder:free") == "openrouter/qwen/qwen3-coder:free"

    def test_appends_free_when_missing(self):
        assert format_model_for_openclaw("qwen/qwen3-coder", append_free=True) == "openrouter/qwen/qwen3-coder:free"

    def test_no_append_free(self):
        assert format_model_for_openclaw("qwen/qwen3-coder", append_free=False) == "openrouter/qwen/qwen3-coder"

    def test_native_openrouter_no_free_suffix(self):
        # Native models DO get the openrouter/ prefix (so the literal
        # openrouter/openrouter/free) but NOT the :free suffix.
        assert format_model_for_openclaw("openrouter/free") == "openrouter/openrouter/free"
        assert format_model_for_openclaw("openrouter/owl-alpha") == "openrouter/openrouter/owl-alpha"


class TestStoredToApiId:
    def test_strips_leading_provider(self):
        assert stored_to_api_id("openrouter/qwen/qwen3-coder:free") == "qwen/qwen3-coder:free"
        assert stored_to_api_id("openrouter/openrouter/free") == "openrouter/free"
        assert stored_to_api_id("openrouter/openrouter/owl-alpha") == "openrouter/owl-alpha"

    def test_no_op_when_no_prefix(self):
        assert stored_to_api_id("bare/id") == "bare/id"


class TestEnsureConfigStructure:
    def test_creates_nested_dicts(self):
        cfg = {}
        ensure_config_structure(cfg)
        assert cfg["agents"]["defaults"]["model"] == {}
        assert cfg["agents"]["defaults"]["models"] == {}

    def test_preserves_existing_values(self):
        cfg = {"agents": {"defaults": {"model": {"primary": "kept"}}}}
        ensure_config_structure(cfg)
        assert cfg["agents"]["defaults"]["model"]["primary"] == "kept"
        # New key created without touching the old
        assert "models" in cfg["agents"]["defaults"]


class TestSetupOpenrouterAuth:
    def test_adds_profile(self):
        cfg = {}
        setup_openrouter_auth(cfg)
        assert cfg["auth"]["profiles"]["openrouter:default"] == {
            "provider": "openrouter",
            "mode": "api_key",
        }

    def test_idempotent(self):
        cfg = {"auth": {"profiles": {"openrouter:default": {"existing": True}}}}
        setup_openrouter_auth(cfg)
        # Should NOT overwrite the existing profile
        assert cfg["auth"]["profiles"]["openrouter:default"] == {"existing": True}


class TestSaveLoadRoundTrip:
    def test_round_trip(self, cfg_path):
        original = {
            "agents": {
                "defaults": {
                    "model": {
                        "primary": "openrouter/qwen/qwen3-coder:free",
                        "fallbacks": ["openrouter/openrouter/free"],
                    },
                    "models": {
                        "openrouter/qwen/qwen3-coder:free": {},
                        "openrouter/openrouter/free": {},
                    },
                }
            },
            "gateway": {"port": 8443, "channels": ["openclaw"]},  # unrelated, must persist
        }
        save_openclaw_config(original, cfg_path)
        loaded = load_openclaw_config(cfg_path)
        assert loaded == original


class TestUpdateModelConfig:
    def _provider(self, ids: list[str]):
        class M:
            def __init__(self, api_id: str) -> None:
                self.api_id = api_id

        return lambda: [M(i) for i in ids]

    def test_sets_primary_and_default_fallbacks(self, cfg_path):
        update_model_config(
            "qwen/qwen3-coder",
            free_models_provider=self._provider(["mistral/mistral", "deepseek/deepseek"]),
            api_keys=["k1"],
            as_primary=True,
            add_fallbacks=True,
            fallback_count=3,
            config_path=cfg_path,
        )
        cfg = load_openclaw_config(cfg_path)
        assert cfg["agents"]["defaults"]["model"]["primary"] == "openrouter/qwen/qwen3-coder:free"
        fallbacks = cfg["agents"]["defaults"]["model"]["fallbacks"]
        assert fallbacks == [
            "openrouter/openrouter/free",
            "openrouter/mistral/mistral:free",
            "openrouter/deepseek/deepseek:free",
        ]

    def test_preserves_unrelated_top_level_keys(self, cfg_path):
        cfg_path.write_text(json.dumps({"gateway": {"port": 8443}, "channels": ["a"]}))
        update_model_config(
            "qwen/q",
            free_models_provider=self._provider([]),
            api_keys=["k"],
            as_primary=True,
            add_fallbacks=False,
            config_path=cfg_path,
        )
        cfg = load_openclaw_config(cfg_path)
        assert cfg["gateway"] == {"port": 8443}
        assert cfg["channels"] == ["a"]

    def test_setup_auth_flag_adds_profile(self, cfg_path):
        update_model_config(
            "x/y",
            free_models_provider=self._provider([]),
            api_keys=["k"],
            setup_auth=True,
            add_fallbacks=False,
            config_path=cfg_path,
        )
        cfg = load_openclaw_config(cfg_path)
        assert "openrouter:default" in cfg["auth"]["profiles"]

    def test_no_keys_skips_fallbacks(self, cfg_path):
        update_model_config(
            "x/y",
            free_models_provider=self._provider(["a/b"]),
            api_keys=[],  # no keys -> no fallback work, even with add_fallbacks=True
            add_fallbacks=True,
            config_path=cfg_path,
        )
        cfg = load_openclaw_config(cfg_path)
        assert cfg["agents"]["defaults"]["model"].get("fallbacks") in (None, [])

    def test_skips_smart_router_when_primary(self, cfg_path):
        update_model_config(
            "openrouter/free",
            free_models_provider=self._provider(["a/b"]),
            api_keys=["k"],
            as_primary=True,
            add_fallbacks=True,
            fallback_count=2,
            config_path=cfg_path,
        )
        cfg = load_openclaw_config(cfg_path)
        # Primary is the smart router; fallbacks should NOT also include it
        assert cfg["agents"]["defaults"]["model"]["primary"] == "openrouter/openrouter/free"
        fallbacks = cfg["agents"]["defaults"]["model"]["fallbacks"]
        assert "openrouter/openrouter/free" not in fallbacks
