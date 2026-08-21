"""Per-(provider, key) cooldown tracker — persisted across restarts.

Direct generalization of v2's in-process ``_RATE_LIMITED_KEYS`` dict.
v2 lost cooldown state on every CLI invocation, so a freshly-rate-
limited key would get hit again 200ms later by the next ``freeride list``.
v3 persists cooldowns to ``~/.freeride/cooldown.json`` so the CLI and
gateway agree on what's currently in penalty.

On-disk keys are SHA-256 prefixes, never the raw secret. Values carry
an absolute ``until`` timestamp so TTL can vary by :class:`ErrorKind`:

.. code-block:: json

    {
      "openrouter": {
        "a1b2c3d4e5f6": {"until": 1778125326.1, "kind": "rate_limit"}
      }
    }

Legacy files that stored ``{raw_key: start_timestamp}`` are migrated
on first read: the key is hashed and the start is converted to
``until = start + 120`` (the historical flat TTL).
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from threading import Lock
from typing import Any

from freeride.core.errors import ErrorKind
from freeride.core.state import read_json_or, write_json_atomic

RATE_LIMIT_TTL_SECONDS: float = 60.0
"""Default cool-off after a 429 when the provider didn't send Retry-After."""

AUTH_TTL_SECONDS: float = 300.0
"""Invalid-key cool-off. Long enough to stop hammering a typo'd key,
short enough that a rotated key comes back within the same session."""

QUOTA_TTL_SECONDS: float = 3600.0
"""Daily-budget cool-off. Most free tiers reset on an hourly-to-daily
cadence; an hour is the cheapest wrong-direction error."""

LEGACY_TTL_SECONDS: float = 120.0
"""TTL assumed when migrating pre-hash cooldown.json files that stored
a start timestamp rather than an absolute ``until``."""

# Back-compat alias — older tests / callers imported this name. It now
# means "the rate-limit default", which is the common case.
COOLDOWN_TTL_SECONDS: float = RATE_LIMIT_TTL_SECONDS

DEFAULT_COOLDOWN_PATH: Path = Path.home() / ".freeride" / "cooldown.json"

_HASH_LEN = 12


def hash_key(key: str) -> str:
    """Stable, non-reversible id for a secret. Same 12-char SHA-256
    prefix :mod:`freeride.core.health` uses, so a key identity is
    comparable across the two stores without either holding the raw
    value.
    """
    return hashlib.sha256((key or "").encode("utf-8")).hexdigest()[:_HASH_LEN]


def ttl_for(kind: ErrorKind | str, retry_after_s: int | float | None = None) -> float:
    """Seconds to cool a key for ``kind``.

    ``RATE_LIMIT`` honors ``retry_after_s`` when the provider sent a
    usable hint, clamped to ``[1, 3600]`` so a bogus ``Retry-After:
    999999`` can't park a key for weeks.
    """
    if isinstance(kind, str):
        try:
            kind = ErrorKind(kind)
        except ValueError:
            kind = ErrorKind.UNKNOWN
    if kind is ErrorKind.AUTH:
        return AUTH_TTL_SECONDS
    if kind is ErrorKind.QUOTA_EXHAUSTED:
        return QUOTA_TTL_SECONDS
    if kind is ErrorKind.RATE_LIMIT:
        if retry_after_s is not None:
            try:
                hinted = float(retry_after_s)
            except (TypeError, ValueError):
                hinted = RATE_LIMIT_TTL_SECONDS
            return max(1.0, min(hinted, 3600.0))
        return RATE_LIMIT_TTL_SECONDS
    # UNAVAILABLE / TIMEOUT / UNKNOWN — callers typically don't cool
    # these, but if they do, a short window is enough.
    return RATE_LIMIT_TTL_SECONDS


def _looks_like_hash(key: str) -> bool:
    return len(key) == _HASH_LEN and all(c in "0123456789abcdef" for c in key)


class KeyCooldown:
    """Thread-safe, file-persisted cooldown tracker.

    Use one instance per process. Reads on every ``is_in_cooldown`` check
    are cheap (in-memory dict); writes happen only when ``mark`` is
    called. The on-disk file is rewritten atomically every mutation.
    """

    def __init__(self, path: Path | str = DEFAULT_COOLDOWN_PATH) -> None:
        self._path = Path(path)
        self._lock = Lock()
        # state[provider][key_hash] -> {"until": float, "kind": str}
        self._state: dict[str, dict[str, dict[str, Any]]] = {}
        raw = read_json_or(self._path, {})
        migrated = self._ingest(raw)
        if migrated:
            self._persist()

    def _ingest(self, raw: Any) -> bool:
        """Load on-disk JSON. Returns True when the in-memory shape
        differs from disk and should be rewritten (legacy migration).
        """
        migrated = False
        if not isinstance(raw, dict):
            return False
        for prov, keys in raw.items():
            if not isinstance(prov, str) or not isinstance(keys, dict):
                continue
            dest: dict[str, dict[str, Any]] = {}
            for k, v in keys.items():
                if not isinstance(k, str):
                    continue
                entry, changed = _normalize_entry(k, v)
                if entry is None:
                    continue
                stored_id, payload = entry
                dest[stored_id] = payload
                migrated = migrated or changed
            self._state[prov] = dest
        return migrated

    # ----- introspection --------------------------------------------------
    def is_in_cooldown(self, provider: str, key: str, *, now: float | None = None) -> bool:
        remaining = self.cooldown_remaining(provider, key, now=now)
        return remaining is not None and remaining > 0

    def available_keys(self, provider: str, all_keys: list[str]) -> list[str]:
        """Return the subset of ``all_keys`` that aren't currently in cooldown."""
        return [k for k in all_keys if not self.is_in_cooldown(provider, k)]

    def cooldown_remaining(self, provider: str, key: str, *, now: float | None = None) -> float | None:
        """Seconds left in cooldown, or None if not cooling."""
        current = time.time() if now is None else now
        entry = self._lookup(provider, key)
        if entry is None:
            return None
        until = float(entry["until"])
        remaining = until - current
        if remaining <= 0:
            with self._lock:
                hashed = hash_key(key)
                bucket = self._state.get(provider, {})
                bucket.pop(hashed, None)
                # Also drop a leftover raw-key entry from a mid-migration file.
                bucket.pop(key, None)
                self._persist()
            return None
        return remaining

    def _lookup(self, provider: str, key: str) -> dict[str, Any] | None:
        bucket = self._state.get(provider, {})
        hashed = hash_key(key)
        entry = bucket.get(hashed)
        if entry is not None:
            return entry
        # Legacy file that still has the raw secret as the JSON key.
        return bucket.get(key)

    # ----- mutation -------------------------------------------------------
    def mark(
        self,
        provider: str,
        key: str,
        kind: ErrorKind | str = ErrorKind.RATE_LIMIT,
        *,
        retry_after_s: int | float | None = None,
        now: float | None = None,
    ) -> None:
        """Cool ``key`` for ``kind``. Duration is :func:`ttl_for`."""
        current = time.time() if now is None else now
        if not isinstance(kind, ErrorKind):
            try:
                kind_enum = ErrorKind(kind)
            except ValueError:
                kind_enum = ErrorKind.UNKNOWN
        else:
            kind_enum = kind
        until = current + ttl_for(kind_enum, retry_after_s)
        hashed = hash_key(key)
        with self._lock:
            bucket = self._state.setdefault(provider, {})
            bucket.pop(key, None)  # drop leftover raw-key entry
            bucket[hashed] = {"until": until, "kind": kind_enum.value}
            self._persist()

    def mark_rate_limited(self, provider: str, key: str, *, now: float | None = None) -> None:
        """Back-compat wrapper: cool as ``RATE_LIMIT`` with the default TTL."""
        self.mark(provider, key, ErrorKind.RATE_LIMIT, now=now)

    def clear(self, provider: str | None = None) -> None:
        """Drop all cooldowns. ``provider=None`` drops everything; otherwise
        only that provider's keys. Mostly for tests and ``freeride status --reset``.
        """
        with self._lock:
            if provider is None:
                self._state.clear()
            else:
                self._state.pop(provider, None)
            self._persist()

    # ----- internal -------------------------------------------------------
    def _persist(self) -> None:
        write_json_atomic(self._path, self._state)


def _normalize_entry(k: str, v: Any) -> tuple[tuple[str, dict[str, Any]] | None, bool]:
    """Return ``((stored_id, payload), migrated)``.

    New format: hashed key → ``{until, kind}``.
    Legacy format: raw key → start timestamp. Converted to
    ``until = start + LEGACY_TTL_SECONDS``.
    """
    if isinstance(v, dict) and "until" in v:
        try:
            until = float(v["until"])
        except (TypeError, ValueError):
            return None, False
        kind = v.get("kind", ErrorKind.RATE_LIMIT.value)
        payload = {"until": until, "kind": str(kind)}
        if _looks_like_hash(k):
            return (k, payload), False
        return (hash_key(k), payload), True
    if isinstance(v, (int, float)):
        payload = {
            "until": float(v) + LEGACY_TTL_SECONDS,
            "kind": ErrorKind.RATE_LIMIT.value,
        }
        return (hash_key(k), payload), True
    return None, False
