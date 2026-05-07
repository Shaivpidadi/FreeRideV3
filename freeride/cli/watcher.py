"""freeride-watcher — background process that keeps OpenClaw routing healthy.

Direct port of v2 ``watcher.py`` into the v3 layout. Lives outside the
agent's inference loop so it can recover from a "every model in the
chain is 429ing" deadlock that the agent itself can't escape.

Phase 5+ may rename or repurpose; for now it preserves v2 behavior.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

from freeride import __version__
from freeride.core.state import read_json_or, write_json_atomic
from freeride.v2compat.commands import _test_model, rotate
from freeride.v2compat.models import get_api_keys
from freeride.v2compat.openclaw import (
    get_current_model,
    load_openclaw_config,
    stored_to_api_id,
)


STATE_FILE = Path.home() / ".openclaw" / ".freeride-watcher-state.json"
DEFAULT_INTERVAL_SECONDS = 60
MIN_INTERVAL_SECONDS = 15


def load_state() -> dict:
    return read_json_or(STATE_FILE, {"rotation_count": 0})


def save_state(state: dict) -> None:
    write_json_atomic(STATE_FILE, state, indent=2)


def _record_rotation(state: dict, reason: str) -> None:
    state["rotation_count"] = state.get("rotation_count", 0) + 1
    state["last_rotation_at"] = datetime.now().isoformat()
    state["last_rotation_reason"] = reason
    save_state(state)


def check_and_rotate(state: dict) -> bool:
    """One probe cycle. Returns True if config was rewritten."""
    config = load_openclaw_config()
    current = get_current_model(config)

    if not current:
        print(f"[{datetime.now().isoformat()}] No primary configured — bootstrapping.")
        changed, err = rotate(force=True)
        if changed:
            _record_rotation(state, "bootstrap")
        elif err:
            print(f"  Bootstrap failed: {err}")
        return changed

    current_base = stored_to_api_id(current)
    ok, err = _test_model(current_base)
    if ok:
        print(f"[{datetime.now().isoformat()}] {current_base} OK")
        return False

    print(f"[{datetime.now().isoformat()}] {current_base} failed ({err}) — rotating.")
    changed, rot_err = rotate(force=True)
    if changed:
        _record_rotation(state, err or "unknown")
    elif rot_err:
        print(f"  Rotation failed: {rot_err}")
    return changed


def run_daemon(interval: int) -> int:
    if not get_api_keys():
        print("Error: OPENROUTER_API_KEY not set")
        return 1

    interval = max(interval, MIN_INTERVAL_SECONDS)
    key_count = len(get_api_keys())
    print(
        f"FreeRide Watcher started ({key_count} API key{'s' if key_count != 1 else ''}, "
        f"interval {interval}s)"
    )
    print("Stop with Ctrl-C or SIGTERM.")
    print("-" * 50)

    running = True

    def stop(signum, frame):
        nonlocal running
        print("\nShutting down watcher...")
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    state = load_state()
    while running:
        try:
            check_and_rotate(state)
        except Exception as e:
            print(f"[{datetime.now().isoformat()}] Watcher error: {e}")
        for _ in range(interval):
            if not running:
                break
            time.sleep(1)
    print("Watcher stopped.")
    return 0


def run_once() -> int:
    if not get_api_keys():
        print("Error: OPENROUTER_API_KEY not set")
        return 1
    state = load_state()
    check_and_rotate(state)
    return 0


def show_status() -> int:
    state = load_state()
    print("FreeRide Watcher Status")
    print("=" * 40)
    print(f"State file: {STATE_FILE}")
    print(f"Total rotations: {state.get('rotation_count', 0)}")
    print(f"Last rotation: {state.get('last_rotation_at', 'never')}")
    print(f"Last reason: {state.get('last_rotation_reason', 'n/a')}")
    return 0


def clear_state() -> int:
    if STATE_FILE.exists():
        STATE_FILE.unlink()
        print(f"Cleared {STATE_FILE}")
    else:
        print("No state file to clear.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="freeride-watcher",
        description="Background process that keeps the OpenClaw model chain healthy.",
    )
    p.add_argument("--version", action="version", version=f"freeride-watcher {__version__}")
    p.add_argument(
        "--interval",
        "-i",
        type=int,
        default=DEFAULT_INTERVAL_SECONDS,
        help=f"Daemon check interval in seconds (default: {DEFAULT_INTERVAL_SECONDS}, min: {MIN_INTERVAL_SECONDS})",
    )
    p.add_argument("--once", action="store_true", help="Run a single check-and-rotate, then exit")
    p.add_argument("--status", "-s", action="store_true", help="Show watcher state and exit")
    p.add_argument("--clear", action="store_true", help="Delete the watcher state file")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.status:
        return show_status()
    if args.clear:
        return clear_state()
    if args.once:
        return run_once()
    return run_daemon(args.interval)


if __name__ == "__main__":
    sys.exit(main())
