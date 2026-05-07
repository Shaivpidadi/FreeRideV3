"""End-to-end: real Hermes Agent subprocess against the gateway.

Skipped automatically when ``hermes`` isn't on PATH. Install via:

    pip install hermes-agent  (or whatever the canonical install is)

Hermes config is written via the binder (preserving any existing user
config); we then run a one-shot ``hermes -p "..."`` and verify a
response landed.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from freeride.binders import hermes as hermes_binder
from tests.e2e.conftest import require_hermes


pytestmark = [pytest.mark.e2e, pytest.mark.timeout(120)]


def test_hermes_one_shot_through_gateway(gateway_url: str, tmp_path: Path):
    require_hermes()

    # Use an isolated HOME so we don't touch the developer's real
    # ~/.hermes/. The binder writes to $HOME/.hermes/config.yaml +
    # $HOME/.hermes/.env.
    fake_home = tmp_path
    hermes_dir = fake_home / ".hermes"
    hermes_dir.mkdir(parents=True, exist_ok=True)
    hermes_binder.bind(
        gateway_url,
        api_key="any",
        config_path=hermes_dir / "config.yaml",
        env_path=hermes_dir / ".env",
    )

    env = {**os.environ, "HOME": str(fake_home)}

    result = subprocess.run(
        [
            "hermes",
            "-z",
            "Reply with one word: ok",
            "--ignore-user-config",  # don't read the developer's real config
            "--yolo",  # no confirmation prompts; fits a non-tty subprocess
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    # Acceptance: exit 0 + non-empty stdout.
    assert result.returncode == 0, (
        f"hermes exited {result.returncode}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    assert result.stdout.strip(), "hermes produced no output"
