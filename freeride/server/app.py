"""FastAPI app factory for the FreeRide gateway.

The server is the heart of v3: an OpenAI-compatible HTTP surface that
agents point at instead of OpenRouter/NIM/etc. directly. Phase 2 lands
``/health``, ``/v1/models``, and non-streaming ``/v1/chat/completions``;
Phase 3 adds streaming + a second provider; Phase 5 wires the hourly
opt-in telemetry beacon.

We use a factory (``create_app``) instead of a module-level ``app =``
so tests can construct fresh app instances and the CLI can pass
provider registries / config in.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, Sequence

from fastapi import FastAPI

from freeride import __version__
from freeride.core import telemetry
from freeride.core.provider import Provider


_TELEMETRY_INTERVAL_SECONDS = 3600  # hourly


logger = logging.getLogger("freeride.server")


def _configure_logging(*, verbose: bool) -> None:
    """Attach a console handler. Default: WARN-level only, no request bodies.
    ``verbose=True`` drops to INFO and (later) opts into truncated body logging.
    """
    level = logging.INFO if verbose else logging.WARNING
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if verbose:
        logger.warning(
            "Verbose logging enabled. Request and response bodies may be logged. "
            "Do NOT enable in production unless you intend to record prompts."
        )


async def _telemetry_loop() -> None:
    """Hourly opt-in beacon. Skips entirely while telemetry is disabled
    (the user can flip it on/off without restarting the gateway —
    ``is_enabled()`` re-reads ``~/.freeride/config.json`` each tick).
    Errors are silent per spec.
    """
    while True:
        try:
            await asyncio.sleep(_TELEMETRY_INTERVAL_SECONDS)
            if telemetry.is_enabled():
                # Run the sync httpx call in a thread so we don't block
                # the event loop on the network round-trip.
                await asyncio.to_thread(telemetry.ship_beacon)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.debug("telemetry beacon failed: %s", e)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    logger.info(
        "freeride gateway starting (v=%s providers=%s telemetry=%s)",
        __version__,
        [p.name for p in app.state.providers],
        "on" if telemetry.is_enabled() else "off",
    )
    task = asyncio.create_task(_telemetry_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        logger.info("freeride gateway shutting down")


def create_app(
    *,
    providers: Sequence[Provider] | None = None,
    verbose: bool = False,
) -> FastAPI:
    """Build a fresh FastAPI app instance.

    Parameters
    ----------
    providers
        The Provider plugins this server will route to. Empty for tests
        that only exercise the framework. The CLI wires the real
        OpenRouterProvider (and later NIM) here.
    verbose
        If True, set logging to INFO. Otherwise WARNING. Body-content
        logging is off in both cases for v3.0; will be opt-in only in
        Phase 5 telemetry work.
    """
    _configure_logging(verbose=verbose)

    app = FastAPI(
        title="FreeRide Gateway",
        version=__version__,
        description="OpenAI-compatible local proxy across free-tier providers.",
        lifespan=_lifespan,
    )
    app.state.providers = list(providers or [])
    app.state.verbose = verbose

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "version": __version__,
            "providers": [p.name for p in app.state.providers],
        }

    # Route modules attach via APIRouter so the app stays composable.
    from freeride.server.routes import chat as chat_route
    from freeride.server.routes import models as models_route

    app.include_router(models_route.router)
    app.include_router(chat_route.router)

    return app
