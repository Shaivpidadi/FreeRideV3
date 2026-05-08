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

from freeride.providers.cerebras import CerebrasProvider
from freeride.providers.cloudflare_wai import CloudflareWAIProvider
from freeride.providers.groq import GroqProvider
from freeride.providers.huggingface import HuggingFaceProvider
from freeride.providers.nvidia_nim import NVIDIANIMProvider
from freeride.providers.ollama import OllamaProvider
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


def build_provider_registry() -> list:
    """Pick up provider plugins based on what env vars are CURRENTLY set.

    OpenRouter is always-on (it's our primary). The remaining providers
    are added when their respective env vars are set. This function is
    pure — re-running it after env-var changes returns a fresh registry,
    which is what the hot-reload endpoint relies on.

    Also re-loads ``~/.freeride/.env`` into ``os.environ`` (OS env wins,
    so this only fills GAPS — nothing already exported gets overwritten).
    Means ``freeride init && freeride serve`` works without a manual
    ``source``, and ``freeride reload`` after editing ``.env`` picks up
    any newly-added provider keys.
    """
    from freeride.core.dotenv import load_dotenv_into_environ

    load_dotenv_into_environ()

    providers: list = [OpenRouterProvider()]
    if os.environ.get("NVIDIA_API_KEY"):
        providers.append(NVIDIANIMProvider())
    if os.environ.get("GROQ_API_KEY"):
        providers.append(GroqProvider())
    if os.environ.get("CLOUDFLARE_API_TOKEN") and os.environ.get("CLOUDFLARE_ACCOUNT_ID"):
        providers.append(CloudflareWAIProvider())
    if os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_API_KEY"):
        providers.append(HuggingFaceProvider())
    if ollama_url := os.environ.get("OLLAMA_BASE_URL"):
        providers.append(OllamaProvider(base_url=ollama_url))
    if os.environ.get("CEREBRAS_API_KEY"):
        providers.append(CerebrasProvider())

    # Third-party plugins registered under the freeride.providers entry
    # point group. Each one constructed by the registry helper so a
    # broken plugin can't prevent serve startup.
    from freeride.core.registry import discover_third_party_providers

    providers.extend(discover_third_party_providers())
    return providers


# Backwards-compat alias for any existing imports.
_build_provider_registry = build_provider_registry


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

    providers = build_provider_registry()
    # Pass the factory so POST /v1/_freeride/reload can rebuild the
    # registry when env vars change without a process restart.
    app = create_app(
        providers=providers,
        verbose=args.verbose,
        provider_factory=build_provider_registry,
    )

    print(f"freeride gateway listening on http://{host}:{port}")
    print(f"  providers: {', '.join(p.name for p in providers)}")
    print(f"  verbose: {args.verbose}")
    print("  point any OpenAI-compatible agent at:")
    print(f"    OPENAI_API_BASE=http://{host}:{port}/v1")
    print("    OPENAI_API_KEY=any")
    print()

    # log_level mapped to uvicorn's; we do our own configuration in the app.
    uvicorn.run(app, host=host, port=port, log_level="info" if args.verbose else "warning")
    return 0
