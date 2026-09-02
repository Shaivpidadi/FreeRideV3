"""Tests for GET /health — specifically ``keyed_providers``.

The ridex launcher decides whether to run the local key-setup wizard
by reading ``keyed_providers`` (registration alone doesn't imply keys:
openrouter is always registered). These tests pin that contract.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from freeride.server.app import create_app
from freeride.providers.openrouter import OpenRouterProvider


def test_health_reports_empty_keyed_providers_without_keys(monkeypatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = TestClient(create_app(providers=[OpenRouterProvider()]))
    body = client.get("/health").json()
    assert body["ok"] is True
    assert "openrouter" in body["providers"]
    assert body["keyed_providers"] == []


def test_health_reports_keyed_provider_when_env_key_present(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    client = TestClient(create_app(providers=[OpenRouterProvider()]))
    body = client.get("/health").json()
    assert body["keyed_providers"] == ["openrouter"]
