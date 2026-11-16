"""Provider switching with per-provider model ids (auto candidates and
explicit-id aliases)."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from freeride.core.auto_model import alias_map_for, resolve_auto_candidates
from freeride.core.chat_schema import ChatRequest
from freeride.core.cooldown import KeyCooldown
from freeride.core.errors import ErrorKind
from freeride.core.failover import FailoverContext, try_call_with_failover, try_stream_with_failover
from tests.fixtures.noop_provider import NoopProvider


class _Stub(NoopProvider):
    """NoopProvider that can be told to fail with a given status, for
    every model (``fail_status``) or for specific ids (``fail_models``)."""

    cooldown_scope = "key"

    def __init__(self, name: str, fail_status: int | None = None, fail_models: dict[str, int] | None = None):
        self.name = name
        self.fail_status = fail_status
        self.fail_models = fail_models or {}
        self.seen: list[str] = []

    def _maybe_fail(self, model_id):
        status = self.fail_models.get(model_id, self.fail_status)
        if status == "timeout":
            raise httpx.ReadTimeout("slow", request=httpx.Request("POST", f"https://{self.name}.test"))
        if status:
            req = httpx.Request("POST", f"https://{self.name}.test/v1/chat/completions")
            resp = httpx.Response(status, json={"error": {"message": "nope"}}, request=req)
            raise httpx.HTTPStatusError("fail", request=req, response=resp)

    async def forward_chat(self, request, model_id, key):
        self.seen.append(model_id)
        self._maybe_fail(model_id)
        return await super().forward_chat(request, model_id, key)

    async def forward_chat_stream(self, request, model_id, key):
        self.seen.append(model_id)
        self._maybe_fail(model_id)
        async for ev in super().forward_chat_stream(request, model_id, key):
            yield ev

    def classify_error(self, e):
        if isinstance(e, httpx.TimeoutException):
            return ErrorKind.TIMEOUT
        status = getattr(getattr(e, "response", None), "status_code", None) or getattr(e, "status_code", None)
        return {401: ErrorKind.AUTH, 402: ErrorKind.QUOTA_EXHAUSTED, 404: ErrorKind.MODEL_NOT_FOUND, 429: ErrorKind.RATE_LIMIT}.get(status, ErrorKind.UNKNOWN)


class _NoCooldown:
    def available_keys(self, provider, keys):
        return list(keys)


@pytest.fixture
def cooldown(tmp_path):
    return KeyCooldown(tmp_path / "cd.json")


class TestCandidates:
    def test_one_best_model_per_provider_in_rank_order(self, monkeypatch):
        monkeypatch.setattr("freeride.core.auto_model._provider_keys", lambda n: ["k"])
        monkeypatch.setattr("freeride.core.auto_model.KeyCooldown.shared", lambda *a, **k: _NoCooldown())
        catalog = [
            {"id": "shared", "available_providers": ["a", "b"], "provider_ids": {"a": "shared", "b": "shared-b"}},
            {"id": "only-c", "available_providers": ["c"], "provider_ids": {"c": "only-c"}},
            {"id": "second-a", "available_providers": ["a"], "provider_ids": {"a": "second-a"}},
        ]
        provs = [_Stub("a"), _Stub("b"), _Stub("c")]
        cands = resolve_auto_candidates(provs, catalog, leaderboard={}, health_cache={}, per_provider=1)
        # "shared" scores highest (two providers); each provider appears once, with its own spelling.
        assert cands == [("shared", "a"), ("shared-b", "b"), ("only-c", "c")]
        # Default: several per provider, still rank-ordered; the map keeps that order.
        cands = resolve_auto_candidates(provs, catalog, leaderboard={}, health_cache={})
        assert cands == [("shared", "a"), ("shared-b", "b"), ("only-c", "c"), ("second-a", "a")]
        from freeride.core.auto_model import candidates_by_provider

        assert candidates_by_provider(cands) == {"a": ["shared", "second-a"], "b": ["shared-b"], "c": ["only-c"]}

    def test_alias_map(self):
        catalog = [{"id": "x:free", "aliases": ["x"], "provider_ids": {"kilo": "x:free", "blockrun": "x"}}]
        assert alias_map_for(catalog, "x", ["kilo", "blockrun", "other"]) == {"kilo": "x:free", "blockrun": "x", "other": "x"}
        assert alias_map_for(catalog, "unknown", ["kilo"]) == {"kilo": "unknown"}


class TestFailoverLoop:
    async def test_non_stream_switches_provider_with_its_own_model(self, cooldown):
        a, b = _Stub("a", fail_status=402), _Stub("b")
        body = ChatRequest(model="model-a", messages=[{"role": "user", "content": "hi"}])
        chosen, resp = await try_call_with_failover(
            [(a, ["ka"]), (b, ["kb"])],
            cooldown,
            FailoverContext(request_id="r"),
            call=lambda p, k: p.forward_chat(body, body.model, k),
            model=body.model,
            model_for={"a": "model-a", "b": "model-b"},
            body=body,
        )
        assert chosen is b and resp.model == "model-b"
        assert a.seen == ["model-a"] and b.seen == ["model-b"]

    async def test_stream_switches_provider_with_its_own_model(self, cooldown):
        a, b = _Stub("a", fail_status=429), _Stub("b")
        body = ChatRequest(model="model-a", messages=[{"role": "user", "content": "hi"}], stream=True)
        chosen, first, _ = await try_stream_with_failover(
            [(a, ["ka"]), (b, ["kb"])],
            body,
            cooldown,
            FailoverContext(request_id="r"),
            model_for={"a": "model-a", "b": "model-b"},
        )
        assert chosen is b and first.model == "model-b"
        assert b.seen == ["model-b"]

    async def test_unmapped_provider_is_skipped(self, cooldown):
        a, b = _Stub("a", fail_status=402), _Stub("b")
        body = ChatRequest(model="model-a", messages=[{"role": "user", "content": "hi"}])
        chosen, _ = await try_call_with_failover(
            [(a, ["ka"]), (b, ["kb"])],
            cooldown,
            FailoverContext(request_id="r"),
            call=lambda p, k: p.forward_chat(body, body.model, k),
            model=body.model,
            model_for={"a": "model-a"},
            body=body,
        )
        assert chosen is None and b.seen == []

    async def test_no_map_keeps_legacy_behaviour(self, cooldown):
        a, b = _Stub("a", fail_status=402), _Stub("b")
        body = ChatRequest(model="same", messages=[{"role": "user", "content": "hi"}])
        chosen, _ = await try_call_with_failover(
            [(a, ["ka"]), (b, ["kb"])],
            cooldown,
            FailoverContext(request_id="r"),
            call=lambda p, k: p.forward_chat(body, body.model, k),
            model=body.model,
        )
        assert chosen is b and b.seen == ["same"]


class TestChatRoute:
    def _client(self, monkeypatch, providers, catalog):
        from freeride.core.health import ProviderHealth
        from freeride.server.app import create_app

        monkeypatch.setenv("FREERIDE_EVENTS", "0")
        monkeypatch.setattr("freeride.core.provider_env.all_keys_for", lambda name: ["k"])
        monkeypatch.setattr("freeride.core.failover.all_keys_for", lambda name: ["k"])
        monkeypatch.setattr("freeride.core.auto_model._provider_keys", lambda name: ["k"])
        monkeypatch.setattr("freeride.core.cooldown.KeyCooldown.available_keys", lambda self, n, keys: list(keys))
        monkeypatch.setattr("freeride.core.auto_model.fetch_leaderboard", lambda: {})
        monkeypatch.setattr("freeride.core.model_health.load_cache", lambda: {})

        async def fake_catalog(*a, **k):
            return catalog

        monkeypatch.setattr("freeride.server.routes.chat.get_or_fetch_catalog", fake_catalog)
        ProviderHealth.reset()
        return TestClient(create_app(providers=providers))

    def test_auto_falls_over_to_next_providers_own_model(self, monkeypatch):
        gemini, ollama = _Stub("gemini", fail_status=402), _Stub("ollama_cloud")
        catalog = [
            {"id": "gemini-3-flash", "available_providers": ["gemini"], "provider_ids": {"gemini": "gemini-3-flash"}},
            {"id": "gpt-oss:120b", "available_providers": ["ollama_cloud"], "provider_ids": {"ollama_cloud": "gpt-oss:120b"}},
        ]
        client = self._client(monkeypatch, [gemini, ollama], catalog)
        r = client.post("/v1/chat/completions", json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200, r.text
        assert r.json()["_freeride_provider"] == "ollama_cloud"
        assert r.json()["model"] == "gpt-oss:120b"
        assert gemini.seen == ["gemini-3-flash"] and ollama.seen == ["gpt-oss:120b"]

    def test_explicit_id_is_spelled_per_provider(self, monkeypatch):
        kilo, blockrun = _Stub("kilo", fail_status=429), _Stub("blockrun")
        catalog = [
            {"id": "nvidia/nemotron:free", "aliases": ["nvidia/nemotron"], "available_providers": ["kilo", "blockrun"],
             "provider_ids": {"kilo": "nvidia/nemotron:free", "blockrun": "nvidia/nemotron"}},
        ]
        client = self._client(monkeypatch, [kilo, blockrun], catalog)
        r = client.post("/v1/chat/completions", json={"model": "nvidia/nemotron:free", "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200, r.text
        assert r.json()["_freeride_provider"] == "blockrun"
        assert blockrun.seen == ["nvidia/nemotron"]



class TestWithinProviderModelSwitch:
    """Model-level failures move to the next candidate on the same
    provider; key-level failures do not retry the same keys for the
    next model; quota exhaustion leaves the provider."""

    async def test_gated_model_then_next_model_same_provider(self, cooldown, monkeypatch):
        demoted = []
        monkeypatch.setattr("freeride.core.model_health.mark_recent_failure", lambda p, m: demoted.append((p, m)))
        a = _Stub("a", fail_models={"gated": 404})
        body = ChatRequest(model="gated", messages=[{"role": "user", "content": "hi"}])
        chosen, resp = await try_call_with_failover(
            [(a, ["ka"])], cooldown, FailoverContext(request_id="r"),
            call=lambda p, k: p.forward_chat(body, body.model, k), model=body.model,
            model_for={"a": ["gated", "good"]}, body=body,
        )
        assert chosen is a and resp.model == "good"
        assert a.seen == ["gated", "good"]
        assert demoted == [("a", "gated")]
        assert not cooldown.is_in_cooldown("a", "ka")

    async def test_stream_timeout_then_next_model(self, cooldown, monkeypatch):
        monkeypatch.setattr("freeride.core.model_health.mark_recent_failure", lambda p, m: None)
        a = _Stub("a", fail_models={"slow": "timeout"})
        body = ChatRequest(model="slow", messages=[{"role": "user", "content": "hi"}], stream=True)
        chosen, first, _ = await try_stream_with_failover(
            [(a, ["ka"])], body, cooldown, FailoverContext(request_id="r"), model_for={"a": ["slow", "fast"]},
        )
        assert chosen is a and first.model == "fast" and a.seen == ["slow", "fast"]

    async def test_model_scoped_429_then_next_model(self, cooldown, monkeypatch):
        monkeypatch.setattr("freeride.core.model_health.mark_recent_failure", lambda p, m: None)
        a = _Stub("a", fail_models={"busy": 429})
        a.cooldown_scope = "model"
        body = ChatRequest(model="busy", messages=[{"role": "user", "content": "hi"}])
        chosen, resp = await try_call_with_failover(
            [(a, ["ka"])], cooldown, FailoverContext(request_id="r"),
            call=lambda p, k: p.forward_chat(body, body.model, k), model=body.model,
            model_for={"a": ["busy", "calm"]}, body=body,
        )
        assert chosen is a and resp.model == "calm"
        assert not cooldown.is_in_cooldown("a", "ka")

    async def test_key_level_failure_does_not_retry_next_model(self, cooldown):
        a, b = _Stub("a", fail_status=401), _Stub("b")
        body = ChatRequest(model="m1", messages=[{"role": "user", "content": "hi"}])
        chosen, resp = await try_call_with_failover(
            [(a, ["ka"]), (b, ["kb"])], cooldown, FailoverContext(request_id="r"),
            call=lambda p, k: p.forward_chat(body, body.model, k), model=body.model,
            model_for={"a": ["m1", "m2"], "b": ["mb"]}, body=body,
        )
        assert chosen is b and a.seen == ["m1"]  # m2 never tried on cooled key

    async def test_quota_leaves_provider_immediately(self, cooldown):
        a, b = _Stub("a", fail_status=402), _Stub("b")
        body = ChatRequest(model="m1", messages=[{"role": "user", "content": "hi"}])
        chosen, _ = await try_call_with_failover(
            [(a, ["ka"]), (b, ["kb"])], cooldown, FailoverContext(request_id="r"),
            call=lambda p, k: p.forward_chat(body, body.model, k), model=body.model,
            model_for={"a": ["m1", "m2"], "b": ["mb"]}, body=body,
        )
        assert chosen is b and a.seen == ["m1"]

    def test_chat_route_auto_switches_model_within_provider(self, monkeypatch):
        monkeypatch.setattr("freeride.core.model_health.mark_recent_failure", lambda p, m: None)
        ollama = _Stub("ollama_cloud", fail_models={"glm-5.3-flash": 404})
        catalog = [
            {"id": "glm-5.3-flash", "available_providers": ["ollama_cloud"], "provider_ids": {"ollama_cloud": "glm-5.3-flash"}},
            {"id": "gpt-oss:120b", "available_providers": ["ollama_cloud"], "provider_ids": {"ollama_cloud": "gpt-oss:120b"}},
        ]
        client = TestChatRoute()._client(monkeypatch, [ollama], catalog)
        r = client.post("/v1/chat/completions", json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200, r.text
        assert r.json()["model"] == "gpt-oss:120b"
        assert ollama.seen == ["glm-5.3-flash", "gpt-oss:120b"]


class TestListFallback:
    def test_list_without_openrouter_key_shows_all_providers(self, monkeypatch, capsys):
        from freeride.v2compat import commands as cmd

        monkeypatch.setattr(cmd, "get_api_keys", lambda: [])
        monkeypatch.setattr("freeride.cli.cmd_serve.build_provider_registry", lambda: [_Stub("xkiro")])
        monkeypatch.setattr("freeride.core.provider_env.all_keys_for", lambda n: ["k"] if n == "xkiro" else [])

        class Args:
            limit = 10
            refresh = False

        cmd.cmd_list(Args())
        out = capsys.readouterr().out
        assert "xkiro" in out and "noop/echo" in out and "Total: 1 free models" in out



class TestSlowRequestRegressions:
    """The 4-minute-review bug: timeout demotions were lost behind a stale
    audit stamp, 413s were retried as unknown, and slow providers ranked
    ahead of fast ones."""

    def test_recent_failures_survive_a_stale_audit_stamp(self, tmp_path, monkeypatch):
        import time

        from freeride.core import model_health as mh

        monkeypatch.setattr(mh, "CACHE_PATH", tmp_path / "model_health.json")
        stale = int(time.time()) - 3 * 86400
        mh.save_cache({"nvidia_nim::old": mh.HealthEntry("ok", 100, stale)}, as_of=stale)
        mh.mark_recent_failure("nvidia_nim", "openai/gpt-oss-20b")
        mh.mark_recent_failure("nvidia_nim", "deepseek-ai/deepseek-v4.1-flash")
        cache = mh.load_cache()
        assert "nvidia_nim::old" not in cache  # audit verdict expired with its stamp
        assert mh.is_model_known_broken("nvidia_nim", "openai/gpt-oss-20b", cache=cache)
        assert mh.is_model_known_broken("nvidia_nim", "deepseek-ai/deepseek-v4.1-flash", cache=cache)

    async def test_413_moves_to_next_model_not_next_key(self, cooldown, monkeypatch):
        demoted = []
        monkeypatch.setattr("freeride.core.model_health.mark_recent_failure", lambda p, m: demoted.append((p, m)))
        a = _Stub("groq", fail_models={"big": 413})
        a.classify_error = lambda e: ErrorKind.PAYLOAD_TOO_LARGE if getattr(e, "status_code", None) == 413 else ErrorKind.UNKNOWN
        body = ChatRequest(model="big", messages=[{"role": "user", "content": "hi"}])
        chosen, resp = await try_call_with_failover(
            [(a, ["k1", "k2", "k3"])], cooldown, FailoverContext(request_id="r"),
            call=lambda p, k: p.forward_chat(body, body.model, k), model=body.model,
            model_for={"groq": ["big", "small"]}, body=body,
        )
        assert chosen is a and resp.model == "small"
        assert a.seen == ["big", "small"]  # not retried on k2/k3
        assert demoted == [("groq", "big")]

    def test_groq_413_classifies_as_payload_too_large(self):
        import httpx

        from freeride.providers.groq import GroqProvider

        r = httpx.Response(413, json={"error": {"message": "Request too large for model"}}, request=httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions"))
        assert GroqProvider().classify_error(r) is ErrorKind.PAYLOAD_TOO_LARGE

    def test_slow_provider_ranks_behind_fast_one(self, monkeypatch):
        from freeride.core.health import ProviderHealth

        monkeypatch.setattr("freeride.core.auto_model._provider_keys", lambda n: ["k"])
        monkeypatch.setattr("freeride.core.auto_model.KeyCooldown.shared", lambda *a, **k: _NoCooldown())
        ProviderHealth.reset()
        h = ProviderHealth.instance()
        for _ in range(5):
            h.record("nvidia_nim", ok=True, duration_ms=30_000, key="k")
            h.record("groq", ok=True, duration_ms=400, key="k")
        catalog = [{"id": "openai/gpt-oss-20b", "available_providers": ["nvidia_nim", "groq"], "provider_ids": {"nvidia_nim": "openai/gpt-oss-20b", "groq": "openai/gpt-oss-20b"}}]
        cands = resolve_auto_candidates([_Stub("nvidia_nim"), _Stub("groq")], catalog, leaderboard={}, health_cache={})
        assert [c[1] for c in cands] == ["groq", "nvidia_nim"]
        ProviderHealth.reset()


    def test_demotion_recorded_under_provider_spelling_is_honoured(self, monkeypatch):
        """A grouped entry's primary id is OpenRouter's ``:free`` spelling;
        the demotion was written under NIM's own id. The filter must see it."""
        import time

        from freeride.core.model_health import RECENT_FAILURE_STATUS, HealthEntry

        monkeypatch.setattr("freeride.core.auto_model._provider_keys", lambda n: ["k"])
        monkeypatch.setattr("freeride.core.auto_model.KeyCooldown.shared", lambda *a, **k: _NoCooldown())
        catalog = [{"id": "google/gemma-4-31b-it:free", "available_providers": ["openrouter", "nvidia_nim"],
                    "provider_ids": {"openrouter": "google/gemma-4-31b-it:free", "nvidia_nim": "google/gemma-4-31b-it"}},
                   {"id": "nvidia/nemotron-3-ultra-550b-a55b", "available_providers": ["nvidia_nim"], "provider_ids": {"nvidia_nim": "nvidia/nemotron-3-ultra-550b-a55b"}}]
        cache = {"nvidia_nim::google/gemma-4-31b-it": HealthEntry(RECENT_FAILURE_STATUS, 0, int(time.time()))}
        cands = resolve_auto_candidates([_Stub("openrouter"), _Stub("nvidia_nim")], catalog, leaderboard={}, health_cache=cache)
        assert ("google/gemma-4-31b-it", "nvidia_nim") not in cands
        assert ("google/gemma-4-31b-it:free", "openrouter") in cands
        assert ("nvidia/nemotron-3-ultra-550b-a55b", "nvidia_nim") in cands


class TestCodexRoute:
    """``POST /v1/responses`` (Codex) must use the same per-provider
    candidate plan as chat: never send one provider's spelling to the
    others, and lead with the best candidates."""

    def _client(self, monkeypatch, providers, catalog):
        from freeride.core.health import ProviderHealth
        from freeride.server.app import create_app

        monkeypatch.setenv("FREERIDE_EVENTS", "0")
        monkeypatch.setattr("freeride.core.provider_env.all_keys_for", lambda name: ["k"])
        monkeypatch.setattr("freeride.core.failover.all_keys_for", lambda name: ["k"])
        monkeypatch.setattr("freeride.core.auto_model._provider_keys", lambda name: ["k"])
        monkeypatch.setattr("freeride.core.cooldown.KeyCooldown.available_keys", lambda self, n, keys: list(keys))
        monkeypatch.setattr("freeride.core.auto_model.fetch_leaderboard", lambda: {})
        monkeypatch.setattr("freeride.core.model_health.load_cache", lambda: {})

        async def fake_catalog(*a, **k):
            return catalog

        monkeypatch.setattr("freeride.server.routes.codex.get_or_fetch_catalog", fake_catalog)
        ProviderHealth.reset()
        return TestClient(create_app(providers=providers))

    _CATALOG = [
        {"id": "google/gemma-4-31b-it:free", "available_providers": ["openrouter"],
         "provider_ids": {"openrouter": "google/gemma-4-31b-it:free"}},
        {"id": "gemma-4-31B-it", "available_providers": ["sambanova"],
         "provider_ids": {"sambanova": "gemma-4-31B-it"}},
    ]

    def test_non_streaming_uses_each_providers_own_spelling(self, monkeypatch):
        openrouter, sambanova = _Stub("openrouter", fail_status=402), _Stub("sambanova")
        client = self._client(monkeypatch, [openrouter, sambanova], self._CATALOG)
        r = client.post("/v1/responses", json={"model": "gpt-5-codex", "input": "hi"})
        assert r.status_code == 200, r.text
        assert r.headers["X-FreeRide-Provider"] == "sambanova"
        assert r.json()["model"] == "gpt-5-codex"  # echoed, not the upstream id
        assert openrouter.seen == ["google/gemma-4-31b-it:free"]
        assert sambanova.seen == ["gemma-4-31B-it"]

    def test_streaming_uses_each_providers_own_spelling(self, monkeypatch):
        openrouter, sambanova = _Stub("openrouter", fail_status=429), _Stub("sambanova")
        client = self._client(monkeypatch, [openrouter, sambanova], self._CATALOG)
        r = client.post("/v1/responses", json={"model": "gpt-5-codex", "input": "hi", "stream": True})
        assert r.status_code == 200, r.text
        assert r.headers["X-FreeRide-Provider"] == "sambanova"
        assert "response.completed" in r.text
        assert openrouter.seen == ["google/gemma-4-31b-it:free"]
        assert sambanova.seen == ["gemma-4-31B-it"]
