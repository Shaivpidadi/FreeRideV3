"""OpenRouter provider plugin.

Ports the v2 ``main.py`` logic into a :class:`Provider` implementation.
This module owns OpenRouter-specific concerns only — free-model
detection, error classification, attribution headers — and depends on
nothing from other provider plugins.

Free-detection rule
-------------------
OpenRouter exposes two signals that disagree occasionally:

* ``model.pricing.prompt == 0`` — the canonical billing-side flag
* ``":free"`` suffix in ``model.id`` — the routing-side flag

We treat a model as free if **either** signal says free. Direct port
of v2's behavior; matches the dual-signal note in
``knowledge/PLAN_GATEWAY.md`` §5 carry-forward principle 1.

Chat-shape filter
-----------------
We surface only text-output models. OpenRouter mixes image-gen,
audio-gen, and multi-modal-output models into the same catalog; without
this filter, ranking has historically picked Lyria (text+image →
text+audio) as a top "chat" model. The filter inspects
``architecture.output_modalities`` first, falls back to parsing
``architecture.modality`` strings of the form ``"text+image->text"``,
and keeps unknown shapes (the live probe catches false positives).
"""

from __future__ import annotations

import httpx

from freeride.core.provider import PROVIDER_API_VERSION
from freeride.core.types import Model

OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"
OPENROUTER_MODELS_URL = f"{OPENROUTER_API_BASE}/models"
OPENROUTER_CHAT_URL = f"{OPENROUTER_API_BASE}/chat/completions"

# Attribution stamped on every outbound request so all FreeRide traffic
# rolls up under one identity on OpenRouter's App Activity page.
# https://openrouter.ai/docs/api-reference/overview#headers
OPENROUTER_REFERER = "https://github.com/Shaivpidadi/FreeRideV3"
OPENROUTER_APP_TITLE = "FreeRide Gateway"


def is_chat_model(model: dict) -> bool:
    """True if ``model`` is a text-output model suitable for /chat/completions.

    Filters out image-gen, audio-gen, and multi-modal-output models that
    aren't chat-shaped. Direct port of v2 ``_is_chat_model``.
    """
    arch = model.get("architecture") or {}

    # Preferred: explicit output_modalities array.
    out_mods = arch.get("output_modalities")
    if isinstance(out_mods, list) and out_mods:
        return out_mods == ["text"]

    # Fallback: parse a "text+image->text" / "text->text+audio" string.
    modality = arch.get("modality", "")
    if isinstance(modality, str) and "->" in modality:
        output_part = modality.split("->", 1)[1].strip()
        return output_part == "text"

    # Unknown shape — keep it; live probe will catch false positives.
    return True


def is_free_model(model: dict) -> bool:
    """True if either OpenRouter free-signal fires for this model.

    Prefer ``pricing.prompt == 0`` (the billing-side flag, definitive).
    Fall back to ``":free"`` suffix in id (the routing-side flag,
    sometimes set without the pricing being zero).
    """
    prompt_cost = model.get("pricing", {}).get("prompt")
    if prompt_cost is not None:
        try:
            if float(prompt_cost) == 0:
                return True
        except (ValueError, TypeError):
            pass
    return ":free" in model.get("id", "")


def filter_free_chat_models(models: list[dict]) -> list[dict]:
    """Return the chat-shaped, free subset of ``models``.

    Direct port of v2 ``filter_free_models`` plus the chat filter.
    Dedupes by ``id`` (OpenRouter occasionally returns duplicates).
    """
    seen: set[str] = set()
    out: list[dict] = []
    for m in models:
        mid = m.get("id", "")
        if mid in seen:
            continue
        if not is_chat_model(m):
            continue
        if not is_free_model(m):
            continue
        out.append(m)
        seen.add(mid)
    return out


def _to_model(raw: dict) -> Model:
    """Convert one OpenRouter catalog entry to a :class:`Model`."""
    arch = raw.get("architecture") or {}
    out_mods = arch.get("output_modalities")
    if not isinstance(out_mods, list) or not out_mods:
        out_mods = ["text"]
    return Model(
        api_id=raw.get("id", ""),
        provider="openrouter",
        context_length=int(raw.get("context_length") or 0),
        output_modalities=tuple(out_mods),
        supported_parameters=tuple(raw.get("supported_parameters") or ()),
        raw=raw,
    )


class OpenRouterProvider:
    """OpenRouter Provider plugin (incremental impl, full surface lands in 1.4.6)."""

    name: str = "openrouter"
    api_version: int = PROVIDER_API_VERSION

    def __init__(self, *, http_timeout: float = 30.0) -> None:
        self._timeout = http_timeout

    # ----- request stamping (Provider Protocol) --------------------------
    def auth_header(self, key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {key}"}

    def attribution_headers(self) -> dict[str, str]:
        return {
            "HTTP-Referer": OPENROUTER_REFERER,
            "X-Title": OPENROUTER_APP_TITLE,
        }

    def _outbound_headers(self, key: str, *, json_content: bool = False) -> dict[str, str]:
        h: dict[str, str] = {**self.auth_header(key), **self.attribution_headers()}
        if json_content:
            h["Content-Type"] = "application/json"
        return h

    # ----- discovery ------------------------------------------------------
    def list_free_models(self, key: str) -> list[Model]:
        """Fetch OpenRouter's catalog with ``key`` and return the free-tier
        chat-shaped subset as :class:`Model` instances.

        Single-key per the Protocol — multi-key rotation belongs in the
        gateway's key pool (lands in Phase 1.6). The CLI's ``freeride list``
        will iterate over keys via that pool, calling this method per-key
        and short-circuiting on first success.
        """
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.get(OPENROUTER_MODELS_URL, headers=self._outbound_headers(key))
        resp.raise_for_status()
        raw_models: list[dict] = resp.json().get("data", []) or []
        free = filter_free_chat_models(raw_models)
        return [_to_model(r) for r in free]
