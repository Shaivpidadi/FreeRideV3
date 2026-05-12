"""``freeride run <command...>`` — scope an env var to a subprocess.

The opt-in surface for FreeRide-as-a-companion. User runs
``freeride run claude``; we point Claude Code at the local gateway
for *that subprocess only*. Outside the wrapper, plain ``claude``
still hits Anthropic natively — the user's subscription is
untouched, the system /etc/hosts is untouched, the user's shell
profile is untouched.

Behavior:

1. Determine the gateway URL (default ``http://localhost:11343``).
   This is the URL Anthropic's SDK will use as ``ANTHROPIC_BASE_URL``
   — note: NO trailing ``/v1`` because the SDK appends
   ``/v1/messages`` itself.
2. Probe ``/health``. If unreachable, spawn ``freeride serve`` in
   the background (unless ``--no-autospawn``) and poll until it
   answers, with a short bounded wait.
3. Build the env for the child:
   - ``ANTHROPIC_BASE_URL`` → the gateway URL
   - ``FREERIDE_ACTIVE`` → ``1`` (a marker prompts and the doctor
     probe can read to detect "we're inside the wrapper")
   - everything else (including ``ANTHROPIC_AUTH_TOKEN`` and
     ``ANTHROPIC_API_KEY``) is passed through unchanged from the
     parent shell. The passthrough route relies on those for the
     native-claude flow.
4. ``execvpe`` into the child. We replace the wrapper process so
   the child has a clean parent in the process tree — `Ctrl-C` and
   shell job control work the way the user expects.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx


logger = logging.getLogger(__name__)


# Default port matches `freeride serve` default. Override via --port.
_DEFAULT_PORT = 11343

# How long to wait for an auto-spawned gateway to answer /health.
# Five seconds is enough for the cold start path (Python import +
# lifespan + telemetry beacon scheduling) on a modern laptop; longer
# than that and we'd hide a real problem (port in use, import error).
_AUTOSPAWN_WAIT_SECONDS = 8.0
_AUTOSPAWN_POLL_INTERVAL = 0.25

# Log file for the auto-spawned background gateway. Lives under the
# usual FreeRide state dir so users can `tail -f` it without hunting.
_AUTOSPAWN_LOG = Path.home() / ".freeride" / "autospawn.log"


# ─── health probe ───────────────────────────────────────────────────


def gateway_healthy(base_url: str, *, timeout: float = 1.0) -> bool:
    """Probe ``<base_url>/health``. Returns True iff we get a 2xx
    quickly. Network errors, 5xx, and timeouts all count as "not
    healthy" — no retry inside this function (the caller is the
    polling loop).
    """
    url = base_url.rstrip("/") + "/health"
    try:
        resp = httpx.get(url, timeout=timeout)
    except (httpx.HTTPError, OSError):
        return False
    return 200 <= resp.status_code < 300


# ─── autospawn ──────────────────────────────────────────────────────


def autospawn_gateway(port: int) -> subprocess.Popen | None:
    """Spawn ``freeride serve --port <port>`` in the background.

    Detached from the wrapper process group so it survives the
    ``execvpe`` into the child command. The user's subsequent
    ``freeride run`` invocations reuse the same gateway.

    Returns the Popen handle on success (used only for the PID echo),
    or None if Popen itself raised. The actual readiness check is the
    caller's polling loop — Popen succeeding only means "we forked",
    not "the gateway is listening".
    """
    _AUTOSPAWN_LOG.parent.mkdir(parents=True, exist_ok=True)
    log_handle = open(_AUTOSPAWN_LOG, "ab", buffering=0)
    try:
        # ``start_new_session=True`` puts the child in its own session,
        # which on POSIX means it's not killed by Ctrl-C in the
        # wrapper's terminal. The user kills it with
        # ``pkill -f 'freeride serve'`` or via the PID file (future).
        proc = subprocess.Popen(
            [sys.executable, "-m", "freeride.cli.main", "serve", "--port", str(port)],
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except (OSError, ValueError) as e:
        log_handle.close()
        logger.warning("autospawn: Popen failed: %s", e)
        return None
    return proc


def wait_for_gateway(base_url: str, *, total_wait: float) -> bool:
    """Poll ``/health`` until 2xx or budget exhausted."""
    deadline = time.monotonic() + total_wait
    while time.monotonic() < deadline:
        if gateway_healthy(base_url, timeout=0.5):
            return True
        time.sleep(_AUTOSPAWN_POLL_INTERVAL)
    return False


# ─── env construction ───────────────────────────────────────────────


def build_child_env(*, base_url: str, parent_env: dict[str, str]) -> dict[str, str]:
    """Build the env vars for the child process.

    Copies the parent env so the child inherits whatever the user
    already had (PATH, HOME, ANTHROPIC_AUTH_TOKEN from a prior
    ``claude login``, ANTHROPIC_API_KEY if set, terminal-specific
    vars, etc.), then layers FreeRide's additions on top.

    We intentionally DO NOT modify or fabricate auth tokens — the
    passthrough route in the gateway expects the caller (Claude Code)
    to attach its own credential via the Authorization header. Our
    job is just to point Claude Code at the gateway.
    """
    env = dict(parent_env)
    # No trailing /v1: the Anthropic SDK appends "/v1/messages".
    env["ANTHROPIC_BASE_URL"] = base_url.rstrip("/")
    env["FREERIDE_ACTIVE"] = "1"
    return env


# ─── command entry ──────────────────────────────────────────────────


def cmd_run(args) -> int:
    """``freeride run`` argparse handler.

    Args namespace fields:
      - ``command_argv``: list of argv tokens for the child command.
        Comes from ``argparse.REMAINDER`` so flags after the command
        name are forwarded untouched (``freeride run claude --model
        claude-opus-4-5`` passes ``--model claude-opus-4-5`` to
        ``claude``, NOT to argparse).
      - ``port``: gateway port (default 11343).
      - ``gateway_url``: explicit override; takes precedence over
        ``--port``. Without /v1 suffix.
      - ``no_autospawn``: if True, fail when gateway isn't running
        instead of trying to start one.
    """
    command_argv: list[str] = args.command_argv
    if not command_argv:
        print(
            "freeride run: no command given.\n"
            "Example: freeride run claude\n"
            "         freeride run -- claude --model claude-opus-4-5",
            file=sys.stderr,
        )
        return 2

    # argparse REMAINDER sometimes captures a leading "--" separator;
    # strip it so the child doesn't see it as part of its own argv.
    if command_argv and command_argv[0] == "--":
        command_argv = command_argv[1:]
    if not command_argv:
        print("freeride run: nothing to execute after '--'.", file=sys.stderr)
        return 2

    base_url = (args.gateway_url or f"http://localhost:{args.port}").rstrip("/")

    if not gateway_healthy(base_url):
        if args.no_autospawn:
            print(
                f"freeride run: gateway not reachable at {base_url}/health.\n"
                f"Start it with: freeride serve --port {args.port}",
                file=sys.stderr,
            )
            return 1
        print(
            f"freeride: gateway not running at {base_url}, starting it…",
            file=sys.stderr,
        )
        proc = autospawn_gateway(args.port)
        if proc is None:
            print(
                "freeride run: autospawn failed. "
                f"Try: freeride serve --port {args.port}",
                file=sys.stderr,
            )
            return 1
        if not wait_for_gateway(base_url, total_wait=_AUTOSPAWN_WAIT_SECONDS):
            print(
                f"freeride run: gateway did not become ready within "
                f"{_AUTOSPAWN_WAIT_SECONDS:.0f}s. "
                f"See {_AUTOSPAWN_LOG} for the gateway log.",
                file=sys.stderr,
            )
            return 1
        print(
            f"freeride: gateway ready on {base_url} (pid {proc.pid}). "
            f"Log: {_AUTOSPAWN_LOG}",
            file=sys.stderr,
        )

    child_env = build_child_env(base_url=base_url, parent_env=os.environ.copy())

    # Banner — only when wrapping `claude` AND running an interactive
    # TTY. claude-cli's hardcoded /model picker doesn't surface
    # freeride/* virtual ids, so print them once at session start.
    # Suppressed when stdin isn't a tty (CI, scripts, --print mode
    # already detached) so we don't clutter machine consumers.
    if (
        command_argv
        and os.path.basename(command_argv[0]) == "claude"
        and "--print" not in command_argv
        and sys.stdin.isatty()
    ):
        print(
            "\n  ╭─ freeride: free-tier model presets ─────────────────╮\n"
            "  │  Inside claude, type /model <id>:                   │\n"
            "  │    freeride/free     — smart-routed                 │\n"
            "  │    freeride/fast     — groq-preferred (low latency) │\n"
            "  │    freeride/quality  — OR-preferred (larger models) │\n"
            "  │    freeride/coding   — code-tuned (Qwen-Coder)      │\n"
            "  │  /model claude-opus-4-7 keeps using your sub.       │\n"
            "  ╰─────────────────────────────────────────────────────╯\n",
            file=sys.stderr,
        )

    try:
        os.execvpe(command_argv[0], command_argv, child_env)
    except FileNotFoundError:
        print(
            f"freeride run: command not found: {command_argv[0]!r}. "
            "Is it on PATH?",
            file=sys.stderr,
        )
        return 127
    except OSError as e:
        print(f"freeride run: exec failed: {e}", file=sys.stderr)
        return 1
    # execvpe replaces the process on success — unreachable below.
    return 0
