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


# ─── CLI detection + env construction ───────────────────────────────


# Sentinel API key — see has_inbound_auth in core/model_router.py for
# the gateway-side recognition that demotes this to "no auth" so
# claude-* / gpt-* / gemini-* model ids fall through to free routing.
_FREERIDE_SENTINEL_KEY = "sk-freeride-no-auth"


def _detect_cli(command_argv: list[str]) -> str:
    """Return a tag for the wrapped CLI: 'claude', 'gemini', 'codex',
    or 'unknown'.

    Each tag drives a different env-var layering strategy in
    build_child_env (and an argv mutation for codex). We dispatch on the
    basename only, so absolute paths and aliases still work
    (``/usr/local/bin/claude`` → ``claude``).
    """
    if not command_argv:
        return "unknown"
    name = os.path.basename(command_argv[0])
    if name == "claude":
        return "claude"
    if name == "gemini":
        return "gemini"
    if name == "codex":
        return "codex"
    return "unknown"


def build_child_env(
    *,
    base_url: str,
    parent_env: dict[str, str],
    cli_name: str = "claude",
) -> dict[str, str]:
    """Build the env vars for the child process.

    Copies the parent env so the child inherits whatever the user
    already had (PATH, HOME, terminal-specific vars, existing
    credentials, etc.), then layers FreeRide's additions on top per the
    wrapped CLI's expected env-var shape:

    * **claude** — sets ``ANTHROPIC_BASE_URL``. Claude Code 2.1.140+
      short-circuits with "Not logged in" if it can't find an API key
      *before* making any HTTP request, so we inject a sentinel
      ``ANTHROPIC_API_KEY`` when the parent has neither
      ``ANTHROPIC_API_KEY`` nor ``ANTHROPIC_AUTH_TOKEN``. The gateway
      recognizes the sentinel and demotes it to "no auth" so claude-*
      ids fall through to free routing.

    * **gemini** — sets ``GOOGLE_GEMINI_BASE_URL``. The official
      ``@google/genai``-backed CLI ships a dedicated ``AuthType.GATEWAY``
      path that allows empty keys when this var is set, so no sentinel
      is needed.

    * **codex** — sets ``CODEX_API_KEY`` to the sentinel when the parent
      has none. The base-URL override is *not* an env var for codex
      (it's a TOML config key); see ``prepare_codex_argv`` for the argv
      injection that handles it.

    * **unknown** — defensively sets both ANTHROPIC and Gemini env vars
      so an experimental wrapped tool that honors either still picks
      the gateway up. Env vars no tool reads are harmless.

    Real credentials in the parent env (``ANTHROPIC_API_KEY``,
    ``ANTHROPIC_AUTH_TOKEN``, ``GEMINI_API_KEY``, ``CODEX_API_KEY``)
    are never overwritten — paid users keep their passthrough flow.
    """
    env = dict(parent_env)
    base = base_url.rstrip("/")
    env["FREERIDE_ACTIVE"] = "1"

    if cli_name == "claude":
        env["ANTHROPIC_BASE_URL"] = base
        if not env.get("ANTHROPIC_API_KEY") and not env.get("ANTHROPIC_AUTH_TOKEN"):
            env["ANTHROPIC_API_KEY"] = _FREERIDE_SENTINEL_KEY
    elif cli_name == "gemini":
        env["GOOGLE_GEMINI_BASE_URL"] = base
        # Newer gemini-cli versions ship a dedicated AuthType.GATEWAY
        # path that allows empty keys when GOOGLE_GEMINI_BASE_URL is
        # set — but 0.42.0 and earlier still require *some* auth env
        # var to be present, otherwise the CLI short-circuits with
        # "Please set an Auth method" before making any HTTP request.
        # Inject a sentinel GEMINI_API_KEY when the parent has none,
        # same pattern as the claude / codex wrappers. The gateway's
        # has_inbound_auth helper recognizes this value and ignores
        # it for routing decisions.
        if not env.get("GEMINI_API_KEY") and not env.get("GOOGLE_API_KEY"):
            env["GEMINI_API_KEY"] = _FREERIDE_SENTINEL_KEY
    elif cli_name == "codex":
        if not env.get("CODEX_API_KEY"):
            env["CODEX_API_KEY"] = _FREERIDE_SENTINEL_KEY
        # Codex base URL injection lives in prepare_codex_argv (TOML
        # config / -c flag, not env).
    else:
        # Unknown tool — set every base-URL env var we know about. If
        # the tool honors *any* of them it'll route through the gateway;
        # if it honors none, this is no worse than the default.
        env["ANTHROPIC_BASE_URL"] = base
        env["GOOGLE_GEMINI_BASE_URL"] = base

    return env


def prepare_codex_argv(command_argv: list[str], base_url: str) -> list[str]:
    """Inject ``-c openai_base_url=<gateway>/v1`` into a codex argv.

    Codex CLI reads its base URL from ``~/.codex/config.toml`` (key
    ``openai_base_url``), not from any env var. To redirect a single
    invocation without mutating the user's config file, we pass the
    same key via the ``-c`` CLI flag. Codex's config precedence is
    last-write-wins per key, so we prepend ours right after the binary
    name — any explicit user ``-c openai_base_url=...`` later in argv
    will still override ours.

    The ``/v1`` suffix is required: codex's default is
    ``https://api.openai.com/v1`` and it appends ``/responses``
    directly, so the base must include /v1.
    """
    if not command_argv:
        return command_argv
    base = base_url.rstrip("/")
    return [command_argv[0], "-c", f"openai_base_url={base}/v1"] + command_argv[1:]


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

    cli_name = _detect_cli(command_argv)
    if cli_name == "codex":
        command_argv = prepare_codex_argv(command_argv, base_url)
    child_env = build_child_env(
        base_url=base_url,
        parent_env=os.environ.copy(),
        cli_name=cli_name,
    )

    # Banner — only when wrapping `claude` AND running an interactive
    # TTY. claude-cli's hardcoded /model picker doesn't surface
    # freeride/* virtual ids, so print them once at session start.
    # gemini-cli and codex have their own model-selection UIs, so we
    # don't show this banner for them.
    # Suppressed when stdin isn't a tty (CI, scripts, --print mode
    # already detached) so we don't clutter machine consumers.
    if (
        cli_name == "claude"
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
