"""Baseline e2e: openai-python SDK round-trip via the gateway.

Pinning the protocol contract — every other agent test is a richer
version of this same flow, so a regression here means everything else
is broken too.
"""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.e2e


def test_chat_completion_roundtrips(gateway_url: str):
    pytest.importorskip("openai")
    from openai import OpenAI

    client = OpenAI(base_url=gateway_url, api_key="any")
    resp = client.chat.completions.create(
        model="openrouter/free",
        messages=[{"role": "user", "content": "Reply with exactly one word: yes."}],
        max_tokens=200,
    )
    assert resp.choices, "no choices returned"
    # The model picked may be any free OpenRouter model; we just verify
    # that a response landed and the gateway tagged itself.
    extra = resp.model_extra or {}
    assert extra.get("_freeride_provider") in ("openrouter", "nvidia_nim"), (
        f"missing _freeride_provider; extra={extra}"
    )


def test_streaming_chat_completion_roundtrips(gateway_url: str):
    pytest.importorskip("openai")
    from openai import OpenAI

    client = OpenAI(base_url=gateway_url, api_key="any")
    stream = client.chat.completions.create(
        model="openrouter/free",
        messages=[{"role": "user", "content": "Count: 1, 2, 3"}],
        max_tokens=40,
        stream=True,
    )
    chunks_seen = 0
    for evt in stream:
        chunks_seen += 1
    assert chunks_seen >= 1, "stream produced no chunks"


def test_models_endpoint_lists_models(gateway_url: str):
    import httpx

    r = httpx.get(f"{gateway_url}/models", timeout=15.0)
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    assert isinstance(body["data"], list)
    # Real OpenRouter returns 20+ free models; allow 5 as a sane lower bound.
    assert len(body["data"]) >= 5, f"too few models in catalog: {len(body['data'])}"
