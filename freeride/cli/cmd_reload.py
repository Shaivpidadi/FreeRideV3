"""``freeride reload`` — refresh provider registry on a running gateway.

POSTs to ``/v1/_freeride/reload`` so a running ``freeride serve`` picks
up new env vars without a restart. Useful for the common case of
"oh I forgot to set GROQ_API_KEY before starting the gateway" — set
the env var in your shell, run ``freeride reload``, done.

Caveats:
- The gateway sees env vars from ITS process. If you set the var in a
  different shell, you need to either restart the gateway or send it
  the new env (e.g., via a wrapper that calls ``kill -HUP``). This
  command picks up vars set in the gateway's environment only.
"""

from __future__ import annotations

import sys

import httpx


def cmd_reload(args) -> int:
    base = args.url.rstrip("/").removesuffix("/v1")
    url = f"{base}/v1/_freeride/reload"
    try:
        r = httpx.post(url, timeout=5.0)
    except httpx.ConnectError:
        print(
            f"error: gateway not reachable at {args.url}.\n"
            "       start it with `freeride serve` first.",
            file=sys.stderr,
        )
        return 1
    if r.status_code != 200:
        print(f"error: gateway returned HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return 1

    body = r.json()
    if not body.get("ok"):
        msg = body.get("message", body.get("error", "reload failed"))
        print(f"error: {msg}", file=sys.stderr)
        return 1

    before = body.get("before", [])
    after = body.get("after", [])
    added = body.get("added", [])
    removed = body.get("removed", [])

    print(f"providers before: {', '.join(before) or '(none)'}")
    print(f"providers after:  {', '.join(after) or '(none)'}")
    if added:
        print(f"  + added:   {', '.join(added)}")
    if removed:
        print(f"  - removed: {', '.join(removed)}")
    if not added and not removed:
        print("  (no changes — set env vars in the gateway's process before reloading)")
    return 0
