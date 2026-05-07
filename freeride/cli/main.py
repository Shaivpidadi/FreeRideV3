"""freeride — Free-AI gateway CLI.

Phase 1: dispatches v2 subcommands (auto, list, switch, status, refresh,
fallbacks, rotate) to the v2compat package so existing v2 users get an
in-place upgrade. Phase 2+ adds ``serve`` for the gateway and ``bind``
for agent setup helpers.
"""

from __future__ import annotations

import argparse
import sys

from freeride import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="freeride",
        description="Free-AI gateway: OpenAI-compatible local proxy across free-tier providers.",
    )
    parser.add_argument("--version", action="version", version=f"freeride {__version__}")

    sub = parser.add_subparsers(dest="command", help="Available commands")

    # ---- v2-parity commands ------------------------------------------------
    p_list = sub.add_parser("list", help="List available free models (v2 behavior)")
    p_list.add_argument("--limit", "-n", type=int, default=15)
    p_list.add_argument("--refresh", "-r", action="store_true",
                        help="Force refresh from API (ignore cache)")

    p_switch = sub.add_parser("switch", help="Switch to a specific model (v2 behavior)")
    p_switch.add_argument("model", help="Model ID to switch to")
    p_switch.add_argument("--fallback-only", "-f", action="store_true",
                          help="Add to fallbacks only, don't change primary")
    p_switch.add_argument("--no-fallbacks", action="store_true",
                          help="Don't configure fallback models")
    p_switch.add_argument("--setup-auth", action="store_true",
                          help="Also set up OpenRouter auth profile")

    p_auto = sub.add_parser("auto", help="Auto-select best free model (v2 behavior)")
    p_auto.add_argument("--fallback-count", "-c", type=int, default=5)
    p_auto.add_argument("--fallback-only", "-f", action="store_true")
    p_auto.add_argument("--setup-auth", action="store_true")

    sub.add_parser("status", help="Show current configuration (v2 behavior)")
    sub.add_parser("refresh", help="Refresh model cache (v2 behavior)")

    p_fb = sub.add_parser("fallbacks", help="Configure fallback models (v2 behavior)")
    p_fb.add_argument("--count", "-c", type=int, default=5)

    p_rot = sub.add_parser("rotate", help="Live-test primary; swap if it fails (v2 behavior)")
    p_rot.add_argument("--force", "-f", action="store_true",
                       help="Rotate even if the current primary is healthy")
    p_rot.add_argument("--fallback-count", "-c", type=int, default=5)

    # ---- gateway commands (stubs; impl in Phase 2/4/5) --------------------
    p_serve = sub.add_parser("serve", help="Start the FreeRide gateway server (Phase 2)")
    p_serve.add_argument("--port", type=int, default=11343)
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--verbose", action="store_true")

    p_bind = sub.add_parser("bind", help="Configure an agent to use the gateway")
    p_bind.add_argument("agent", choices=["openclaw", "aider", "continue", "hermes", "opencode"])
    p_bind.add_argument("--gateway-url", default="http://localhost:11343/v1")
    p_bind.add_argument(
        "--scope",
        choices=["home", "cwd", "git"],
        default="home",
        help="Aider only: which config-file scope to write (default: home)",
    )

    p_tel = sub.add_parser("telemetry", help="Manage telemetry beacon (Phase 5)")
    p_tel.add_argument("state", nargs="?", choices=["on", "off"])

    p_watch = sub.add_parser(
        "watch",
        help="Tail live failover events from a running gateway (~/.freeride/events.jsonl)",
    )
    p_watch.add_argument(
        "--since-start",
        action="store_true",
        help="Replay all existing events before tailing new ones",
    )
    p_watch.add_argument(
        "--no-color", action="store_true", help="Disable ANSI color output"
    )

    p_bench = sub.add_parser(
        "bench",
        help="Per-provider latency comparison via the running gateway",
    )
    p_bench.add_argument(
        "--url",
        default="http://localhost:11343/v1",
        help="Gateway base URL (default: http://localhost:11343/v1)",
    )
    p_bench.add_argument(
        "--n",
        type=int,
        default=3,
        help="Requests per provider (default: 3)",
    )
    p_bench.add_argument(
        "--model",
        default="openrouter/free",
        help="Model id to ask each provider for (default: openrouter/free)",
    )
    p_bench.add_argument(
        "--prompt",
        default=None,
        help="Override the test prompt (default: 'Reply with exactly one word: hi.')",
    )
    p_bench.add_argument("--no-color", action="store_true")

    p_reload = sub.add_parser(
        "reload",
        help="Reload providers from env vars on a running gateway (no restart)",
    )
    p_reload.add_argument(
        "--url",
        default="http://localhost:11343/v1",
        help="Gateway base URL (default: http://localhost:11343/v1)",
    )

    p_providers = sub.add_parser(
        "providers",
        help="Show live provider health from a running gateway",
    )
    p_providers.add_argument(
        "--url",
        default="http://localhost:11343/v1",
        help="Gateway base URL (default: http://localhost:11343/v1)",
    )
    p_providers.add_argument("--no-color", action="store_true")

    p_doctor = sub.add_parser(
        "doctor",
        help="Diagnose common setup issues (env vars, PATH, port, gateway reachability)",
    )
    p_doctor.add_argument("--no-color", action="store_true")

    p_upgrade = sub.add_parser(
        "upgrade",
        help="Bump installed freeride-gateway to the latest PyPI release",
    )
    p_upgrade.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the command that would run, don't execute it",
    )

    p_init = sub.add_parser(
        "init",
        help="Interactive setup wizard: collects provider keys, writes .env",
    )
    p_init.add_argument(
        "--out",
        default=None,
        help="Where to write the .env file (default: ~/.freeride/.env)",
    )
    p_init.add_argument(
        "--open-browser",
        action="store_true",
        help="Open each provider's signup URL in your default browser",
    )

    p_keys = sub.add_parser(
        "keys",
        help="Show which provider keys are available vs cooling",
    )
    p_keys.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show per-key breakdown with remaining cooldown",
    )
    p_keys.add_argument("--no-color", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    # Show the telemetry disclosure banner BEFORE argparse runs.
    # argparse handles `--help` and `--version` by writing to stdout and
    # calling sys.exit() — anything we do after parse_args() never runs
    # for those flag invocations. So the banner check must happen up
    # front, gated only on "is the user explicitly managing telemetry?"
    # which we detect from argv directly.
    raw_argv = argv if argv is not None else sys.argv[1:]
    if not (raw_argv and raw_argv[0] == "telemetry"):
        from freeride.core.telemetry import show_disclosure_banner_once

        show_disclosure_banner_once()

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    # v2-parity dispatch
    if args.command in {"list", "switch", "auto", "status", "refresh", "fallbacks", "rotate"}:
        from freeride.v2compat import commands as cmd

        handler = getattr(cmd, f"cmd_{args.command}")
        handler(args)
        return 0

    if args.command == "serve":
        from freeride.cli.cmd_serve import cmd_serve

        return cmd_serve(args)

    if args.command == "bind":
        from freeride.cli.cmd_bind import cmd_bind

        return cmd_bind(args)

    if args.command == "telemetry":
        from freeride.cli.cmd_telemetry import cmd_telemetry

        return cmd_telemetry(args)

    if args.command == "watch":
        from freeride.cli.cmd_watch import cmd_watch

        return cmd_watch(args)

    if args.command == "bench":
        from freeride.cli.cmd_bench import cmd_bench

        return cmd_bench(args)

    if args.command == "reload":
        from freeride.cli.cmd_reload import cmd_reload

        return cmd_reload(args)

    if args.command == "providers":
        from freeride.cli.cmd_providers import cmd_providers

        return cmd_providers(args)

    if args.command == "doctor":
        from freeride.cli.cmd_doctor import cmd_doctor

        return cmd_doctor(args)

    if args.command == "upgrade":
        from freeride.cli.cmd_upgrade import cmd_upgrade

        return cmd_upgrade(args)

    if args.command == "init":
        from freeride.cli.cmd_init import cmd_init

        return cmd_init(args)

    if args.command == "keys":
        from freeride.cli.cmd_keys import cmd_keys

        return cmd_keys(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
