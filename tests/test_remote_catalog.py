"""The live catalog: fetch, cache, merge into model health, roster overrides, publishing."""

from __future__ import annotations

import json
import time

import httpx
import pytest

from freeride.core import model_health as mh
from freeride.core import remote_catalog as rc
from freeride.core.types import Model


def _doc(generated_at=None, **providers):
    return {"schema": 1, "generated_at": generated_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "generated_by": "test", "providers": providers}


@pytest.fixture
def local(tmp_path, monkeypatch):
    path = tmp_path / "remote_catalog.json"
    monkeypatch.setattr(rc, "LOCAL_PATH", path)
    monkeypatch.delenv("FREERIDE_REMOTE_CATALOG", raising=False)
    monkeypatch.delenv("FREERIDE_CATALOG_URL", raising=False)
    return path


class TestFetch:
    def test_fetch_stores_catalog_and_etag(self, local, httpx_mock):
        httpx_mock.add_response(url=rc.DEFAULT_CATALOG_URL, json=_doc(groq={"broken": {}}), headers={"etag": '"abc"'})
        doc = rc.fetch_remote_catalog()
        assert doc["providers"] == {"groq": {"broken": {}}}
        stored = json.loads(local.read_text())
        assert stored["_etag"] == '"abc"' and stored["catalog"]["schema"] == 1
        req = httpx_mock.get_request()
        assert req.headers["user-agent"].startswith("freeride-gateway/")
        assert "if-none-match" not in req.headers

    def test_304_keeps_current(self, local, httpx_mock):
        rc._write_local(local, {"catalog": _doc(a={}), "_etag": '"v1"', "fetched_at": time.time() - 100})
        httpx_mock.add_response(url=rc.DEFAULT_CATALOG_URL, status_code=304)
        doc = rc.fetch_remote_catalog()
        assert doc["providers"] == {"a": {}}
        assert httpx_mock.get_request().headers["if-none-match"] == '"v1"'
        assert time.time() - json.loads(local.read_text())["fetched_at"] < 5

    def test_invalid_or_error_keeps_current(self, local, httpx_mock):
        rc._write_local(local, {"catalog": _doc(a={}), "_etag": None, "fetched_at": time.time()})
        httpx_mock.add_response(url=rc.DEFAULT_CATALOG_URL, json={"schema": 2})
        assert rc.fetch_remote_catalog()["providers"] == {"a": {}}
        httpx_mock.add_response(url=rc.DEFAULT_CATALOG_URL, status_code=500)
        assert rc.fetch_remote_catalog()["providers"] == {"a": {}}
        httpx_mock.add_exception(httpx.ConnectError("down"), url=rc.DEFAULT_CATALOG_URL)
        assert rc.fetch_remote_catalog()["providers"] == {"a": {}}

    def test_disabled_and_stale(self, local, monkeypatch):
        rc._write_local(local, {"catalog": _doc(a={}), "_etag": None, "fetched_at": time.time() - 3 * 86400})
        assert rc.load_remote_catalog() == {}  # stale
        rc._write_local(local, {"catalog": _doc(a={}), "_etag": None, "fetched_at": time.time()})
        assert rc.load_remote_catalog()["providers"] == {"a": {}}
        monkeypatch.setenv("FREERIDE_REMOTE_CATALOG", "0")
        assert rc.load_remote_catalog() == {} and rc.fetch_remote_catalog() is None


class TestConsumption:
    def test_broken_entries_respect_24h_clock(self):
        now = time.time()
        fresh = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 3600))
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 2 * 86400))
        doc = _doc(nim={"broken": {"slow": {"status": "timeout", "since": fresh, "latency_ms": 30000}, "ancient": {"status": "timeout", "since": old}}})
        out = rc.broken_entries(doc, now=now)
        assert set(out) == {"nim::slow"} and out["nim::slow"][0] == rc.REMOTE_BROKEN_STATUS

    def test_merged_into_health_cache_and_filtered_by_router(self, local, tmp_path, monkeypatch):
        monkeypatch.setattr(mh, "CACHE_PATH", tmp_path / "model_health.json")
        rc._write_local(local, {"catalog": _doc(nvidia_nim={"broken": {"openai/gpt-oss-20b": {"status": "timeout", "since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}}}), "_etag": None, "fetched_at": time.time()})
        cache = mh.load_cache()  # no local file at all
        assert mh.is_model_known_broken("nvidia_nim", "openai/gpt-oss-20b", cache=cache)
        assert not mh.is_model_known_broken("groq", "openai/gpt-oss-20b", cache=cache)
        # A fresher local verdict wins over the remote row.
        mh.save_cache({"nvidia_nim::openai/gpt-oss-20b": mh.HealthEntry("ok", 500, int(time.time()) + 5)})
        cache = mh.load_cache()
        assert not mh.is_model_known_broken("nvidia_nim", "openai/gpt-oss-20b", cache=cache)

    def test_roster_override_order(self, local, monkeypatch, httpx_mock):
        from freeride.core.provider_specs import spec_by_name
        from freeride.providers.openai_compat import make_provider

        rc._write_local(local, {"catalog": _doc(ollama_cloud={"free_ids": ["beam", "new-starter"]}), "_etag": None, "fetched_at": time.time()})
        p = make_provider(spec_by_name("ollama_cloud"))
        httpx_mock.add_response(url="https://ollama.com/v1/models", json={"data": [{"id": "beam"}, {"id": "new-starter"}, {"id": "gpt-oss:120b"}, {"id": "glm-5.3-flash"}]}, is_reusable=True)
        assert [m.api_id for m in p.list_free_models("k")] == ["beam", "new-starter"]  # remote roster over the spec's
        monkeypatch.setenv("OLLAMA_CLOUD_FREE_MODELS_OVERRIDE", "gpt-oss:120b")
        assert [m.api_id for m in p.list_free_models("k")] == ["gpt-oss:120b"]  # env wins over remote


class TestPublishing:
    def test_build_artifact(self):
        now = int(time.time())
        health = {
            "groq::ok-model": mh.HealthEntry("ok", 400, now),
            "groq::dead": mh.HealthEntry("model_not_found", 100, now),
            "groq::slow": mh.HealthEntry("timeout", 30000, now),
            "groq::blip": mh.HealthEntry(mh.RECENT_FAILURE_STATUS, 0, now),  # live blip, not published
            "groq::my-quota": mh.HealthEntry("quota_exhausted", 50, now),  # about the publisher's key, not the model
            "groq::my-limit": mh.HealthEntry("rate_limit", 50, now),
        }
        models = {"groq": [Model(api_id="ok-model", provider="groq", context_length=131072, supported_parameters=("tools",)), Model(api_id="listed-only", provider="groq")]}
        art = rc.build_artifact(health, models, generated_by="test", rosters={"ollama_cloud": ["beam"]})
        assert rc.valid(art)
        g = art["providers"]["groq"]
        assert set(g["broken"]) == {"dead", "slow"}
        assert g["models"]["my-quota"] == {"ok": False, "latency_ms": 50, "status": "quota_exhausted"}
        assert g["models"]["ok-model"] == {"ok": True, "latency_ms": 400, "status": "ok", "tools": True, "context_length": 131072}
        assert g["models"]["listed-only"]["ok"] is None
        assert art["providers"]["ollama_cloud"]["free_ids"] == ["beam"]

    def test_publish_posts_with_token(self, monkeypatch, httpx_mock):
        monkeypatch.setenv("FREERIDE_ADMIN_TOKEN", "t0k")
        httpx_mock.add_response(url="https://api.free-ride.xyz/v1/_admin/catalog", json={"ok": True, "providers": 1})
        assert rc.publish(_doc(a={}))["ok"] is True
        req = httpx_mock.get_request()
        assert req.headers["authorization"] == "Bearer t0k" and json.loads(req.content)["schema"] == 1

    def test_publish_without_token(self, monkeypatch):
        monkeypatch.delenv("FREERIDE_ADMIN_TOKEN", raising=False)
        with pytest.raises(RuntimeError):
            rc.publish(_doc())


class TestCli:
    def test_catalog_status(self, local, capsys):
        from freeride.cli.cmd_catalog import cmd_catalog

        class Args:
            action = "status"

        assert cmd_catalog(Args()) == 1
        rc._write_local(local, {"catalog": _doc(groq={"models": {"a": {}}, "broken": {}}), "_etag": None, "fetched_at": time.time()})
        assert cmd_catalog(Args()) == 0
        out = capsys.readouterr().out
        assert "providers : 1" in out and "models    : 1" in out
