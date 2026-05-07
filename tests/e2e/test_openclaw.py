"""End-to-end: OpenClaw schema-validity check via `openclaw doctor`.

OpenClaw's real interaction surface is messaging channels (WhatsApp,
Telegram, Discord, dashboard); a fully-headless "send a prompt, get a
response" CLI flow requires writing OpenClaw's encrypted
``auth-profiles.json`` store directly and there's no public CLI to
populate that without an interactive wizard. So our e2e gate is the
next-strongest property: **OpenClaw's own doctor accepts the config
our binder wrote** with no config-shape errors.

This catches the most common regression — a binder write that produces
a schema-invalid file — which is exactly what happened on the first
v3 cut (we wrote ``base_url`` directly under the auth profile, which
OpenClaw rejected as "Unrecognized keys").
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from freeride.binders import openclaw as openclaw_binder
from tests.e2e.conftest import require_openclaw


pytestmark = [pytest.mark.e2e, pytest.mark.timeout(60)]


# Wording that openclaw doctor uses to surface config-shape errors.
# Update if a future OpenClaw version changes the diagnostic.
_OPENCLAW_CONFIG_ERROR_NEEDLES = (
    "unrecognized keys",
    "invalid config at",
    "config error",
    "config invalid",
    "schema",  # generic; any schema-validation diagnostic
)


def test_openclaw_config_passes_doctor(gateway_url: str, tmp_path: Path, monkeypatch):
    require_openclaw()

    # Drive openclaw with an isolated config dir so we don't touch
    # the developer's real ~/.openclaw.
    fake_state = tmp_path / "openclaw"
    fake_state.mkdir()
    cfg_path = fake_state / "openclaw.json"
    cfg_path.write_text("{}")

    openclaw_binder.bind(gateway_url, api_key="any", config_path=cfg_path)

    written = json.loads(cfg_path.read_text())
    # Sanity: the binder wrote the schema-valid shape we expect
    prov = written["models"]["providers"]["freeride"]
    assert prov["baseUrl"] == gateway_url
    assert prov["apiKey"] == "any"
    assert written["auth"]["profiles"]["freeride:default"]["provider"] == "freeride"
    assert written["agents"]["defaults"]["model"]["primary"] == "freeride/free"

    # Run openclaw doctor against our config dir. We don't assert on
    # exit code (doctor surfaces unrelated warnings like "gateway.mode
    # not set" that we don't care about) — only that no config-shape
    # diagnostic appears.
    result = subprocess.run(
        ["openclaw", "doctor"],
        env={
            **os.environ,
            "OPENCLAW_STATE_DIR": str(fake_state),
            "OPENCLAW_CONFIG_PATH": str(cfg_path),
        },
        capture_output=True,
        text=True,
        timeout=20,
    )
    output = (result.stdout + "\n" + result.stderr).lower()
    for needle in _OPENCLAW_CONFIG_ERROR_NEEDLES:
        assert needle not in output, (
            f"openclaw doctor surfaced a config-shape error containing "
            f"'{needle}':\n{result.stdout}\n{result.stderr}"
        )
