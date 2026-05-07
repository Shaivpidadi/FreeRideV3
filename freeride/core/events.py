"""Append-only event log for `freeride watch` and post-hoc debugging.

Events are written as JSONL lines to ``~/.freeride/events.jsonl`` while
``freeride serve`` is running. ``freeride watch`` tails the file and
pretty-prints transitions in real time. The file is opt-out (set
``FREERIDE_EVENTS=0``) and rotates aggressively so disk usage stays
bounded.

Privacy: events deliberately exclude prompts, completions, full model
ids, and key material. Only the provider name, key index, model id (the
catalog-level id, not user content), error kind, and timings.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


# 1 MiB cap with a single rotation slot. Worst-case ~2 MiB on disk.
_MAX_BYTES = 1 * 1024 * 1024
_BACKUP_SUFFIX = ".1"

_DEFAULT_PATH = Path.home() / ".freeride" / "events.jsonl"
_LOCK = threading.Lock()


def _enabled() -> bool:
    """Events default ON; opt out via ``FREERIDE_EVENTS=0``."""
    val = os.environ.get("FREERIDE_EVENTS", "").strip().lower()
    return val not in ("0", "false", "no", "off")


def _path() -> Path:
    """Allow tests to point at a tmp file via ``FREERIDE_EVENTS_PATH``."""
    override = os.environ.get("FREERIDE_EVENTS_PATH", "").strip()
    return Path(override) if override else _DEFAULT_PATH


def new_request_id() -> str:
    """Short opaque id correlating a multi-event request flow."""
    return f"req_{uuid.uuid4().hex[:8]}"


def emit(event_type: str, **fields: Any) -> None:
    """Append one event line. Best-effort; never raises."""
    if not _enabled():
        return
    record: dict[str, Any] = {
        "ts": round(time.time(), 3),
        "type": event_type,
    }
    record.update(fields)
    try:
        path = _path()
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            _maybe_rotate(path)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    except Exception:
        # Event-logging must never break a real request. Log once and move on.
        logger.debug("event emit failed; continuing", exc_info=True)


def _maybe_rotate(path: Path) -> None:
    """When the active file exceeds ``_MAX_BYTES``, move it to ``.1`` and
    start fresh. Single-backup rotation; we don't keep history.
    """
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return
    if size < _MAX_BYTES:
        return
    backup = path.with_name(path.name + _BACKUP_SUFFIX)
    try:
        if backup.exists():
            backup.unlink()
        path.rename(backup)
    except Exception:
        # If rotation fails, fall back to truncating in place so we don't
        # grow without bound.
        try:
            path.write_text("")
        except Exception:
            pass
