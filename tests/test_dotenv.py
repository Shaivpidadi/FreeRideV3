"""Tests for the pure-Python dotenv loader.

Critical contract: OS env wins. We never overwrite a value that's
already in os.environ — only fill gaps. That guarantee is what makes
auto-loading at gateway startup safe (an explicit `export` always
takes precedence over a stale `~/.freeride/.env`).
"""

from __future__ import annotations

import os

import pytest

from freeride.core.dotenv import load_dotenv_into_environ, parse_dotenv


# ---- parser ---------------------------------------------------------------


class TestParseDotenv:
    def test_basic_kv(self):
        assert parse_dotenv("FOO=bar") == {"FOO": "bar"}

    def test_strips_whitespace(self):
        assert parse_dotenv("  FOO  =  bar  ") == {"FOO": "bar"}

    def test_skips_comments_and_blanks(self):
        text = """
        # this is a comment
        FOO=bar

        # another comment
        BAZ=qux
        """
        assert parse_dotenv(text) == {"FOO": "bar", "BAZ": "qux"}

    def test_value_can_contain_equals_sign(self):
        # JWTs and base64 values often contain '='. Split on first =.
        assert parse_dotenv("TOKEN=eyJ.foo=bar") == {"TOKEN": "eyJ.foo=bar"}

    def test_strips_outer_double_quotes(self):
        assert parse_dotenv('FOO="bar baz"') == {"FOO": "bar baz"}

    def test_strips_outer_single_quotes(self):
        assert parse_dotenv("FOO='bar baz'") == {"FOO": "bar baz"}

    def test_doesnt_strip_mismatched_quotes(self):
        assert parse_dotenv("FOO=\"bar'") == {"FOO": "\"bar'"}

    def test_skips_lines_without_equals(self):
        assert parse_dotenv("not-a-kv-line\nFOO=bar") == {"FOO": "bar"}

    def test_skips_empty_keys(self):
        # `=value` (no key on the LHS) gets dropped.
        assert parse_dotenv("=orphan\nFOO=bar") == {"FOO": "bar"}


# ---- loader ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("DOTENV_TEST_NEW", "DOTENV_TEST_EXISTING"):
        monkeypatch.delenv(k, raising=False)
    yield


class TestLoadDotenvIntoEnviron:
    def test_loads_keys_not_in_environ(self, tmp_path, monkeypatch):
        p = tmp_path / ".env"
        p.write_text("DOTENV_TEST_NEW=from-file\n")
        result = load_dotenv_into_environ(p)
        assert result == {"DOTENV_TEST_NEW": "from-file"}
        assert os.environ["DOTENV_TEST_NEW"] == "from-file"

    def test_does_not_overwrite_existing_environ(self, tmp_path, monkeypatch):
        # OS env says X=os-value; .env says X=file-value. OS wins.
        monkeypatch.setenv("DOTENV_TEST_EXISTING", "os-value")
        p = tmp_path / ".env"
        p.write_text("DOTENV_TEST_EXISTING=file-value\n")
        result = load_dotenv_into_environ(p)
        assert result == {}, "should not have set anything"
        assert os.environ["DOTENV_TEST_EXISTING"] == "os-value"

    def test_missing_file_returns_empty(self, tmp_path):
        p = tmp_path / "does-not-exist.env"
        assert load_dotenv_into_environ(p) == {}

    def test_default_path_used_when_none(self, tmp_path, monkeypatch):
        # Use monkeypatch.setattr to override the module-level
        # DEFAULT_DOTENV_PATH so we don't have to reload the module
        # (importlib.reload causes test pollution that bleeds into
        # later tests; setattr is reverted cleanly on teardown).
        from freeride.core import dotenv as dotenv_module

        env_file = tmp_path / "alt.env"
        env_file.write_text("DOTENV_TEST_NEW=default-path\n")
        monkeypatch.setattr(dotenv_module, "DEFAULT_DOTENV_PATH", env_file)
        result = dotenv_module.load_dotenv_into_environ()
        assert result == {"DOTENV_TEST_NEW": "default-path"}

    def test_partial_overlap_only_fills_gaps(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DOTENV_TEST_EXISTING", "from-os")
        p = tmp_path / ".env"
        p.write_text(
            "DOTENV_TEST_EXISTING=from-file\n"
            "DOTENV_TEST_NEW=also-from-file\n"
        )
        result = load_dotenv_into_environ(p)
        # Only the new key actually got set.
        assert result == {"DOTENV_TEST_NEW": "also-from-file"}
        assert os.environ["DOTENV_TEST_EXISTING"] == "from-os"
        assert os.environ["DOTENV_TEST_NEW"] == "also-from-file"

    def test_malformed_file_returns_empty_silently(self, tmp_path):
        p = tmp_path / ".env"
        # Binary garbage that's not valid utf-8.
        p.write_bytes(b"\xff\xfe garbage \x00")
        # Should not raise, should return empty.
        assert load_dotenv_into_environ(p) == {}


# ---- integration with build_provider_registry -----------------------------


class TestRegistryAutoloadsDotenv:
    """End-to-end: cmd_serve.build_provider_registry() should see provider
    keys that are ONLY in ~/.freeride/.env (not in OS env).
    """

    def test_groq_loads_when_only_in_dotenv(self, tmp_path, monkeypatch):
        # Strip every provider env var so the test starts from scratch.
        # Use monkeypatch — pytest will restore them on teardown even if
        # build_provider_registry() set them via os.environ[]= directly
        # (because monkeypatch tracks delenv'd vars to restore by name,
        # and their snapshot value at test-setup wins on teardown).
        for k in ("OPENROUTER_API_KEY", "GROQ_API_KEY", "NVIDIA_API_KEY",
                  "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID",
                  "HF_TOKEN", "HUGGINGFACE_API_KEY", "CEREBRAS_API_KEY",
                  "OLLAMA_BASE_URL"):
            monkeypatch.delenv(k, raising=False)

        # Point the dotenv default at a tmp file via monkeypatch.setattr
        # — no module reload, no HOME manipulation, no pollution.
        from freeride.core import dotenv as dotenv_module

        env_file = tmp_path / "freeride.env"
        env_file.write_text("GROQ_API_KEY=gsk-test-from-dotenv\n")
        monkeypatch.setattr(dotenv_module, "DEFAULT_DOTENV_PATH", env_file)

        from freeride.cli.cmd_serve import build_provider_registry
        try:
            providers = build_provider_registry()
            provider_names = [p.name for p in providers]
            # OpenRouter is always-on; Groq should now be registered too
            # because .env supplied the key.
            assert "groq" in provider_names, (
                f"GROQ_API_KEY in .env should auto-load. Got: {provider_names}"
            )
        finally:
            # build_provider_registry() set GROQ_API_KEY via os.environ[]
            # which monkeypatch may not track. Explicit cleanup keeps
            # later tests honest.
            os.environ.pop("GROQ_API_KEY", None)
