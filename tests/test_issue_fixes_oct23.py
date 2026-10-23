"""Fixes for the four 2026-10-21 issues: log redaction and whitespace
keys (#11), the OpenCode binder (#12), dotenv for v2 commands (#13),
preset ids on the OpenAI route (#14)."""

from __future__ import annotations

import json
import logging

import pytest

from freeride.core.log_redact import RedactingFilter, redact
from freeride.core.model_router import preset_from_model_id
from freeride.core.provider_env import parse_api_keys


class TestRedaction:
    @pytest.mark.parametrize(
        "text",
        [
            "Illegal header value b'Bearer sk-or-v1-0123456789abcdef0123456789abcdef'",
            "Authorization: Bearer gsk_ABCDEFGHIJKLMNOPQRST",
            "token hf_abcdefghijklmnopqrstuvwxyz12 rejected",
            "nvapi-AbCdEfGhIjKlMnOpQrStUvWx failed",
            "OPENROUTER_API_KEY_2=sk-or-v1-ffffffffffffffffffffffff\nGROQ_API_KEY=gsk_zzzzzzzzzzzzzzzzzzzz",
        ],
    )
    def test_known_shapes_are_scrubbed(self, text):
        out = redact(text)
        for needle in ("sk-or-v1-", "gsk_", "hf_abc", "nvapi-Ab"):
            assert needle not in out, out
        assert "***" in out

    def test_plain_text_untouched(self):
        s = "provider groq list_free_models failed: 503 Service Unavailable"
        assert redact(s) == s

    def test_filter_rewrites_records_with_args(self, caplog):
        logger = logging.getLogger("freeride.test.redact")
        logger.addFilter(RedactingFilter())
        with caplog.at_level(logging.WARNING, logger="freeride.test.redact"):
            logger.warning("provider %s failed: %s", "openrouter", "Illegal header value b'Bearer sk-or-v1-abcdefabcdefabcdefabcdef'")
        assert "sk-or-v1-abc" not in caplog.text
        assert "provider openrouter failed" in caplog.text

    def test_server_installs_filter(self):
        from freeride.server.app import create_app
        from tests.fixtures.noop_provider import NoopProvider

        create_app(providers=[NoopProvider()])
        root = logging.getLogger()
        assert any(isinstance(f, RedactingFilter) for f in root.filters)


class TestWhitespaceKeys:
    def test_multiline_value_is_dropped_not_sent(self, caplog):
        with caplog.at_level(logging.WARNING):
            keys = parse_api_keys("sk-or-v1-aaaaaaaaaaaaaaaa\nOPENROUTER_API_KEY_2=sk-or-v1-bbbbbbbbbbbbbbbb")
        assert keys == []
        assert "contains whitespace" in caplog.text
        # The warning never carries the value itself.
        assert "aaaaaaaa" not in caplog.text and "bbbbbbbb" not in caplog.text

    def test_json_array_and_plain_still_work(self):
        assert parse_api_keys('["a", " b ", ""]') == ["a", "b"]
        assert parse_api_keys("  single  ") == ["single"]
        assert parse_api_keys(["x y", "z"]) == ["z"]


class TestOpenCodeBinder:
    def test_writes_provider_block_and_default_model(self, tmp_path):
        from freeride.binders import opencode

        p = tmp_path / "opencode.json"
        msg = opencode.bind("http://x:1/v1", config_path=p)
        cfg = json.loads(p.read_text())
        prov = cfg["provider"]["freeride"]
        assert prov["npm"] == "@ai-sdk/openai-compatible"
        assert prov["options"] == {"baseURL": "http://x:1/v1", "apiKey": "any"}
        assert "auto" in prov["models"] and "freeride/coding" in prov["models"]
        assert cfg["model"] == "freeride/auto"
        assert cfg["$schema"] == "https://opencode.ai/config.json"
        assert "opencode.json updated" in msg

    def test_preserves_other_providers_and_model(self, tmp_path):
        from freeride.binders import opencode

        p = tmp_path / "opencode.json"
        p.write_text(json.dumps({"model": "anthropic/claude", "provider": {"other": {"npm": "x"}}, "theme": "dark"}))
        opencode.bind("http://x:1/v1", config_path=p)
        cfg = json.loads(p.read_text())
        assert cfg["model"] == "anthropic/claude"
        assert cfg["provider"]["other"] == {"npm": "x"}
        assert cfg["theme"] == "dark"
        assert "freeride" in cfg["provider"]

    def test_jsonc_line_comments_are_tolerated(self, tmp_path):
        from freeride.binders import opencode

        p = tmp_path / "opencode.json"
        p.write_text('{\n  // my settings\n  "theme": "dark",\n}\n')
        opencode.bind("http://x:1/v1", config_path=p)
        assert json.loads(p.read_text())["theme"] == "dark"

    def test_unparseable_config_is_refused_not_clobbered(self, tmp_path):
        from freeride.binders import opencode

        p = tmp_path / "opencode.json"
        p.write_text("{ this is not json")
        with pytest.raises(ValueError):
            opencode.bind("http://x:1/v1", config_path=p)
        assert p.read_text() == "{ this is not json"

    def test_cli_dispatch(self, tmp_path, monkeypatch, capsys):
        from freeride.binders import opencode
        from freeride.cli.cmd_bind import cmd_bind

        monkeypatch.setattr(opencode, "OPENCODE_CONFIG_PATH", tmp_path / "opencode.json")

        class Args:
            agent = "opencode"
            gateway_url = "http://localhost:11343/v1"

        assert cmd_bind(Args()) == 0
        assert "provider.freeride" in capsys.readouterr().out


class TestV2CommandsSeeDotenv:
    def test_status_loads_env_file(self, monkeypatch, tmp_path, capsys):
        from freeride.cli import main as cli_main

        env_file = tmp_path / ".env"
        env_file.write_text("OPENROUTER_API_KEY=sk-or-v1-testtesttesttesttest\n")
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.setattr("freeride.core.dotenv._DEFAULT_PATH", env_file, raising=False)
        import freeride.core.dotenv as dotenv_mod

        real = dotenv_mod.load_dotenv_into_environ
        monkeypatch.setattr(dotenv_mod, "load_dotenv_into_environ", lambda path=None: real(env_file))
        monkeypatch.setattr("freeride.v2compat.commands.load_openclaw_config", lambda *a, **k: {})
        monkeypatch.setattr("freeride.v2compat.commands.get_current_model", lambda *a, **k: None)
        monkeypatch.setattr("freeride.v2compat.commands.get_current_fallbacks", lambda *a, **k: [])
        rc = cli_main.main(["status"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "NOT SET" not in out


class TestPresetOnOpenAIRoute:
    def test_preset_parsing(self):
        assert preset_from_model_id("freeride/coding") == "coding"
        assert preset_from_model_id("FreeRide/Fast") == "fast"
        assert preset_from_model_id("freeride/nope") is None
        assert preset_from_model_id("openai/gpt-oss-120b") is None

    def test_coding_preset_routes_like_auto_with_preset_order(self, monkeypatch):
        from fastapi.testclient import TestClient

        from freeride.core.health import ProviderHealth
        from freeride.server.app import create_app
        from tests.test_failover_candidates import _Stub

        monkeypatch.setenv("FREERIDE_EVENTS", "0")
        monkeypatch.setattr("freeride.core.provider_env.all_keys_for", lambda name: ["k"])
        monkeypatch.setattr("freeride.core.failover.all_keys_for", lambda name: ["k"])
        monkeypatch.setattr("freeride.core.auto_model._provider_keys", lambda name: ["k"])
        monkeypatch.setattr("freeride.core.cooldown.KeyCooldown.available_keys", lambda self, n, keys: list(keys))
        monkeypatch.setattr("freeride.core.auto_model.fetch_leaderboard", lambda: {})
        monkeypatch.setattr("freeride.core.model_health.load_cache", lambda: {})
        # groq ranks higher by score (two providers list its model) but
        # the coding preset prefers openrouter.
        catalog = [
            {"id": "shared", "available_providers": ["groq", "other"], "provider_ids": {"groq": "shared", "other": "shared"}},
            {"id": "or-model", "available_providers": ["openrouter"], "provider_ids": {"openrouter": "or-model"}},
        ]

        async def fake_catalog(*a, **k):
            return catalog

        monkeypatch.setattr("freeride.server.routes.chat.get_or_fetch_catalog", fake_catalog)
        ProviderHealth.reset()
        groq, other, openrouter = _Stub("groq"), _Stub("other"), _Stub("openrouter")
        client = TestClient(create_app(providers=[groq, other, openrouter]))
        r = client.post("/v1/chat/completions", json={"model": "freeride/coding", "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200, r.text
        assert r.json()["_freeride_provider"] == "openrouter"
        assert r.json()["model"] == "or-model"
        # And the plain form still ranks by score.
        r = client.post("/v1/chat/completions", json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]})
        assert r.json()["_freeride_provider"] == "groq"
