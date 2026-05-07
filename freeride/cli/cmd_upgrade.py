"""``freeride upgrade`` — bump the installed FreeRide to the latest PyPI release.

Detects how FreeRide was installed (uv tool / pipx / pip) and runs the
right upgrade command, then prints before/after version. Picks ``uv``
first since the curl|sh installer uses it.

Doesn't require ``freeride serve`` to be running — operates purely on
the local Python install.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from typing import Sequence

from freeride import __version__


# Each tuple: (binary name on PATH, command argv to upgrade).
# Order matters: prefer uv (what install.sh installs), fall back to pipx,
# then plain pip. The first one whose binary is present wins.
_UPGRADE_STRATEGIES: list[tuple[str, list[str]]] = [
    (
        "uv",
        ["uv", "tool", "install", "--upgrade", "--prerelease=allow", "freeride-gateway"],
    ),
    ("pipx", ["pipx", "upgrade", "--pip-args=--pre", "freeride-gateway"]),
    (
        sys.executable,  # Always present
        [sys.executable, "-m", "pip", "install", "--upgrade", "--pre", "freeride-gateway"],
    ),
]


def _pick_strategy() -> tuple[str, list[str]] | None:
    """First strategy whose binary is on PATH wins."""
    for binary, argv in _UPGRADE_STRATEGIES:
        if binary == sys.executable or shutil.which(binary):
            return binary, argv
    return None


def _query_installed_version() -> str | None:
    """Re-import freeride in a subprocess to read the post-upgrade version.

    Can't just check `__version__` from this process — that's frozen at
    the version that was already imported. Subprocess gets a fresh
    interpreter that imports the just-installed package.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-c", "import freeride; print(freeride.__version__)"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _run_upgrade(argv: Sequence[str]) -> int:
    """Run the upgrade subprocess, streaming its output to the user.

    We don't capture stdout/stderr so the user sees uv/pipx/pip's own
    progress output (download, install, etc.) — those tools already
    pretty-print, no need to re-format.
    """
    try:
        result = subprocess.run(list(argv), check=False)
    except FileNotFoundError as e:
        print(f"error: {e!s}", file=sys.stderr)
        return 1
    return result.returncode


def cmd_upgrade(args) -> int:
    before = __version__
    print(f"FreeRide upgrade — current version: {before}")

    pick = _pick_strategy()
    if pick is None:
        print(
            "error: couldn't find uv, pipx, or pip on PATH. "
            "Reinstall via the curl|sh installer:\n"
            "  curl -sSL https://api.free-ride.xyz/install.sh | sh",
            file=sys.stderr,
        )
        return 1

    binary, argv = pick
    if getattr(args, "dry_run", False):
        print(f"would run: {' '.join(argv)}")
        return 0

    print(f"using: {binary}")
    print(f"running: {' '.join(argv)}")
    print()

    rc = _run_upgrade(argv)
    if rc != 0:
        print(
            f"\nupgrade failed (exit {rc}). The output above should explain why; "
            "if you need a clean reinstall:\n"
            "  curl -sSL https://api.free-ride.xyz/install.sh | sh",
            file=sys.stderr,
        )
        return rc

    after = _query_installed_version()
    print()
    if after is None:
        print(
            f"upgrade ran. Couldn't auto-detect the new version — "
            f"run `freeride --version` from a fresh shell to confirm. "
            f"(was: {before})"
        )
    elif after == before:
        print(f"already at latest: {after}")
    else:
        print(f"upgraded: {before} → {after}")
        print("note: if you have `freeride serve` running, restart it to pick up the new version.")
    return 0
