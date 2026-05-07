"""freeride — Free-AI gateway CLI."""

import argparse
import sys

from freeride import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="freeride",
        description="Free-AI gateway: OpenAI-compatible local proxy across free-tier providers.",
    )
    parser.add_argument("--version", action="version", version=f"freeride {__version__}")
    parser.add_argument(
        "command",
        nargs="?",
        choices=[
            "auto",
            "list",
            "switch",
            "status",
            "refresh",
            "fallbacks",
            "rotate",
            "serve",
            "bind",
            "telemetry",
        ],
        help="Subcommand to run (subcommands implemented in later phases)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    print(f"freeride {__version__}: '{args.command}' is not yet implemented in this dev build.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
