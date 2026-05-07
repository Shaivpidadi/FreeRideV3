"""End-to-end: OpenClaw integration check.

OpenClaw's real interaction surface is messaging channels (WhatsApp,
Telegram, Discord, dashboard) — there's no obvious headless "send a
prompt, get a response" CLI mode that's pytest-friendly. This test is
intentionally minimal:

* Run the binder against a tmp OpenClaw config
* Assert the config file landed in the v3-expected shape
* Run ``openclaw doctor`` (if available) to confirm OpenClaw can parse
  the config we wrote

Anything beyond that — sending a real chat message and observing the
agent reply — requires the messaging-channel infra to be wired up,
which is out of scope for this pytest suite.
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


def test_openclaw_config_round_trip(gateway_url: str, tmp_path: Path):
    """Smoke: bind writes a v3 config that OpenClaw's doctor accepts."""
    require_openclaw()

    cfg_path = tmp_path / "openclaw.json"
    cfg_path.write_text("{}")
    openclaw_binder.bind(gateway_url, api_key="any", config_path=cfg_path)

    written = json.loads(cfg_path.read_text())
    assert written["auth"]["profiles"]["freeride:default"]["base_url"] == gateway_url
    assert written["agents"]["defaults"]["model"]["primary"] == "freeride/free"

    # If OpenClaw has a `doctor` subcommand, run it against our config
    # to confirm shape validity. Skip the assertion if doctor isn't
    # available (some versions don't ship it).
    if shutil.which("openclaw"):
        result = subprocess.run(
            ["openclaw", "doctor", "--config", str(cfg_path)],
            capture_output=True,
            text=True,
            timeout=20,
        )
        # Accept any exit code that doesn't indicate a config-shape
        # parse error. OpenClaw doctor surfaces config errors with
        # specific wording; bail loudly if seen.
        out = result.stdout + result.stderr
        assert "config error" not in out.lower(), (
            f"openclaw doctor flagged the config:\n{out}"
        )
