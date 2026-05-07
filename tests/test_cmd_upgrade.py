"""Tests for `freeride upgrade`.

Heavily mocked — we don't actually shell out to uv/pipx/pip during the
suite. The pieces we test:

- _pick_strategy returns the first available strategy from the priority list
- cmd_upgrade with --dry-run prints the command without executing it
- cmd_upgrade reports the picked binary
- cmd_upgrade exits non-zero when the upgrade subprocess fails
- cmd_upgrade reports old → new version when the post-upgrade query succeeds
"""

from __future__ import annotations

import argparse
from unittest.mock import patch


from freeride.cli import cmd_upgrade as upgrade_module


def _args(**overrides) -> argparse.Namespace:
    base = {"dry_run": False}
    base.update(overrides)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------------------


class TestPickStrategy:
    def test_picks_uv_when_available(self, monkeypatch):
        # shutil.which returns the binary path for whatever's on PATH;
        # mock it so 'uv' is "found".
        def fake_which(name):
            return f"/usr/local/bin/{name}" if name == "uv" else None

        monkeypatch.setattr(upgrade_module.shutil, "which", fake_which)
        binary, argv = upgrade_module._pick_strategy()
        assert binary == "uv"
        assert argv[0] == "uv"
        assert "--upgrade" in argv

    def test_falls_through_to_pipx_when_uv_missing(self, monkeypatch):
        def fake_which(name):
            return f"/usr/local/bin/{name}" if name == "pipx" else None

        monkeypatch.setattr(upgrade_module.shutil, "which", fake_which)
        binary, argv = upgrade_module._pick_strategy()
        assert binary == "pipx"
        assert argv[:2] == ["pipx", "upgrade"]

    def test_pip_strategy_always_available_as_last_resort(self, monkeypatch):
        # Even if NEITHER uv nor pipx is on PATH, the pip strategy uses
        # sys.executable which is always present.
        monkeypatch.setattr(upgrade_module.shutil, "which", lambda _: None)
        result = upgrade_module._pick_strategy()
        assert result is not None
        binary, argv = result
        assert "-m" in argv
        assert "pip" in argv


# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_doesnt_execute(self, capsys, monkeypatch):
        monkeypatch.setattr(
            upgrade_module.shutil,
            "which",
            lambda name: f"/bin/{name}" if name == "uv" else None,
        )
        # Make sure _run_upgrade is NOT called.
        with patch.object(upgrade_module, "_run_upgrade") as run_mock:
            rc = upgrade_module.cmd_upgrade(_args(dry_run=True))
        run_mock.assert_not_called()
        out = capsys.readouterr().out
        assert "would run:" in out
        assert rc == 0


class TestExecution:
    def test_failure_propagates_exit_code(self, monkeypatch, capsys):
        monkeypatch.setattr(
            upgrade_module.shutil,
            "which",
            lambda name: f"/bin/{name}" if name == "uv" else None,
        )
        # Subprocess returns rc=2 (e.g., upgrade conflict).
        with patch.object(upgrade_module, "_run_upgrade", return_value=2):
            rc = upgrade_module.cmd_upgrade(_args())
        assert rc == 2
        err = capsys.readouterr().err
        assert "upgrade failed" in err

    def test_success_reports_old_to_new(self, monkeypatch, capsys):
        monkeypatch.setattr(
            upgrade_module.shutil,
            "which",
            lambda name: f"/bin/{name}" if name == "uv" else None,
        )
        # Subprocess succeeds; post-upgrade version query returns a
        # different version than the in-process __version__.
        with patch.object(upgrade_module, "_run_upgrade", return_value=0), \
             patch.object(upgrade_module, "_query_installed_version", return_value="9.9.9"):
            rc = upgrade_module.cmd_upgrade(_args())
        assert rc == 0
        out = capsys.readouterr().out
        assert "9.9.9" in out
        assert "upgraded" in out
        assert "restart" in out  # nudge to restart `freeride serve`

    def test_already_latest_reports_no_change(self, monkeypatch, capsys):
        from freeride import __version__ as current

        monkeypatch.setattr(
            upgrade_module.shutil,
            "which",
            lambda name: f"/bin/{name}" if name == "uv" else None,
        )
        with patch.object(upgrade_module, "_run_upgrade", return_value=0), \
             patch.object(upgrade_module, "_query_installed_version", return_value=current):
            rc = upgrade_module.cmd_upgrade(_args())
        assert rc == 0
        out = capsys.readouterr().out
        assert "already at latest" in out

    def test_no_strategy_available_errors(self, monkeypatch, capsys):
        # Mock both shutil.which AND sys.executable to simulate "nothing
        # installable available". Easiest way: monkeypatch _pick_strategy
        # to return None directly.
        with patch.object(upgrade_module, "_pick_strategy", return_value=None):
            rc = upgrade_module.cmd_upgrade(_args())
        assert rc == 1
        err = capsys.readouterr().err
        assert "couldn't find uv" in err
