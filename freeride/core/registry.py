"""Third-party provider plugin discovery via Python entry points.

Distributors of FreeRide-compatible provider plugins ship them as
separate pip packages and register the class under the
``freeride.providers`` entry-point group:

    # In a third-party plugin's pyproject.toml:
    [project.entry-points."freeride.providers"]
    awesome = "freeride_awesome_provider:AwesomeProvider"

At ``freeride serve`` startup, ``discover_third_party_providers()``
iterates the entry points, loads each class, instantiates it, and
returns the list. Construction failures (missing env vars, network
errors during init, etc.) are logged and skipped so one bad plugin
can't prevent the gateway from starting.

Conformance bar: every loaded class must implement the v1 Provider
Protocol (``api_version = 1``). Plugins on a different api_version
are loaded but logged as a version-mismatch warning so the user
knows to upgrade.
"""

from __future__ import annotations

import logging
from importlib import metadata
from typing import Any

from freeride.core.provider import PROVIDER_API_VERSION


logger = logging.getLogger(__name__)


_ENTRY_POINT_GROUP = "freeride.providers"


def discover_third_party_providers() -> list[Any]:
    """Return instantiated third-party provider plugins, in entry-point
    iteration order.

    Best-effort: anything that fails to import or construct is logged at
    WARNING and skipped. Anything that returns a class but doesn't
    declare ``api_version = PROVIDER_API_VERSION`` is also skipped (with
    a note to upgrade the plugin).
    """
    out: list[Any] = []
    try:
        eps = metadata.entry_points(group=_ENTRY_POINT_GROUP)
    except Exception as e:
        logger.warning("could not enumerate entry points: %s", e)
        return out

    for ep in eps:
        try:
            cls = ep.load()
        except Exception as e:
            logger.warning(
                "failed to load freeride provider plugin %r: %s", ep.name, e
            )
            continue

        api_version = getattr(cls, "api_version", None)
        if api_version != PROVIDER_API_VERSION:
            logger.warning(
                "skipping plugin %r — api_version=%r doesn't match "
                "PROVIDER_API_VERSION=%r. Upgrade the plugin or downgrade FreeRide.",
                ep.name,
                api_version,
                PROVIDER_API_VERSION,
            )
            continue

        try:
            instance = cls()
        except Exception as e:
            # CloudflareWAIProvider-style raise on missing env vars is
            # the canonical "I'm not configured, skip me" path. Plugins
            # SHOULD raise ValueError when they need env vars that
            # aren't set, exactly like CloudflareWAIProvider does.
            logger.info(
                "third-party plugin %r not loaded: %s", ep.name, e
            )
            continue

        out.append(instance)
        logger.info(
            "loaded third-party freeride provider %r (%s.%s)",
            ep.name,
            cls.__module__,
            cls.__name__,
        )

    return out
