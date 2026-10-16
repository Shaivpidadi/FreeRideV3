"""Gemini thought-signature round-trip and schema scrubbing."""

from __future__ import annotations

import json

import pytest

from freeride.core.chat_schema import ChatRequest
from freeride.core.provider_specs import spec_by_name
from freeride.providers import gemini_compat
from freeride.providers.gemini_compat import SKIP_SIGNATURE_SENTINEL, GeminiCompatProvider

CHAT = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"


@pytest.fixture
def p():
    gemini_compat._CACHE = gemini_compat._SignatureCache()
    return GeminiCompatProvider(spec_by_name("gemini"))


def _tool_call(cid="call_1", name="read", args='{"path":"a"}', sig="SIG1"):
    call = {"id": cid, "type": "function", "function": {"name": name, "arguments": args}}
    if sig:
        call["extra_content"] = {"google": {"thought_signature": sig}}
    return call


def _response_with_call(sig="SIG1"):
    return {
        "id": "x",
        "object": "chat.completion",
        "created": 0,
        "model": "gemini-3-flash",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": None, "tool_calls": [_tool_call(sig=sig)]}, "finish_reason": "tool_calls"}],
    }


async def test_signature_is_cached_and_replayed_by_id(p, httpx_mock):
    httpx_mock.add_response(url=CHAT, json=_response_with_call())
    first = ChatRequest(model="x", messages=[{"role": "user", "content": "go"}])
    await p.forward_chat(first, "gemini-3-flash", "k")

    httpx_mock.add_response(url=CHAT, json={"id": "y", "object": "chat.completion", "created": 0, "model": "gemini-3-flash", "choices": [{"index": 0, "message": {"role": "assistant", "content": "done"}}]})
    second = ChatRequest(
        model="x",
        messages=[
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": None, "tool_calls": [_tool_call(sig=None)]},
            {"role": "tool", "tool_call_id": "call_1", "content": "file contents"},
        ],
    )
    await p.forward_chat(second, "gemini-3-flash", "k")
    sent = json.loads(httpx_mock.get_requests()[-1].content)
    replayed = sent["messages"][1]["tool_calls"][0]
    assert replayed["extra_content"]["google"]["thought_signature"] == "SIG1"


async def test_signature_replayed_by_fingerprint_when_id_rewritten(p, httpx_mock):
    httpx_mock.add_response(url=CHAT, json=_response_with_call())
    await p.forward_chat(ChatRequest(model="x", messages=[{"role": "user", "content": "go"}]), "gemini-3-flash", "k")
    httpx_mock.add_response(url=CHAT, json={"id": "y", "object": "chat.completion", "created": 0, "model": "gemini-3-flash", "choices": []})
    rewritten = ChatRequest(
        model="x",
        messages=[{"role": "assistant", "content": None, "tool_calls": [_tool_call(cid="toolu_other", sig=None)]}],
    )
    await p.forward_chat(rewritten, "gemini-3-flash", "k")
    sent = json.loads(httpx_mock.get_requests()[-1].content)
    assert sent["messages"][0]["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "SIG1"


async def test_sentinel_when_nothing_cached(p, httpx_mock):
    httpx_mock.add_response(url=CHAT, json={"id": "y", "object": "chat.completion", "created": 0, "model": "gemini-3-flash", "choices": []})
    req = ChatRequest(model="x", messages=[{"role": "assistant", "content": None, "tool_calls": [_tool_call(cid="unknown", name="other", sig=None)]}])
    await p.forward_chat(req, "gemini-3-flash", "k")
    sent = json.loads(httpx_mock.get_requests()[-1].content)
    assert sent["messages"][0]["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == SKIP_SIGNATURE_SENTINEL


async def test_client_supplied_signature_is_kept(p, httpx_mock):
    httpx_mock.add_response(url=CHAT, json={"id": "y", "object": "chat.completion", "created": 0, "model": "gemini-3-flash", "choices": []})
    req = ChatRequest(model="x", messages=[{"role": "assistant", "content": None, "tool_calls": [_tool_call(sig="CLIENT")]}])
    await p.forward_chat(req, "gemini-3-flash", "k")
    sent = json.loads(httpx_mock.get_requests()[-1].content)
    assert sent["messages"][0]["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "CLIENT"


async def test_stream_captures_signatures(p, httpx_mock):
    chunk = {
        "id": "1", "object": "chat.completion.chunk", "created": 0, "model": "gemini-3-flash",
        "choices": [{"index": 0, "delta": {"tool_calls": [_tool_call(cid="call_s", sig="SIGS")]}}],
    }
    sse = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
    httpx_mock.add_response(url=CHAT, content=sse.encode(), headers={"content-type": "text/event-stream"})
    req = ChatRequest(model="x", messages=[{"role": "user", "content": "go"}], stream=True)
    _ = [e async for e in p.forward_chat_stream(req, "gemini-3-flash", "k")]
    assert gemini_compat._CACHE.get("call_s") == "SIGS"


async def test_tool_schema_scrubbed(p, httpx_mock):
    httpx_mock.add_response(url=CHAT, json={"id": "y", "object": "chat.completion", "created": 0, "model": "gemini-3-flash", "choices": []})
    req = ChatRequest(
        model="x",
        messages=[{"role": "user", "content": "go"}],
        tools=[{"type": "function", "function": {"name": "f", "strict": True, "parameters": {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object", "additionalProperties": False, "properties": {"a": {"type": "string"}}}}}],
    )
    await p.forward_chat(req, "gemini-3-flash", "k")
    sent = json.loads(httpx_mock.get_requests()[-1].content)
    fn = sent["tools"][0]["function"]
    assert "strict" not in fn
    assert "$schema" not in fn["parameters"] and "additionalProperties" not in fn["parameters"]
    assert fn["parameters"]["properties"] == {"a": {"type": "string"}}
