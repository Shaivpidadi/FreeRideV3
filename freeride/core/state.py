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
import secrets
import time
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
    # Unique temp name per writer. A fixed ``<path>.tmp`` let two
    # concurrent writers (gateway + CLI, or two request handlers in one
    # process) truncate each other's temp file, so the loser's
    # ``os.replace`` either raised or published the other's bytes.
    tmp = p.with_name(f".{p.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    # Create with the final mode from the start (O_EXCL: ours alone) so
    # a secret never sits world-readable between write and chmod.
    create_mode = mode if mode is not None else 0o666
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, create_mode)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        if mode is not None:
            try:
                os.chmod(tmp, mode)
            except (OSError, NotImplementedError):
                # Windows or non-POSIX FS — chmod best-effort; the rename
                # below is what actually matters for atomicity.
                pass
        _replace_with_retry(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _replace_with_retry(tmp: Path, dest: Path, *, attempts: int = 20) -> None:
    """``os.replace`` that tolerates Windows' transient ``PermissionError``.

    On POSIX a rename over an existing path is atomic and never blocked
    by other readers or writers. On Windows ``MoveFileEx`` fails with
    ``Access is denied`` while another process or thread has the
    destination open, including the instant another writer is replacing
    it. Concurrent writers (gateway + CLI, two request handlers) hit
    exactly that. Retry with a short backoff; anything that persists
    past ~1s is a real permission problem and is re-raised.
    """
    delay = 0.001
    for attempt in range(attempts):
        try:
            os.replace(tmp, dest)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.1)


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
