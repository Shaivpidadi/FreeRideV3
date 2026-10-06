"""Tests for freeride.core.cooldown — TTL behavior and restart persistence."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from freeride.core.cooldown import COOLDOWN_TTL_SECONDS, KeyCooldown, hash_key
from freeride.core.errors import ErrorKind


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
        # Raw secret never lands on disk.
        body = cd_path.read_text()
        assert "k1" not in body
        assert hash_key("k1") in body

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

    def test_quota_ttl_is_an_hour(self, cd_path):
        cd = KeyCooldown(cd_path)
        cd.mark("openrouter", "k1", ErrorKind.QUOTA_EXHAUSTED, now=1000.0)
        assert cd.is_in_cooldown("openrouter", "k1", now=1000.0 + 3599)
        assert not cd.is_in_cooldown("openrouter", "k1", now=1000.0 + 3601)

    def test_auth_ttl_is_five_minutes(self, cd_path):
        cd = KeyCooldown(cd_path)
        cd.mark("openrouter", "k1", ErrorKind.AUTH, now=1000.0)
        assert cd.is_in_cooldown("openrouter", "k1", now=1000.0 + 299)
        assert not cd.is_in_cooldown("openrouter", "k1", now=1000.0 + 301)

    def test_rate_limit_honors_retry_after(self, cd_path):
        cd = KeyCooldown(cd_path)
        cd.mark("openrouter", "k1", ErrorKind.RATE_LIMIT, retry_after_s=15, now=1000.0)
        assert cd.is_in_cooldown("openrouter", "k1", now=1014)
        assert not cd.is_in_cooldown("openrouter", "k1", now=1016)

    def test_legacy_raw_key_file_is_migrated(self, cd_path):
        cd_path.parent.mkdir(parents=True, exist_ok=True)
        cd_path.write_text('{"openrouter": {"sk-or-v1-secret": 1000.0}}')
        cd = KeyCooldown(cd_path)
        assert cd.is_in_cooldown("openrouter", "sk-or-v1-secret", now=1100.0)
        persisted = cd_path.read_text()
        assert "sk-or-v1-secret" not in persisted
        assert hash_key("sk-or-v1-secret") in persisted

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


class TestConcurrentInstances:
    """Regression: every route used to build its own KeyCooldown per
    request and persist its own snapshot, so two concurrent requests that
    cooled different keys left only the later writer's mark on disk."""

    def test_two_instances_same_path_do_not_clobber(self, cd_path):
        a = KeyCooldown(cd_path)
        b = KeyCooldown(cd_path)
        a.mark_rate_limited("openrouter", "key-a")
        b.mark_rate_limited("openrouter", "key-b")
        fresh = KeyCooldown(cd_path)
        assert fresh.is_in_cooldown("openrouter", "key-a")
        assert fresh.is_in_cooldown("openrouter", "key-b")
        # The instance that wrote first also sees the other's mark.
        assert a.is_in_cooldown("openrouter", "key-b")

    def test_mark_keeps_other_writers_marks(self, cd_path):
        a = KeyCooldown(cd_path)
        b = KeyCooldown(cd_path)
        b.mark("openrouter", "k1", ErrorKind.QUOTA_EXHAUSTED, now=1000.0)
        a.mark("groq", "g1", ErrorKind.RATE_LIMIT, now=1000.0)
        # a's write must not shorten or drop b's hour-long quota mark.
        assert a.cooldown_remaining("openrouter", "k1", now=1000.0 + 3000) is not None

    def test_clear_is_not_undone_by_stale_instance_read(self, cd_path):
        a = KeyCooldown(cd_path)
        a.mark_rate_limited("openrouter", "k1")
        b = KeyCooldown(cd_path)
        b.clear()
        # a re-syncs from disk on read and sees the clear.
        assert not a.is_in_cooldown("openrouter", "k1")

    def test_shared_returns_one_instance_per_path(self, cd_path):
        assert KeyCooldown.shared(cd_path) is KeyCooldown.shared(cd_path)
        other = cd_path.with_name("other.json")
        assert KeyCooldown.shared(other) is not KeyCooldown.shared(cd_path)

    def test_threaded_marks_all_persist(self, cd_path):
        from threading import Thread

        cd = KeyCooldown.shared(cd_path)
        keys = [f"key-{i}" for i in range(16)]
        threads = [
            Thread(target=cd.mark_rate_limited, args=("openrouter", k)) for k in keys
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        fresh = KeyCooldown(cd_path)
        assert fresh.available_keys("openrouter", keys) == []
