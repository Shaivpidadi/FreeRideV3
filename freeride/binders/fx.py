"""``freeride bind fx`` — make FreeRide a provider connection in fx.

Stock fx (vercel-labs/fx) reads custom connections from
``~/.fx/settings.json``: a ``providers.<name>`` block with a protocol,
a base URL, an auth mode and optional model metadata, plus a top-level
``provider`` selection and a ``models.<name>`` default. This binder
writes exactly that for the local gateway, declares the routing presets
(and the gateway's tool-capable models, with their context sizes, so fx
can plan compaction), and installs the ``freeride`` skill under
``~/.fx/skills`` so the agent can diagnose its own model plumbing.

Everything else in the file is preserved. fx rejects unknown keys, so
only documented fields are written.
"""

from __future__ import annotations

import json
import re
from importlib import resources
from pathlib import Path
from typing import Any

import httpx

from freeride.core.state import write_json_atomic

FX_SETTINGS_PATH = Path.home() / ".fx" / "settings.json"
FX_SKILLS_DIR = Path.home() / ".fx" / "skills"
CONNECTION = "freeride"
DEFAULT_MODEL = "freeride/coding"
MAX_CATALOG_MODELS = 60

# Routing presets the gateway resolves itself. Declared so fx knows
# their context budget and that they can run tools.
PRESET_METADATA: dict[str, dict[str, Any]] = {
    "freeride/coding": {"context_window": 131072, "max_output_tokens": 8192, "supports_tool_use": True},
    "freeride/fast": {"context_window": 131072, "max_output_tokens": 8192, "supports_tool_use": True},
    "freeride/quality": {"context_window": 131072, "max_output_tokens": 8192, "supports_tool_use": True},
    "auto": {"context_window": 131072, "max_output_tokens": 8192, "supports_tool_use": True},
}

_LINE_COMMENT = re.compile(r"^\s*//.*$", re.MULTILINE)


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        stripped = re.sub(r",(\s*[}\]])", r"\1", _LINE_COMMENT.sub("", raw))
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"{path} is not JSON I can safely rewrite ({e.msg} at line {e.lineno}); "
                "add the connection by hand (docs/agents/binders.md)."
            ) from e
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not hold a JSON object")
    return data


def catalog_metadata(gateway_url: str, *, limit: int = MAX_CATALOG_MODELS, timeout: float = 20.0) -> dict[str, dict[str, Any]]:
    """Tool-capable models from the gateway's catalog with their context
    sizes, largest first. Empty when the gateway is unreachable; the
    binder still works, the agent just can't plan compaction for ids
    outside the presets."""
    try:
        resp = httpx.get(gateway_url.rstrip("/") + "/models", timeout=timeout)
        resp.raise_for_status()
        data = resp.json().get("data", [])
    except Exception:  # noqa: BLE001 — best effort
        return {}
    rows = []
    for m in data:
        if not isinstance(m, dict) or "tools" not in (m.get("supported_parameters") or []):
            continue
        ctx = int(m.get("context_length") or 0)
        if not m.get("id") or ctx <= 0:
            continue
        rows.append((ctx, str(m["id"])))
    rows.sort(key=lambda t: (-t[0], t[1]))
    return {
        mid: {"context_window": ctx, "max_output_tokens": min(8192, max(1024, ctx // 8)), "supports_tool_use": True}
        for ctx, mid in rows[:limit]
    }


def _install_skill(skills_dir: Path) -> Path:
    text = resources.files("freeride.assets").joinpath("skills/freeride/SKILL.md").read_text(encoding="utf-8")
    dst = skills_dir / "freeride" / "SKILL.md"
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not dst.exists() or dst.read_text(encoding="utf-8") != text:
        dst.write_text(text, encoding="utf-8")
    return dst


def bind(
    gateway_url: str,
    *,
    config_path: Path | None = None,
    skills_dir: Path | None = None,
    with_catalog: bool = True,
) -> str:
    path = config_path or FX_SETTINGS_PATH
    config = _load(path)

    metadata = dict(PRESET_METADATA)
    catalog = catalog_metadata(gateway_url) if with_catalog else {}
    metadata.update(catalog)

    providers = config.setdefault("providers", {})
    if not isinstance(providers, dict):
        raise ValueError(f"{path}: 'providers' is not an object")
    providers[CONNECTION] = {
        "protocol": "openai-chat-completions",
        "base_url": gateway_url,
        "auth": {"type": "none"},
        "fetch_models": True,
        "model_metadata": metadata,
    }
    models = config.setdefault("models", {})
    if isinstance(models, dict):
        models.setdefault(CONNECTION, DEFAULT_MODEL)
    selected_now = False
    if not config.get("provider"):
        config["provider"] = CONNECTION
        selected_now = True

    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, config, indent=2)
    skill = _install_skill(skills_dir or FX_SKILLS_DIR)

    lines = [
        f"fx settings at {path} updated.",
        f"  providers.{CONNECTION}: openai-chat-completions -> {gateway_url} (no auth, fetch_models on)",
        f"  model_metadata: {len(PRESET_METADATA)} presets + {len(catalog)} tool-capable catalog models",
        f"  models.{CONNECTION}: {models.get(CONNECTION) if isinstance(models, dict) else DEFAULT_MODEL}",
        f"  skill: {skill}",
    ]
    if selected_now:
        lines.append(f"  provider: {CONNECTION} (selected, none was set)")
    else:
        lines.append(f"  provider: left as {config.get('provider')!r}; run `fx provider {CONNECTION}` to switch")
    lines.append("  fx re-reads settings on its next start; `fx status --json` confirms the connection.")
    return "\n".join(lines)
