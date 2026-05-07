"""Tests for `freeride doctor` checks.

Each check is a pure function that returns a _Check object — easy to
unit-test without process-level state.
"""

from __future__ import annotations

import re
import socket
from contextlib import closing

import pytest

from freeride.cli.cmd_doctor import (
    _Check,
    _check_freeride_dir,
    _check_provider_env_vars,
    _check_python_version,
    _format_check,
    _check_port_or_gateway,
)


def _strip_ansi(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Strip all provider env vars so each test starts deterministic."""
    for var in (
        "OPENROUTER_API_KEY",
        "GROQ_API_KEY",
        "NVIDIA_API_KEY",
        "CLOUDFLARE_API_TOKEN",
        "CLOUDFLARE_ACCOUNT_ID",
        "HF_TOKEN",
        "HUGGINGFACE_API_KEY",
        "OLLAMA_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


# ---------------------------------------------------------------------------
# Per-check tests
# ---------------------------------------------------------------------------


class TestPythonVersion:
    def test_passes_on_310_plus(self):
        # We're running on >= 3.10 (project requirement), so this should
        # always pass when the suite runs.
        c = _check_python_version()
        assert c.severity == "ok"
        assert "3.10" in c.label or "3.1" in c.label or "3.2" in c.label


class TestFreerideDir:
    def test_creates_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        c = _check_freeride_dir()
        assert c.severity == "ok"
        assert (tmp_path / ".freeride").exists()

    def test_passes_when_already_exists(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".freeride").mkdir()
        c = _check_freeride_dir()
        assert c.severity == "ok"


class TestProviderEnvVars:
    def test_all_unset_raises_error(self):
        checks = _check_provider_env_vars()
        # Last item is the "no providers set" error summary.
        assert any(c.severity == "error" and "no provider env vars" in c.label for c in checks)

    def test_one_provider_set_passes(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-test")
        checks = _check_provider_env_vars()
        # Should NOT have the error summary now.
        assert not any(c.severity == "error" and "no provider env vars" in c.label for c in checks)
        # OR row should be ok.
        or_row = next(c for c in checks if c.label.startswith("openrouter:"))
        assert or_row.severity == "ok"

    def test_cf_partial_warns(self, monkeypatch):
        # CF needs both vars; setting only one should warn.
        monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "x")
        # Also set OR so we don't hit the "no providers" error.
        monkeypatch.setenv("OPENROUTER_API_KEY", "y")
        checks = _check_provider_env_vars()
        cf_row = next(c for c in checks if c.label.startswith("cloudflare_wai"))
        assert cf_row.severity == "warn"
        assert "CLOUDFLARE_ACCOUNT_ID" in cf_row.detail

    def test_hf_accepts_either_env_var(self, monkeypatch):
        # HF accepts HF_TOKEN OR HUGGINGFACE_API_KEY.
        monkeypatch.setenv("HUGGINGFACE_API_KEY", "hf-x")
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-x")
        checks = _check_provider_env_vars()
        hf_row = next(c for c in checks if c.label.startswith("huggingface:"))
        assert hf_row.severity == "ok"


class TestPortOrGateway:
    def test_free_port_is_ok(self):
        # Use an OS-assigned port that nothing is bound to in this test
        # session, then probe it.
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
            s.bind(("127.0.0.1", 0))
            free_port = s.getsockname()[1]
        # The fixture closed the socket — port is free again.
        checks = _check_port_or_gateway(port=free_port)
        assert len(checks) == 1
        assert checks[0].severity == "ok"
        assert "free" in checks[0].label

    def test_in_use_but_not_gateway_warns(self):
        """If something else is bound to the port and not responding to
        /health, we should warn rather than silently pass.
        """
        # Bind a socket but DON'T listen as HTTP — a connection attempt
        # will succeed at the TCP layer but the HTTP probe fails.
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            port = s.getsockname()[1]
            checks = _check_port_or_gateway(port=port)
        assert len(checks) == 1
        assert checks[0].severity == "warn"


class TestFormat:
    def test_no_color_strips_escapes(self):
        c = _Check("ok", "test passed")
        out = _format_check(c, no_color=True)
        assert "\x1b[" not in out

    def test_severity_glyphs(self):
        for sev, glyph in [("ok", "✓"), ("warn", "!"), ("error", "✗"), ("info", "·")]:
            c = _Check(sev, "x")
            out = _strip_ansi(_format_check(c, no_color=True))
            assert glyph in out

    def test_detail_indented(self):
        c = _Check("warn", "label", "extra detail line")
        out = _strip_ansi(_format_check(c, no_color=True))
        # Detail should appear on its own line under the label.
        lines = out.split("\n")
        assert len(lines) == 2
        assert "label" in lines[0]
        assert "extra detail line" in lines[1]
