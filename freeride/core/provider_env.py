"""Single registry of provider ↔ env-var mappings.

Every call site that used to hardcode ``OPENROUTER_API_KEY`` /
``HF_TOKEN`` / etc. reads from here: failover chain construction,
``freeride keys``, ``freeride doctor``, ``freeride audit-models``,
``build_provider_registry``, and auto-model resolution.

Third-party plugins that are not in :data:`BUILTIN_PROVIDERS` fall
back to ``{NAME}_API_KEY`` (and ``{NAME}_API_KEY_2``, ``_3``, …).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ProviderEnv:
    """How a built-in provider is wired to the process environment.

    ``env_vars`` is ordered: the first name is canonical, the rest are
    aliases. Any one of them is enough to collect keys (HuggingFace
    accepts ``HF_TOKEN`` *or* ``HUGGINGFACE_API_KEY``).

    ``extra_required`` must *all* be set in addition to a key — Cloudflare
    Workers AI needs ``CLOUDFLARE_ACCOUNT_ID`` in the URL, not as a key.
    """

    name: str
    env_vars: tuple[str, ...]
    extra_required: tuple[str, ...] = ()


BUILTIN_PROVIDERS: tuple[ProviderEnv, ...] = (
    ProviderEnv("openrouter", ("OPENROUTER_API_KEY",)),
    ProviderEnv("groq", ("GROQ_API_KEY",)),
    ProviderEnv("nvidia_nim", ("NVIDIA_API_KEY", "NIM_API_KEY")),
    ProviderEnv(
        "cloudflare_wai",
        ("CLOUDFLARE_API_TOKEN",),
        extra_required=("CLOUDFLARE_ACCOUNT_ID",),
    ),
    ProviderEnv("huggingface", ("HF_TOKEN", "HUGGINGFACE_API_KEY")),
    ProviderEnv("cerebras", ("CEREBRAS_API_KEY",)),
    ProviderEnv("ollama", ("OLLAMA_BASE_URL",)),
)

_BY_NAME: dict[str, ProviderEnv] = {p.name: p for p in BUILTIN_PROVIDERS}


def spec_for(provider_name: str) -> ProviderEnv:
    """Return the built-in spec, or a synthetic ``{NAME}_API_KEY`` spec
    for third-party plugins."""
    found = _BY_NAME.get(provider_name)
    if found is not None:
        return found
    return ProviderEnv(provider_name, (f"{provider_name.upper()}_API_KEY",))


def env_var_for(provider_name: str) -> str:
    """Canonical env-var name (the one docs and error messages cite)."""
    return spec_for(provider_name).env_vars[0]


def parse_api_keys(raw: Any) -> list[str]:
    """Single key string, JSON-array literal, or a real Python list.

    Shared by the gateway, the CLI, and v2compat. Empty / whitespace
    entries are dropped.
    """
    if isinstance(raw, list):
        return [k.strip() for k in raw if isinstance(k, str) and k.strip()]
    if not isinstance(raw, str):
        return []
    raw = raw.strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            keys = json.loads(raw)
            if isinstance(keys, list):
                return [k.strip() for k in keys if isinstance(k, str) and k.strip()]
        except (json.JSONDecodeError, ValueError):
            pass
    return [raw]


def _collect_from_name(env_name: str) -> list[str]:
    """Primary value plus numbered suffixes ``NAME_2``, ``NAME_3``, …

    README documents ``OPENROUTER_API_KEY_2`` / ``_3`` as the multi-key
    form alongside the JSON-array form.
    """
    out: list[str] = []
    seen: set[str] = set()

    def _add(raw: str) -> None:
        for k in parse_api_keys(raw):
            if k not in seen:
                seen.add(k)
                out.append(k)

    _add(os.environ.get(env_name, ""))
    n = 2
    while True:
        extra = os.environ.get(f"{env_name}_{n}", "")
        if not extra:
            break
        _add(extra)
        n += 1
    return out


def all_keys_for(provider_name: str) -> list[str]:
    """Every configured secret (or Ollama URL) for ``provider_name``.

    Walks aliases and numbered suffixes. Order is: canonical env var
    (plus its ``_2``/``_3``), then each alias the same way. Duplicates
    across aliases are dropped, first-seen wins.
    """
    spec = spec_for(provider_name)
    for env_name in spec.env_vars:
        keys = _collect_from_name(env_name)
        if keys:
            return keys
    return []


def is_configured(provider_name: str) -> bool:
    """True when the provider has at least one key *and* every
    ``extra_required`` var is set. Used by ``build_provider_registry``."""
    spec = spec_for(provider_name)
    if not all_keys_for(provider_name):
        return False
    return all(os.environ.get(v) for v in spec.extra_required)
