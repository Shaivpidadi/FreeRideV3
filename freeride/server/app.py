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
from typing import Any, Callable, Sequence

from fastapi import FastAPI

from freeride import __version__
from freeride.core import telemetry
from freeride.core.provider import Provider


_TELEMETRY_INTERVAL_SECONDS = 3600  # hourly
# Delay before firing the startup beacon. Long enough for the gateway
# to settle past lifespan startup (provider registry, route mounting)
# but short enough that even a 60-second `freeride serve` session
# fires at least one beacon — closing the gap where users who run
# serve briefly never report at all.
_TELEMETRY_STARTUP_DELAY_SECONDS = 30


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
    """Opt-in beacon — fires once shortly after startup, then hourly.

    The startup tick (after a 30-second settling delay) closes the gap
    where short ``freeride serve`` sessions never run long enough to
    hit the hourly tick. Without it, anyone who runs the gateway for
    less than an hour is invisible to our adoption metrics.

    Skips entirely while telemetry is disabled — ``is_enabled()`` re-
    reads ``~/.freeride/config.json`` each tick, so the user can flip
    it on/off without restarting the gateway. Errors are silent per
    spec.
    """
    try:
        # Startup tick: settle, then fire once.
        try:
            await asyncio.sleep(_TELEMETRY_STARTUP_DELAY_SECONDS)
            if telemetry.is_enabled():
                await asyncio.to_thread(telemetry.ship_beacon)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.debug("telemetry startup beacon failed: %s", e)

        # Steady-state hourly loop.
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
    except asyncio.CancelledError:
        return


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
    provider_factory: "Callable[[], list[Provider]] | None" = None,
) -> FastAPI:
    """Build a fresh FastAPI app instance.

    Parameters
    ----------
    providers
        The Provider plugins this server will route to. Empty for tests
        that only exercise the framework.
    verbose
        If True, set logging to INFO. Otherwise WARNING.
    provider_factory
        Optional zero-arg callable that returns a fresh provider list.
        When provided, ``POST /v1/_freeride/reload`` rebuilds
        ``app.state.providers`` by calling it — useful for picking up
        new env vars without restarting the gateway. ``None`` (the
        default) makes the reload endpoint return 501.
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
    app.state.provider_factory = provider_factory

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "version": __version__,
            "providers": [p.name for p in app.state.providers],
        }

    @app.post("/v1/_freeride/reload")
    async def freeride_reload() -> dict[str, Any]:
        """Re-build the provider registry from the current process env.

        Useful when adding a new provider key (e.g., ``GROQ_API_KEY``)
        without restarting the gateway. Atomically replaces
        ``app.state.providers`` so in-flight requests holding their own
        snapshot of the list don't see partial state.

        Returns 501 when the server was started without a
        ``provider_factory`` (e.g., a test app pinned to a fixed list).
        Returns the before/after provider names so the caller can see
        what changed.
        """
        factory = app.state.provider_factory
        if factory is None:
            return {
                "ok": False,
                "error": "reload_not_enabled",
                "message": "Server was started without a provider_factory; restart `freeride serve` to pick up new env vars.",
            }
        before = [p.name for p in app.state.providers]
        try:
            new_providers = list(factory())
        except Exception as e:
            return {
                "ok": False,
                "error": "factory_failed",
                "message": f"provider_factory raised: {e!s}",
            }
        app.state.providers = new_providers
        after = [p.name for p in new_providers]
        return {
            "ok": True,
            "before": before,
            "after": after,
            "added": [n for n in after if n not in before],
            "removed": [n for n in before if n not in after],
        }

    @app.get("/v1/_freeride/providers")
    async def freeride_providers() -> dict[str, Any]:
        """Diagnostic endpoint: per-provider health stats from the
        in-process tracker. Read-only; no auth (gateway listens on
        localhost so the user already controls access). Useful for
        ``freeride status``-style introspection without subprocess'ing
        into the running gateway.
        """
        from freeride.core.health import ProviderHealth

        h = ProviderHealth.instance()
        return {
            "providers": [
                {
                    "name": p.name,
                    "embeddings_supported": bool(getattr(p, "embeddings_supported", False)),
                    **h.stats(p.name),
                }
                for p in app.state.providers
            ],
        }

    # Route modules attach via APIRouter so the app stays composable.
    from freeride.server.routes import chat as chat_route
    from freeride.server.routes import embeddings as embeddings_route
    from freeride.server.routes import models as models_route

    app.include_router(models_route.router)
    app.include_router(chat_route.router)
    app.include_router(embeddings_route.router)

    return app
