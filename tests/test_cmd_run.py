"""Tests for ``freeride run <command...>`` — the env-scoped wrapper.

We can't actually exec ``claude`` in CI, so we exercise the wrapper
by:

- mocking the gateway health probe (httpx_mock fixture)
- mocking ``os.execvpe`` to capture what would be exec'd
- mocking ``subprocess.Popen`` for the autospawn path

…and verifying the wrapper builds the right env, makes the right
decisions, and surfaces actionable errors on the failure paths.
"""

from __future__ import annotations

import argparse
from unittest.mock import patch

from freeride.cli.cmd_run import (
    _detect_cli,
    autospawn_gateway,
    build_child_env,
    cmd_run,
    gateway_healthy,
    prepare_codex_argv,
    seed_cli_configs,
    wait_for_gateway,
)


# ─── helpers ────────────────────────────────────────────────────


def _make_args(
    *,
    command_argv: list[str],
    port: int = 11343,
    gateway_url: str | None = None,
    no_autospawn: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        command_argv=command_argv,
        port=port,
        gateway_url=gateway_url,
        no_autospawn=no_autospawn,
    )


# ─── build_child_env ─────────────────────────────────────────────


def test_build_child_env_sets_anthropic_base_url() -> None:
    env = build_child_env(
        base_url="http://localhost:11343",
        parent_env={"PATH": "/usr/bin", "HOME": "/home/u"},
    )
    assert env["ANTHROPIC_BASE_URL"] == "http://localhost:11343"
    # Parent env carried through
    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/home/u"


def test_build_child_env_strips_trailing_slash() -> None:
    """Anthropic SDK appends /v1/messages — if we left a trailing
    slash, the SDK would generate //v1/messages."""
    env = build_child_env(base_url="http://localhost:11343/", parent_env={})
    assert env["ANTHROPIC_BASE_URL"] == "http://localhost:11343"


def test_build_child_env_stamps_freeride_active_marker() -> None:
    """FREERIDE_ACTIVE=1 lets the child detect it's inside the
    wrapper. Used by the doctor probe + future prompt indicators."""
    env = build_child_env(base_url="http://localhost:11343", parent_env={})
    assert env["FREERIDE_ACTIVE"] == "1"


def test_build_child_env_passes_through_anthropic_auth_token() -> None:
    """User's existing ANTHROPIC_AUTH_TOKEN (set by `claude login`)
    must flow to the child. The passthrough route needs it."""
    env = build_child_env(
        base_url="http://localhost:11343",
        parent_env={"ANTHROPIC_AUTH_TOKEN": "sk-ant-oat01-user-token"},
    )
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-ant-oat01-user-token"


def test_build_child_env_passes_through_anthropic_api_key() -> None:
    env = build_child_env(
        base_url="http://localhost:11343",
        parent_env={"ANTHROPIC_API_KEY": "sk-ant-api03-direct"},
    )
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-api03-direct"


def test_build_child_env_injects_sentinel_when_no_real_credential() -> None:
    """claude-cli 2.1.140+ short-circuits with "Not logged in" before
    making any HTTP call if it can't find an API key. To keep the
    free-routing flow working for users with no Anthropic account, we
    inject a sentinel ANTHROPIC_API_KEY. The gateway recognizes the
    sentinel via has_inbound_auth and still routes claude-* ids to
    free mode."""
    env = build_child_env(base_url="http://localhost:11343", parent_env={})
    assert env["ANTHROPIC_API_KEY"] == "sk-freeride-no-auth"
    assert "ANTHROPIC_AUTH_TOKEN" not in env


def test_build_child_env_does_not_overwrite_real_key() -> None:
    """If the parent already has a real ANTHROPIC_API_KEY we must NOT
    clobber it with the sentinel — paid users want their real key to
    flow through for passthrough on claude-* ids."""
    env = build_child_env(
        base_url="http://localhost:11343",
        parent_env={"ANTHROPIC_API_KEY": "sk-ant-api03-real"},
    )
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-api03-real"


def test_build_child_env_does_not_overwrite_oauth_token() -> None:
    """OAuth session via `claude login` also satisfies claude-cli's
    auth check, so we must not stomp on it with the sentinel either."""
    env = build_child_env(
        base_url="http://localhost:11343",
        parent_env={"ANTHROPIC_AUTH_TOKEN": "sk-ant-oat01-user"},
    )
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-ant-oat01-user"
    assert "ANTHROPIC_API_KEY" not in env


# ─── _detect_cli ─────────────────────────────────────────────────


def test_detect_cli_recognizes_known_binaries() -> None:
    assert _detect_cli(["claude"]) == "claude"
    assert _detect_cli(["gemini"]) == "gemini"
    assert _detect_cli(["codex"]) == "codex"


def test_detect_cli_basename_only() -> None:
    """Absolute paths and PATH-relative names both work — we strip the
    directory before matching."""
    assert _detect_cli(["/usr/local/bin/claude"]) == "claude"
    assert _detect_cli(["./codex"]) == "codex"
    assert _detect_cli(["/opt/gemini-cli/bin/gemini"]) == "gemini"


def test_detect_cli_unknown_falls_through() -> None:
    assert _detect_cli(["aider"]) == "unknown"
    assert _detect_cli([]) == "unknown"
    assert _detect_cli(["bash"]) == "unknown"


# ─── build_child_env per-CLI dispatch ────────────────────────────


def test_build_child_env_gemini_sets_google_base_url_and_sentinel() -> None:
    """gemini-cli short-circuits before making any HTTP request if no
    GEMINI_API_KEY / GOOGLE_API_KEY is set, even when
    GOOGLE_GEMINI_BASE_URL points at a gateway. Inject the sentinel
    when the parent env has neither, same pattern as claude/codex."""
    env = build_child_env(
        base_url="http://localhost:11343",
        parent_env={},
        cli_name="gemini",
    )
    assert env["GOOGLE_GEMINI_BASE_URL"] == "http://localhost:11343"
    assert env["FREERIDE_ACTIVE"] == "1"
    assert env["GEMINI_API_KEY"] == "sk-freeride-no-auth"
    # And we must not set ANTHROPIC_BASE_URL — that would leak into
    # other tools that read it.
    assert "ANTHROPIC_BASE_URL" not in env


def test_build_child_env_gemini_passes_through_real_key() -> None:
    env = build_child_env(
        base_url="http://localhost:11343",
        parent_env={"GEMINI_API_KEY": "real-google-key"},
        cli_name="gemini",
    )
    assert env["GEMINI_API_KEY"] == "real-google-key"


def test_build_child_env_codex_sets_sentinel_when_no_key() -> None:
    """Codex's auth gate reads CODEX_API_KEY at request time. Without
    a real one we inject the sentinel to unblock the request — the
    base URL itself isn't an env var for codex (handled in
    prepare_codex_argv)."""
    env = build_child_env(
        base_url="http://localhost:11343",
        parent_env={},
        cli_name="codex",
    )
    assert env["CODEX_API_KEY"] == "sk-freeride-no-auth"
    assert env["FREERIDE_ACTIVE"] == "1"
    # No ANTHROPIC_BASE_URL leakage to the codex env.
    assert "ANTHROPIC_BASE_URL" not in env


def test_build_child_env_codex_preserves_real_key() -> None:
    env = build_child_env(
        base_url="http://localhost:11343",
        parent_env={"CODEX_API_KEY": "real-user-key"},
        cli_name="codex",
    )
    assert env["CODEX_API_KEY"] == "real-user-key"


def test_build_child_env_unknown_cli_sets_both_base_urls() -> None:
    """For an experimental tool we don't recognize, set both base-URL
    env vars so whichever the tool reads still routes through us."""
    env = build_child_env(
        base_url="http://localhost:11343",
        parent_env={},
        cli_name="unknown",
    )
    assert env["ANTHROPIC_BASE_URL"] == "http://localhost:11343"
    assert env["GOOGLE_GEMINI_BASE_URL"] == "http://localhost:11343"


# ─── prepare_codex_argv ──────────────────────────────────────────


def test_prepare_codex_argv_injects_base_url_flag_with_v1_suffix() -> None:
    """Codex's default base URL is https://api.openai.com/v1 and it
    appends /responses directly — so the override MUST include /v1."""
    argv = prepare_codex_argv(["codex"], "http://localhost:11343")
    assert argv == ["codex", "-c", "openai_base_url=http://localhost:11343/v1"]


def test_prepare_codex_argv_inserts_after_binary_keeps_user_flags() -> None:
    """User-supplied flags must still reach codex untouched — and they
    must come AFTER our -c so a user override via their own -c
    wins via last-write-wins config layering."""
    argv = prepare_codex_argv(
        ["codex", "exec", "--model", "gpt-5"],
        "http://localhost:11343",
    )
    assert argv == [
        "codex",
        "-c",
        "openai_base_url=http://localhost:11343/v1",
        "exec",
        "--model",
        "gpt-5",
    ]


def test_prepare_codex_argv_strips_trailing_slash_on_base_url() -> None:
    argv = prepare_codex_argv(["codex"], "http://localhost:11343/")
    assert argv[2] == "openai_base_url=http://localhost:11343/v1"


def test_prepare_codex_argv_empty_argv_no_change() -> None:
    assert prepare_codex_argv([], "http://localhost:11343") == []


# ─── seed_cli_configs ────────────────────────────────────────────


def test_seed_cli_configs_gemini_writes_settings(tmp_path) -> None:
    """First-run auth-picker bypass: write selectedAuthType so the CLI
    doesn't pop the picker on the very first invocation."""
    import json
    seed_cli_configs("gemini", home=tmp_path)
    settings = tmp_path / ".gemini" / "settings.json"
    assert settings.exists()
    body = json.loads(settings.read_text())
    assert body["selectedAuthType"] == "gemini-api-key"


def test_seed_cli_configs_gemini_preserves_existing(tmp_path) -> None:
    """Real user config must not be overwritten — a user who ran
    `gemini login` may have picked a different auth type."""
    settings = tmp_path / ".gemini" / "settings.json"
    settings.parent.mkdir()
    settings.write_text('{"selectedAuthType": "oauth-personal"}')
    seed_cli_configs("gemini", home=tmp_path)
    import json
    body = json.loads(settings.read_text())
    assert body["selectedAuthType"] == "oauth-personal"  # unchanged


def test_seed_cli_configs_codex_writes_auth(tmp_path) -> None:
    """Codex's first-run gate is satisfied by the presence of
    ~/.codex/auth.json with an api_key field. Sentinel value goes here
    so the CLI doesn't prompt for a real one before letting the
    request fly."""
    import json
    seed_cli_configs("codex", home=tmp_path)
    auth = tmp_path / ".codex" / "auth.json"
    assert auth.exists()
    body = json.loads(auth.read_text())
    assert body["OPENAI_API_KEY"] == "sk-freeride-no-auth"


def test_seed_cli_configs_codex_preserves_existing(tmp_path) -> None:
    auth = tmp_path / ".codex" / "auth.json"
    auth.parent.mkdir()
    auth.write_text('{"OPENAI_API_KEY": "sk-real-user-key"}')
    seed_cli_configs("codex", home=tmp_path)
    import json
    body = json.loads(auth.read_text())
    assert body["OPENAI_API_KEY"] == "sk-real-user-key"


def test_seed_cli_configs_claude_is_noop(tmp_path) -> None:
    """claude-code reads its sentinel from env only — no config file
    pre-flight is needed. The function should not create stray files."""
    seed_cli_configs("claude", home=tmp_path)
    assert not (tmp_path / ".claude").exists()


def test_seed_cli_configs_unknown_is_noop(tmp_path) -> None:
    seed_cli_configs("unknown", home=tmp_path)
    # No directories created for unknown CLIs.
    assert list(tmp_path.iterdir()) == []


# ─── gateway_healthy ─────────────────────────────────────────────


def test_gateway_healthy_returns_true_on_200(httpx_mock) -> None:
    httpx_mock.add_response(
        url="http://localhost:11343/health",
        status_code=200,
        json={"ok": True},
    )
    assert gateway_healthy("http://localhost:11343") is True


def test_gateway_healthy_returns_false_on_5xx(httpx_mock) -> None:
    httpx_mock.add_response(
        url="http://localhost:11343/health",
        status_code=503,
        json={"error": "starting up"},
    )
    assert gateway_healthy("http://localhost:11343") is False


def test_gateway_healthy_returns_false_on_connection_error(httpx_mock) -> None:
    import httpx

    httpx_mock.add_exception(httpx.ConnectError("connection refused"))
    assert gateway_healthy("http://localhost:11343") is False


def test_gateway_healthy_strips_trailing_slash(httpx_mock) -> None:
    """Accept either form of base_url — caller might pass with or
    without trailing slash."""
    httpx_mock.add_response(
        url="http://localhost:11343/health",
        status_code=200,
        json={"ok": True},
    )
    assert gateway_healthy("http://localhost:11343/") is True


# ─── wait_for_gateway ────────────────────────────────────────────


def test_wait_for_gateway_returns_true_on_immediate_health(httpx_mock) -> None:
    httpx_mock.add_response(
        url="http://localhost:11343/health",
        status_code=200,
        json={"ok": True},
        is_reusable=True,
    )
    assert wait_for_gateway("http://localhost:11343", total_wait=0.5) is True


def test_wait_for_gateway_times_out_when_never_ready(httpx_mock) -> None:
    import httpx

    httpx_mock.add_exception(httpx.ConnectError("nope"), is_reusable=True)
    assert wait_for_gateway("http://localhost:11343", total_wait=0.5) is False


# ─── cmd_run dispatch ────────────────────────────────────────────


def test_cmd_run_empty_command_returns_2(capsys) -> None:
    rc = cmd_run(_make_args(command_argv=[]))
    assert rc == 2
    err = capsys.readouterr().err
    assert "no command given" in err


def test_cmd_run_empty_after_double_dash_returns_2(capsys) -> None:
    rc = cmd_run(_make_args(command_argv=["--"]))
    assert rc == 2
    err = capsys.readouterr().err
    assert "nothing to execute" in err


def test_cmd_run_strips_leading_double_dash() -> None:
    """argparse REMAINDER captures the '--' separator; the wrapper
    must strip it so the child doesn't see it as part of its argv."""
    with patch("freeride.cli.cmd_run.gateway_healthy", return_value=True):
        with patch("freeride.cli.cmd_run.os.execvpe") as mock_exec:
            cmd_run(_make_args(command_argv=["--", "claude", "--help"]))
    args, kwargs = mock_exec.call_args
    # execvpe(file, argv, env)
    assert args[0] == "claude"
    assert args[1] == ["claude", "--help"]
    # Env carries our markers
    assert args[2]["ANTHROPIC_BASE_URL"] == "http://localhost:11343"
    assert args[2]["FREERIDE_ACTIVE"] == "1"


def test_cmd_run_exec_inherits_parent_env_plus_overrides() -> None:
    """Child env = parent env + ANTHROPIC_BASE_URL + FREERIDE_ACTIVE.
    Mock os.environ to a known parent state and check the exec call."""
    parent_env = {"PATH": "/usr/local/bin:/usr/bin", "ANTHROPIC_AUTH_TOKEN": "user-token"}
    with patch("freeride.cli.cmd_run.gateway_healthy", return_value=True):
        with patch("freeride.cli.cmd_run.os.environ", parent_env):
            with patch("freeride.cli.cmd_run.os.execvpe") as mock_exec:
                cmd_run(_make_args(command_argv=["claude"]))
    args, _ = mock_exec.call_args
    env = args[2]
    assert env["PATH"] == "/usr/local/bin:/usr/bin"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "user-token"
    assert env["ANTHROPIC_BASE_URL"] == "http://localhost:11343"
    assert env["FREERIDE_ACTIVE"] == "1"


def test_cmd_run_no_autospawn_with_dead_gateway_returns_1(capsys) -> None:
    with patch("freeride.cli.cmd_run.gateway_healthy", return_value=False):
        rc = cmd_run(_make_args(command_argv=["claude"], no_autospawn=True))
    assert rc == 1
    err = capsys.readouterr().err
    assert "not reachable" in err
    assert "freeride serve" in err


def test_cmd_run_autospawn_path_calls_spawn_and_waits() -> None:
    """If the gateway isn't up and --no-autospawn isn't set, we
    spawn and wait. After it comes up, we exec the child."""
    health_responses = iter([False, True])  # first probe fails, then succeeds

    def fake_healthy(*args, **kwargs):
        return next(health_responses)

    class FakeProc:
        pid = 42

    with patch("freeride.cli.cmd_run.gateway_healthy", side_effect=fake_healthy):
        with patch("freeride.cli.cmd_run.autospawn_gateway") as mock_spawn, patch(
            "freeride.cli.cmd_run.wait_for_gateway", return_value=True
        ) as mock_wait, patch("freeride.cli.cmd_run.os.execvpe") as mock_exec:
            mock_spawn.return_value = FakeProc()
            cmd_run(_make_args(command_argv=["claude"]))

    mock_spawn.assert_called_once_with(11343)
    mock_wait.assert_called_once()
    mock_exec.assert_called_once()


def test_cmd_run_autospawn_failure_returns_1(capsys) -> None:
    """When Popen itself fails, we surface a clear error and exit 1
    instead of trying to exec into an unwired Claude Code session."""
    with patch("freeride.cli.cmd_run.gateway_healthy", return_value=False):
        with patch("freeride.cli.cmd_run.autospawn_gateway", return_value=None):
            rc = cmd_run(_make_args(command_argv=["claude"]))
    assert rc == 1
    err = capsys.readouterr().err
    assert "autospawn failed" in err


def test_cmd_run_autospawn_never_ready_returns_1(capsys) -> None:
    """Popen succeeded but the gateway never answered /health within
    the wait budget. Tell the user where to find the log."""

    class FakeProc:
        pid = 42

    with patch("freeride.cli.cmd_run.gateway_healthy", return_value=False):
        with patch(
            "freeride.cli.cmd_run.autospawn_gateway", return_value=FakeProc()
        ):
            with patch("freeride.cli.cmd_run.wait_for_gateway", return_value=False):
                rc = cmd_run(_make_args(command_argv=["claude"]))
    assert rc == 1
    err = capsys.readouterr().err
    assert "did not become ready" in err
    assert "autospawn.log" in err


def test_cmd_run_command_not_found_returns_127(capsys) -> None:
    """POSIX convention: 127 means command-not-found. Surface that
    when execvpe can't find the binary."""

    def raise_fnf(*a, **kw):
        raise FileNotFoundError

    with patch("freeride.cli.cmd_run.gateway_healthy", return_value=True):
        with patch("freeride.cli.cmd_run.os.execvpe", side_effect=raise_fnf):
            rc = cmd_run(_make_args(command_argv=["totally-not-real-binary"]))
    assert rc == 127
    err = capsys.readouterr().err
    assert "command not found" in err


def test_cmd_run_explicit_gateway_url_overrides_port() -> None:
    """--gateway-url takes precedence over --port."""
    with patch("freeride.cli.cmd_run.gateway_healthy", return_value=True) as mock_h:
        with patch("freeride.cli.cmd_run.os.execvpe") as mock_exec:
            cmd_run(
                _make_args(
                    command_argv=["claude"],
                    port=11343,
                    gateway_url="http://otherhost:9000",
                )
            )
    # Health probe was called with the explicit URL
    mock_h.assert_called_with("http://otherhost:9000")
    # And the child got that as BASE_URL
    env = mock_exec.call_args[0][2]
    assert env["ANTHROPIC_BASE_URL"] == "http://otherhost:9000"


# ─── autospawn_gateway (Popen integration) ──────────────────────


def test_autospawn_gateway_invokes_python_module_with_port(tmp_path) -> None:
    """The autospawn must invoke ``python -m freeride.cli.main serve``
    so it works regardless of how freeride was installed (pip, uv tool,
    editable). Direct binary lookup would fail in some envs."""
    captured = {}

    class FakeProc:
        pid = 99

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return FakeProc()

    with patch("freeride.cli.cmd_run.subprocess.Popen", side_effect=fake_popen):
        with patch("freeride.cli.cmd_run._AUTOSPAWN_LOG", tmp_path / "autospawn.log"):
            proc = autospawn_gateway(11500)
    assert proc is not None
    cmd = captured["cmd"]
    assert "freeride.cli.main" in cmd
    assert "serve" in cmd
    assert "--port" in cmd
    assert "11500" in cmd
    # Detached from the wrapper's session — survives execvpe.
    assert captured["kwargs"].get("start_new_session") is True


def test_autospawn_gateway_popen_failure_returns_none(tmp_path, caplog) -> None:
    """OS-level Popen failures (ENOMEM, etc.) shouldn't raise out of
    the wrapper — return None so the caller surfaces a clean error."""
    import logging

    def raise_oserror(*a, **kw):
        raise OSError("ENOMEM")

    with patch("freeride.cli.cmd_run.subprocess.Popen", side_effect=raise_oserror):
        with patch("freeride.cli.cmd_run._AUTOSPAWN_LOG", tmp_path / "log"):
            with caplog.at_level(logging.WARNING):
                proc = autospawn_gateway(11343)
    assert proc is None
    assert any("Popen failed" in r.message for r in caplog.records)
