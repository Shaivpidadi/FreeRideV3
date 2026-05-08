"""End-to-end test fixtures.

These tests are slow, hit the network, and need real API keys. They're
opt-in via either ``-m e2e`` or by setting ``FREERIDE_E2E=1``. CI runs
them on push (when the org provides keys); local devs run them
explicitly when verifying agent integrations.

Each test gets a fresh gateway subprocess on a free-ish port, with the
configured providers loaded via env. Tests subprocess out to the agent
under test, then assert the agent got a real response and that the
gateway logged the request.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from contextlib import closing
from pathlib import Path
from typing import Generator

import httpx
import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
GATEWAY_BIN = REPO_ROOT / ".venv" / "bin" / "freeride"


def _free_port() -> int:
    """Pick an OS-assigned free port, then close it."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_health(url: str, *, timeout_s: float = 15.0) -> None:
    """Poll /health until 200 or timeout."""
    deadline = time.time() + timeout_s
    last_err = None
    while time.time() < deadline:
        try:
            r = httpx.get(url, timeout=2.0)
            if r.status_code == 200:
                return
        except httpx.HTTPError as e:
            last_err = e
        time.sleep(0.3)
    raise RuntimeError(f"gateway never became healthy at {url}: {last_err}")


@pytest.fixture(scope="session")
def gateway_url() -> Generator[str, None, None]:
    """Session-scoped gateway subprocess.

    Skips the entire session if no provider API key is available — the
    gateway can technically start without keys but every e2e test needs
    real providers.
    """
    # At least one provider key must be set for any agent e2e to do anything
    # useful — the gateway can technically start without keys but every
    # downstream agent test will then fail on the first chat completion.
    any_key = any(
        os.environ.get(k)
        for k in (
            "OPENROUTER_API_KEY",
            "NVIDIA_API_KEY",
            "GROQ_API_KEY",
            "CLOUDFLARE_API_TOKEN",
            "HF_TOKEN",
            "HUGGINGFACE_API_KEY",
            "CEREBRAS_API_KEY",
            "OLLAMA_BASE_URL",
        )
    )
    if not any_key:
        pytest.skip(
            "no provider API key set "
            "(set one of OPENROUTER_API_KEY, GROQ_API_KEY, NVIDIA_API_KEY, "
            "CLOUDFLARE_API_TOKEN+CLOUDFLARE_ACCOUNT_ID, HF_TOKEN, "
            "CEREBRAS_API_KEY, or OLLAMA_BASE_URL)"
        )

    if not GATEWAY_BIN.exists():
        pytest.skip(f"gateway binary missing at {GATEWAY_BIN} (run `pip install -e .`)")

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    health = f"{base}/health"

    # start_new_session=True puts the gateway in its own session group
    # so SIGINT/SIGTERM to pytest doesn't propagate. stdout/stderr go
    # to DEVNULL so the gateway never holds onto pytest's stdio (which
    # would prevent the parent SSH session from cleanly exiting after
    # pytest completes).
    proc = subprocess.Popen(
        [str(GATEWAY_BIN), "serve", "--port", str(port), "--host", "127.0.0.1"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        env={**os.environ},
        start_new_session=True,
    )
    try:
        _wait_for_health(health)
        yield f"{base}/v1"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _have(binary: str) -> bool:
    return shutil.which(binary) is not None


# Per-agent skip helpers — each e2e test calls the matching one.

def require_aider():
    if not _have("aider"):
        pytest.skip("aider not installed (curl -sLS https://aider.chat/install.sh | sh)")


def require_hermes():
    if not _have("hermes"):
        pytest.skip("hermes-agent not installed")


def require_openclaw():
    if not _have("openclaw"):
        pytest.skip("openclaw not installed")
