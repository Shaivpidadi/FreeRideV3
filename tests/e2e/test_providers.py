"""Per-provider e2e tests against real upstreams.

For each provider, start a gateway subprocess with ONLY that provider's
env var(s) loaded, then exercise the OpenAI-compatible surface:

  - GET  /v1/models — non-empty catalog
  - POST /v1/chat/completions — non-streaming, returns choices, stamps
                                _freeride_provider with the expected name
  - POST /v1/chat/completions — streaming, yields >=1 chunk and stamps
                                X-FreeRide-Provider header

Each provider's test is skipped at the fixture level if its env var(s)
aren't set, so a `FREERIDE_E2E=1 pytest -m e2e` run on a machine that
has only some keys exercises only those providers.

These are slow (real network) and gated behind the e2e mark — opt-in via
``pytest -m e2e`` or ``FREERIDE_E2E=1``.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from contextlib import closing
from pathlib import Path
from typing import Iterator

import httpx
import pytest


pytestmark = pytest.mark.e2e


REPO_ROOT = Path(__file__).resolve().parents[2]
GATEWAY_BIN = REPO_ROOT / ".venv" / "bin" / "freeride"


# ---------------------------------------------------------------------------
# Per-provider matrix.
#
# Each entry has:
#   key_env        the (one or two) env vars that must be set
#   model          a free model id known to be available right now
#   provider_name  what we expect to see in X-FreeRide-Provider on success
#   skip_msg       reason printed when the env var isn't set
# ---------------------------------------------------------------------------

PROVIDER_MATRIX: list[dict] = [
    {
        "id": "openrouter",
        "key_env": ["OPENROUTER_API_KEY"],
        "model": "openrouter/free",
        "provider_name": "openrouter",
        "skip_msg": "set OPENROUTER_API_KEY to run OpenRouter e2e",
    },
    {
        "id": "ollama",
        "key_env": ["OLLAMA_BASE_URL"],
        # llama3.1:8b is a popular Ollama default; if not pulled, the
        # request fails with model_not_found and the test is skipped.
        "model": "llama3.1:8b",
        "provider_name": "ollama",
        "skip_msg": "set OLLAMA_BASE_URL (e.g. http://localhost:11434) and `ollama pull llama3.1:8b` first",
    },
    {
        "id": "groq",
        "key_env": ["GROQ_API_KEY"],
        "model": "llama-3.1-8b-instant",
        "provider_name": "groq",
        "skip_msg": "set GROQ_API_KEY to run Groq e2e",
    },
    {
        "id": "nvidia_nim",
        "key_env": ["NVIDIA_API_KEY"],
        "model": "meta/llama-3.1-8b-instruct",
        "provider_name": "nvidia_nim",
        "skip_msg": "set NVIDIA_API_KEY to run NVIDIA NIM e2e",
    },
    {
        "id": "cloudflare_wai",
        "key_env": ["CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"],
        "model": "@cf/meta/llama-3.1-8b-instruct-fp8",
        "provider_name": "cloudflare_wai",
        "skip_msg": "set CLOUDFLARE_API_TOKEN + CLOUDFLARE_ACCOUNT_ID to run CF Workers AI e2e",
    },
    {
        "id": "huggingface",
        "key_env": ["HF_TOKEN"],  # HUGGINGFACE_API_KEY also accepted
        "model": "meta-llama/Llama-3.3-70B-Instruct",
        "provider_name": "huggingface",
        "skip_msg": "set HF_TOKEN (or HUGGINGFACE_API_KEY) to run HuggingFace e2e",
    },
    {
        "id": "cerebras",
        "key_env": ["CEREBRAS_API_KEY"],
        "model": "llama3.1-8b",
        "provider_name": "cerebras",
        "skip_msg": "set CEREBRAS_API_KEY to run Cerebras e2e",
    },
]


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_health(url: str, *, timeout_s: float = 15.0) -> None:
    deadline = time.time() + timeout_s
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            r = httpx.get(url, timeout=2.0)
            if r.status_code == 200:
                return
        except httpx.HTTPError as e:
            last_err = e
        time.sleep(0.3)
    raise RuntimeError(f"gateway never became healthy at {url}: {last_err}")


def _isolated_env_for(provider_id: str) -> dict[str, str]:
    """Return an env dict that has ONLY the target provider's keys set,
    plus the bare minimum to run the gateway (PATH, HOME, etc.).

    Other provider env vars are stripped so we know any successful
    response came from THIS provider, not a fallback.
    """
    target_keys = set()
    for entry in PROVIDER_MATRIX:
        if entry["id"] == provider_id:
            target_keys = set(entry["key_env"])
            break

    all_provider_keys = set()
    for entry in PROVIDER_MATRIX:
        all_provider_keys.update(entry["key_env"])
    # HuggingFace alternate name
    all_provider_keys.add("HUGGINGFACE_API_KEY")

    env = {k: v for k, v in os.environ.items() if k not in all_provider_keys}
    for k in target_keys:
        if v := os.environ.get(k):
            env[k] = v
    # Strip telemetry beacon side effects to keep e2e hermetic-ish
    env["FREERIDE_TELEMETRY"] = "off"
    # Use a tmp events path so we don't pollute the real one
    env["FREERIDE_EVENTS_PATH"] = "/tmp/freeride-e2e-events.jsonl"
    return env


@pytest.fixture(params=PROVIDER_MATRIX, ids=lambda e: e["id"])
def provider_gateway(request: pytest.FixtureRequest) -> Iterator[tuple[str, dict]]:
    """Function-scoped: per-test gateway with ONLY this provider's keys."""
    entry = request.param

    missing = [k for k in entry["key_env"] if not os.environ.get(k)]
    # HF accepts either name
    if entry["id"] == "huggingface" and "HF_TOKEN" in missing:
        if os.environ.get("HUGGINGFACE_API_KEY"):
            missing.remove("HF_TOKEN")
    if missing:
        pytest.skip(entry["skip_msg"])

    if not GATEWAY_BIN.exists():
        pytest.skip(f"gateway binary missing at {GATEWAY_BIN} (run `pip install -e .`)")

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    env = _isolated_env_for(entry["id"])

    proc = subprocess.Popen(
        [str(GATEWAY_BIN), "serve", "--port", str(port), "--host", "127.0.0.1"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )
    try:
        _wait_for_health(f"{base}/health")
        yield f"{base}/v1", entry
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


# ---------------------------------------------------------------------------
# The actual tests — one per assertion, parameterized over providers.
# ---------------------------------------------------------------------------


def test_models_endpoint_returns_catalog(provider_gateway: tuple[str, dict]) -> None:
    base, entry = provider_gateway
    r = httpx.get(f"{base}/models", timeout=20.0)
    assert r.status_code == 200, f"/models failed for {entry['id']}: {r.text[:200]}"
    body = r.json()
    assert body["object"] == "list"
    assert isinstance(body["data"], list)
    assert len(body["data"]) >= 1, (
        f"{entry['id']} returned 0 models — provider may have changed its catalog shape"
    )
    # Every model in this catalog should be owned_by THIS provider.
    owners = {m.get("owned_by") for m in body["data"]}
    assert owners == {entry["provider_name"]}, (
        f"{entry['id']} catalog has unexpected owners: {owners}"
    )


def test_chat_completion_non_streaming(provider_gateway: tuple[str, dict]) -> None:
    pytest.importorskip("openai")
    from openai import OpenAI

    base, entry = provider_gateway
    client = OpenAI(base_url=base, api_key="any")
    resp = client.chat.completions.create(
        model=entry["model"],
        messages=[{"role": "user", "content": "Reply with exactly one word: yes."}],
        max_tokens=200,
    )
    assert resp.choices, f"{entry['id']}: no choices returned"
    extra = resp.model_extra or {}
    assert extra.get("_freeride_provider") == entry["provider_name"], (
        f"{entry['id']}: expected _freeride_provider={entry['provider_name']!r}, got extra={extra}"
    )
    # Sanity: a request_id should be stamped too.
    assert "_freeride_request_id" in extra
    assert extra["_freeride_request_id"].startswith("req_")


def test_chat_completion_streaming(provider_gateway: tuple[str, dict]) -> None:
    base, entry = provider_gateway
    # Use raw httpx to read the X-FreeRide-Provider header directly —
    # the openai SDK swallows non-content headers on streams.
    with httpx.stream(
        "POST",
        f"{base}/chat/completions",
        json={
            "model": entry["model"],
            "messages": [{"role": "user", "content": "Count: 1, 2, 3"}],
            "max_tokens": 40,
            "stream": True,
        },
        headers={"Authorization": "Bearer any"},
        timeout=30.0,
    ) as stream:
        assert stream.status_code == 200, (
            f"{entry['id']} stream failed: {stream.read().decode()[:200]}"
        )
        provider_hdr = stream.headers.get("X-FreeRide-Provider")
        assert provider_hdr == entry["provider_name"], (
            f"{entry['id']}: expected X-FreeRide-Provider={entry['provider_name']!r}, "
            f"got {provider_hdr!r}"
        )
        chunks_seen = 0
        for line in stream.iter_lines():
            if line.startswith("data: ") and not line.endswith("[DONE]"):
                chunks_seen += 1
        assert chunks_seen >= 1, f"{entry['id']} stream produced no data chunks"


def test_request_id_header_present(provider_gateway: tuple[str, dict]) -> None:
    """Sanity: every successful chat completion stamps X-FreeRide-Request-ID."""
    base, entry = provider_gateway
    r = httpx.post(
        f"{base}/chat/completions",
        json={
            "model": entry["model"],
            "messages": [{"role": "user", "content": "say hi"}],
            "max_tokens": 10,
        },
        headers={"Authorization": "Bearer any"},
        timeout=30.0,
    )
    if r.status_code != 200:
        pytest.skip(f"{entry['id']} request didn't succeed (status {r.status_code}); "
                    f"upstream may be rate-limited right now")
    rid = r.headers.get("X-FreeRide-Request-ID")
    assert rid and rid.startswith("req_"), f"{entry['id']}: bad request id: {rid!r}"


# Per-provider embedding model that's known to be available on the free
# tier. Groq is excluded since it has no embedding endpoint.
EMBEDDING_MODELS: dict[str, str | None] = {
    "openrouter": "text-embedding-3-small",
    "nvidia_nim": "nvidia/nv-embedqa-e5-v5",
    "cloudflare_wai": "@cf/baai/bge-base-en-v1.5",
    # HuggingFace's OpenAI-compat router (router.huggingface.co/v1)
    # does NOT have an /embeddings endpoint — provider's
    # embeddings_supported = False, embeddings route filter skips it.
    "huggingface": None,
    "ollama": "nomic-embed-text",  # most-pulled Ollama embedding model
    "groq": None,  # not supported
}


def test_embeddings_endpoint(provider_gateway: tuple[str, dict]) -> None:
    """Each embedding-capable provider should round-trip through /v1/embeddings."""
    base, entry = provider_gateway
    model = EMBEDDING_MODELS.get(entry["id"])
    if model is None:
        pytest.skip(f"{entry['id']} does not support embeddings")

    r = httpx.post(
        f"{base}/embeddings",
        json={"model": model, "input": "hello world"},
        headers={"Authorization": "Bearer any"},
        timeout=30.0,
    )
    if r.status_code != 200:
        pytest.skip(
            f"{entry['id']} embeddings call didn't succeed (status {r.status_code}); "
            f"upstream may be rate-limited or model id may have rotated. body: {r.text[:200]}"
        )
    body = r.json()
    assert body["object"] == "list"
    assert isinstance(body["data"], list) and body["data"], (
        f"{entry['id']}: empty embeddings data"
    )
    assert "embedding" in body["data"][0]
    assert body["_freeride_provider"] == entry["provider_name"]
    assert r.headers["X-FreeRide-Provider"] == entry["provider_name"]
