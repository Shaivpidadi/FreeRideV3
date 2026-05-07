"""``freeride bind continue`` — append a model entry to Continue's config.

Per ``docs/agent-binders.md``: current Continue uses ``~/.continue/config.yaml``
(legacy ``config.json`` may exist on older installs). The provider type
must be ``openai`` (NOT ``openai-compatible``). Continue hot-reloads on
next prompt — no restart needed.

We support both YAML and JSON paths. If both exist we update YAML
(the current canonical format). If neither exists we create the YAML.
"""

from __future__ import annotations

import json
from pathlib import Path

from freeride.core.state import atomic_write


_DEFAULT_DIR = Path.home() / ".continue"
_YAML_NAME = "config.yaml"
_JSON_NAME = "config.json"

_FREERIDE_TITLE = "freeride"


def _yaml_block(gateway_url: str, api_key: str) -> str:
    """Return a single ``models:`` entry block as YAML — appended to whatever
    list already exists. Avoids requiring PyYAML for the common path.
    """
    return (
        f"  - title: {_FREERIDE_TITLE}\n"
        f"    provider: openai\n"
        f"    model: free\n"
        f"    apiBase: {gateway_url}\n"
        f"    apiKey: {api_key}\n"
        f"    roles: [chat, edit, autocomplete]\n"
    )


def _bind_yaml(path: Path, gateway_url: str, api_key: str) -> None:
    if not path.exists():
        atomic_write(path, "models:\n" + _yaml_block(gateway_url, api_key))
        return

    text = path.read_text()
    # Strip any prior freeride entry (idempotent re-runs).
    if f"title: {_FREERIDE_TITLE}\n" in text or f"title: {_FREERIDE_TITLE}" in text.splitlines()[-1] if text else False:
        # Simple drop of the previous block: split on lines, skip 6
        # consecutive lines starting with "  - title: freeride".
        lines = text.splitlines()
        out: list[str] = []
        skip = 0
        for line in lines:
            if skip > 0:
                skip -= 1
                continue
            if line.strip() == f"- title: {_FREERIDE_TITLE}" or line.strip() == f"- title: {_FREERIDE_TITLE}\n":
                skip = 5  # 5 indented lines after the - title line
                continue
            out.append(line)
        text = "\n".join(out)
        if not text.endswith("\n"):
            text += "\n"

    if "models:" not in text:
        text = (text.rstrip() + "\n" if text else "") + "models:\n"

    # Append the freeride block right after the "models:" line.
    new_text = text.rstrip() + "\n" + _yaml_block(gateway_url, api_key)
    atomic_write(path, new_text)


def _bind_json(path: Path, gateway_url: str, api_key: str) -> None:
    try:
        config = json.loads(path.read_text()) if path.exists() else {}
    except json.JSONDecodeError:
        config = {}

    models = config.setdefault("models", [])
    models = [m for m in models if m.get("title") != _FREERIDE_TITLE]
    models.append(
        {
            "title": _FREERIDE_TITLE,
            "provider": "openai",
            "model": "free",
            "apiBase": gateway_url,
            "apiKey": api_key,
            "roles": ["chat", "edit", "autocomplete"],
        }
    )
    config["models"] = models
    atomic_write(path, json.dumps(config, indent=2))


def bind(
    gateway_url: str,
    *,
    api_key: str = "any",
    config_dir: Path | None = None,
) -> str:
    base = config_dir or _DEFAULT_DIR
    base.mkdir(parents=True, exist_ok=True)
    yaml_path = base / _YAML_NAME
    json_path = base / _JSON_NAME

    if yaml_path.exists() or not json_path.exists():
        # Default modern path: YAML
        _bind_yaml(yaml_path, gateway_url, api_key)
        chosen = yaml_path
    else:
        _bind_json(json_path, gateway_url, api_key)
        chosen = json_path

    return (
        f"Continue config at {chosen} updated.\n"
        f"  Added model: freeride (provider=openai, apiBase={gateway_url})\n"
        f"  Roles: chat, edit, autocomplete\n"
        f"  Continue hot-reloads — no restart needed."
    )
