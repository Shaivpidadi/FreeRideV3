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

    return parser


def main(argv: list[str] | None = None) -> int:
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

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
