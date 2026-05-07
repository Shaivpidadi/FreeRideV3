"""TTL cache primitive used by the gateway's models endpoint."""

from __future__ import annotations

import time
from threading import Lock
from typing import Generic, TypeVar


T = TypeVar("T")


class TTLCache(Generic[T]):
    """Tiny in-memory TTL cache. Single value per key, thread-safe.

    Used by ``/v1/models`` to avoid hitting providers' catalog endpoint on
    every client query — most callers (Aider, Continue) ask once per
    session. 6h default matches v2's behavior.
    """

    def __init__(self, ttl_seconds: float = 6 * 3600) -> None:
        self._ttl = ttl_seconds
        self._store: dict[str, tuple[float, T]] = {}
        self._lock = Lock()

    def get(self, key: str) -> T | None:
        with self._lock:
            item = self._store.get(key)
            if item is None:
                return None
            ts, value = item
            if time.time() - ts > self._ttl:
                self._store.pop(key, None)
                return None
            return value

    def set(self, key: str, value: T) -> None:
        with self._lock:
            self._store[key] = (time.time(), value)

    def invalidate(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._store.clear()
            else:
                self._store.pop(key, None)
