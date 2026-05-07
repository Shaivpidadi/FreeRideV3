"""Per-provider rolling health stats for failover ordering.

Lives in-process, no persistence. Each ``provider_response`` event in
``server/routes/{chat,embeddings}.py`` calls ``record()`` to add an
attempt to the rolling window. The chat route's ``_resolve_provider_chain``
sorts providers by ``score()`` so a flaky provider gets tried last.

Defaults are intentionally non-aggressive — a single failure shouldn't
bury a provider; a sustained failure pattern should. Tunable via env:

    FREERIDE_HEALTH_WINDOW    rolling-window size (default 50)
    FREERIDE_HEALTH_MIN_N     min attempts before health affects order
                              (default 5; below this every provider is
                              treated as fully healthy)
    FREERIDE_HEALTH_OFF       set to '1' to disable reordering entirely
"""

from __future__ import annotations

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
class _ProviderStats:
    name: str
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
        neutral high score so brand-new providers aren't penalized for
        having no data.
        """
        if self.n() < _min_n():
            return 100.0
        sr = self.success_rate()
        p50 = self.p50_latency_ms()
        # 100 * success_rate gives a 0-100 success component, then we
        # subtract a small latency penalty so among two equally
        # successful providers the faster one wins.
        return 100.0 * sr - (p50 / 100.0)


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
        self._stats: dict[str, _ProviderStats] = {}
        self._lock = threading.Lock()

    def record(self, provider: str, *, ok: bool, duration_ms: int) -> None:
        with self._lock:
            s = self._stats.get(provider)
            if s is None:
                s = _ProviderStats(name=provider)
                self._stats[provider] = s
            s.record(ok=ok, duration_ms=duration_ms)

    def score(self, provider: str) -> float:
        with self._lock:
            s = self._stats.get(provider)
            return s.score() if s else 100.0

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
