"""``freeride bind openclaw`` — point OpenClaw at the gateway.

OpenClaw's schema (verified by reading ``dist/config/types.models.d.ts``
and ``dist/config/types.auth.d.ts`` in the installed npm package, 2026-05-07):

* ``auth.profiles.<id>`` accepts only ``provider``, ``mode``, ``email`` —
  NOT ``base_url`` or ``api_key``. Auth profile is a *pointer* to a
  credential stored elsewhere.
* Custom model providers go under ``models.providers.<name>`` with
  ``baseUrl`` (camelCase), optional ``apiKey``, and a required
  ``models[]`` array.
* Each ``ModelDefinitionConfig`` requires
  ``id``, ``name``, ``reasoning``, ``input``, ``cost``, ``contextWindow``,
  ``maxTokens``.
* Live API keys for the ``mode: "api_key"`` profiles are persisted by
  OpenClaw to ``~/.openclaw/agents/<agent>/agent/auth-profiles.json``
  (NOT openclaw.json) — that file is owned by ``openclaw login`` /
  ``openclaw auth add`` and we don't write it.

The binder writes a schema-valid ``models.providers.freeride`` block
plus the matching auth profile, sets ``agents.defaults.model.primary
= "freeride/free"``, and points the user at the next step
(`openclaw login freeride:default`) to put the API key in the place
OpenClaw expects.
"""

from __future__ import annotations

from pathlib import Path

from freeride.core.state import write_json_atomic
from freeride.v2compat.openclaw import (
    OPENCLAW_CONFIG_PATH,
    ensure_config_structure,
    load_openclaw_config,
)


_FREE_MODEL_DEFINITION: dict = {
    "id": "free",
    "name": "FreeRide free-tier router",
    "api": "openai",  # tells pi-ai to use the OpenAI-compatible client class
    "reasoning": False,
    "input": ["text", "image"],
    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
    "contextWindow": 131072,
    "maxTokens": 4096,
}


def bind(
    gateway_url: str,
    *,
    api_key: str = "any",
    config_path: Path | None = None,
) -> str:
    """Write a schema-valid OpenClaw config pointing at the gateway.

    What we touch (atomic write, all unrelated keys preserved):
    * ``models.providers.freeride.{baseUrl, apiKey, auth, models}`` —
      the custom provider entry.
    * ``auth.profiles["freeride:default"] = {provider, mode}`` —
      schema-valid pointer (no base_url/api_key inside).
    * ``agents.defaults.model.primary = "freeride/free"``.

    What we *don't* touch:
    * ``~/.openclaw/agents/<agent>/agent/auth-profiles.json`` —
      OpenClaw's encrypted credential store. The user runs
      ``openclaw login freeride:default`` (or ``openclaw auth add``)
      to put the API key there.
    """
    path = config_path or OPENCLAW_CONFIG_PATH
    config = load_openclaw_config(path)
    config = ensure_config_structure(config)

    # 1. Custom model provider definition
    config.setdefault("models", {})
    config["models"].setdefault("mode", "merge")
    config["models"].setdefault("providers", {})
    config["models"]["providers"]["freeride"] = {
        "baseUrl": gateway_url,
        "apiKey": api_key,
        "auth": "api-key",
        "models": [_FREE_MODEL_DEFINITION],
    }

    # 2. Schema-valid auth profile pointer (no base_url/api_key inside)
    config.setdefault("auth", {})
    config["auth"].setdefault("profiles", {})
    config["auth"]["profiles"]["freeride:default"] = {
        "provider": "freeride",
        "mode": "api_key",
    }

    # 3. Make the gateway-routed model the primary
    config["agents"]["defaults"]["model"]["primary"] = "freeride/free"
    # Old shape from v2: also register under agents.defaults.models for
    # OpenClaw's local primary/fallback lookup. Keep for v2 compat.
    config["agents"]["defaults"]["models"]["freeride/free"] = {}

    write_json_atomic(path, config, indent=2)
    return (
        f"OpenClaw config at {path} updated.\n"
        f"  models.providers.freeride: -> {gateway_url}\n"
        f"  auth.profiles.freeride:default: registered\n"
        f"  agents.defaults.model.primary: freeride/free\n"
        f"  Next: run `openclaw login freeride:default` to store the\n"
        f"  API key (or any value — the gateway accepts any), then\n"
        f"  restart OpenClaw."
    )
