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


def _set_home(monkeypatch, tmp_path) -> None:
    """Point Path.home() at tmp_path on both POSIX and Windows.

    pathlib uses HOME on Unix and USERPROFILE (then HOMEDRIVE+HOMEPATH)
    on Windows. Setting only HOME is a no-op in the Windows CI job.
    """
    home = str(tmp_path)
    monkeypatch.setenv("HOME", home)
    monkeypatch.setenv("USERPROFILE", home)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Strip all provider env vars so each test starts deterministic."""
    for var in (
        "OPENROUTER_API_KEY",
        "GROQ_API_KEY",
        "NVIDIA_API_KEY",
        "NIM_API_KEY",
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
        _set_home(monkeypatch, tmp_path)
        c = _check_freeride_dir()
        assert c.severity == "ok"
        assert (tmp_path / ".freeride").exists()

    def test_passes_when_already_exists(self, tmp_path, monkeypatch):
        _set_home(monkeypatch, tmp_path)
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


# ---------------------------------------------------------------------------
# Claude Code probes (--claude-code flag)
# ---------------------------------------------------------------------------


from freeride.cli.cmd_doctor import (  # noqa: E402
    _check_anthropic_base_url,
    _check_claude_cli_on_path,
    _check_claude_routing,
    _check_freeride_active_marker,
    _check_freeride_free_via_gateway,
    run_checks,
)


class TestFreerideActiveMarker:
    def test_inside_wrapper_reports_ok(self, monkeypatch):
        monkeypatch.setenv("FREERIDE_ACTIVE", "1")
        c = _check_freeride_active_marker()
        assert c.severity == "ok"
        assert "FREERIDE_ACTIVE" in c.label

    def test_outside_wrapper_reports_info(self, monkeypatch):
        monkeypatch.delenv("FREERIDE_ACTIVE", raising=False)
        c = _check_freeride_active_marker()
        assert c.severity == "info"
        assert "freeride run" in c.detail


class TestAnthropicBaseUrlCheck:
    def test_unset_reports_info(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        c = _check_anthropic_base_url()
        assert c.severity == "info"
        assert "not set" in c.label

    def test_points_at_anthropic_directly_warns(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
        c = _check_anthropic_base_url()
        assert c.severity == "warn"
        assert "bypasses FreeRide" in c.detail

    def test_reachable_gateway_reports_ok(self, monkeypatch, httpx_mock):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:11343")
        httpx_mock.add_response(
            url="http://localhost:11343/health", status_code=200, json={"ok": True}
        )
        c = _check_anthropic_base_url()
        assert c.severity == "ok"
        assert "gateway reachable" in c.label

    def test_unreachable_gateway_reports_error(self, monkeypatch, httpx_mock):
        import httpx

        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:11343")
        httpx_mock.add_exception(httpx.ConnectError("nope"))
        c = _check_anthropic_base_url()
        assert c.severity == "error"
        assert "unreachable" in c.label


class TestClaudeRoutingCheck:
    def test_resolver_passes_all_three_buckets(self):
        """The resolver MUST keep these three cases stable — they're
        the core mental model of Phase 4."""
        c = _check_claude_routing()
        assert c.severity == "ok"
        assert "sane" in c.label


class TestClaudeCliOnPath:
    def test_missing_claude_reports_info(self, monkeypatch):
        """When `which claude` returns None, info-level message
        directing user to npm install."""
        monkeypatch.setattr("freeride.cli.cmd_doctor.shutil.which", lambda x: None)
        c = _check_claude_cli_on_path()
        assert c.severity == "info"
        assert "npm" in c.detail

    def test_present_claude_reports_ok(self, monkeypatch, tmp_path):
        """When the binary exists, we should report ok and try to grab
        the version."""
        fake = tmp_path / "claude"
        fake.write_text("#!/bin/sh\necho '2.1.19 (Claude Code)'")
        fake.chmod(0o755)
        monkeypatch.setattr(
            "freeride.cli.cmd_doctor.shutil.which", lambda x: str(fake)
        )
        c = _check_claude_cli_on_path()
        assert c.severity == "ok"
        assert str(fake) in c.detail


class TestFreerideFreeViaGateway:
    def test_no_gateway_reports_info(self, monkeypatch, httpx_mock):
        """When /health fails, skip the live probe — don't make it an
        error, the user might not have started the gateway yet."""
        import httpx

        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        httpx_mock.add_exception(httpx.ConnectError("nope"))
        c = _check_freeride_free_via_gateway()
        assert c.severity == "info"
        assert "skipped" in c.label

    def test_gateway_up_and_free_route_works(self, monkeypatch, httpx_mock):
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        httpx_mock.add_response(
            url="http://127.0.0.1:11343/health", status_code=200, json={"ok": True}
        )
        httpx_mock.add_response(
            url="http://127.0.0.1:11343/v1/messages",
            method="POST",
            status_code=200,
            json={
                "id": "msg_x",
                "type": "message",
                "role": "assistant",
                "model": "freeride/free",
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
            headers={"X-FreeRide-Provider": "openrouter"},
        )
        c = _check_freeride_free_via_gateway()
        assert c.severity == "ok"
        assert "openrouter" in c.label

    def test_anthropic_base_url_skips_probe(self, monkeypatch):
        """If ANTHROPIC_BASE_URL points at Anthropic directly, we
        can't probe freeride/* (there's no gateway in the path).
        Skip with info."""
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
        c = _check_freeride_free_via_gateway()
        assert c.severity == "info"


class TestRunChecksWithClaudeCodeFlag:
    @pytest.fixture(autouse=True)
    def _no_dotenv_load(self, monkeypatch):
        """``run_checks`` calls ``load_dotenv_into_environ`` which reads
        ``~/.freeride/.env`` and writes its contents to ``os.environ``
        — invisible to monkeypatch's restore, so values bleed into
        sibling test files. Stub it out for these tests."""
        monkeypatch.setattr(
            "freeride.core.dotenv.load_dotenv_into_environ", lambda: None
        )

    def test_default_run_does_not_include_claude_section(self, monkeypatch):
        """Without --claude-code, no Claude-Code-specific checks are
        emitted. Existing users see the same report they always have."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "x")
        checks = run_checks(claude_code=False)
        labels = [c.label for c in checks]
        assert not any("Claude Code integration" in label for label in labels)
        assert not any("FREERIDE_ACTIVE" in label for label in labels)

    def test_claude_code_flag_appends_section(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "x")
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        monkeypatch.delenv("FREERIDE_ACTIVE", raising=False)
        checks = run_checks(claude_code=True)
        labels = [c.label for c in checks]
        assert any("Claude Code integration" in label for label in labels)
        # And at least one of the claude-code-specific probes ran
        assert any("freeride run" in (c.detail or "") for c in checks) or any(
            "FREERIDE_ACTIVE" in c.label for c in checks
        )
