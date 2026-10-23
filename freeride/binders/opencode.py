"""``freeride bind opencode`` — point OpenCode (sst/opencode) at the gateway.

Config: ``~/.config/opencode/opencode.json`` (JSON, or JSONC with
``//`` line comments). We touch exactly ``provider.freeride`` and, only
when the user has no model selected yet, top-level ``model``. See
``docs/agents/binders.md`` for the verified schema.

OpenCode sends the key under ``provider.<id>.models`` as the model id
(the ``<id>/`` prefix is stripped), so the entries here are ids the
gateway resolves itself: ``auto`` and the routing presets.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from freeride.core.state import write_json_atomic

OPENCODE_CONFIG_PATH = Path.home() / ".config" / "opencode" / "opencode.json"

_MODELS: dict[str, dict] = {
    "auto": {"name": "FreeRide smart routing"},
    "freeride/coding": {"name": "FreeRide coding preset (tool-capable models first)"},
    "freeride/fast": {"name": "FreeRide fast preset"},
    "freeride/quality": {"name": "FreeRide quality preset"},
}

_LINE_COMMENT = re.compile(r"^\s*//.*$", re.MULTILINE)


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # JSONC: drop whole-line comments and trailing commas, then retry.
        stripped = re.sub(r",(\s*[}\]])", r"\1", _LINE_COMMENT.sub("", raw))
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"{path} is not JSON I can safely rewrite ({e.msg} at line {e.lineno}); "
                "add the provider block by hand (docs/agents/binders.md)."
            ) from e
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not hold a JSON object")
    return data


def bind(gateway_url: str, *, api_key: str = "any", config_path: Path | None = None) -> str:
    path = config_path or OPENCODE_CONFIG_PATH
    config = _load(path)
    config.setdefault("$schema", "https://opencode.ai/config.json")
    providers = config.setdefault("provider", {})
    if not isinstance(providers, dict):
        raise ValueError(f"{path}: 'provider' is not an object")
    providers["freeride"] = {
        "npm": "@ai-sdk/openai-compatible",
        "name": "FreeRide Gateway",
        "options": {"baseURL": gateway_url, "apiKey": api_key},
        "models": {mid: dict(meta) for mid, meta in _MODELS.items()},
    }
    chose_model = False
    if not config.get("model"):
        config["model"] = "freeride/auto"
        chose_model = True
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, config, indent=2)
    lines = [
        f"OpenCode config at {path} updated.",
        f"  provider.freeride.options.baseURL: {gateway_url}",
        f"  provider.freeride.models: {', '.join(_MODELS)}",
    ]
    if chose_model:
        lines.append("  model: freeride/auto (set because none was selected)")
    lines.append("  OpenCode re-reads the config on the next prompt (or /reload).")
    return "\n".join(lines)
