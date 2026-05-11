"""Routing decisions for ``/v1/messages``.

FreeRide is a *support* layer for Claude Code, not a replacement.
The mental model:

- ``claude-*`` model ids belong to Anthropic. If the caller has auth
  (OAuth bearer from a Pro/Max subscription, or a raw API key), we
  relay the request to ``api.anthropic.com`` unchanged. FreeRide is
  transparent — the user's real subscription works.
- ``freeride/*`` model ids belong to us. We resolve them to a free
  provider via the smart router. Users opt in per request by typing
  ``freeride/free``, ``freeride/fast``, etc. in Claude Code's
  ``/model`` slash command.
- ``claude-*`` with NO auth header: graceful fallback to free
  providers. Lets users who never ran ``claude login`` still get a
  response when they wire up the gateway.

This module is pure: it returns a ``RoutingDecision`` based on the
model id and whether an auth header is present. The route handler
acts on the decision. No I/O, no provider lookups — those happen
downstream once the decision is made.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


# ─── presets ────────────────────────────────────────────────────────


# Each preset is a hint to the smart-router about which free provider
# family to prefer. The actual selection still goes through health-
# ranked failover so the user always gets *some* response.
#
# Today the smart-router doesn't read these hints — it picks by health
# alone. Once `freeride audit-models` matures we'll wire the preset
# into provider scoring so "freeride/coding" routes to a code-tuned
# free model first, etc. For now the preset just gets stamped on the
# resolved model id so it shows up in telemetry and the user sees
# their intent reflected back.
PRESET_FREE = "free"
PRESET_FAST = "fast"
PRESET_QUALITY = "quality"
PRESET_CODING = "coding"

_KNOWN_PRESETS = {PRESET_FREE, PRESET_FAST, PRESET_QUALITY, PRESET_CODING}


# ─── decision type ──────────────────────────────────────────────────


@dataclass(frozen=True)
class RoutingDecision:
    """What the route handler should do with this request.

    Attributes
    ----------
    mode
        ``"passthrough"`` — forward to api.anthropic.com with the
        inbound auth header.
        ``"free"`` — translate + route through FreeRide's failover
        chain to a free provider.
    preset
        For ``"free"`` mode, which preset the user picked
        (``free`` / ``fast`` / ``quality`` / ``coding``). For
        ``"passthrough"`` mode, always None.
    reason
        Human-readable string explaining the decision. Surfaced in
        telemetry + the doctor probe so users can see WHY a request
        routed the way it did.
    """

    mode: Literal["passthrough", "free"]
    preset: str | None
    reason: str


# ─── classification ─────────────────────────────────────────────────


def _normalize_model_id(model_id: str) -> str:
    """Trim and lowercase the bit we match on. We don't lowercase the
    full id (the caller may expect the original case echoed back) —
    just normalize for the prefix decision.
    """
    return (model_id or "").strip()


def is_anthropic_model(model_id: str) -> bool:
    """``claude-*`` (any case, leading/trailing whitespace tolerated).

    We deliberately match a permissive prefix — Anthropic ships new
    model ids regularly (``claude-opus-4-5``, ``claude-sonnet-4-6``,
    ``claude-haiku-4-5``, future ``claude-5-*``…) and we'd rather
    transparently passthrough than gate on a hardcoded allowlist.
    """
    return _normalize_model_id(model_id).lower().startswith("claude-")


def parse_freeride_model(model_id: str) -> str | None:
    """Return the preset name for a ``freeride/<preset>`` id, or None
    if it's not a freeride-prefixed id.

    Unknown presets (e.g. ``freeride/banana``) fall back to ``free``
    silently — the user clearly meant *something* free, and surfacing
    a 400 here would feel hostile when the alternative is "just route
    smart". The reason string records the fallback so it shows in
    telemetry.
    """
    norm = _normalize_model_id(model_id).lower()
    if not norm.startswith("freeride/"):
        return None
    preset = norm[len("freeride/"):].strip()
    if not preset:
        return PRESET_FREE
    if preset in _KNOWN_PRESETS:
        return preset
    # Unknown preset — fall back to the default. We don't return None
    # because that would signal "not a freeride id", which is wrong.
    return PRESET_FREE


def has_inbound_auth(headers: dict | None) -> bool:
    """Did the caller send a credential we can relay to Anthropic?

    Anthropic accepts BOTH ``Authorization: Bearer <oauth-token>``
    (Claude Code subscription flow) AND ``x-api-key: <key>`` (direct
    API key flow). Either header counts. Empty values don't count.

    Header keys in FastAPI are lowercased; we accept any case the
    caller used.
    """
    if not headers:
        return False
    # Normalize keys to lowercase for the lookup; values are passed
    # through to upstream as-is.
    norm = {k.lower(): v for k, v in headers.items() if v}
    if norm.get("authorization", "").strip():
        return True
    if norm.get("x-api-key", "").strip():
        return True
    return False


# ─── main entry ─────────────────────────────────────────────────────


def decide(model_id: str, headers: dict | None) -> RoutingDecision:
    """Decide where this ``/v1/messages`` request should go.

    Priority:

    1. ``freeride/<preset>`` → free mode, even if auth is present.
       Users who type ``freeride/free`` are explicitly opting OUT of
       their subscription for this request; respect that.
    2. ``claude-*`` + auth header present → passthrough. Native
       subscription works untouched.
    3. ``claude-*`` + no auth → free fallback. Gateway becomes useful
       without a subscription.
    4. Anything else (OpenAI ids, custom strings) → free. Same path
       as today's ``/v1/chat/completions`` — keeps non-Anthropic
       clients working through ``/v1/messages``.
    """
    # 1. Explicit freeride/* opt-in
    preset = parse_freeride_model(model_id)
    if preset is not None:
        return RoutingDecision(
            mode="free",
            preset=preset,
            reason=f"user-selected preset {preset!r}",
        )

    # 2. claude-* + auth: passthrough
    if is_anthropic_model(model_id):
        if has_inbound_auth(headers):
            return RoutingDecision(
                mode="passthrough",
                preset=None,
                reason="claude-* id with caller auth — relaying to Anthropic",
            )
        # 3. claude-* + no auth: free fallback
        return RoutingDecision(
            mode="free",
            preset=PRESET_FREE,
            reason=(
                "claude-* id but no Authorization/x-api-key on request — "
                "falling back to free providers"
            ),
        )

    # 4. Default — free route (covers OpenAI ids, unknown ids, etc.)
    return RoutingDecision(
        mode="free",
        preset=PRESET_FREE,
        reason="non-Anthropic model id — routing to free providers",
    )
