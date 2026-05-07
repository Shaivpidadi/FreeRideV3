"""End-to-end: real OpenClaw subprocess against the gateway.

This used to be a config-shape smoke test (binder writes valid YAML,
openclaw doctor accepts it). It is now a real chat-completion test:
``openclaw agent --local --message ...`` round-trips through the
gateway and returns a real response.

The full chain it pins:

  freeride bind openclaw   ->  ~/.openclaw/openclaw.json
  openclaw agent --local   ->  gateway   ->  OpenRouter
                            <-           <-

Catches the regression class we hit in development:
- "Unrecognized keys" (auth profile schema)
- "No API provider registered" (missing ``api`` field on the model)
- 503 from OpenRouter for an unknown model id (the bare-id /
  prefixed-id confusion)
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from freeride.binders import openclaw as openclaw_binder
from tests.e2e.conftest import require_openclaw


pytestmark = [pytest.mark.e2e, pytest.mark.timeout(120)]


def test_openclaw_agent_local_through_gateway(gateway_url: str, tmp_path: Path):
    require_openclaw()

    # Isolated state dir so we don't touch the developer's real ~/.openclaw.
    fake_state = tmp_path / "openclaw"
    fake_state.mkdir()
    cfg_path = fake_state / "openclaw.json"
    cfg_path.write_text("{}")

    openclaw_binder.bind(gateway_url, api_key="any", config_path=cfg_path)

    # Sanity: the binder wrote the schema-valid shape we expect.
    written = json.loads(cfg_path.read_text())
    prov = written["models"]["providers"]["freeride"]
    assert prov["baseUrl"] == gateway_url
    assert prov["apiKey"] == "any"
    assert prov["models"][0]["api"] == "openai-completions"
    assert prov["models"][0]["id"] == "openrouter/free"
    assert (
        written["agents"]["defaults"]["model"]["primary"]
        == "freeride/openrouter/free"
    )

    out_file = tmp_path / "oc-out.json"

    result = subprocess.run(
        [
            "openclaw",
            "agent",
            "--local",
            "--message",
            "Reply only the word: ok",
            "--to",
            "+15551234567",
            "--json",
            "--timeout",
            "60",
        ],
        env={
            **os.environ,
            "OPENCLAW_STATE_DIR": str(fake_state),
            "OPENCLAW_CONFIG_PATH": str(cfg_path),
            "HOME": str(tmp_path),
        },
        capture_output=True,
        text=True,
        timeout=90,
    )
    out_file.write_text(result.stdout)

    assert result.returncode == 0, (
        f"openclaw exited {result.returncode}\n"
        f"stdout: {result.stdout[:1000]}\n"
        f"stderr: {result.stderr[:1000]}"
    )

    # Parse the JSON envelope OpenClaw emits with --json.
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise AssertionError(
            f"openclaw stdout is not valid JSON: {e}\n"
            f"stdout: {result.stdout[:1000]}"
        )

    text = (data.get("payloads") or [{}])[0].get("text") or ""
    meta = data.get("meta") or {}
    agent_meta = meta.get("agentMeta") or {}

    assert agent_meta.get("provider") == "freeride", (
        f"openclaw should report freeride as provider; got {agent_meta!r}"
    )
    assert agent_meta.get("model") == "openrouter/free"
    assert not meta.get("aborted"), f"openclaw run aborted: meta={meta!r}"
    # Output may be a literal "ok" or a longer string; we just verify
    # the model produced something non-empty (and not an error message).
    assert text.strip(), "openclaw produced empty text"
    assert "error" not in text.lower(), f"openclaw text contains error: {text!r}"
