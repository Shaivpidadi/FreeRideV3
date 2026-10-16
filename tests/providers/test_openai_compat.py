"""Behaviour tests for the spec-driven OpenAI-compatible adapter."""

from __future__ import annotations

import httpx
import pytest

from freeride.core.chat_schema import ChatRequest
from freeride.core.errors import ErrorKind
from freeride.core.provider_specs import (
    KEYLESS_SENTINEL,
    ModelMeta,
    OpenAICompatSpec,
    PROVIDER_SPECS,
    spec_by_name,
)
from freeride.providers.openai_compat import (
    OpenAICompatProvider,
    make_provider,
    normalize_model_id,
    parse_retry_after_value,
    retry_hint_from_body,
)


def _spec(**kw) -> OpenAICompatSpec:
    base = dict(name="acme", display_name="Acme", base_url="https://api.acme.test/v1", env_vars=("ACME_API_KEY",))
    base.update(kw)
    return OpenAICompatSpec(**base)


MODELS = "https://api.acme.test/v1/models"
CHAT = "https://api.acme.test/v1/chat/completions"


# ---- headers ---------------------------------------------------------------


class TestHeaders:
    def test_bearer_for_keyed(self):
        p = OpenAICompatProvider(_spec())
        assert p.auth_header("k1") == {"Authorization": "Bearer k1"}

    def test_keyless_sends_no_auth_and_product_headers(self):
        p = OpenAICompatProvider(_spec(keyless=True, env_vars=(), extra_headers={"User-Agent": "FreeRide/1.0"}))
        assert p.auth_header(KEYLESS_SENTINEL) == {}
        assert p.auth_header("anything") == {}
        assert p.attribution_headers() == {"User-Agent": "FreeRide/1.0"}

    def test_outbound_request_carries_extra_headers(self, httpx_mock):
        p = OpenAICompatProvider(_spec(extra_headers={"User-Agent": "Mozilla/5.0 FreeRide"}))
        httpx_mock.add_response(url=MODELS, json={"data": []})
        p.list_free_models("k")
        req = httpx_mock.get_request()
        assert req.headers["user-agent"] == "Mozilla/5.0 FreeRide"
        assert req.headers["authorization"] == "Bearer k"


# ---- catalog ---------------------------------------------------------------


class TestCatalog:
    def test_allowlist_and_metadata(self, httpx_mock):
        spec = _spec(
            free_ids=frozenset({"a", "b"}),
            metadata={"a": ModelMeta(200_000, ("tools",))},
            default_context_length=4_096,
        )
        httpx_mock.add_response(url=MODELS, json={"data": [{"id": "a"}, {"id": "b"}, {"id": "paid"}]})
        models = {m.api_id: m for m in OpenAICompatProvider(spec).list_free_models("k")}
        assert set(models) == {"a", "b"}
        assert models["a"].context_length == 200_000 and models["a"].supports("tools")
        assert models["b"].context_length == 4_096 and not models["b"].supports("tools")

    def test_env_override_wins(self, httpx_mock, monkeypatch):
        monkeypatch.setenv("ACME_FREE_MODELS_OVERRIDE", "paid")
        spec = _spec(free_ids=frozenset({"a"}))
        httpx_mock.add_response(url=MODELS, json={"data": [{"id": "a"}, {"id": "paid"}]})
        assert [m.api_id for m in OpenAICompatProvider(spec).list_free_models("k")] == ["paid"]

    def test_suffix_rule(self, httpx_mock):
        spec = _spec(free_id_suffix=":free")
        httpx_mock.add_response(url=MODELS, json={"data": [{"id": "x:free"}, {"id": "x"}]})
        assert [m.api_id for m in OpenAICompatProvider(spec).list_free_models("k")] == ["x:free"]

    def test_zero_pricing_rule(self, httpx_mock):
        spec = _spec()
        httpx_mock.add_response(
            url=MODELS,
            json={"data": [{"id": "z", "pricing": {"prompt": "0"}}, {"id": "p", "pricing": {"prompt": "0.001"}}]},
        )
        assert [m.api_id for m in OpenAICompatProvider(spec).list_free_models("k")] == ["z"]

    def test_everything_free_when_no_signal(self, httpx_mock):
        httpx_mock.add_response(url=MODELS, json={"data": [{"id": "a"}, {"id": "b"}]})
        assert len(OpenAICompatProvider(_spec()).list_free_models("k")) == 2

    def test_catalog_tools_flags_are_read(self, httpx_mock):
        spec = _spec(tools_default=True)
        httpx_mock.add_response(
            url=MODELS,
            json={
                "data": [
                    {"id": "kilo", "supported_parameters": ["tools", "temperature"]},
                    {"id": "xkiro", "capabilities": {"tools": False}},
                    {"id": "requesty", "supports_tool_calling": True},
                    {"id": "plain"},
                ]
            },
        )
        models = {m.api_id: m.supports("tools") for m in OpenAICompatProvider(spec).list_free_models("k")}
        assert models == {"kilo": True, "xkiro": False, "requesty": True, "plain": True}

    def test_prefix_strip_and_broken_ids(self, httpx_mock):
        spec = _spec(id_prefix_strip="models/", broken_ids=frozenset({"ghost"}))
        httpx_mock.add_response(url=MODELS, json={"data": [{"id": "models/real"}, {"id": "models/ghost"}]})
        assert [m.api_id for m in OpenAICompatProvider(spec).list_free_models("k")] == ["real"]

    def test_fallback_when_catalog_is_gated(self, httpx_mock):
        spec = _spec(keyless=True, env_vars=(), fallback_models=("m1", "m2"), tools_default=True)
        httpx_mock.add_response(url=MODELS, status_code=401)
        models = OpenAICompatProvider(spec).list_free_models(KEYLESS_SENTINEL)
        assert [m.api_id for m in models] == ["m1", "m2"]
        assert all(m.supports("tools") for m in models)

    def test_catalog_error_without_fallback_raises(self, httpx_mock):
        httpx_mock.add_response(url=MODELS, status_code=500)
        with pytest.raises(httpx.HTTPStatusError):
            OpenAICompatProvider(_spec()).list_free_models("k")

    def test_non_chat_entries_are_dropped(self, httpx_mock):
        spec = _spec(free_predicate=lambda raw: True, tools_default=True)
        httpx_mock.add_response(
            url=MODELS,
            json={
                "data": [
                    {"id": "gpt-oss-120b"},
                    {"id": "stable-diffusion-xl-base-v10"},
                    {"id": "whisper-large-v3"},
                    {"id": "nvr-tts-en-us"},
                    {"id": "Qwen3Guard-Gen-8B"},
                    {"id": "bge-m3"},
                    {"id": "Qwen3-Embedding-8B"},
                    {"id": "img-model", "categories": ["image"]},
                    {"id": "chatty", "categories": ["chat", "coding"]},
                    {"id": "typed-audio", "type": "audio"},
                ]
            },
        )
        ids = [m.api_id for m in OpenAICompatProvider(spec).list_free_models("k")]
        assert ids == ["gpt-oss-120b", "chatty"]

    def test_blockrun_billing_mode_rule(self, httpx_mock):
        p = make_provider(spec_by_name("blockrun"))
        httpx_mock.add_response(
            url="https://blockrun.ai/api/v1/models",
            json={"data": [{"id": "openai/gpt-6", "billing_mode": "paid", "categories": ["chat"]}, {"id": "nvidia/nemotron-3.5-lightning", "billing_mode": "free", "categories": ["chat", "reasoning"]}]},
        )
        assert [m.api_id for m in p.list_free_models("anonymous")] == ["nvidia/nemotron-3.5-lightning"]

    def test_context_length_sources(self, httpx_mock):
        httpx_mock.add_response(
            url=MODELS,
            json={"data": [{"id": "a", "context_window": 8192}, {"id": "b", "top_provider": {"context_length": 65536}}]},
        )
        models = {m.api_id: m.context_length for m in OpenAICompatProvider(_spec()).list_free_models("k")}
        assert models == {"a": 8192, "b": 65536}


# ---- error classification --------------------------------------------------


class TestClassify:
    @pytest.fixture
    def p(self):
        return OpenAICompatProvider(_spec())

    def _resp(self, status, json=None, text=None, headers=None):
        return httpx.Response(status, json=json, text=text, headers=headers, request=httpx.Request("POST", CHAT))

    def test_status_table(self, p):
        assert p.classify_error(self._resp(200, json={})) is ErrorKind.OK
        assert p.classify_error(self._resp(401, json={})) is ErrorKind.AUTH
        assert p.classify_error(self._resp(402, json={})) is ErrorKind.QUOTA_EXHAUSTED
        assert p.classify_error(self._resp(429, json={})) is ErrorKind.RATE_LIMIT
        assert p.classify_error(self._resp(503, json={})) is ErrorKind.UNAVAILABLE
        assert p.classify_error(self._resp(504, json={})) is ErrorKind.TIMEOUT

    def test_403_tier_gate_is_model_not_found_not_auth(self, p):
        r = self._resp(403, json={"error": {"message": "This is a premium model. Upgrade your plan."}})
        assert p.classify_error(r) is ErrorKind.MODEL_NOT_FOUND
        r = self._resp(403, json={"error": {"message": "this model requires a subscription"}})
        assert p.classify_error(r) is ErrorKind.MODEL_NOT_FOUND

    def test_keyless_401_403_are_model_not_found(self):
        p = OpenAICompatProvider(_spec(keyless=True, env_vars=()))
        assert p.classify_error(self._resp(401, json={"error": {"message": "auth required"}})) is ErrorKind.MODEL_NOT_FOUND
        assert p.classify_error(self._resp(403, json={"error": {"message": "forbidden"}})) is ErrorKind.MODEL_NOT_FOUND

    def test_403_plain_is_auth(self, p):
        assert p.classify_error(self._resp(403, json={"error": {"message": "Forbidden"}})) is ErrorKind.AUTH

    def test_404_and_400_model_patterns(self, p):
        assert p.classify_error(self._resp(404, text="404 page not found")) is ErrorKind.MODEL_NOT_FOUND
        assert p.classify_error(self._resp(400, json={"error": {"code": "model_not_found"}})) is ErrorKind.MODEL_NOT_FOUND
        assert p.classify_error(self._resp(410, json={"error": {"code": "model_retired"}})) is ErrorKind.MODEL_NOT_FOUND
        assert p.classify_error(self._resp(400, json={"error": {"message": "bad request"}})) is ErrorKind.UNKNOWN

    def test_402_tier_gate_is_model_not_found_but_account_402_is_quota(self, p):
        gated = self._resp(402, json={"error": {"message": "This model is not in the Free plan. Pro is $20/month"}})
        assert p.classify_error(gated) is ErrorKind.MODEL_NOT_FOUND
        account = self._resp(402, json={"error": {"code": "PAYMENT_METHOD_REQUIRED", "message": "A payment method is required."}})
        assert p.classify_error(account) is ErrorKind.QUOTA_EXHAUSTED
        prepay = self._resp(402, json=[{"error": {"message": "Your prepayment credits are depleted."}}])
        assert p.classify_error(prepay) is ErrorKind.QUOTA_EXHAUSTED

    def test_400_insufficient_balance_is_quota(self, p):
        r = self._resp(400, json={"error": {"message": "insufficient balance"}})
        assert p.classify_error(r) is ErrorKind.QUOTA_EXHAUSTED

    def test_exceptions(self, p):
        assert p.classify_error(httpx.ReadTimeout("t")) is ErrorKind.TIMEOUT
        assert p.classify_error(httpx.ConnectError("c")) is ErrorKind.UNAVAILABLE
        assert p.classify_error(ValueError("x")) is ErrorKind.UNKNOWN


# ---- retry hints -----------------------------------------------------------


class TestRetryHints:
    def test_header_delta_and_http_date(self):
        assert parse_retry_after_value("30") == 30
        assert parse_retry_after_value("Wed, 21 Oct 2015 07:28:00 GMT") == 0  # in the past -> 0
        assert parse_retry_after_value("99999999") == 86400

    def test_body_structured_fields(self):
        gemini = {"error": {"details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "17s"}]}}
        assert retry_hint_from_body(gemini) == 17
        assert retry_hint_from_body({"retry_after": 60}) == 60
        assert retry_hint_from_body({"error": {"retryAfterSeconds": "5"}}) == 5
        assert retry_hint_from_body({"error": {"retry_after": {"seconds": 12}}}) == 12

    def test_body_prose_is_anchored(self):
        assert retry_hint_from_body({"error": {"message": "Rate limit reached. Please try again in 2m"}}) == 120
        assert retry_hint_from_body({"error": {"message": "try again in 30 seconds"}}) == 30
        # An unrelated number is never a delay.
        assert retry_hint_from_body({"error": {"message": "model has 70 billion parameters"}}) is None

    def test_clamped_and_depth_capped(self):
        assert retry_hint_from_body({"retry_after": 10**9}) == 86400
        deep = {"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": {"retry_after": 5}}}}}}}}}
        assert retry_hint_from_body(deep) is None

    def test_provider_prefers_header_then_body(self):
        p = OpenAICompatProvider(_spec())
        req = httpx.Request("POST", CHAT)
        with_header = httpx.Response(429, headers={"retry-after": "7"}, json={"retry_after": 99}, request=req)
        assert p.retry_after_hint(with_header) == 7
        body_only = httpx.Response(429, json={"error": {"message": "retry after 45 seconds"}}, request=req)
        assert p.retry_after_hint(body_only) == 45
        assert p.retry_after_hint(None) is None


# ---- forwarding ------------------------------------------------------------


def _chat_response(model="m"):
    return {
        "id": "x",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


class TestForwarding:
    async def test_forward_chat_shapes_payload(self, httpx_mock):
        spec = _spec(drop_params=("tool_choice", "logprobs"), force_single_tool_call=True)
        httpx_mock.add_response(url=CHAT, json=_chat_response())
        req = ChatRequest(
            model="freeride/free",
            messages=[{"role": "user", "content": "hi"}],
            tools=[{"type": "function", "function": {"name": "f", "parameters": {}}}],
            tool_choice="auto",
            logprobs=True,
            stream=True,
        )
        out = await OpenAICompatProvider(spec).forward_chat(req, "m", "k")
        assert out.choices[0].message.content == "hi"
        sent = httpx_mock.get_request()
        import json

        body = json.loads(sent.content)
        assert body["model"] == "m" and body["stream"] is False
        assert "tool_choice" not in body and "logprobs" not in body
        assert body["parallel_tool_calls"] is False

    async def test_substitution_is_counted_not_fatal(self, httpx_mock, caplog):
        p = OpenAICompatProvider(_spec())
        httpx_mock.add_response(url=CHAT, json=_chat_response(model="other-model"))
        req = ChatRequest(model="x", messages=[{"role": "user", "content": "hi"}])
        await p.forward_chat(req, "requested-model", "k")
        assert p.substitutions_seen == 1
        httpx_mock.add_response(url=CHAT, json=_chat_response(model="z-ai/glm-5.3-flash"))
        await p.forward_chat(req, "glm-5.3-flash:free", "k")
        assert p.substitutions_seen == 1  # normalised ids match
        httpx_mock.add_response(url=CHAT, json=_chat_response(model="whatever-upstream"))
        await p.forward_chat(req, "auto", "k")
        assert p.substitutions_seen == 1  # auto routes are exempt

    async def test_strict_substitution_raises_model_not_found(self, httpx_mock):
        p = OpenAICompatProvider(_spec(strict_model_match=True))
        httpx_mock.add_response(url=CHAT, json=_chat_response(model="other-model"))
        req = ChatRequest(model="x", messages=[{"role": "user", "content": "hi"}])
        with pytest.raises(httpx.HTTPStatusError) as ei:
            await p.forward_chat(req, "requested-model", "k")
        assert p.classify_error(ei.value.response) is ErrorKind.MODEL_NOT_FOUND
        # Exact and auto routes still pass.
        httpx_mock.add_response(url=CHAT, json=_chat_response(model="requested-model"))
        await p.forward_chat(req, "requested-model", "k")
        httpx_mock.add_response(url=CHAT, json=_chat_response(model="whatever"))
        await p.forward_chat(req, "auto", "k")

    async def test_strict_substitution_in_stream_raises_before_first_event(self, httpx_mock):
        p = OpenAICompatProvider(_spec(strict_model_match=True))
        sse = 'data: {"id":"1","object":"chat.completion.chunk","created":0,"model":"other","choices":[{"index":0,"delta":{"content":"x"}}]}\n\ndata: [DONE]\n\n'
        httpx_mock.add_response(url=CHAT, content=sse.encode(), headers={"content-type": "text/event-stream"})
        req = ChatRequest(model="x", messages=[{"role": "user", "content": "hi"}], stream=True)
        gen = p.forward_chat_stream(req, "requested", "k")
        with pytest.raises(httpx.HTTPStatusError):
            await gen.__anext__()

    async def test_stream_parses_sse_and_stops_at_done(self, httpx_mock):
        sse = (
            'data: {"id":"1","object":"chat.completion.chunk","created":0,"model":"m","choices":[{"index":0,"delta":{"role":"assistant","content":"he"}}]}\n\n'
            ": keepalive\n\n"
            'data: {"id":"1","object":"chat.completion.chunk","created":0,"model":"m","choices":[{"index":0,"delta":{"content":"llo"}}]}\n\n'
            "data: [DONE]\n\n"
            'data: {"should":"not be read"}\n\n'
        )
        httpx_mock.add_response(url=CHAT, content=sse.encode(), headers={"content-type": "text/event-stream"})
        req = ChatRequest(model="x", messages=[{"role": "user", "content": "hi"}], stream=True)
        events = [e async for e in OpenAICompatProvider(_spec()).forward_chat_stream(req, "m", "k")]
        assert "".join(e.choices[0].delta.content or "" for e in events) == "hello"

    async def test_embeddings_only_when_spec_has_path(self, httpx_mock):
        from freeride.core.embedding_schema import EmbeddingRequest

        p = OpenAICompatProvider(_spec(embeddings_path="/embeddings"))
        assert p.embeddings_supported
        httpx_mock.add_response(
            url="https://api.acme.test/v1/embeddings",
            json={"object": "list", "data": [{"object": "embedding", "index": 0, "embedding": [0.1]}], "model": "e", "usage": {"prompt_tokens": 1, "total_tokens": 1}},
        )
        out = await p.forward_embeddings(EmbeddingRequest(model="e", input="x"), "e", "k")
        assert out.data[0].embedding == [0.1]
        assert not OpenAICompatProvider(_spec()).embeddings_supported


# ---- probe -----------------------------------------------------------------


class TestProbe:
    def test_probe_ok_and_error(self, httpx_mock):
        p = OpenAICompatProvider(_spec())
        httpx_mock.add_response(url=CHAT, json=_chat_response())
        assert p.probe("m", "k").ok
        httpx_mock.add_response(url=CHAT, status_code=429)
        r = p.probe("m", "k")
        assert not r.ok and r.error is ErrorKind.RATE_LIMIT


# ---- the spec table itself -------------------------------------------------


class TestSpecTable:
    def test_names_unique_and_do_not_collide_with_builtins(self):
        names = [s.name for s in PROVIDER_SPECS]
        assert len(names) == len(set(names))
        assert not set(names) & {"openrouter", "groq", "nvidia_nim", "cloudflare_wai", "huggingface", "cerebras", "ollama"}

    def test_keyless_rows_are_opt_in(self):
        for s in PROVIDER_SPECS:
            if s.keyless:
                assert not s.env_vars and not s.default_on
            else:
                assert s.env_vars and s.signup_url

    def test_training_rows_are_not_default_on_unless_first_party(self):
        for s in PROVIDER_SPECS:
            if s.data_use == "train" and s.name != "gemini":
                assert not s.default_on, s.name

    def test_make_provider_picks_gemini_subclass(self):
        from freeride.providers.gemini_compat import GeminiCompatProvider

        assert isinstance(make_provider(spec_by_name("gemini")), GeminiCompatProvider)
        assert type(make_provider(spec_by_name("mistral"))) is OpenAICompatProvider

    def test_gemini_free_predicate(self, httpx_mock):
        p = make_provider(spec_by_name("gemini"))
        httpx_mock.add_response(
            url="https://generativelanguage.googleapis.com/v1beta/openai/models",
            json={
                "data": [
                    {"id": "models/gemini-2.5-flash"},
                    {"id": "models/gemini-2.5-pro"},
                    {"id": "models/gemini-3.5-flash-lite"},
                    {"id": "models/gemini-embedding-001"},
                    {"id": "models/gemma-4-26b-it"},
                    {"id": "models/gemini-2.5-flash-image"},
                ]
            },
            is_reusable=True,
        )
        ids = [m.api_id for m in p.list_free_models("k")]
        # gemini-2.x answers 404 "no longer available to new users".
        assert ids == ["gemini-3.5-flash-lite", "gemma-4-26b-it"]
        assert all(m.supports("tools") for m in p.list_free_models("k"))

    def test_ollama_cloud_allowlist(self, httpx_mock):
        p = make_provider(spec_by_name("ollama_cloud"))
        httpx_mock.add_response(
            url="https://ollama.com/v1/models",
            json={"data": [{"id": "gpt-oss:120b"}, {"id": "glm-5.3-flash"}, {"id": "beam"}]},
        )
        assert [m.api_id for m in p.list_free_models("k")] == ["gpt-oss:120b", "beam"]

    def test_normalize(self):
        assert normalize_model_id("models/Gemini-2.5-Flash") == normalize_model_id("gemini-2.5-flash")
        assert normalize_model_id("openai/gpt-oss-120b:free") == "gptoss120b"
