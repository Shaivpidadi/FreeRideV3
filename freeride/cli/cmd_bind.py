"""``freeride bind <agent>`` — dispatcher to per-agent binders."""

from __future__ import annotations

import sys


def cmd_bind(args) -> int:
    agent = args.agent
    gateway_url = args.gateway_url

    if agent == "openclaw":
        from freeride.binders import openclaw

        print(openclaw.bind(gateway_url))
        return 0
    if agent == "aider":
        from freeride.binders import aider

        scope = getattr(args, "scope", None) or "home"
        print(aider.bind(gateway_url, scope=scope))
        return 0
    if agent == "continue":
        from freeride.binders import continue_

        print(continue_.bind(gateway_url))
        return 0
    if agent == "hermes":
        from freeride.binders import hermes

        print(hermes.bind(gateway_url))
        return 0

    if agent == "opencode":
        from freeride.binders import opencode

        try:
            print(opencode.bind(gateway_url))
        except ValueError as e:
            print(f"freeride bind: {e}", file=sys.stderr)
            return 2
        return 0

    print(
        f"freeride bind: '{agent}' is not yet supported. "
        f"Recognized: openclaw, aider, continue, hermes, opencode.",
        file=sys.stderr,
    )
    return 2
