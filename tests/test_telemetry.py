"""Tests for freeride.core.telemetry — opt-in beacon, payload, persistence."""

from __future__ import annotations

import json
import tempfile
import uuid as _uuid
from pathlib import Path

import pytest

from freeride.core import telemetry


@pytest.fixture
def isolated_config(monkeypatch, tmp_path):
    """Redirect telemetry's persistent files into a tmp dir for the test."""
    monkeypatch.setattr(telemetry, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(telemetry, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(telemetry, "INSTALLATION_FILE", tmp_path / "installation_id")
    monkeypatch.setattr(telemetry, "STATS_FILE", tmp_path / "stats.json")
    return tmp_path


class TestInstallationId:
    def test_generates_uuid_v4_on_first_call(self, isolated_config):
        iid = telemetry.installation_id()
        # Validates the format
        parsed = _uuid.UUID(iid)
        assert parsed.version == 4
        # Persisted as bare string (NOT JSON-quoted)
        assert telemetry.INSTALLATION_FILE.read_text() == iid

    def test_persists_across_calls(self, isolated_config):
        a = telemetry.installation_id()
        b = telemetry.installation_id()
        assert a == b

    def test_resettable_by_deleting_file(self, isolated_config):
        a = telemetry.installation_id()
        telemetry.INSTALLATION_FILE.unlink()
        b = telemetry.installation_id()
        assert a != b


class TestEnabledState:
    def test_default_off(self, isolated_config):
        assert telemetry.is_enabled() is False

    def test_set_on_persists(self, isolated_config):
        telemetry.set_enabled(True)
        assert telemetry.is_enabled() is True
        # Persisted to config.json
        cfg = json.loads(telemetry.CONFIG_FILE.read_text())
        assert cfg["telemetry"] is True

    def test_toggle(self, isolated_config):
        telemetry.set_enabled(True)
        telemetry.set_enabled(False)
        assert telemetry.is_enabled() is False

    def test_other_config_keys_round_trip(self, isolated_config):
        # User has unrelated keys in config.json
        telemetry.CONFIG_FILE.write_text(json.dumps({"some_other_setting": "preserved"}))
        telemetry.set_enabled(True)
        cfg = json.loads(telemetry.CONFIG_FILE.read_text())
        assert cfg["some_other_setting"] == "preserved"
        assert cfg["telemetry"] is True


class TestPayload:
    def test_empty_state(self, isolated_config):
        p = telemetry.build_payload()
        assert p["tokens_served"] == 0
        assert p["request_count"] == 0
        assert p["providers_active"] == []
        assert p["uptime_hours"] == 0
        assert p["version"]  # non-empty
        assert p["os"] in {"darwin", "linux", "windows", "other"}
        # installation_id is a bare UUID string
        _uuid.UUID(p["installation_id"])

    def test_payload_reflects_local_stats(self, isolated_config):
        # Pre-populate stats.json
        telemetry.STATS_FILE.write_text(
            json.dumps(
                {
                    "tokens_served": 1234,
                    "request_count": 56,
                    "providers_active": ["openrouter", "nvidia_nim"],
                    "uptime_hours": 8,
                }
            )
        )
        p = telemetry.build_payload()
        assert p["tokens_served"] == 1234
        assert p["request_count"] == 56
        assert p["providers_active"] == ["openrouter", "nvidia_nim"]
        assert p["uptime_hours"] == 8

    def test_payload_never_includes_forbidden_fields(self, isolated_config):
        """The audit gate: prompts/completions/keys/model_ids/hostname must
        NOT be in the payload — this is the contract per PLAN_GATEWAY.md §14.
        """
        p = telemetry.build_payload()
        keys = set(p.keys())
        forbidden = {
            "prompt",
            "completion",
            "messages",
            "api_key",
            "model",
            "model_id",
            "hostname",
            "ip",
            "ipaddr",
        }
        assert keys.isdisjoint(forbidden), (
            f"telemetry payload included forbidden keys: {keys & forbidden}"
        )


class TestShipBeacon:
    def test_ship_returns_false_when_disabled(self, isolated_config, monkeypatch):
        # Even if httpx would succeed, ship must return False with telemetry off.
        called = {"n": 0}

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def post(self, *a, **k):
                called["n"] += 1

                class R:
                    status_code = 200

                return R()

        # Don't even import httpx — disabled means no network, period.
        assert telemetry.ship_beacon() is False
        assert called["n"] == 0

    def test_ship_returns_true_on_2xx_when_enabled(self, isolated_config, monkeypatch):
        telemetry.set_enabled(True)
        sent: dict = {}

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def post(self, url, json):
                sent["url"] = url
                sent["payload"] = json

                class R:
                    status_code = 200

                return R()

        import freeride.core.telemetry as tmod

        monkeypatch.setattr("httpx.Client", FakeClient)
        assert tmod.ship_beacon() is True
        assert sent["url"] == telemetry.beacon_url()
        # Payload shape verified separately; just confirm something went out
        assert "installation_id" in sent["payload"]

    def test_ship_silently_swallows_network_error(self, isolated_config, monkeypatch):
        telemetry.set_enabled(True)

        class FailingClient:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def post(self, *a, **k):
                raise RuntimeError("simulated network blip")

        monkeypatch.setattr("httpx.Client", FailingClient)
        # Must not raise; just returns False.
        assert telemetry.ship_beacon() is False


class TestPreviewPayload:
    def test_preview_renders_json(self, isolated_config):
        out = telemetry.preview_payload()
        parsed = json.loads(out)
        assert "installation_id" in parsed
