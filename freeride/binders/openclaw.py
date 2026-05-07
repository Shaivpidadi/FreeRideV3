"""``freeride bind openclaw`` — point OpenClaw at the gateway.

OpenClaw routes by ``<provider>/<api_id>``: every config value gets a
leading provider prefix that OpenClaw strips before forwarding. So we
register a synthetic ``freeride`` provider that points at the gateway,
and write models in the form ``freeride/<api_id>``.

We deliberately do *not* set the FreeRide gateway as ``openrouter:default``
— that would let OpenClaw believe it's talking to OpenRouter directly.
A dedicated ``freeride:default`` profile makes the indirection visible
in OpenClaw's `freeride status` output.
"""

from __future__ import annotations

from pathlib import Path

from freeride.core.state import write_json_atomic
from freeride.v2compat.openclaw import (
    OPENCLAW_CONFIG_PATH,
    ensure_config_structure,
    load_openclaw_config,
)


def bind(gateway_url: str, *, api_key: str = "any", config_path: Path | None = None) -> str:
    """Write a ``freeride:default`` auth profile pointing at the gateway.

    Preserves all unrelated top-level keys, all other auth profiles, and
    OpenClaw's gateway/channels/plugins config.
    """
    path = config_path or OPENCLAW_CONFIG_PATH
    config = load_openclaw_config(path)
    config = ensure_config_structure(config)

    config.setdefault("auth", {})
    config["auth"].setdefault("profiles", {})
    config["auth"]["profiles"]["freeride:default"] = {
        "provider": "openai",
        "mode": "api_key",
        "base_url": gateway_url,
        "api_key": api_key,
    }

    # Set freeride/free as the primary so OpenClaw routes everything
    # through the gateway, which then picks the best free model itself.
    config["agents"]["defaults"]["model"]["primary"] = "freeride/free"
    config["agents"]["defaults"]["models"]["freeride/free"] = {}

    write_json_atomic(path, config, indent=2)
    return (
        f"OpenClaw config at {path} updated.\n"
        f"  Auth profile: freeride:default -> {gateway_url}\n"
        f"  Primary model: freeride/free\n"
        f"  Restart OpenClaw for changes to take effect."
    )
