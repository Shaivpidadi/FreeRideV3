"""Tests for freeride.core.cooldown — TTL behavior and restart persistence."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

from freeride.core.cooldown import COOLDOWN_TTL_SECONDS, KeyCooldown


@pytest.fixture
def cd_path() -> Path:
    with tempfile.TemporaryDirectory() as d:
        yield Path(d) / "cooldown.json"


class TestKeyCooldown:
    def test_empty_initial_state(self, cd_path):
        cd = KeyCooldown(cd_path)
        assert not cd.is_in_cooldown("openrouter", "k1")
        assert cd.cooldown_remaining("openrouter", "k1") is None

    def test_mark_and_check(self, cd_path):
        cd = KeyCooldown(cd_path)
        cd.mark_rate_limited("openrouter", "k1")
        assert cd.is_in_cooldown("openrouter", "k1")
        assert cd.cooldown_remaining("openrouter", "k1") is not None

    def test_per_provider_isolation(self, cd_path):
        cd = KeyCooldown(cd_path)
        cd.mark_rate_limited("openrouter", "shared-name")
        assert cd.is_in_cooldown("openrouter", "shared-name")
        assert not cd.is_in_cooldown("nvidia_nim", "shared-name")

    def test_available_keys_filters_cooled(self, cd_path):
        cd = KeyCooldown(cd_path)
        cd.mark_rate_limited("openrouter", "k2")
        assert cd.available_keys("openrouter", ["k1", "k2", "k3"]) == ["k1", "k3"]

    def test_ttl_expiry_via_injected_now(self, cd_path):
        cd = KeyCooldown(cd_path)
        # Mark "now" at t=0
        cd.mark_rate_limited("openrouter", "k1", now=1000.0)
        # Just inside TTL → still cooling
        assert cd.is_in_cooldown("openrouter", "k1", now=1000.0 + COOLDOWN_TTL_SECONDS - 1)
        # Just outside TTL → expired
        assert not cd.is_in_cooldown("openrouter", "k1", now=1000.0 + COOLDOWN_TTL_SECONDS + 1)

    def test_expiry_evicts_from_state(self, cd_path):
        cd = KeyCooldown(cd_path)
        cd.mark_rate_limited("openrouter", "k1", now=1000.0)
        # Trigger eviction by checking after expiry
        cd.is_in_cooldown("openrouter", "k1", now=1000.0 + COOLDOWN_TTL_SECONDS + 1)
        # State on disk should have removed the entry
        cd2 = KeyCooldown(cd_path)
        assert not cd2.is_in_cooldown("openrouter", "k1", now=1000.0 + COOLDOWN_TTL_SECONDS + 1)

    def test_persistence_across_instances(self, cd_path):
        cd = KeyCooldown(cd_path)
        cd.mark_rate_limited("openrouter", "k1")
        # New instance reads from disk
        cd2 = KeyCooldown(cd_path)
        assert cd2.is_in_cooldown("openrouter", "k1")

    def test_clear_specific_provider(self, cd_path):
        cd = KeyCooldown(cd_path)
        cd.mark_rate_limited("openrouter", "k1")
        cd.mark_rate_limited("nvidia_nim", "k2")
        cd.clear("openrouter")
        assert not cd.is_in_cooldown("openrouter", "k1")
        assert cd.is_in_cooldown("nvidia_nim", "k2")

    def test_clear_all(self, cd_path):
        cd = KeyCooldown(cd_path)
        cd.mark_rate_limited("openrouter", "k1")
        cd.mark_rate_limited("nvidia_nim", "k2")
        cd.clear()
        assert not cd.is_in_cooldown("openrouter", "k1")
        assert not cd.is_in_cooldown("nvidia_nim", "k2")

    def test_corrupted_state_file_ignored(self, cd_path):
        # Pre-populate a garbled file; constructor should decay to empty.
        cd_path.parent.mkdir(parents=True, exist_ok=True)
        cd_path.write_text("{not json")
        cd = KeyCooldown(cd_path)
        assert cd.available_keys("openrouter", ["k1", "k2"]) == ["k1", "k2"]

    def test_cooldown_remaining_returns_positive_seconds(self, cd_path):
        cd = KeyCooldown(cd_path)
        cd.mark_rate_limited("openrouter", "k1", now=1000.0)
        # 30 seconds in
        rem = cd.cooldown_remaining("openrouter", "k1", now=1030.0)
        assert rem is not None
        assert abs(rem - (COOLDOWN_TTL_SECONDS - 30)) < 0.5

    def test_cooldown_remaining_none_when_not_cooling(self, cd_path):
        cd = KeyCooldown(cd_path)
        assert cd.cooldown_remaining("openrouter", "never-marked") is None
