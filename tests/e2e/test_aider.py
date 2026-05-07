"""End-to-end: real Aider subprocess against the gateway.

Verifies that an existing Aider install can use FreeRide as its
upstream OPENAI_API_BASE with no FreeRide-specific patches. The
acceptance bar is "Aider exits 0 and writes its applied-edit message"
— the model's actual edit quality is irrelevant to the gateway test.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests.e2e.conftest import require_aider


pytestmark = [pytest.mark.e2e, pytest.mark.timeout(120)]


def test_aider_one_shot_through_gateway(gateway_url: str, tmp_path: Path):
    require_aider()

    # Set up a tiny working dir Aider will edit.
    sample = tmp_path / "sample.py"
    sample.write_text('print("hello")\n')

    env = {
        **os.environ,
        "OPENAI_API_BASE": gateway_url,
        "OPENAI_API_KEY": "any",
    }

    result = subprocess.run(
        [
            "aider",
            "--no-show-model-warnings",
            "--no-git",
            "--no-auto-commits",
            "--yes-always",
            "--no-stream",
            "--message",
            "Add a top-line comment that just says hello world",
            "--model",
            "openai/openrouter/free",
            str(sample),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )

    assert result.returncode == 0, (
        f"aider exited {result.returncode}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    # Aider prints either "Applied edit" or "No changes" — either is a
    # successful round-trip from our perspective.
    out = result.stdout + result.stderr
    assert any(kw in out for kw in ("Applied edit", "No changes")), (
        f"aider did not produce an edit-marker line:\n{out}"
    )
