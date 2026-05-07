"""v2 CLI command implementations.

Each function maps 1:1 to a v2 ``cmd_*`` and produces v2's exact stdout
shape — required for the parity gate at Phase 1.8. Any improvement to
UX waits for Phase 5+.

The functions take an ``argparse.Namespace`` exactly like v2 did.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime

from freeride.core.cooldown import KeyCooldown
from freeride.providers.openrouter import (
    OPENROUTER_CHAT_URL,
)
from freeride.v2compat.models import (
    CACHE_DURATION_HOURS,
    CACHE_FILE,
    _openrouter_headers,
    get_api_keys,
    get_free_models,
)
from freeride.v2compat.openclaw import (
    OPENCLAW_CONFIG_PATH,
    ensure_config_structure,
    format_model_for_openclaw,
    get_current_fallbacks,
    get_current_model,
    load_openclaw_config,
    save_openclaw_config,
    stored_to_api_id,
    update_model_config,
)


_PROVIDER_NAME = "openrouter"


def _test_model(model_id: str, *, cooldown: KeyCooldown | None = None, timeout: float = 30.0) -> tuple[bool, str | None]:
    """v2-style probe: rotate across keys, return (ok, error_string).

    Error strings preserve v2's vocabulary: ``all_keys_exhausted``,
    ``model_not_found``, ``unavailable``, ``timeout``, ``request_error``,
    ``error_<status>``. The rotate() printer compares to these literals.
    """
    import httpx

    cd = cooldown or KeyCooldown()
    available = [k for k in get_api_keys() if not cd.is_in_cooldown(_PROVIDER_NAME, k)]
    if not available:
        return False, "all_keys_exhausted"

    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": "Hi"}],
        "max_tokens": 5,
        "stream": False,
    }
    for key in available:
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.post(OPENROUTER_CHAT_URL, headers=_openrouter_headers(key), json=payload)
        except httpx.TimeoutException:
            return False, "timeout"
        except httpx.RequestError:
            return False, "request_error"

        if response.status_code == 200:
            return True, None
        if response.status_code in (401, 429):
            cd.mark_rate_limited(_PROVIDER_NAME, key)
            continue
        if response.status_code == 503:
            return False, "unavailable"

        # Body inspection — accept multiple OpenRouter shapes; v3 found
        # an additional pattern not in v2 ("is not a valid model ID").
        try:
            body = response.json()
            err = body.get("error", {}) if isinstance(body, dict) else {}
            err_code = err.get("code", "")
            err_msg = str(err.get("message", ""))
            if err_code == "model_not_found":
                return False, "model_not_found"
            for needle in ("Unknown model", "model_not_found", "not a valid model"):
                if needle in err_msg:
                    return False, "model_not_found"
        except (ValueError, KeyError):
            pass
        return False, f"error_{response.status_code}"

    return False, "all_keys_exhausted"


def rotate(*, force: bool = False, fallback_count: int = 5) -> tuple[bool, str | None]:
    """Live-test the current primary; swap to a verified working model if it fails.

    Direct port of v2 ``rotate``. Returns ``(changed, error)`` — ``changed``
    is True iff the config was rewritten; ``error`` is set when nothing
    could be done.
    """
    if not get_api_keys():
        return False, "no_keys"

    config = load_openclaw_config()
    config = ensure_config_structure(config)
    current = get_current_model(config)
    current_base = stored_to_api_id(current) if current else None

    if current_base and not force:
        print(f"Testing current primary: {current_base}")
        ok, err = _test_model(current_base)
        if ok:
            print("  Status: OK — no rotation needed.")
            return False, None
        print(f"  Status: {err}")

    print("Finding a working free model...")
    models = get_free_models(force_refresh=True)
    if not models:
        return False, "fetch_failed"

    fallback_target = max(0, fallback_count - 1)
    new_primary: str | None = None
    verified_fallbacks: list[str] = []

    for m in models:
        model_id = m["id"]
        if "openrouter/free" in model_id:
            continue
        if model_id == current_base:
            continue
        ok, err = _test_model(model_id)
        if ok:
            if new_primary is None:
                new_primary = model_id
                print(f"  Verified primary: {model_id}")
            else:
                verified_fallbacks.append(model_id)
                print(f"  Verified fallback: {model_id}")
            if len(verified_fallbacks) >= fallback_target:
                break
        elif err == "all_keys_exhausted":
            print("  Stopped: all API keys are rate-limited or invalid.")
            break
        # Silent on per-model failures.

    if not new_primary:
        return False, "no_working_models"

    formatted = format_model_for_openclaw(new_primary)
    config["agents"]["defaults"]["model"]["primary"] = formatted
    config["agents"]["defaults"]["models"][formatted] = {}

    smart_router = format_model_for_openclaw("openrouter/free")
    fallbacks = [smart_router]
    config["agents"]["defaults"]["models"][smart_router] = {}
    for fb_id in verified_fallbacks:
        fb_fmt = format_model_for_openclaw(fb_id)
        fallbacks.append(fb_fmt)
        config["agents"]["defaults"]["models"][fb_fmt] = {}

    config["agents"]["defaults"]["model"]["fallbacks"] = fallbacks
    save_openclaw_config(config)

    print(f"Done. Primary: {formatted}")
    print(f"Fallbacks ({len(fallbacks)}):")
    for fb in fallbacks:
        print(f"  - {fb}")
    return True, None


# ---- CLI command handlers (v2 stdout shape) -------------------------------


def _free_models_provider():
    """Adapter so update_model_config gets v2-shape dicts."""
    return get_free_models()


def cmd_list(args) -> None:
    if not get_api_keys():
        print("Error: OPENROUTER_API_KEY not set")
        print("Set it via: export OPENROUTER_API_KEY='sk-or-...'")
        print("Or get a free key at: https://openrouter.ai/keys")
        sys.exit(1)

    print("Fetching free models from OpenRouter...")
    models = get_free_models(force_refresh=args.refresh)
    if not models:
        print("No free models available.")
        return

    current = get_current_model()
    fallbacks = get_current_fallbacks()
    limit = args.limit if args.limit else 15

    print(f"\nTop {min(limit, len(models))} Free AI Models (ranked by quality):\n")
    print(f"{'#':<3} {'Model ID':<50} {'Context':<12} {'Score':<8} {'Status'}")
    print("-" * 90)
    for i, model in enumerate(models[:limit], 1):
        model_id = model.get("id", "unknown")
        context = model.get("context_length", 0)
        score = model.get("_score", 0)

        if context >= 1_000_000:
            context_str = f"{context // 1_000_000}M tokens"
        elif context >= 1_000:
            context_str = f"{context // 1_000}K tokens"
        else:
            context_str = f"{context} tokens"

        formatted = format_model_for_openclaw(model_id)
        if current and formatted == current:
            status = "[PRIMARY]"
        elif formatted in fallbacks:
            status = "[FALLBACK]"
        else:
            status = ""
        print(f"{i:<3} {model_id:<50} {context_str:<12} {score:.3f}    {status}")

    if len(models) > limit:
        print(f"\n... and {len(models) - limit} more. Use --limit to see more.")
    print(f"\nTotal free models available: {len(models)}")
    print("\nCommands:")
    print("  freeride switch <model>      Set as primary model")
    print("  freeride switch <model> -f   Add to fallbacks only (keep current primary)")
    print("  freeride auto                Auto-select best model")


def cmd_switch(args) -> None:
    if not get_api_keys():
        print("Error: OPENROUTER_API_KEY not set")
        sys.exit(1)

    model_id = args.model
    as_fallback = args.fallback_only
    models = get_free_models()
    model_ids = [m["id"] for m in models]

    matched = None
    if model_id in model_ids:
        matched = model_id
    else:
        for m_id in model_ids:
            if model_id.lower() in m_id.lower():
                matched = m_id
                break
    if not matched:
        print(f"Error: Model '{model_id}' not found in free models list.")
        print("Use 'freeride list' to see available models.")
        sys.exit(1)

    print(f"{'Adding to fallbacks' if as_fallback else 'Setting as primary'}: {matched}")

    update_model_config(
        matched,
        free_models_provider=_free_models_provider,
        api_keys=get_api_keys(),
        as_primary=not as_fallback,
        add_fallbacks=not args.no_fallbacks,
        setup_auth=args.setup_auth,
        append_free=False,
    )
    config = load_openclaw_config()
    if as_fallback:
        print("Success! Added to fallbacks.")
        print(f"Primary model (unchanged): {get_current_model(config)}")
    else:
        print("Success! OpenClaw config updated.")
        print(f"Primary model: {get_current_model(config)}")
    fallbacks = get_current_fallbacks(config)
    if fallbacks:
        print(f"Fallback models ({len(fallbacks)}):")
        for fb in fallbacks[:5]:
            print(f"  - {fb}")
        if len(fallbacks) > 5:
            print(f"  ... and {len(fallbacks) - 5} more")
    print("\nRestart OpenClaw for changes to take effect.")


def cmd_auto(args) -> None:
    if not get_api_keys():
        print("Error: OPENROUTER_API_KEY not set")
        sys.exit(1)

    config = load_openclaw_config()
    current_primary = get_current_model(config)

    print("Finding best free model...")
    models = get_free_models(force_refresh=True)
    if not models:
        print("Error: No free models available.")
        sys.exit(1)

    best = next((m for m in models if "openrouter/free" not in m["id"]), models[0])
    model_id = best["id"]
    context = best.get("context_length", 0)
    score = best.get("_score", 0)
    as_fallback = args.fallback_only

    if not as_fallback:
        if current_primary:
            print(f"\nReplacing current primary: {current_primary}")
        print(f"\nBest free model: {model_id}")
        print(f"Context length: {context:,} tokens")
        print(f"Quality score: {score:.3f}")
    else:
        print("\nKeeping current primary, adding fallbacks only.")
        print(f"Best available: {model_id} ({context:,} tokens, score: {score:.3f})")

    update_model_config(
        model_id,
        free_models_provider=_free_models_provider,
        api_keys=get_api_keys(),
        as_primary=not as_fallback,
        add_fallbacks=True,
        fallback_count=args.fallback_count,
        setup_auth=args.setup_auth,
    )
    config = load_openclaw_config()
    if as_fallback:
        print("\nFallbacks configured!")
        print(f"Primary (unchanged): {get_current_model(config)}")
        print("First fallback: openrouter/free (smart router - auto-selects best available)")
    else:
        print("\nOpenClaw config updated!")
        print(f"Primary: {get_current_model(config)}")
    fallbacks = get_current_fallbacks(config)
    if fallbacks:
        print(f"Fallbacks ({len(fallbacks)}):")
        for fb in fallbacks:
            print(f"  - {fb}")
    print("\nRestart OpenClaw for changes to take effect.")


def cmd_status(args) -> None:
    keys = get_api_keys()
    config = load_openclaw_config()
    current = get_current_model(config)
    fallbacks = get_current_fallbacks(config)

    print("FreeRide Status")
    print("=" * 50)
    if keys:
        if len(keys) == 1:
            k = keys[0]
            masked = k[:8] + "..." + k[-4:] if len(k) > 12 else "***"
            print(f"OpenRouter API Key: {masked}")
        else:
            print(f"OpenRouter API Keys: {len(keys)} configured")
            for i, k in enumerate(keys, 1):
                masked = k[:8] + "..." + k[-4:] if len(k) > 12 else "***"
                print(f"  {i}. {masked}")
    else:
        print("OpenRouter API Key: NOT SET")
        print("  Single key: export OPENROUTER_API_KEY='sk-or-...'")
        print("  Multiple:   export OPENROUTER_API_KEY='[\"sk-or-key1\", \"sk-or-key2\"]'")

    auth_profiles = config.get("auth", {}).get("profiles", {})
    if "openrouter:default" in auth_profiles:
        print("OpenRouter Auth Profile: Configured")
    else:
        print("OpenRouter Auth Profile: Not set (use --setup-auth to add)")

    print(f"\nPrimary Model: {current or 'Not configured'}")
    if fallbacks:
        print(f"Fallback Models ({len(fallbacks)}):")
        for fb in fallbacks:
            print(f"  - {fb}")
    else:
        print("Fallback Models: None configured")

    if CACHE_FILE.exists():
        try:
            cache = json.loads(CACHE_FILE.read_text())
            cached_at = datetime.fromisoformat(cache.get("cached_at", ""))
            count = len(cache.get("models", []))
            age = datetime.now() - cached_at
            hours = age.seconds // 3600
            mins = (age.seconds % 3600) // 60
            print(f"\nModel Cache: {count} models (updated {hours}h {mins}m ago)")
        except (json.JSONDecodeError, ValueError, KeyError):
            print("\nModel Cache: Invalid")
    else:
        print("\nModel Cache: Not created yet")

    print(f"\nOpenClaw Config: {OPENCLAW_CONFIG_PATH}")
    print(f"  Exists: {'Yes' if OPENCLAW_CONFIG_PATH.exists() else 'No'}")


def cmd_refresh(args) -> None:
    if not get_api_keys():
        print("Error: OPENROUTER_API_KEY not set")
        sys.exit(1)
    print("Refreshing free models cache...")
    models = get_free_models(force_refresh=True)
    print(f"Cached {len(models)} free models.")
    print(f"Cache expires in {CACHE_DURATION_HOURS} hours.")


def cmd_fallbacks(args) -> None:
    if not get_api_keys():
        print("Error: OPENROUTER_API_KEY not set")
        sys.exit(1)

    config = load_openclaw_config()
    current = get_current_model(config)
    if not current:
        print("Warning: No primary model configured.")
        print("Fallbacks will still be added.")
    print(f"Current primary: {current or 'None'}")
    print(f"Setting up {args.count} fallback models...")

    models = get_free_models()
    config = ensure_config_structure(config)
    fallbacks: list[str] = []

    smart_router = format_model_for_openclaw("openrouter/free")
    if not current or current != smart_router:
        fallbacks.append(smart_router)
        config["agents"]["defaults"]["models"][smart_router] = {}

    for m in models:
        formatted = format_model_for_openclaw(m["id"])
        if current and formatted == current:
            continue
        if "openrouter/free" in m["id"]:
            continue
        if len(fallbacks) >= args.count:
            break
        fallbacks.append(formatted)
        config["agents"]["defaults"]["models"][formatted] = {}

    config["agents"]["defaults"]["model"]["fallbacks"] = fallbacks
    save_openclaw_config(config)
    print(f"\nConfigured {len(fallbacks)} fallback models:")
    for i, fb in enumerate(fallbacks, 1):
        print(f"  {i}. {fb}")
    print("\nWhen rate limited, OpenClaw will automatically try these models.")
    print("Restart OpenClaw for changes to take effect.")


def cmd_rotate(args) -> None:
    if not get_api_keys():
        print("Error: OPENROUTER_API_KEY not set")
        sys.exit(1)

    changed, err = rotate(force=args.force, fallback_count=args.fallback_count)
    if err == "fetch_failed":
        print("Error: could not fetch free model list (all keys exhausted?).")
        sys.exit(1)
    if err == "no_working_models":
        print("Error: no working free models found.")
        sys.exit(1)
    if changed:
        print("\nRestart OpenClaw for changes to take effect.")
    elif not err:
        print("  (Use --force to rotate anyway.)")
