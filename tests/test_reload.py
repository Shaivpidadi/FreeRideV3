"""Tests for /v1/_freeride/reload + freeride reload CLI.

Covers: factory-driven reload swaps providers atomically, returns
before/after/added/removed; 501-shape when no factory wired; CLI
command surfaces output cleanly.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from freeride.server.app import create_app


class _StubProvider:
    api_version = 1
    embeddings_supported = False

    def __init__(self, name: str):
        self.name = name


# ---------------------------------------------------------------------------


class TestReloadEndpoint:
    def test_reload_swaps_providers(self):
        # First call returns [openrouter]; second call returns
        # [openrouter, groq]. Reload should pick up the new groq.
        call_count = {"n": 0}

        def factory():
            call_count["n"] += 1
            if call_count["n"] == 1:
                return [_StubProvider("openrouter")]
            return [_StubProvider("openrouter"), _StubProvider("groq")]

        # Construct with the result of the FIRST factory call so app
        # starts in a known state.
        initial = factory()
        app = create_app(providers=initial, provider_factory=factory)
        client = TestClient(app)

        r = client.post("/v1/_freeride/reload")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["before"] == ["openrouter"]
        assert body["after"] == ["openrouter", "groq"]
        assert body["added"] == ["groq"]
        assert body["removed"] == []

    def test_reload_can_remove_providers_too(self):
        call_count = {"n": 0}

        def factory():
            call_count["n"] += 1
            if call_count["n"] == 1:
                return [_StubProvider("openrouter"), _StubProvider("groq")]
            return [_StubProvider("openrouter")]  # groq env var got unset

        initial = factory()
        app = create_app(providers=initial, provider_factory=factory)
        client = TestClient(app)
        r = client.post("/v1/_freeride/reload")
        body = r.json()
        assert body["removed"] == ["groq"]
        assert body["added"] == []

    def test_reload_501_when_factory_not_wired(self):
        app = create_app(providers=[_StubProvider("openrouter")])
        client = TestClient(app)
        r = client.post("/v1/_freeride/reload")
        # Endpoint always returns 200; the body indicates the disabled state.
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False
        assert body["error"] == "reload_not_enabled"

    def test_reload_handles_factory_failure_gracefully(self):
        def bad_factory():
            raise RuntimeError("env var parse failed")

        app = create_app(
            providers=[_StubProvider("openrouter")], provider_factory=bad_factory
        )
        client = TestClient(app)
        r = client.post("/v1/_freeride/reload")
        body = r.json()
        assert body["ok"] is False
        assert body["error"] == "factory_failed"
        assert "env var parse failed" in body["message"]

    def test_reload_actually_picks_up_new_env_var(self, monkeypatch, tmp_path):
        """Integration-style: use the real build_provider_registry to
        prove that flipping an env var between calls flips the
        registered provider list.
        """
        from freeride.cli.cmd_serve import build_provider_registry
        from freeride.core import dotenv as dotenv_mod

        # load_dotenv_into_environ() reads DEFAULT_DOTENV_PATH at call
        # time (captured from Path.home() at import). Point it at an
        # empty tmp dir so the developer's real ~/.freeride/.env cannot
        # refill keys we just deleted.
        monkeypatch.setattr(
            dotenv_mod, "DEFAULT_DOTENV_PATH", tmp_path / ".freeride" / ".env"
        )

        for var in (
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
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")

        initial = build_provider_registry()
        app = create_app(providers=initial, provider_factory=build_provider_registry)
        client = TestClient(app)

        # Add Groq mid-process and reload.
        monkeypatch.setenv("GROQ_API_KEY", "groq-key")
        r = client.post("/v1/_freeride/reload")
        body = r.json()
        assert body["ok"] is True
        assert "groq" in body["after"]
        assert "groq" in body["added"]
