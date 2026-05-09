"""``freeride audit-models`` — probe every catalog model for current
health and persist results to ``~/.freeride/cache/model_health.json``.

The cache is consumed by :mod:`freeride.core.smart_routing` so
``model: "auto"`` resolution skips known-broken models without
re-probing on the request hot path.

Usage:

  freeride audit-models                  # full audit, all configured providers
  freeride audit-models --workers 8      # tune concurrency
  freeride audit-models --provider groq  # restrict to one provider
  freeride audit-models --quiet          # only the summary line
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from freeride.core.model_health import (
    CACHE_PATH,
    HealthEntry,
    audit_providers,
    load_cache,
    save_cache,
)


_GREEN = "\033[32m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_DIM = "\033[2m"
_RESET = "\033[0m"


# Same env-var matrix the /v1/models route uses; kept here so the CLI
# can run audits without importing the FastAPI route module.
_PROVIDER_ENV_VAR: dict[str, str] = {
    "openrouter": "OPENROUTER_API_KEY",
    "nvidia_nim": "NVIDIA_API_KEY",
    "groq": "GROQ_API_KEY",
    "cloudflare_wai": "CLOUDFLARE_API_TOKEN",
    "huggingface": "HF_TOKEN",
    "cerebras": "CEREBRAS_API_KEY",
    "ollama": "OLLAMA_BASE_URL",
}


def _key_for(provider_name: str) -> str | None:
    raw = os.environ.get(_PROVIDER_ENV_VAR.get(provider_name, ""), "")
    if not raw:
        # Some providers accept multiple env-var aliases; check the
        # alternate for HF.
        if provider_name == "huggingface":
            raw = os.environ.get("HUGGINGFACE_API_KEY", "")
    if not raw:
        return None
    # If multi-key JSON syntax is used, take the first.
    try:
        from freeride.v2compat.models import _parse_api_keys

        keys = _parse_api_keys(raw)
        return keys[0] if keys else None
    except Exception:  # noqa: BLE001
        return raw.strip() or None


def _color(use_color: bool, code: str) -> str:
    return code if use_color else ""


def cmd_audit_models(args: argparse.Namespace) -> int:
    use_color = sys.stdout.isatty() and not getattr(args, "no_color", False)
    G = _color(use_color, _GREEN)
    R = _color(use_color, _RED)
    Y = _color(use_color, _YELLOW)
    D = _color(use_color, _DIM)
    Z = _color(use_color, _RESET)

    # Build the provider registry lazily — same path serve uses.
    from freeride.cli.cmd_serve import build_provider_registry

    providers = build_provider_registry()
    if args.provider:
        providers = [p for p in providers if p.name == args.provider]
        if not providers:
            print(f"{R}error{Z}: no provider named {args.provider!r} is registered")
            return 2

    keys_for: dict[str, str] = {}
    for p in providers:
        k = _key_for(p.name)
        if k:
            keys_for[p.name] = k

    if not keys_for:
        print(f"{R}error{Z}: no provider keys found in env — configure at least one before auditing")
        return 2

    if not args.quiet:
        print(f"{D}auditing {len(keys_for)} provider(s) — fetching catalogs...{Z}")

    # Pre-load existing cache so we can MERGE new probes into it. This
    # matters for --provider runs: re-probing only Cerebras shouldn't
    # wipe the OR / Groq verdicts.
    merged: dict[str, HealthEntry] = load_cache()

    counter = {"seen": 0, "ok": 0, "fail": 0}

    def on_progress(provider: str, model_id: str, entry: HealthEntry) -> None:
        counter["seen"] += 1
        if entry.status == "ok":
            counter["ok"] += 1
            mark = f"{G}✓{Z}"
        else:
            counter["fail"] += 1
            mark = f"{R}✗{Z}"
        if not args.quiet:
            print(
                f"  {mark}  {provider:14s}  "
                f"{entry.status:18s}  "
                f"{D}{entry.latency_ms:>5}ms{Z}  "
                f"{model_id}"
            )

    fresh = audit_providers(
        providers,
        keys_for,
        workers=args.workers,
        on_progress=on_progress,
    )
    merged.update(fresh)
    save_cache(merged)

    # Summary
    total = counter["seen"]
    ok = counter["ok"]
    fail = counter["fail"]
    rate = (ok / total * 100) if total else 0
    if total == 0:
        print(f"{Y}warning{Z}: no models were probed (catalogs empty?)")
        return 0

    if not args.quiet:
        print()
    print(
        f"{G}done{Z}: {ok}/{total} ok ({rate:.0f}%) · {fail} broken · "
        f"cache → {CACHE_PATH}"
    )

    # Per-provider rollup so the user immediately sees which provider
    # is dragging.
    if not args.quiet:
        from collections import Counter

        by_provider: dict[str, Counter[str]] = {}
        for k, v in fresh.items():
            prov, _, _ = k.partition("::")
            by_provider.setdefault(prov, Counter())[v.status] += 1
        print()
        print("by provider:")
        for prov in sorted(by_provider):
            counts = by_provider[prov]
            total_p = sum(counts.values())
            ok_p = counts.get("ok", 0)
            statuses = " · ".join(
                f"{s}={n}" for s, n in counts.most_common() if s != "ok"
            )
            badge = f"{G}{ok_p}/{total_p}{Z}" if ok_p == total_p else f"{Y}{ok_p}/{total_p}{Z}"
            tail = f" · {statuses}" if statuses else ""
            print(f"  {prov:14s}  {badge}{tail}")

    return 0
