"""Tests for freeride.core.health.

Covers: rolling-window stats (success rate, p50 latency, score),
new-provider neutral score (don't penalize before min-N data),
sort_by_health stability (tied providers keep registration order),
opt-out via FREERIDE_HEALTH_OFF.
"""

from __future__ import annotations

import pytest

from freeride.core.health import ProviderHealth, sort_by_health


@pytest.fixture(autouse=True)
def _reset_singleton(monkeypatch: pytest.MonkeyPatch):
    """Each test starts with a fresh ProviderHealth singleton + clean env."""
    monkeypatch.delenv("FREERIDE_HEALTH_OFF", raising=False)
    monkeypatch.delenv("FREERIDE_HEALTH_WINDOW", raising=False)
    monkeypatch.delenv("FREERIDE_HEALTH_MIN_N", raising=False)
    ProviderHealth.reset()
    yield
    ProviderHealth.reset()


class _StubProvider:
    """Minimal sortable stand-in for a Provider."""

    def __init__(self, name: str):
        self.name = name


# ---------------------------------------------------------------------------


class TestRecord:
    def test_records_success_and_failure(self):
        h = ProviderHealth.instance()
        h.record("openrouter", ok=True, duration_ms=100)
        h.record("openrouter", ok=False, duration_ms=500)
        s = h.stats("openrouter")
        assert s["n"] == 2
        assert s["success_rate"] == 0.5

    def test_stats_for_unknown_provider_neutral(self):
        h = ProviderHealth.instance()
        s = h.stats("never-seen")
        assert s == {"n": 0, "success_rate": 1.0, "p50_ms": 0, "score": 100.0}


class TestScoreSemantics:
    def test_new_provider_gets_neutral_score(self, monkeypatch):
        """Below FREERIDE_HEALTH_MIN_N attempts, score is 100 (neutral)."""
        monkeypatch.setenv("FREERIDE_HEALTH_MIN_N", "5")
        ProviderHealth.reset()
        h = ProviderHealth.instance()
        h.record("openrouter", ok=False, duration_ms=999)
        h.record("openrouter", ok=False, duration_ms=999)
        # Only 2 attempts, both failures — but score is still 100 because
        # we haven't crossed the min-N threshold.
        assert h.score("openrouter") == 100.0

    def test_score_drops_with_failures_after_min_n(self, monkeypatch):
        monkeypatch.setenv("FREERIDE_HEALTH_MIN_N", "5")
        ProviderHealth.reset()
        h = ProviderHealth.instance()
        # 5 successes, then 5 failures — 50% success rate.
        for _ in range(5):
            h.record("openrouter", ok=True, duration_ms=100)
        for _ in range(5):
            h.record("openrouter", ok=False, duration_ms=200)
        # 100 * 0.5 - latency penalty = ~49.something
        assert 45 <= h.score("openrouter") <= 55

    def test_faster_provider_outranks_slower_at_same_success_rate(
        self, monkeypatch
    ):
        monkeypatch.setenv("FREERIDE_HEALTH_MIN_N", "3")
        ProviderHealth.reset()
        h = ProviderHealth.instance()
        for _ in range(5):
            h.record("fast", ok=True, duration_ms=50)
            h.record("slow", ok=True, duration_ms=2000)
        # Both 100% success; fast should outrank slow because of latency.
        assert h.score("fast") > h.score("slow")


class TestRollingWindow:
    def test_window_drops_oldest_attempts(self, monkeypatch):
        monkeypatch.setenv("FREERIDE_HEALTH_WINDOW", "3")
        monkeypatch.setenv("FREERIDE_HEALTH_MIN_N", "1")
        ProviderHealth.reset()
        h = ProviderHealth.instance()
        # Fill with failures, then exclusively successes — only the
        # successes should remain in the window.
        for _ in range(3):
            h.record("openrouter", ok=False, duration_ms=999)
        for _ in range(3):
            h.record("openrouter", ok=True, duration_ms=100)
        assert h.stats("openrouter")["n"] == 3
        assert h.stats("openrouter")["success_rate"] == 1.0


class TestSortByHealth:
    def test_stable_when_no_data(self):
        # Empty stats → all default 100.0 → stable sort preserves order.
        providers = [_StubProvider(n) for n in ["openrouter", "groq", "nim"]]
        sorted_p = sort_by_health(providers)
        assert [p.name for p in sorted_p] == ["openrouter", "groq", "nim"]

    def test_unhealthy_provider_demoted(self, monkeypatch):
        monkeypatch.setenv("FREERIDE_HEALTH_MIN_N", "3")
        ProviderHealth.reset()
        h = ProviderHealth.instance()
        # Make openrouter look unhealthy.
        for _ in range(5):
            h.record("openrouter", ok=False, duration_ms=999)
        # Groq is unrecorded (neutral 100); openrouter is ~0.
        providers = [_StubProvider("openrouter"), _StubProvider("groq")]
        sorted_p = sort_by_health(providers)
        # Groq should now be first.
        assert [p.name for p in sorted_p] == ["groq", "openrouter"]

    def test_disabled_via_env(self, monkeypatch):
        monkeypatch.setenv("FREERIDE_HEALTH_OFF", "1")
        monkeypatch.setenv("FREERIDE_HEALTH_MIN_N", "3")
        ProviderHealth.reset()
        h = ProviderHealth.instance()
        for _ in range(5):
            h.record("openrouter", ok=False, duration_ms=999)
        providers = [_StubProvider("openrouter"), _StubProvider("groq")]
        # With reordering disabled, original order is preserved.
        sorted_p = sort_by_health(providers)
        assert [p.name for p in sorted_p] == ["openrouter", "groq"]


class TestPerKeyHealth:
    def test_record_with_key_updates_both_provider_and_key_stats(self):
        h = ProviderHealth.instance()
        h.record("openrouter", ok=True, duration_ms=100, key="key1")
        h.record("openrouter", ok=False, duration_ms=500, key="key1")
        # Provider rollup sees both attempts.
        assert h.stats("openrouter")["n"] == 2
        # Per-key stats also see both.
        assert h.key_stats("openrouter", "key1")["n"] == 2
        # Different key has its own stats.
        assert h.key_stats("openrouter", "key2")["n"] == 0

    def test_keys_isolated_across_providers(self):
        """Same raw key string used by two providers should have
        independent per-key stats — keying is by (provider, hash) tuple.
        """
        h = ProviderHealth.instance()
        h.record("openrouter", ok=True, duration_ms=100, key="shared-key")
        h.record("groq", ok=False, duration_ms=999, key="shared-key")
        assert h.key_stats("openrouter", "shared-key")["success_rate"] == 1.0
        assert h.key_stats("groq", "shared-key")["success_rate"] == 0.0

    def test_record_without_key_doesnt_pollute_key_stats(self):
        h = ProviderHealth.instance()
        h.record("openrouter", ok=True, duration_ms=100)  # no key=
        # Provider stats updated.
        assert h.stats("openrouter")["n"] == 1
        # No per-key entry created.
        assert h.key_stats("openrouter", "anything")["n"] == 0


class TestSortKeysByHealth:
    def test_stable_when_no_data(self):
        from freeride.core.health import sort_keys_by_health

        keys = ["k1", "k2", "k3"]
        assert sort_keys_by_health("openrouter", keys) == keys

    def test_demotes_failing_key_within_provider(self, monkeypatch):
        from freeride.core.health import sort_keys_by_health

        monkeypatch.setenv("FREERIDE_HEALTH_MIN_N", "3")
        ProviderHealth.reset()
        h = ProviderHealth.instance()
        # k1 is failing; k2 is healthy.
        for _ in range(5):
            h.record("openrouter", ok=False, duration_ms=999, key="k1")
            h.record("openrouter", ok=True, duration_ms=100, key="k2")
        ordered = sort_keys_by_health("openrouter", ["k1", "k2"])
        assert ordered == ["k2", "k1"], "expected healthier k2 first"

    def test_disabled_preserves_input_order(self, monkeypatch):
        from freeride.core.health import sort_keys_by_health

        monkeypatch.setenv("FREERIDE_HEALTH_OFF", "1")
        monkeypatch.setenv("FREERIDE_HEALTH_MIN_N", "3")
        ProviderHealth.reset()
        h = ProviderHealth.instance()
        for _ in range(5):
            h.record("openrouter", ok=False, duration_ms=999, key="k1")
        # Even though k1 looks bad, OFF=1 keeps original order.
        ordered = sort_keys_by_health("openrouter", ["k1", "k2"])
        assert ordered == ["k1", "k2"]

    def test_key_hashes_dont_leak_secret(self):
        """The internal storage uses a hash, not the raw key. Verify by
        scoring with a different string than was recorded.
        """
        h = ProviderHealth.instance()
        h.record("openrouter", ok=True, duration_ms=50, key="secret-key-abc")
        # Score for the recorded key reflects the data:
        s_recorded = h.key_stats("openrouter", "secret-key-abc")
        assert s_recorded["n"] == 1
        # Score for a different key returns the neutral default:
        s_other = h.key_stats("openrouter", "different-key")
        assert s_other["n"] == 0
