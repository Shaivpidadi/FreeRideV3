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

Concurrency
-----------
Several writers touch the same file: every gateway request handler,
``/health``, and any CLI process (``freeride list``, the watcher,
``audit-models``). Each used to build its own instance and persist its
own in-memory snapshot, so two concurrent requests that cooled
*different* keys left only the later writer's mark on disk. Now the
gateway uses one :meth:`KeyCooldown.shared` instance per path; every
mutation re-reads the file under the lock before applying itself and
writing back; and reads reload when the file's (mtime, size) moved.
Because every mutation persists immediately, the file is always the
authority and memory is only a cache of it. ``clear`` skips the
re-read on purpose: it is a user action and must drop whatever is
there.
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

# Process-wide instances, one per path. See KeyCooldown.shared().
_SHARED: dict[Path, "KeyCooldown"] = {}
_SHARED_LOCK = Lock()


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


def _disk_signature(path: Path) -> tuple[int, int] | None:
    """Cheap change detector for the on-disk file: (mtime_ns, size), or
    None when the file doesn't exist. A stat per read is far cheaper
    than the full read-and-migrate every caller used to pay."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


class KeyCooldown:
    """Thread-safe, file-persisted cooldown tracker.

    Prefer :meth:`shared` — one instance per path per process. Direct
    construction stays supported for tests and one-off tooling, and is
    safe: every instance re-reads the file before each mutation instead
    of overwriting it with a private snapshot.

    Reads (``is_in_cooldown`` and friends) stat the file and reload
    only when it moved; ``mark`` always reloads, since a missed change
    there is exactly the clobber this design exists to prevent.
    """

    def __init__(self, path: Path | str = DEFAULT_COOLDOWN_PATH) -> None:
        self._path = Path(path)
        self._lock = Lock()
        # state[provider][key_hash] -> {"until": float, "kind": str}
        self._state: dict[str, dict[str, dict[str, Any]]] = {}
        self._disk_sig: tuple[int, int] | None = None
        migrated = self._load_from_disk()
        if migrated:
            self._persist()

    @classmethod
    def shared(cls, path: Path | str = DEFAULT_COOLDOWN_PATH) -> "KeyCooldown":
        """The process-wide instance for ``path`` (created on first use).

        Routes, ``/health``, auto-model resolution, and the CLI all go
        through this so a single in-memory view backs every request in
        the process; the per-mutation re-read covers other processes.
        """
        key = Path(path)
        with _SHARED_LOCK:
            inst = _SHARED.get(key)
            if inst is None:
                inst = cls(key)
                _SHARED[key] = inst
            return inst

    # ----- disk sync ------------------------------------------------------
    def _load_from_disk(self) -> bool:
        """Replace memory with the file's contents. Returns True when a
        legacy entry was migrated (caller should persist). Records the
        file signature so a later ``_sync_locked`` knows nothing moved.
        """
        self._disk_sig = _disk_signature(self._path)
        raw = read_json_or(self._path, {})
        self._state = {}
        return self._ingest(raw)

    def _ingest(self, raw: Any) -> bool:
        """Load on-disk JSON into ``self._state``. Returns True when the
        in-memory shape differs from disk and should be rewritten
        (legacy migration).
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

    def _sync_locked(self, *, force: bool = False) -> None:
        """Reload from disk if the file moved since we last read or wrote
        it, or unconditionally with ``force``. Caller holds ``self._lock``.

        Writers pass ``force``: the (mtime, size) check is cheap for the
        hot read path but can miss a same-size write on a filesystem
        with coarse mtime, and a missed change on a write path means
        overwriting another writer's mark — the original bug.
        """
        if force or _disk_signature(self._path) != self._disk_sig:
            self._load_from_disk()

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
        hashed = hash_key(key)
        with self._lock:
            self._sync_locked()
            entry = self._lookup(provider, key)
            if entry is None:
                return None
            until = float(entry["until"])
            remaining = until - current
            if remaining <= 0:
                self._sync_locked(force=True)
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
        """Cool ``key`` for ``kind``. Duration is :func:`ttl_for`.

        Re-reads the on-disk file first so marks written by another
        instance or process since our last sync survive this write.
        """
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
            self._sync_locked(force=True)
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

        Deliberately does NOT re-read from disk first: a clear is a user
        action and must drop whatever another writer put there.
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
        self._disk_sig = _disk_signature(self._path)


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
