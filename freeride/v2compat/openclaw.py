"""OpenClaw configuration writer (v2 behavior, atomic-write).

Preserves v2's exact ``~/.openclaw/openclaw.json`` mutation surface. The
single behavior delta vs v2: writes go through
:func:`~freeride.core.state.write_json_atomic` so a crash mid-write
can't corrupt the user's OpenClaw config (latent v2 bug; see
PLAN_GATEWAY.md §5.5).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from freeride.core.state import write_json_atomic


OPENCLAW_CONFIG_PATH = Path.home() / ".openclaw" / "openclaw.json"


def load_openclaw_config(path: Path | None = None) -> dict:
    """Load OpenClaw config; return ``{}`` for missing or unparseable files."""
    p = path or OPENCLAW_CONFIG_PATH
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return {}


def save_openclaw_config(config: dict, path: Path | None = None) -> None:
    """Atomic-write the config back to disk. Indent matches v2's output."""
    write_json_atomic(path or OPENCLAW_CONFIG_PATH, config, indent=2)


def format_model_for_openclaw(model_id: str, *, append_free: bool = True) -> str:
    """Convert an OpenRouter model ID into OpenClaw's ``<provider>/<api_id>`` shape.

    OpenClaw routes by leading segment: it strips ``<provider>/`` from the
    config value and forwards the remainder verbatim to that provider's
    API. So *every* config value gets a leading ``openrouter/``, which
    means OpenRouter-native models become a literal ``openrouter/openrouter/free``.
    OpenClaw strips the leading segment, OpenRouter sees the bare
    ``openrouter/free`` it expects.

    Examples::

      qwen/qwen3-coder:free    -> openrouter/qwen/qwen3-coder:free
      qwen/qwen3-coder         -> openrouter/qwen/qwen3-coder:free   (append_free=True)
      openrouter/free          -> openrouter/openrouter/free
      openrouter/owl-alpha     -> openrouter/openrouter/owl-alpha

    OpenRouter-native models (those whose API ID already starts with
    ``openrouter/``) don't take the ``:free`` suffix.
    """
    is_native = model_id.startswith("openrouter/")
    base_id = model_id
    if append_free and not is_native and ":free" not in base_id:
        base_id = f"{base_id}:free"
    return f"openrouter/{base_id}"


def stored_to_api_id(stored_id: str) -> str:
    """Inverse of :func:`format_model_for_openclaw` — strip OpenClaw's
    leading ``openrouter/`` provider prefix.

    Examples::

      openrouter/qwen/qwen3-coder:free  -> qwen/qwen3-coder:free
      openrouter/openrouter/free        -> openrouter/free
      openrouter/openrouter/owl-alpha   -> openrouter/owl-alpha
    """
    prefix = "openrouter/"
    return stored_id[len(prefix):] if stored_id.startswith(prefix) else stored_id


def get_current_model(config: dict | None = None) -> str | None:
    """Return the current primary model ID stored in OpenClaw, or None."""
    cfg = config if config is not None else load_openclaw_config()
    return cfg.get("agents", {}).get("defaults", {}).get("model", {}).get("primary")


def get_current_fallbacks(config: dict | None = None) -> list[str]:
    """Return the current fallback chain stored in OpenClaw, or []."""
    cfg = config if config is not None else load_openclaw_config()
    return cfg.get("agents", {}).get("defaults", {}).get("model", {}).get("fallbacks", [])


def ensure_config_structure(config: dict) -> dict:
    """Ensure the nested ``agents.defaults.{model,models}`` structure exists
    without overwriting any existing values. Returns the same dict for
    chaining.
    """
    config.setdefault("agents", {})
    config["agents"].setdefault("defaults", {})
    config["agents"]["defaults"].setdefault("model", {})
    config["agents"]["defaults"].setdefault("models", {})
    return config


def setup_openrouter_auth(config: dict) -> dict:
    """Add the canonical OpenRouter auth profile if missing."""
    config.setdefault("auth", {})
    config["auth"].setdefault("profiles", {})
    if "openrouter:default" not in config["auth"]["profiles"]:
        config["auth"]["profiles"]["openrouter:default"] = {
            "provider": "openrouter",
            "mode": "api_key",
        }
    return config


def update_model_config(
    model_id: str,
    *,
    free_models_provider: Any | None = None,
    api_keys: list[str] | None = None,
    as_primary: bool = True,
    add_fallbacks: bool = True,
    fallback_count: int = 5,
    setup_auth: bool = False,
    append_free: bool = True,
    config_path: Path | None = None,
) -> bool:
    """Apply v2's ``update_model_config`` behavior using v3 building blocks.

    The ``free_models_provider`` callable yields a list of v3
    :class:`~freeride.core.types.Model` instances (typically a closure
    over :meth:`OpenRouterProvider.list_free_models`); ``api_keys`` is
    used as a feature gate (parity with v2: skip fallbacks if no keys).
    Tests pass synthetic providers and key lists; the real CLI wires
    these to the live v3 provider.
    """
    config = load_openclaw_config(config_path)
    config = ensure_config_structure(config)

    if setup_auth:
        config = setup_openrouter_auth(config)

    formatted = format_model_for_openclaw(model_id, append_free=append_free)

    if as_primary:
        config["agents"]["defaults"]["model"]["primary"] = formatted
        config["agents"]["defaults"]["models"][formatted] = {}

    if add_fallbacks and api_keys and free_models_provider is not None:
        free_models = list(free_models_provider())
        new_fallbacks: list[str] = []

        # openrouter/free smart router always leads unless it's the primary.
        smart_router = format_model_for_openclaw("openrouter/free")
        if formatted != smart_router:
            new_fallbacks.append(smart_router)
            config["agents"]["defaults"]["models"][smart_router] = {}

        for m in free_models:
            if len(new_fallbacks) >= fallback_count:
                break
            if isinstance(m, dict):
                api_id = m.get("id", "")
            else:
                api_id = getattr(m, "api_id", "")
            if not api_id:
                continue
            m_formatted = format_model_for_openclaw(api_id)
            if "openrouter/free" in api_id:
                continue
            if as_primary and m_formatted == formatted:
                continue
            current_primary = config["agents"]["defaults"]["model"].get("primary", "")
            if not as_primary and m_formatted == current_primary:
                continue
            new_fallbacks.append(m_formatted)
            config["agents"]["defaults"]["models"][m_formatted] = {}

        if not as_primary:
            if formatted not in new_fallbacks:
                insert_pos = 1 if smart_router in new_fallbacks else 0
                new_fallbacks.insert(insert_pos, formatted)
            config["agents"]["defaults"]["models"][formatted] = {}

        config["agents"]["defaults"]["model"]["fallbacks"] = new_fallbacks

    save_openclaw_config(config, config_path)
    return True
