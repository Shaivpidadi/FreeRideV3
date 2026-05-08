"""Tiny pure-Python dotenv loader for ``~/.freeride/.env``.

Why not python-dotenv? Adding a dependency for a 30-line parser doesn't
pay for itself. We only support the small dotenv subset FreeRide writes:

  KEY=value
  KEY=value with spaces (no quotes needed)
  # comments
  blank lines

No interpolation, no multi-line values, no ``export`` prefixes. The file
is owned by FreeRide; users hand-edit it but the canonical writer is
`freeride init` which produces this exact subset.

Load policy:
  - Existing ``os.environ`` values WIN. We never overwrite a variable
    that's already set in the process env. This means OS-level / shell
    exports are stronger than `.env` values, which matches what users
    expect (an explicit `export FOO=...` overrides a stale `.env`).
  - Missing or unreadable file is silently ignored. Tests exercise this.

Used by:
  - ``freeride/cli/cmd_serve.py`` at startup, so `freeride init &&
    freeride serve` works without a manual `source`.
  - ``freeride/cli/cmd_serve.build_provider_registry()`` on every call,
    so `freeride reload` after editing `.env` picks up new keys.
  - ``freeride/cli/cmd_doctor.py`` so the diagnostic agrees with the
    gateway's view of the world.
"""

from __future__ import annotations

import os
from pathlib import Path


DEFAULT_DOTENV_PATH = Path.home() / ".freeride" / ".env"


def parse_dotenv(text: str) -> dict[str, str]:
    """Return KEY → value mapping from dotenv-shaped ``text``.

    Skips blank lines and ``#``-prefixed comments. Splits on the FIRST
    ``=`` so values containing ``=`` (e.g., a JWT) round-trip cleanly.
    Strips matching outer quotes — both single and double — but doesn't
    process escapes. Anything malformed (no ``=``) is skipped silently.
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if "=" not in s:
            continue
        k, _, v = s.partition("=")
        k = k.strip()
        v = v.strip()
        # Strip a single layer of matching outer quotes.
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        if k:
            out[k] = v
    return out


def load_dotenv_into_environ(path: Path | None = None) -> dict[str, str]:
    """Read ``path`` (default ``~/.freeride/.env``) and merge into ``os.environ``.

    OS-level env values WIN — we never overwrite an existing variable.
    Returns the dict of values that WERE actually set (i.e., were
    missing from ``os.environ`` before this call). Empty dict means
    either the file didn't exist or every key was already in env.

    Best-effort: any IO/parse error returns ``{}`` silently rather than
    raising, since the gateway should boot even with a malformed `.env`.
    """
    p = path if path is not None else DEFAULT_DOTENV_PATH
    try:
        text = p.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return {}
    parsed = parse_dotenv(text)
    actually_set: dict[str, str] = {}
    for k, v in parsed.items():
        if k in os.environ:
            continue
        os.environ[k] = v
        actually_set[k] = v
    return actually_set
