"""``freeride serve`` — start the FastAPI gateway under uvicorn.

Refuses to start if the requested port is already in use. v3.0 doesn't
auto-pick a different port because client config (Aider's
``OPENAI_API_BASE``, Continue's ``apiBase``, etc.) is hard-coded to a
specific value; silently switching ports would break the bind.

For the gateway to actually serve free models, we instantiate
:class:`OpenRouterProvider` (and later NIM) and pass to
:func:`~freeride.server.app.create_app`. NIM is registered only when
``NVIDIA_API_KEY`` is in the environment so casual users with only an
OpenRouter key don't see startup spam.
"""

from __future__ import annotations

import os
import socket
import sys

import uvicorn

from freeride.providers.nvidia_nim import NVIDIANIMProvider
from freeride.providers.openrouter import OpenRouterProvider
from freeride.server.app import create_app


def _port_in_use(host: str, port: int) -> bool:
    """True if something is already bound at ``host:port``. Used so we can
    refuse to start with a clear error rather than silently failing in uvicorn.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        try:
            sock.bind((host, port))
        except OSError:
            return True
        return False


def _build_provider_registry() -> list:
    """Pick up provider plugins based on what env vars are set.

    OpenRouter is always-on (it's our primary). NIM is added if
    NVIDIA_API_KEY is present — the actual class lands in Phase 3, so
    for now this is just OpenRouter.
    """
    providers: list = [OpenRouterProvider()]
    if os.environ.get("NVIDIA_API_KEY"):
        providers.append(NVIDIANIMProvider())
    return providers


def cmd_serve(args) -> int:
    host = args.host
    port = args.port
    if _port_in_use(host, port):
        print(
            f"Error: {host}:{port} is already in use.\n"
            f"  Pick a different --port, or stop the process bound there.\n"
            f"  freeride does not auto-pick a port — agent configs are hard-coded.",
            file=sys.stderr,
        )
        return 1

    providers = _build_provider_registry()
    app = create_app(providers=providers, verbose=args.verbose)

    print(f"freeride gateway listening on http://{host}:{port}")
    print(f"  providers: {', '.join(p.name for p in providers)}")
    print(f"  verbose: {args.verbose}")
    print("  point any OpenAI-compatible agent at:")
    print(f"    OPENAI_API_BASE=http://{host}:{port}/v1")
    print(f"    OPENAI_API_KEY=any")
    print()

    # log_level mapped to uvicorn's; we do our own configuration in the app.
    uvicorn.run(app, host=host, port=port, log_level="info" if args.verbose else "warning")
    return 0
