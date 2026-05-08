"""Crash-safe local state primitives.

Anything FreeRide writes to disk goes through :func:`atomic_write` so a
crash mid-write can never corrupt state. Lifted and generalized from
v2's ``watcher._atomic_write``; the v2 ``save_openclaw_config`` was a
latent bug — non-atomic ``Path.write_text`` could leave a half-written
JSON file. the design plan carry-forward principle 5.

We also expose :func:`read_json_or` and :func:`write_json_atomic` because
nearly every state file in FreeRide is small JSON.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def atomic_write(path: Path | str, content: str, *, mode: int | None = 0o600) -> None:
    """Write ``content`` to ``path`` atomically via temp + ``os.replace``.

    The replace is POSIX-atomic on the same filesystem, so a reader that
    opens ``path`` either sees the old version or the new — never a
    partial write. Creates parent directories as needed.

    File mode defaults to ``0o600`` — owner read/write only. Files we
    write under ``~/.freeride/`` (cooldown.json, config.json, .env from
    `freeride init`, etc.) frequently contain provider API keys; world-
    or group-readable would leak them on multi-user systems. Pass
    ``mode=None`` to skip the chmod (useful for tests on Windows where
    POSIX modes don't apply meaningfully).
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(content)
    if mode is not None:
        try:
            os.chmod(tmp, mode)
        except (OSError, NotImplementedError):
            # Windows or non-POSIX FS — chmod best-effort; the rename
            # below is what actually matters for atomicity.
            pass
    os.replace(tmp, p)


def write_json_atomic(path: Path | str, obj: Any, *, indent: int | None = 2) -> None:
    """Serialize ``obj`` and atomic-write it to ``path``."""
    atomic_write(path, json.dumps(obj, indent=indent))


def read_json_or(path: Path | str, default: Any) -> Any:
    """Read JSON from ``path``; return ``default`` on missing-or-corrupted.

    Used for state files where "no file" and "garbled file" should both
    decay to a sane default rather than raise. Callers that want hard
    failures should read the file themselves.
    """
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return default
