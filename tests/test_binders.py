"""Hermetic tests for freeride.binders.{openclaw, aider, continue_, hermes}.

Each binder is exercised against tmp config files. The load-bearing
property is "preserves unrelated keys" — the user must trust that
running `freeride bind` won't clobber their other agent settings.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from freeride.binders import aider, continue_, hermes, openclaw


@pytest.fixture
def tmpdir() -> Path:
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


# ---- openclaw -------------------------------------------------------------


class TestOpenClawBinder:
    def test_writes_auth_profile_and_primary(self, tmpdir):
        p = tmpdir / "openclaw.json"
        p.write_text("{}")
        openclaw.bind("http://x:1/v1", config_path=p)
        cfg = json.loads(p.read_text())
        assert cfg["auth"]["profiles"]["freeride:default"]["base_url"] == "http://x:1/v1"
        assert cfg["auth"]["profiles"]["freeride:default"]["api_key"] == "any"
        assert cfg["auth"]["profiles"]["freeride:default"]["provider"] == "openai"
        assert cfg["agents"]["defaults"]["model"]["primary"] == "freeride/free"

    def test_preserves_unrelated_top_level_keys(self, tmpdir):
        p = tmpdir / "openclaw.json"
        p.write_text(json.dumps({"gateway": {"port": 8443}, "channels": ["a"]}))
        openclaw.bind("http://x:1/v1", config_path=p)
        cfg = json.loads(p.read_text())
        assert cfg["gateway"] == {"port": 8443}
        assert cfg["channels"] == ["a"]

    def test_preserves_other_auth_profiles(self, tmpdir):
        p = tmpdir / "openclaw.json"
        p.write_text(
            json.dumps(
                {"auth": {"profiles": {"some-other:profile": {"keep": True}}}}
            )
        )
        openclaw.bind("http://x:1/v1", config_path=p)
        cfg = json.loads(p.read_text())
        assert cfg["auth"]["profiles"]["some-other:profile"] == {"keep": True}
        assert cfg["auth"]["profiles"]["freeride:default"]["base_url"] == "http://x:1/v1"

    def test_idempotent(self, tmpdir):
        p = tmpdir / "openclaw.json"
        p.write_text("{}")
        openclaw.bind("http://x:1/v1", config_path=p)
        openclaw.bind("http://x:1/v1", config_path=p)  # second run
        cfg = json.loads(p.read_text())
        # Still exactly one freeride profile, exactly one freeride/free entry
        assert list(cfg["auth"]["profiles"].keys()) == ["freeride:default"]
        assert "freeride/free" in cfg["agents"]["defaults"]["models"]


# ---- aider ----------------------------------------------------------------


class TestAiderBinder:
    def test_creates_new_config(self, tmpdir):
        p = tmpdir / ".aider.conf.yml"
        aider.bind("http://x:1/v1", config_path=p)
        text = p.read_text()
        assert "openai-api-base: http://x:1/v1" in text
        assert "openai-api-key: any" in text

    def test_preserves_existing_lines(self, tmpdir):
        p = tmpdir / ".aider.conf.yml"
        p.write_text(
            "# top comment\n"
            "edit-format: diff\n"
            "auto-commits: false\n"
        )
        aider.bind("http://x:1/v1", config_path=p)
        text = p.read_text()
        assert "# top comment" in text
        assert "edit-format: diff" in text
        assert "auto-commits: false" in text
        assert "openai-api-base: http://x:1/v1" in text

    def test_replaces_existing_api_base(self, tmpdir):
        p = tmpdir / ".aider.conf.yml"
        p.write_text("openai-api-base: http://old/v1\n")
        aider.bind("http://new:2/v1", config_path=p)
        text = p.read_text()
        assert "openai-api-base: http://old/v1" not in text
        assert "openai-api-base: http://new:2/v1" in text

    def test_idempotent(self, tmpdir):
        p = tmpdir / ".aider.conf.yml"
        aider.bind("http://x:1/v1", config_path=p)
        aider.bind("http://x:1/v1", config_path=p)
        text = p.read_text()
        assert text.count("openai-api-base:") == 1
        assert text.count("openai-api-key:") == 1


# ---- continue -------------------------------------------------------------


class TestContinueBinder:
    def test_creates_yaml(self, tmpdir):
        continue_.bind("http://x:1/v1", config_dir=tmpdir)
        text = (tmpdir / "config.yaml").read_text()
        assert "title: freeride" in text
        assert "provider: openai" in text  # NOT openai-compatible
        assert "apiBase: http://x:1/v1" in text
        assert "roles: [chat, edit, autocomplete]" in text

    def test_preserves_existing_yaml_models(self, tmpdir):
        p = tmpdir / "config.yaml"
        p.write_text(
            "models:\n"
            "  - title: claude\n"
            "    provider: anthropic\n"
            "    model: claude-sonnet-4-6\n"
        )
        continue_.bind("http://x:1/v1", config_dir=tmpdir)
        text = p.read_text()
        # claude entry is preserved
        assert "title: claude" in text
        assert "provider: anthropic" in text
        # freeride entry was added
        assert "title: freeride" in text

    def test_idempotent_yaml(self, tmpdir):
        continue_.bind("http://x:1/v1", config_dir=tmpdir)
        continue_.bind("http://x:1/v1", config_dir=tmpdir)
        text = (tmpdir / "config.yaml").read_text()
        # No duplicate freeride blocks
        assert text.count("title: freeride") == 1

    def test_json_path_when_yaml_absent_but_json_exists(self, tmpdir):
        json_path = tmpdir / "config.json"
        json_path.write_text(json.dumps({"models": [{"title": "claude"}]}))
        continue_.bind("http://x:1/v1", config_dir=tmpdir)
        cfg = json.loads(json_path.read_text())
        titles = [m["title"] for m in cfg["models"]]
        assert "claude" in titles
        assert "freeride" in titles


# ---- hermes ---------------------------------------------------------------


class TestHermesBinder:
    def test_creates_new_config(self, tmpdir):
        p = tmpdir / "cli-config.yaml"
        hermes.bind("http://x:1/v1", config_path=p)
        text = p.read_text()
        assert 'provider: "custom"' in text
        assert 'base_url: "http://x:1/v1"' in text
        assert 'api_key: "any"' in text

    def test_preserves_user_comments_and_other_keys(self, tmpdir):
        p = tmpdir / "cli-config.yaml"
        p.write_text(
            "# user comment\n"
            "model:\n"
            '  default: "anthropic/claude"\n'
            "logging:\n"
            '  level: "info"\n'
        )
        hermes.bind("http://x:1/v1", config_path=p)
        text = p.read_text()
        assert "# user comment" in text
        # logging block preserved
        assert 'level: "info"' in text
        # provider was added under model:
        assert 'provider: "custom"' in text
        # base_url was set
        assert 'base_url: "http://x:1/v1"' in text

    def test_replaces_existing_provider(self, tmpdir):
        p = tmpdir / "cli-config.yaml"
        p.write_text(
            "model:\n"
            '  provider: "openrouter"\n'
            '  base_url: "https://openrouter.ai/api/v1"\n'
        )
        hermes.bind("http://x:1/v1", config_path=p)
        text = p.read_text()
        assert 'provider: "openrouter"' not in text
        assert 'provider: "custom"' in text
        assert 'base_url: "http://x:1/v1"' in text
