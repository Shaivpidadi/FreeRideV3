"""``freeride bind hermes`` — point Hermes Agent at the gateway.

Per ``docs/hermes.md``: ``NousResearch/hermes-agent`` ships a
first-class ``provider: "custom"`` mode for "Any other OpenAI-compatible
endpoint" with a ``base_url:`` field. The repo distributes a sample
file as ``cli-config.yaml.example``, but the canonical install reads
**``~/.hermes/config.yaml``** (per
``hermes_cli/config.py`` — ``return get_hermes_home() / "config.yaml"``).
The example filename is misleading; verified by running hermes against
both paths in Phase 4 e2e debugging.

We also write a ``~/.hermes/.env`` with ``LM_API_KEY`` so hermes's
auth-resolver doesn't refuse to start with "no inference provider
configured" — even with ``provider: custom`` set, hermes wants *some*
key to be present.

Closes v2 issue #11.

We do line-based YAML edits so we don't need PyYAML and so the user's
comments and unrelated keys (``provider_routing``, ``providers:``
overrides, ``platform_toolsets``, ``human_delay`` etc.) round-trip
verbatim.
"""

from __future__ import annotations

from pathlib import Path

from freeride.core.state import atomic_write


_DEFAULT_PATH = Path.home() / ".hermes" / "config.yaml"
_DEFAULT_ENV_PATH = Path.home() / ".hermes" / ".env"


def _set_under_model(lines: list[str], key: str, value: str) -> list[str]:
    """Insert or replace ``key: value`` inside the top-level ``model:`` block.

    Hermes' YAML uses 2-space indentation. We treat any line under
    ``model:`` indented by 2 spaces as a member of the block. If the key
    is missing we insert it just after ``model:``.
    """
    out: list[str] = []
    in_model = False
    inserted = False
    saw_existing = False

    for i, line in enumerate(lines):
        stripped_left = line.lstrip(" ")
        indent = len(line) - len(stripped_left)
        # Top-level block detection
        if not line.startswith(" ") and line.rstrip().endswith(":"):
            if line.startswith("model:"):
                in_model = True
                out.append(line)
                # Insert immediately if the key isn't already present
                # in this block (we'll find out as we walk; for now,
                # defer the insert to the end of the block).
                continue
            else:
                if in_model and not inserted and not saw_existing:
                    out.append(f"  {key}: {value}")
                    inserted = True
                in_model = False

        if in_model and indent == 2 and stripped_left.startswith(f"{key}:"):
            out.append(f"  {key}: {value}")
            saw_existing = True
            inserted = True
            continue

        out.append(line)

    if not inserted:
        if in_model:
            out.append(f"  {key}: {value}")
        else:
            # No model: block at all — create one.
            out.append("model:")
            out.append(f"  {key}: {value}")
    return out


def _ensure_env_key(env_path: Path, api_key: str) -> None:
    """Add LM_API_KEY=<api_key> to ~/.hermes/.env if no API key is present.

    Hermes's auth resolver refuses to start when no provider key is set,
    even when the YAML says ``provider: "custom"``. We use ``LM_API_KEY``
    because it's the most generic name in hermes's allowed-key list and
    matches the local-server / vllm convention; we never overwrite a real
    user key (OPENROUTER_API_KEY etc.) if one is already in .env.
    """
    env_path.parent.mkdir(parents=True, exist_ok=True)
    existing = env_path.read_text() if env_path.exists() else ""
    interesting = (
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "LM_API_KEY",
        "ANTHROPIC_API_KEY",
        "NOUS_API_KEY",
    )
    for needle in interesting:
        if any(line.split("=", 1)[0].strip() == needle for line in existing.splitlines()):
            return  # user already has a key; don't clobber
    new_content = existing.rstrip() + ("\n" if existing else "") + f"LM_API_KEY={api_key}\n"
    atomic_write(env_path, new_content)


def bind(
    gateway_url: str,
    *,
    api_key: str = "any",
    default_model: str = "free",
    config_path: Path | None = None,
    env_path: Path | None = None,
) -> str:
    path = config_path or _DEFAULT_PATH
    env_p = env_path or (path.parent / ".env")
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text().splitlines() if path.exists() else []

    lines = _set_under_model(lines, "provider", '"custom"')
    lines = _set_under_model(lines, "base_url", f'"{gateway_url}"')
    lines = _set_under_model(lines, "api_key", f'"{api_key}"')
    lines = _set_under_model(lines, "default", f'"{default_model}"')

    atomic_write(path, "\n".join(lines) + "\n")
    _ensure_env_key(env_p, api_key)

    return (
        f"Hermes config at {path} updated.\n"
        f"  model.provider: \"custom\"\n"
        f"  model.base_url: {gateway_url}\n"
        f"  model.default: {default_model}\n"
        f"  + {env_p}: LM_API_KEY ensured (won't overwrite existing keys)\n"
        f"  Restart hermes for changes to take effect."
    )
