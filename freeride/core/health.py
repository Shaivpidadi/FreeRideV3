"""Rolling health stats for provider AND key-level failover ordering.

Lives in-process, no persistence. Each ``provider_response`` event in
``server/routes/{chat,embeddings}.py`` calls ``record()`` to add an
attempt to the rolling window. The chat route uses two helpers:

- ``sort_by_health(providers)`` — orders the provider chain by per-provider
  score so a flaky provider gets tried last
- ``sort_keys_by_health(provider, keys)`` — orders the keys WITHIN a
  provider so a flaky key gets tried last (keys hashed for storage)

Defaults are intentionally non-aggressive — a single failure shouldn't
bury a provider OR a key; a sustained failure pattern should. Tunable
via env:

    FREERIDE_HEALTH_WINDOW    rolling-window size (default 50)
    FREERIDE_HEALTH_MIN_N     min attempts before health affects order
                              (default 5; below this every provider is
                              treated as fully healthy)
    FREERIDE_HEALTH_OFF       set to '1' to disable reordering entirely
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Iterable


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _disabled() -> bool:
    return os.environ.get("FREERIDE_HEALTH_OFF", "").strip() in ("1", "true", "yes", "on")


def _window_size() -> int:
    return _env_int("FREERIDE_HEALTH_WINDOW", 50)


def _min_n() -> int:
    return _env_int("FREERIDE_HEALTH_MIN_N", 5)


@dataclass
class _Attempt:
    ts: float
    ok: bool
    duration_ms: int


@dataclass
class _RollingStats:
    """Generic rolling-window stats container shared by provider-level
    and per-key tracking. Same scoring formula either way.
    """

    attempts: deque = field(default_factory=lambda: deque(maxlen=_window_size()))

    def record(self, ok: bool, duration_ms: int) -> None:
        self.attempts.append(_Attempt(ts=time.time(), ok=ok, duration_ms=duration_ms))

    def n(self) -> int:
        return len(self.attempts)

    def success_rate(self) -> float:
        if not self.attempts:
            return 1.0
        return sum(1 for a in self.attempts if a.ok) / len(self.attempts)

    def p50_latency_ms(self) -> int:
        durations = sorted(a.duration_ms for a in self.attempts if a.ok)
        if not durations:
            return 0
        return durations[len(durations) // 2]

    def score(self) -> float:
        """Higher is healthier. Range: roughly 0 (always-failing slow) to
        100+ (fast and reliable). Below the min-N threshold, return a
        neutral high score so brand-new entries aren't penalized for
        having no data.
        """
        if self.n() < _min_n():
            return 100.0
        sr = self.success_rate()
        p50 = self.p50_latency_ms()
        return 100.0 * sr - (p50 / 100.0)


# Backward-compat alias — older imports / docstrings reference
# _ProviderStats. The class is now the generic rolling-window container.
_ProviderStats = _RollingStats


def _hash_key(key: str) -> str:
    """Stable, non-reversible 12-char id for a secret key. We never
    store the raw key in health stats — only this hash. Truncated SHA256
    is overkill for collision avoidance with a few keys per provider.
    """
    return hashlib.sha256((key or "").encode("utf-8")).hexdigest()[:12]


class ProviderHealth:
    """Process-wide singleton for provider health stats. Thread-safe via
    a single coarse lock; all paths are O(1) so contention is minimal.
    """

    _instance: "ProviderHealth | None" = None
    _instance_lock = threading.Lock()

    @classmethod
    def instance(cls) -> "ProviderHealth":
        # Double-checked locking — first guard avoids the lock entirely
        # in the steady state.
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Clear the singleton (tests call this between cases)."""
        with cls._instance_lock:
            cls._instance = None

    def __init__(self) -> None:
        # Provider-level rolling stats.
        self._stats: dict[str, _RollingStats] = {}
        # Per-key rolling stats. Keys are (provider_name, key_hash) tuples
        # so we never store the raw secret. Updated alongside the
        # provider-level rollup whenever record() is given a `key` arg.
        self._key_stats: dict[tuple[str, str], _RollingStats] = {}
        self._lock = threading.Lock()

    def record(
        self,
        provider: str,
        *,
        ok: bool,
        duration_ms: int,
        key: str | None = None,
    ) -> None:
        """Record an attempt outcome.

        Always updates the provider-level rollup. When ``key`` is supplied,
        also updates the per-key rolling stats so ``sort_keys_by_health``
        can demote a single bad key without dragging the whole provider
        down.
        """
        with self._lock:
            s = self._stats.get(provider)
            if s is None:
                s = _RollingStats()
                self._stats[provider] = s
            s.record(ok=ok, duration_ms=duration_ms)

            if key is not None:
                kh = _hash_key(key)
                ks = self._key_stats.get((provider, kh))
                if ks is None:
                    ks = _RollingStats()
                    self._key_stats[(provider, kh)] = ks
                ks.record(ok=ok, duration_ms=duration_ms)

    def score(self, provider: str) -> float:
        with self._lock:
            s = self._stats.get(provider)
            return s.score() if s else 100.0

    def key_score(self, provider: str, key: str) -> float:
        with self._lock:
            ks = self._key_stats.get((provider, _hash_key(key)))
            return ks.score() if ks else 100.0

    def stats(self, provider: str) -> dict[str, float | int]:
        """Snapshot for diagnostics / freeride status output."""
        with self._lock:
            s = self._stats.get(provider)
            if s is None:
                return {"n": 0, "success_rate": 1.0, "p50_ms": 0, "score": 100.0}
            return {
                "n": s.n(),
                "success_rate": round(s.success_rate(), 3),
                "p50_ms": s.p50_latency_ms(),
                "score": round(s.score(), 2),
            }

    def key_stats(self, provider: str, key: str) -> dict[str, float | int]:
        """Per-key snapshot. Same shape as stats() but for one specific key."""
        with self._lock:
            ks = self._key_stats.get((provider, _hash_key(key)))
            if ks is None:
                return {"n": 0, "success_rate": 1.0, "p50_ms": 0, "score": 100.0}
            return {
                "n": ks.n(),
                "success_rate": round(ks.success_rate(), 3),
                "p50_ms": ks.p50_latency_ms(),
                "score": round(ks.score(), 2),
            }


def sort_by_health(providers: Iterable) -> list:
    """Stable-sort providers by health score, healthiest first.

    Stable means tied providers keep their original (registration) order
    — the steady state where health is unset for everything matches the
    pre-feature behavior exactly.
    """
    if _disabled():
        return list(providers)
    h = ProviderHealth.instance()
    return sorted(providers, key=lambda p: h.score(getattr(p, "name", "")), reverse=True)


def sort_keys_by_health(provider: str, keys: list[str]) -> list[str]:
    """Stable-sort a key list for one provider by per-key health score.

    Used by the chat / embeddings failover loop to demote a single
    flaky key without affecting the provider's overall ordering. Tied
    keys (or all-cold keys at startup) keep the input order — matches
    pre-feature behavior in the cold-start case.
    """
    if _disabled():
        return list(keys)
    h = ProviderHealth.instance()
    return sorted(keys, key=lambda k: h.key_score(provider, k), reverse=True)
