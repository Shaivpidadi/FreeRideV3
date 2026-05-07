"""freeride-watcher — background process that keeps the gateway healthy."""

import argparse
import sys

from freeride import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="freeride-watcher",
        description="Background process that probes the gateway's primary model and rotates on failure.",
    )
    parser.add_argument(
        "--version", action="version", version=f"freeride-watcher {__version__}"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single check-and-rotate, then exit (implemented in later phase)",
    )
    parser.add_argument(
        "--status", "-s", action="store_true", help="Show watcher state (implemented in later phase)"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    print(
        f"freeride-watcher {__version__}: stub. Real watcher loop lands in Phase 3 "
        f"(args parsed: once={args.once}, status={args.status})."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
