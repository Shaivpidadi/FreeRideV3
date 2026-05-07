"""``freeride bind hermes`` — point Hermes Agent at the gateway.

Per ``knowledge/HERMES.md``: ``NousResearch/hermes-agent`` ships a
first-class ``provider: "custom"`` mode in ``~/.hermes/cli-config.yaml``
explicitly documented as "Any other OpenAI-compatible endpoint" with a
``base_url:`` field (verified against repo's ``cli-config.yaml.example``
line 36).

Closes v2 issue #11.

We do line-based YAML edits so we don't need PyYAML and so the user's
comments and unrelated keys (``provider_routing``, ``providers:``
overrides, ``platform_toolsets``, ``human_delay`` etc.) round-trip
verbatim.
"""

from __future__ import annotations

from pathlib import Path

from freeride.core.state import atomic_write


_DEFAULT_PATH = Path.home() / ".hermes" / "cli-config.yaml"


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


def bind(
    gateway_url: str,
    *,
    api_key: str = "any",
    default_model: str = "free",
    config_path: Path | None = None,
) -> str:
    path = config_path or _DEFAULT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text().splitlines() if path.exists() else []

    lines = _set_under_model(lines, "provider", '"custom"')
    lines = _set_under_model(lines, "base_url", f'"{gateway_url}"')
    lines = _set_under_model(lines, "api_key", f'"{api_key}"')
    lines = _set_under_model(lines, "default", f'"{default_model}"')

    atomic_write(path, "\n".join(lines) + "\n")
    return (
        f"Hermes config at {path} updated.\n"
        f"  model.provider: \"custom\"\n"
        f"  model.base_url: {gateway_url}\n"
        f"  model.default: {default_model}\n"
        f"  Restart hermes for changes to take effect."
    )
