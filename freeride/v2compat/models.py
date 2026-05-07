"""v2-style multi-key model fetching, ranking, and caching.

Self-contained — uses ``httpx`` directly and the v3 :class:`KeyCooldown`
to give cross-invocation cooldown that v2 lacked. Returns raw OpenRouter
catalog dicts (with an added ``_score`` field) to preserve v2's data
shape end-to-end. The v3 gateway uses :class:`OpenRouterProvider` and
:class:`Model` instead; this module exists for v2 CLI parity only.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from freeride.core.cooldown import KeyCooldown
from freeride.core.state import write_json_atomic
from freeride.providers.openrouter import (
    OPENROUTER_MODELS_URL,
    OPENROUTER_REFERER,
    OPENROUTER_APP_TITLE,
    filter_free_chat_models,
)


_PROVIDER_NAME = "openrouter"

OPENCLAW_CONFIG_PATH = Path.home() / ".openclaw" / "openclaw.json"
CACHE_FILE = Path.home() / ".openclaw" / ".freeride-cache.json"
CACHE_DURATION_HOURS = 6

# Ranking weights — same as v2.
RANKING_WEIGHTS = {
    "context_length": 0.4,
    "capabilities": 0.3,
    "recency": 0.2,
    "provider_trust": 0.1,
}

TRUSTED_PROVIDERS = [
    "google",
    "meta-llama",
    "mistralai",
    "deepseek",
    "nvidia",
    "qwen",
    "microsoft",
    "allenai",
    "arcee-ai",
]


def _openrouter_headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": OPENROUTER_REFERER,
        "X-Title": OPENROUTER_APP_TITLE,
    }


def _parse_api_keys(raw: Any) -> list[str]:
    """Single key string, JSON-array literal, or real Python list."""
    if isinstance(raw, list):
        return [k.strip() for k in raw if isinstance(k, str) and k.strip()]
    if not isinstance(raw, str):
        return []
    raw = raw.strip()
    if raw.startswith("["):
        try:
            keys = json.loads(raw)
            if isinstance(keys, list):
                return [k.strip() for k in keys if isinstance(k, str) and k.strip()]
        except (json.JSONDecodeError, ValueError):
            pass
    return [raw] if raw else []


def get_api_keys() -> list[str]:
    """Resolve OpenRouter API keys from env or OpenClaw config."""
    raw = os.environ.get("OPENROUTER_API_KEY")
    if raw:
        return _parse_api_keys(raw)

    if OPENCLAW_CONFIG_PATH.exists():
        try:
            cfg = json.loads(OPENCLAW_CONFIG_PATH.read_text())
            raw = cfg.get("env", {}).get("OPENROUTER_API_KEY")
            if raw:
                return _parse_api_keys(raw)
        except (json.JSONDecodeError, KeyError):
            pass
    return []


def get_api_key() -> str | None:
    keys = get_api_keys()
    return keys[0] if keys else None


def fetch_all_models(
    *,
    cooldown: KeyCooldown | None = None,
    timeout: float = 30.0,
) -> list[dict]:
    """Fetch the full OpenRouter catalog, rotating across keys on 429/401.

    Persistent cooldown via :class:`KeyCooldown` means a key that hit 429
    in one CLI invocation stays out of rotation in the next, until TTL.
    Returns raw catalog dicts (or ``[]`` on total failure); prints
    progress lines that match v2's UX.
    """
    keys = get_api_keys()
    if not keys:
        return []
    cd = cooldown or KeyCooldown()

    last_status: int | None = None
    for i, key in enumerate(keys, 1):
        if cd.is_in_cooldown(_PROVIDER_NAME, key):
            continue
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.get(OPENROUTER_MODELS_URL, headers=_openrouter_headers(key))
        except httpx.RequestError as e:
            print(f"  Key {i}: network error ({e})")
            continue

        if response.status_code == 200:
            return response.json().get("data", []) or []
        if response.status_code in (401, 429):
            label = "invalid" if response.status_code == 401 else "rate-limited"
            print(f"  Key {i}: {label}, trying next...")
            cd.mark_rate_limited(_PROVIDER_NAME, key)
            last_status = response.status_code
            continue
        print(f"Error fetching models: HTTP {response.status_code}")
        return []

    if last_status:
        print(f"Error: all API keys exhausted (last status: {last_status}).")
    else:
        print("Error: no usable API keys.")
    return []


def calculate_model_score(model: dict) -> float:
    """v2 ranking — same weights, same logic."""
    score = 0.0

    context_length = model.get("context_length", 0) or 0
    score += min(context_length / 1_000_000, 1.0) * RANKING_WEIGHTS["context_length"]

    capabilities = model.get("supported_parameters") or []
    score += min(len(capabilities) / 10, 1.0) * RANKING_WEIGHTS["capabilities"]

    created = model.get("created") or 0
    if created:
        days_old = (time.time() - created) / 86400
        score += max(0, 1 - (days_old / 365)) * RANKING_WEIGHTS["recency"]

    model_id = model.get("id", "")
    provider = model_id.split("/", 1)[0] if "/" in model_id else ""
    if provider in TRUSTED_PROVIDERS:
        trust_index = TRUSTED_PROVIDERS.index(provider)
        score += (1 - (trust_index / len(TRUSTED_PROVIDERS))) * RANKING_WEIGHTS["provider_trust"]

    return score


def rank_free_models(models: list[dict]) -> list[dict]:
    scored = [{**m, "_score": calculate_model_score(m)} for m in models]
    scored.sort(key=lambda x: x["_score"], reverse=True)
    return scored


def get_cached_models() -> list[dict] | None:
    if not CACHE_FILE.exists():
        return None
    try:
        cache = json.loads(CACHE_FILE.read_text())
        cached_at = datetime.fromisoformat(cache.get("cached_at", ""))
        if datetime.now() - cached_at < timedelta(hours=CACHE_DURATION_HOURS):
            return cache.get("models", [])
    except (json.JSONDecodeError, ValueError):
        pass
    return None


def save_models_cache(models: list[dict]) -> None:
    """Atomic write of the model cache."""
    write_json_atomic(
        CACHE_FILE,
        {"cached_at": datetime.now().isoformat(), "models": models},
        indent=2,
    )


def get_free_models(*, force_refresh: bool = False) -> list[dict]:
    """v2 ``get_free_models``: serve from cache or refetch+rank+cache."""
    if not force_refresh:
        cached = get_cached_models()
        if cached:
            return cached

    all_models = fetch_all_models()
    free = filter_free_chat_models(all_models)
    ranked = rank_free_models(free)
    if ranked:
        save_models_cache(ranked)
    return ranked
