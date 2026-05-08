"""Tests for freeride.core.registry — third-party plugin discovery."""

from __future__ import annotations

from unittest.mock import patch

from freeride.core import registry as registry_module
from freeride.core.provider import PROVIDER_API_VERSION


# ---- fake plugin classes used by the tests --------------------------------


class _ValidPlugin:
    """A plugin with the right api_version that constructs cleanly."""

    name = "valid"
    api_version = PROVIDER_API_VERSION

    def __init__(self):
        self.constructed = True


class _MissingApiVersion:
    """No api_version attribute — should be skipped with a warning."""

    name = "missing"


class _WrongApiVersion:
    """Wrong api_version — version-mismatch skip path."""

    name = "wrong"
    api_version = 999


class _ConstructionFails:
    """Plugin class loads but raises during __init__. The registry
    should log and skip it (same path CloudflareWAIProvider takes when
    its required env vars are missing).
    """

    name = "broken"
    api_version = PROVIDER_API_VERSION

    def __init__(self):
        raise ValueError("missing required env var FAKE_API_KEY")


# ---- helpers --------------------------------------------------------------


class _FakeEntryPoint:
    """Stand-in for importlib.metadata.EntryPoint with .load()."""

    def __init__(self, name: str, target):
        self.name = name
        self._target = target

    def load(self):
        return self._target


def _patch_entry_points(eps: list[_FakeEntryPoint]):
    return patch.object(
        registry_module.metadata,
        "entry_points",
        return_value=eps,
    )


# ---- tests ----------------------------------------------------------------


class TestDiscover:
    def test_no_entry_points_returns_empty(self):
        with _patch_entry_points([]):
            assert registry_module.discover_third_party_providers() == []

    def test_loads_valid_plugin(self):
        with _patch_entry_points([_FakeEntryPoint("ok", _ValidPlugin)]):
            out = registry_module.discover_third_party_providers()
        assert len(out) == 1
        assert isinstance(out[0], _ValidPlugin)
        assert out[0].constructed is True

    def test_skips_plugin_with_missing_api_version(self, caplog):
        import logging

        caplog.set_level(logging.WARNING)
        with _patch_entry_points([_FakeEntryPoint("missing", _MissingApiVersion)]):
            out = registry_module.discover_third_party_providers()
        assert out == []
        assert "api_version" in caplog.text

    def test_skips_plugin_with_wrong_api_version(self, caplog):
        import logging

        caplog.set_level(logging.WARNING)
        with _patch_entry_points([_FakeEntryPoint("wrong", _WrongApiVersion)]):
            out = registry_module.discover_third_party_providers()
        assert out == []
        assert "999" in caplog.text or "api_version" in caplog.text

    def test_skips_plugin_when_construction_raises(self, caplog):
        import logging

        caplog.set_level(logging.INFO)
        with _patch_entry_points([_FakeEntryPoint("broken", _ConstructionFails)]):
            out = registry_module.discover_third_party_providers()
        assert out == []
        # The 'I'm not configured' path logs at INFO, not WARNING.
        assert "broken" in caplog.text
        assert "FAKE_API_KEY" in caplog.text

    def test_one_broken_plugin_doesnt_kill_others(self):
        eps = [
            _FakeEntryPoint("first", _ValidPlugin),
            _FakeEntryPoint("broken", _ConstructionFails),
            _FakeEntryPoint("third", _ValidPlugin),
        ]
        with _patch_entry_points(eps):
            out = registry_module.discover_third_party_providers()
        # Two valid plugins should both load even though one in the
        # middle raised.
        assert len(out) == 2
        assert all(isinstance(p, _ValidPlugin) for p in out)

    def test_load_failure_doesnt_kill_others(self):
        """Entry-point .load() itself can raise (e.g., import error in
        the plugin module). That should also be a skip-with-log, not a
        crash.
        """
        broken_ep = _FakeEntryPoint("import-broken", None)
        broken_ep.load = lambda: (_ for _ in ()).throw(  # type: ignore[assignment]
            ImportError("plugin module import failed")
        )
        eps = [broken_ep, _FakeEntryPoint("ok", _ValidPlugin)]
        with _patch_entry_points(eps):
            out = registry_module.discover_third_party_providers()
        assert len(out) == 1
        assert isinstance(out[0], _ValidPlugin)
