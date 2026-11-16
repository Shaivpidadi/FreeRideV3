"""``freeride bind fx`` and ``freeride daemon``."""

from __future__ import annotations

import json
import subprocess

import pytest

from freeride.binders import fx
from freeride.cli import cmd_daemon


class TestFxBinder:
    def test_writes_connection_presets_default_and_skill(self, tmp_path):
        cfg = tmp_path / "settings.json"
        skills = tmp_path / "skills"
        msg = fx.bind("http://127.0.0.1:11343/v1", config_path=cfg, skills_dir=skills, with_catalog=False)
        data = json.loads(cfg.read_text())
        conn = data["providers"]["freeride"]
        assert conn["protocol"] == "openai-chat-completions"
        assert conn["base_url"] == "http://127.0.0.1:11343/v1"
        assert conn["auth"] == {"type": "none"} and conn["fetch_models"] is True
        assert set(conn["model_metadata"]) == set(fx.PRESET_METADATA)
        assert data["models"]["freeride"] == "freeride/coding"
        assert data["provider"] == "freeride"
        skill = skills / "freeride" / "SKILL.md"
        assert skill.exists() and "freeride daemon" in skill.read_text() and "ridex" not in skill.read_text()
        assert "selected, none was set" in msg

    def test_preserves_other_connections_and_selection(self, tmp_path):
        cfg = tmp_path / "settings.json"
        cfg.write_text(json.dumps({
            "provider": "local",
            "providers": {"local": {"protocol": "openai-chat-completions", "base_url": "http://localhost:11434/v1", "auth": {"type": "none"}}},
            "models": {"local": "qwen2.5:1.5b"},
            "permission_mode": "auto",
        }))
        msg = fx.bind("http://127.0.0.1:11343/v1", config_path=cfg, skills_dir=tmp_path / "s", with_catalog=False)
        data = json.loads(cfg.read_text())
        assert data["provider"] == "local"
        assert data["providers"]["local"]["base_url"] == "http://localhost:11434/v1"
        assert data["models"] == {"local": "qwen2.5:1.5b", "freeride": "freeride/coding"}
        assert data["permission_mode"] == "auto"
        assert "fx provider freeride" in msg

    def test_catalog_metadata_from_gateway(self, tmp_path, httpx_mock):
        httpx_mock.add_response(
            url="http://127.0.0.1:11343/v1/models",
            json={"data": [
                {"id": "big/tools", "context_length": 262144, "supported_parameters": ["tools"]},
                {"id": "small/tools", "context_length": 8192, "supported_parameters": ["tools"]},
                {"id": "no/tools", "context_length": 131072, "supported_parameters": []},
                {"id": "no/ctx", "context_length": 0, "supported_parameters": ["tools"]},
            ]},
        )
        fx.bind("http://127.0.0.1:11343/v1", config_path=tmp_path / "settings.json", skills_dir=tmp_path / "s")
        meta = json.loads((tmp_path / "settings.json").read_text())["providers"]["freeride"]["model_metadata"]
        assert list(k for k in meta if k not in fx.PRESET_METADATA) == ["big/tools", "small/tools"]
        assert meta["big/tools"] == {"context_window": 262144, "max_output_tokens": 8192, "supports_tool_use": True}
        assert meta["small/tools"]["max_output_tokens"] == 1024

    def test_unreachable_gateway_still_binds(self, tmp_path, httpx_mock):
        import httpx

        httpx_mock.add_exception(httpx.ConnectError("refused"), url="http://127.0.0.1:11343/v1/models")
        msg = fx.bind("http://127.0.0.1:11343/v1", config_path=tmp_path / "settings.json", skills_dir=tmp_path / "s")
        assert "0 tool-capable catalog models" in msg

    def test_refuses_unparseable_settings(self, tmp_path):
        cfg = tmp_path / "settings.json"
        cfg.write_text("{ nope")
        with pytest.raises(ValueError):
            fx.bind("http://127.0.0.1:11343/v1", config_path=cfg, skills_dir=tmp_path / "s", with_catalog=False)
        assert cfg.read_text() == "{ nope"

    def test_cli_dispatch(self, tmp_path, monkeypatch, capsys):
        from freeride.cli.cmd_bind import cmd_bind

        monkeypatch.setattr(fx, "FX_SETTINGS_PATH", tmp_path / "settings.json")
        monkeypatch.setattr(fx, "FX_SKILLS_DIR", tmp_path / "skills")
        monkeypatch.setattr(fx, "catalog_metadata", lambda *a, **k: {})

        class Args:
            agent = "fx"
            gateway_url = "http://localhost:11343/v1"

        assert cmd_bind(Args()) == 0
        assert "providers.freeride" in capsys.readouterr().out


class _FakeRun:
    """Records supervisor commands; launchctl print says 'gone' after bootout."""

    def __init__(self, fail_bootstrap=False):
        self.calls: list[list[str]] = []
        self.fail_bootstrap = fail_bootstrap
        self.registered = False

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        rc = 0
        if cmd[:2] == ["launchctl", "bootstrap"]:
            rc = 1 if self.fail_bootstrap else 0
            self.registered = rc == 0
        elif cmd[:2] == ["launchctl", "bootout"]:
            self.registered = False
        elif cmd[:2] == ["launchctl", "print"]:
            rc = 0 if self.registered else 1
        return subprocess.CompletedProcess(cmd, rc, "", "")


class _FakePopen:
    def __init__(self):
        self.started = 0

    def __call__(self, argv, **kw):
        self.started += 1

        class P:
            pid = 4242

        return P()


@pytest.fixture
def paths(tmp_path):
    return cmd_daemon.Paths(home=tmp_path, port=11399)


class TestDaemon:
    def test_launchd_install_writes_plist_and_bootstraps(self, paths, monkeypatch):
        run, popen = _FakeRun(), _FakePopen()
        health = iter([False, True])
        monkeypatch.setattr(cmd_daemon, "healthy", lambda url, timeout=2.0: next(health, True))
        d = cmd_daemon.Daemon(paths, run=run, popen=popen, kind="launchd")
        ok, msg = d.start(wait_s=1)
        assert ok and "launchd" in msg
        assert paths.plist.exists() and LAUNCHD_LABEL_IN(paths.plist.read_text())
        assert ["launchctl", "bootstrap", f"gui/{__import__('os').getuid()}", str(paths.plist)] in run.calls
        assert popen.started == 0
        assert paths.log.exists() and (paths.log.stat().st_mode & 0o777) == 0o600

    def test_launchd_bootstrap_failure_falls_back_to_plain_process(self, paths, monkeypatch):
        run, popen = _FakeRun(fail_bootstrap=True), _FakePopen()
        monkeypatch.setattr(cmd_daemon, "healthy", lambda url, timeout=2.0: True)
        d = cmd_daemon.Daemon(paths, run=run, popen=popen, kind="launchd")
        monkeypatch.setattr(cmd_daemon, "healthy", lambda url, timeout=2.0: popen.started > 0)
        ok, msg = d.start(wait_s=1)
        assert ok and "plain background process" in msg
        assert paths.pid_file.read_text() == "4242"

    def test_systemd_unit_and_stop_sticks(self, paths, monkeypatch):
        run, popen = _FakeRun(), _FakePopen()
        monkeypatch.setattr(cmd_daemon, "healthy", lambda url, timeout=2.0: True)
        monkeypatch.setattr(cmd_daemon, "_port_bound", lambda port: False)
        d = cmd_daemon.Daemon(paths, run=run, popen=popen, kind="systemd")
        d.p.stop_marker.parent.mkdir(parents=True, exist_ok=True)
        d.spawn()
        unit = paths.unit.read_text()
        assert "ExecStart=" in unit and "--port 11399" in unit and "UMask=0077" in unit
        assert ["systemctl", "--user", "enable", "--now", "freeride.service"] in run.calls
        d.stop()
        assert paths.stop_marker.exists()
        assert ["systemctl", "--user", "disable", "freeride.service"] in run.calls
        st = d.status()
        assert st["stopped_by_user"] and st["supervision"] == "systemd"

    def test_start_clears_stop_marker_and_skips_spawn_when_healthy(self, paths, monkeypatch):
        run, popen = _FakeRun(), _FakePopen()
        monkeypatch.setattr(cmd_daemon, "healthy", lambda url, timeout=2.0: True)
        d = cmd_daemon.Daemon(paths, run=run, popen=popen, kind="none")
        paths.state_dir.mkdir(parents=True)
        paths.stop_marker.touch()
        ok, msg = d.start()
        assert ok and "already healthy" in msg and not paths.stop_marker.exists()
        assert popen.started == 0

    def test_uninstall_removes_registration(self, paths, monkeypatch):
        run, popen = _FakeRun(), _FakePopen()
        monkeypatch.setattr(cmd_daemon, "healthy", lambda url, timeout=2.0: False)
        monkeypatch.setattr(cmd_daemon, "_port_bound", lambda port: False)
        d = cmd_daemon.Daemon(paths, run=run, popen=popen, kind="launchd")
        paths.plist.parent.mkdir(parents=True)
        paths.plist.write_text("x")
        out = d.uninstall()
        assert str(paths.plist) in out and not paths.plist.exists() and not paths.stop_marker.exists()

    def test_serve_argv_is_absolute_or_interpreter(self, monkeypatch):
        monkeypatch.setattr(cmd_daemon.shutil, "which", lambda name: None)
        argv = cmd_daemon.serve_argv(11343)
        assert argv[1:3] == ["-m", "freeride.cli.main"] and argv[-2:] == ["--port", "11343"]
        monkeypatch.setattr(cmd_daemon.shutil, "which", lambda name: "/opt/bin/freeride")
        assert cmd_daemon.serve_argv(11343)[0] == "/opt/bin/freeride"


def LAUNCHD_LABEL_IN(text: str) -> bool:
    return cmd_daemon.LAUNCHD_LABEL in text and "<key>KeepAlive</key>" in text
