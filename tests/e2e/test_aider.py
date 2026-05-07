"""End-to-end: real Aider subprocess against the gateway.

Verifies that an existing Aider install can use FreeRide as its
upstream OPENAI_API_BASE with no FreeRide-specific patches. The
acceptance bar is "Aider exits 0 and writes its applied-edit message"
— the model's actual edit quality is irrelevant to the gateway test.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.e2e.conftest import require_aider


pytestmark = [pytest.mark.e2e, pytest.mark.timeout(120)]


def test_aider_one_shot_through_gateway(gateway_url: str, tmp_path: Path):
    """env-var path: OPENAI_API_BASE + --model passed explicitly.
    Pins the lowest-friction integration: any aider install can use
    the gateway by setting two env vars, no FreeRide-specific config.
    """
    require_aider()

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
    out = result.stdout + result.stderr
    assert any(kw in out for kw in ("Applied edit", "No changes")), (
        f"aider did not produce an edit-marker line:\n{out}"
    )


def test_aider_after_freeride_bind_no_model_flag(gateway_url: str, tmp_path: Path):
    """The 'fully automatic' contract: after `freeride bind aider`, the
    user should be able to invoke `aider` with NO flags (no --model, no
    OPENAI_API_BASE in env) and have it round-trip through the gateway.

    Relies on the binder writing the `model:` key in addition to api-
    base/api-key. If this test fails it means the binder regressed and
    users will hit "what model do I pass?" as a UX cliff.
    """
    require_aider()
    from freeride.binders import aider as aider_binder

    fake_home = tmp_path
    aider_config = fake_home / ".aider.conf.yml"
    aider_binder.bind(gateway_url, api_key="any", config_path=aider_config)

    sample = tmp_path / "sample.py"
    sample.write_text('print("hello")\n')

    # IMPORTANT: clear OPENAI_API_BASE/OPENAI_API_KEY from inherited env
    # so this test really exercises the config-file path, not env-var
    # leak from the developer machine.
    env = {k: v for k, v in os.environ.items()
           if k not in ("OPENAI_API_BASE", "OPENAI_API_KEY")}
    env["HOME"] = str(fake_home)

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
            # NO --model flag; the binder's `model:` config line should
            # take effect.
            str(sample),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, (
        f"aider exited {result.returncode} after `freeride bind aider`\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    out = result.stdout + result.stderr
    assert any(kw in out for kw in ("Applied edit", "No changes")), out
