"""Per-(provider, key) cooldown tracker — persisted across restarts.

Direct generalization of v2's in-process ``_RATE_LIMITED_KEYS`` dict.
v2 lost cooldown state on every CLI invocation, so a freshly-rate-
limited key would get hit again 200ms later by the next ``freeride list``.
v3 persists cooldowns to ``~/.freeride/cooldown.json`` so the CLI and
gateway agree on what's currently in penalty.

State shape::

    {
        "openrouter": {
            "sk-or-v1-...c9b": 1778125266.123,
            "sk-or-v1-...4a8": 1778125301.456
        },
        "nvidia_nim": {
            "nvapi-...": 1778125300.789
        }
    }

Values are POSIX timestamps when the cooldown started. Keys older than
:data:`COOLDOWN_TTL_SECONDS` are evicted on read.
"""

from __future__ import annotations

import time
from pathlib import Path
from threading import Lock

from freeride.core.state import read_json_or, write_json_atomic


COOLDOWN_TTL_SECONDS: float = 120.0
"""How long a key stays in cooldown after a rate-limit hit. Matches v2.
Conservative — most providers' rate windows are shorter, but a soft
over-cooldown is cheaper than burning quota probing a still-throttled key.
"""

DEFAULT_COOLDOWN_PATH: Path = Path.home() / ".freeride" / "cooldown.json"


class KeyCooldown:
    """Thread-safe, file-persisted cooldown tracker.

    Use one instance per process. Reads on every ``is_in_cooldown`` check
    are cheap (in-memory dict); writes happen only when ``mark_*`` is
    called. The on-disk file is rewritten atomically every mutation.
    """

    def __init__(self, path: Path | str = DEFAULT_COOLDOWN_PATH) -> None:
        self._path = Path(path)
        self._lock = Lock()
        # state[provider][key] -> timestamp the cooldown started
        raw = read_json_or(self._path, {})
        self._state: dict[str, dict[str, float]] = {}
        if isinstance(raw, dict):
            for prov, keys in raw.items():
                if not isinstance(prov, str) or not isinstance(keys, dict):
                    continue
                self._state[prov] = {
                    k: float(v) for k, v in keys.items() if isinstance(k, str)
                }

    # ----- introspection --------------------------------------------------
    def is_in_cooldown(self, provider: str, key: str, *, now: float | None = None) -> bool:
        ts = self._state.get(provider, {}).get(key)
        if ts is None:
            return False
        current = time.time() if now is None else now
        if current - ts > COOLDOWN_TTL_SECONDS:
            # Expired — evict and persist.
            with self._lock:
                self._state.get(provider, {}).pop(key, None)
                self._persist()
            return False
        return True

    def available_keys(self, provider: str, all_keys: list[str]) -> list[str]:
        """Return the subset of ``all_keys`` that aren't currently in cooldown."""
        return [k for k in all_keys if not self.is_in_cooldown(provider, k)]

    def cooldown_remaining(self, provider: str, key: str, *, now: float | None = None) -> float | None:
        """Seconds left in cooldown, or None if not cooling. Useful for
        ``freeride status`` to show 'key X back in 47s'.
        """
        ts = self._state.get(provider, {}).get(key)
        if ts is None:
            return None
        current = time.time() if now is None else now
        remaining = COOLDOWN_TTL_SECONDS - (current - ts)
        return max(0.0, remaining) if remaining > 0 else None

    # ----- mutation -------------------------------------------------------
    def mark_rate_limited(self, provider: str, key: str, *, now: float | None = None) -> None:
        """Record that ``key`` for ``provider`` just returned 429/auth-failure
        and should not be picked again until ``COOLDOWN_TTL_SECONDS`` pass.
        """
        with self._lock:
            self._state.setdefault(provider, {})[key] = time.time() if now is None else now
            self._persist()

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
