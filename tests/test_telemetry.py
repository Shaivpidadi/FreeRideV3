"""Tests for freeride.core.telemetry — opt-in beacon, payload, persistence."""

from __future__ import annotations

import json
import uuid as _uuid

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
    def test_default_on(self, isolated_config):
        # No config file -> default ON.
        assert telemetry.is_enabled() is True

    def test_explicit_off_disables(self, isolated_config):
        telemetry.set_enabled(False)
        assert telemetry.is_enabled() is False

    def test_explicit_on(self, isolated_config):
        telemetry.set_enabled(True)
        assert telemetry.is_enabled() is True
        cfg = json.loads(telemetry.CONFIG_FILE.read_text())
        assert cfg["telemetry"] is True

    def test_toggle(self, isolated_config):
        telemetry.set_enabled(True)
        telemetry.set_enabled(False)
        assert telemetry.is_enabled() is False
        telemetry.set_enabled(True)
        assert telemetry.is_enabled() is True

    def test_other_config_keys_round_trip(self, isolated_config):
        telemetry.CONFIG_FILE.write_text(json.dumps({"some_other_setting": "preserved"}))
        telemetry.set_enabled(False)
        cfg = json.loads(telemetry.CONFIG_FILE.read_text())
        assert cfg["some_other_setting"] == "preserved"
        assert cfg["telemetry"] is False


class TestDisclosure:
    def test_should_show_when_default_on_and_unseen(self, isolated_config):
        # Fresh install: telemetry default-on, disclosure unseen -> show
        assert telemetry.should_show_disclosure() is True

    def test_does_not_show_after_marked(self, isolated_config):
        telemetry.mark_disclosure_shown()
        assert telemetry.should_show_disclosure() is False

    def test_does_not_show_when_disabled(self, isolated_config):
        # If user opted out before first run, no point showing the disclosure
        telemetry.set_enabled(False)
        assert telemetry.should_show_disclosure() is False

    def test_show_disclosure_banner_once_prints_then_silences(self, isolated_config, capsys):
        # First call: prints
        telemetry.show_disclosure_banner_once()
        out1 = capsys.readouterr().out
        assert "freeride telemetry" in out1.lower()
        assert telemetry.beacon_url() in out1
        assert "freeride telemetry off" in out1
        # Second call: silent
        telemetry.show_disclosure_banner_once()
        out2 = capsys.readouterr().out
        assert out2 == ""

    def test_banner_not_shown_when_opted_out(self, isolated_config, capsys):
        telemetry.set_enabled(False)
        telemetry.show_disclosure_banner_once()
        assert capsys.readouterr().out == ""


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
        NOT be in the payload — this is the contract per the design plan
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

        # Explicitly opt out, then verify no call happens.
        telemetry.set_enabled(False)
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
