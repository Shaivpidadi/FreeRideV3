"""``freeride watch`` — tail live failover events.

Reads ``~/.freeride/events.jsonl`` (or ``$FREERIDE_EVENTS_PATH``) and
pretty-prints each transition. Useful for:

- Demoing failover during launch ("watch FreeRide pick a different
  provider when OpenRouter rate-limits")
- Debugging "is my agent actually using FreeRide?"
- Spotting which keys are cooling vs healthy

Output shape (one line per event):

    [HH:MM:SS.mmm] req_a3f8e2c1  → openrouter[k0] llama-3.1-8b
    [HH:MM:SS.mmm] req_a3f8e2c1  ← openrouter[k0] 412ms RATE_LIMIT (retry-after 47s)
    [HH:MM:SS.mmm] req_a3f8e2c1  → groq[k0] llama-3.1-8b
    [HH:MM:SS.mmm] req_a3f8e2c1  ← groq[k0] 318ms OK ✓

The tail loop is plain stdlib polling — no inotify dependency. ~50ms
poll interval is fine for human-eyeballed traffic; for higher-volume
inspection use ``tail -f`` + ``jq`` directly.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any


# ANSI color escapes. Disabled when stdout isn't a TTY OR --no-color is set.
_RESET = "\033[0m"
_DIM = "\033[2m"
_BOLD = "\033[1m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_BLUE = "\033[34m"
_GREY = "\033[90m"


def _events_path() -> Path:
    override = os.environ.get("FREERIDE_EVENTS_PATH", "").strip()
    return Path(override) if override else Path.home() / ".freeride" / "events.jsonl"


def _color_status(status: str, no_color: bool) -> str:
    if no_color:
        return status
    palette = {
        "OK": _GREEN,
        "RATE_LIMIT": _YELLOW,
        "AUTH": _RED,
        "MODEL_NOT_FOUND": _YELLOW,
        "QUOTA_EXHAUSTED": _YELLOW,
        "TIMEOUT": _YELLOW,
        "UNAVAILABLE": _YELLOW,
        "UNKNOWN": _RED,
    }
    return f"{palette.get(status, '')}{status}{_RESET}" if palette.get(status) else status


def _fmt_ts(ts: float) -> str:
    """HH:MM:SS.mmm — short enough to scan, precise enough to debug timing."""
    lt = time.localtime(ts)
    ms = int((ts - int(ts)) * 1000)
    return f"{lt.tm_hour:02d}:{lt.tm_min:02d}:{lt.tm_sec:02d}.{ms:03d}"


def _format_event(rec: dict[str, Any], no_color: bool) -> str | None:
    typ = rec.get("type")
    ts = _fmt_ts(rec.get("ts", time.time()))
    rid = rec.get("request_id", "?")[:14]
    grey = "" if no_color else _GREY
    bold = "" if no_color else _BOLD
    reset = "" if no_color else _RESET
    blue = "" if no_color else _BLUE

    prefix = f"{grey}[{ts}]{reset} {blue}{rid}{reset}"

    if typ == "request_start":
        model = rec.get("model", "?")
        stream = " stream" if rec.get("streaming") else ""
        return f"{prefix}  {bold}▶ request{reset} model={model}{stream}"

    if typ == "provider_attempt":
        prov = rec.get("provider", "?")
        ki = rec.get("key_index", 0)
        model = rec.get("model", "?")
        return f"{prefix}  → {prov}[k{ki}] {model}"

    if typ == "provider_response":
        prov = rec.get("provider", "?")
        ki = rec.get("key_index", 0)
        ms = rec.get("duration_ms", 0)
        status = rec.get("status", "?")
        col_status = _color_status(status, no_color)
        extra = ""
        if "retry_after_s" in rec:
            extra = f" {grey}(retry-after {rec['retry_after_s']}s){reset}"
        if rec.get("first_chunk"):
            extra += f" {grey}first-chunk{reset}"
        glyph = "✓" if status == "OK" else "✗"
        return f"{prefix}  ← {prov}[k{ki}] {ms}ms {col_status} {glyph}{extra}"

    if typ == "request_complete":
        prov = rec.get("provider", "?")
        green = "" if no_color else _GREEN
        return f"{prefix}  {green}■ complete{reset} via {prov}"

    if typ == "request_failed":
        red = "" if no_color else _RED
        phase = rec.get("phase", "?")
        tried = ",".join(rec.get("tried", []))
        return f"{prefix}  {red}✗ failed{reset} phase={phase} tried=[{tried}]"

    if typ == "request_mid_stream_error":
        prov = rec.get("provider", "?")
        red = "" if no_color else _RED
        return f"{prefix}  {red}~ mid-stream error{reset} via {prov}"

    # Unknown event type — fall back to compact JSON
    return f"{prefix}  {grey}{json.dumps(rec, separators=(',', ':'))}{reset}"


def _tail(path: Path, *, since_start: bool, no_color: bool) -> None:
    """Tail the JSONL file, polling for appends every 50ms."""
    print(
        f"watching {path} — Ctrl-C to stop",
        file=sys.stderr,
    )
    print(file=sys.stderr)

    # Wait for the file to exist if the gateway hasn't started yet.
    while not path.exists():
        try:
            time.sleep(0.5)
        except KeyboardInterrupt:
            return

    with path.open("r", encoding="utf-8") as fh:
        if not since_start:
            fh.seek(0, os.SEEK_END)

        while True:
            line = fh.readline()
            if not line:
                # Detect rotation: if file size shrank, reopen.
                try:
                    if path.stat().st_size < fh.tell():
                        fh.close()
                        # Brief beat to let the rotator land.
                        time.sleep(0.05)
                        return _tail(path, since_start=False, no_color=no_color)
                except FileNotFoundError:
                    time.sleep(0.5)
                    continue
                try:
                    time.sleep(0.05)
                except KeyboardInterrupt:
                    return
                continue
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                # Partial write — the next pass will pick up the full line.
                continue
            out = _format_event(rec, no_color)
            if out is not None:
                print(out)
                sys.stdout.flush()


def cmd_watch(args) -> int:
    no_color = bool(args.no_color) or not sys.stdout.isatty()
    path = _events_path()
    try:
        _tail(path, since_start=bool(args.since_start), no_color=no_color)
    except KeyboardInterrupt:
        print(file=sys.stderr)  # newline so the prompt isn't on the same line
        return 0
    return 0
