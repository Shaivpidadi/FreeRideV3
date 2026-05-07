"""``freeride bind aider`` — point Aider at the gateway.

Per ``docs/agent-binders.md``: Aider's config search order is
git-root → cwd → home, so we default to the home-scoped
``~/.aider.conf.yml``. The user can pass a ``scope`` to force one of
the other locations.

Aider requires a restart after config changes (no hot reload).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from freeride.core.state import atomic_write


Scope = Literal["home", "cwd", "git"]


def _aider_config_path(scope: Scope) -> Path:
    if scope == "home":
        return Path.home() / ".aider.conf.yml"
    if scope == "cwd":
        return Path.cwd() / ".aider.conf.yml"
    # git: walk up from cwd looking for a .git directory
    cur = Path.cwd().resolve()
    while True:
        if (cur / ".git").exists():
            return cur / ".aider.conf.yml"
        if cur.parent == cur:
            # No git root found; fall back to cwd.
            return Path.cwd() / ".aider.conf.yml"
        cur = cur.parent


def _read_yaml_lines(path: Path) -> list[str]:
    """Read existing aider config preserving all other lines.

    We avoid pulling in PyYAML by doing line-based edits — Aider's
    config file is a flat key:value YAML, no nested structures. This
    keeps the binder dep-free.
    """
    if not path.exists():
        return []
    return path.read_text().splitlines()


def _set_or_append(lines: list[str], key: str, value: str) -> list[str]:
    """Replace the first matching ``key:`` line, or append at end."""
    out: list[str] = []
    seen = False
    for line in lines:
        if not seen and line.lstrip().startswith(f"{key}:"):
            out.append(f"{key}: {value}")
            seen = True
        else:
            out.append(line)
    if not seen:
        out.append(f"{key}: {value}")
    return out


def bind(
    gateway_url: str,
    *,
    api_key: str = "any",
    scope: Scope = "home",
    default_model: str = "openai/openrouter/free",
    config_path: Path | None = None,
) -> str:
    """Write Aider's three load-bearing keys: openai-api-base, openai-api-key,
    and `model:` so `aider` (no flags) just works.

    The default model uses Aider's ``openai/`` prefix syntax — this tells
    Aider to use its OpenAI-compatible client (which we are, via the
    gateway). The rest of the model id (``openrouter/free``) is what the
    gateway resolves to a free model on the configured provider chain.
    """
    path = config_path or _aider_config_path(scope)
    lines = _read_yaml_lines(path)
    lines = _set_or_append(lines, "openai-api-base", gateway_url)
    lines = _set_or_append(lines, "openai-api-key", api_key)
    lines = _set_or_append(lines, "model", default_model)

    atomic_write(path, "\n".join(lines) + "\n")
    return (
        f"Aider config at {path} updated.\n"
        f"  openai-api-base: {gateway_url}\n"
        f"  openai-api-key: {api_key}\n"
        f"  model: {default_model}\n"
        f"  Aider has no hot-reload — restart aider for changes to take effect."
    )
