"""Minimal ARGUS command-line entry point.

Phase 1 foundation intentionally exposes only a version command here. Runtime
commands are added by later workstreams once their durable contracts exist.
"""

from __future__ import annotations

import argparse

from argus import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="argus",
        description="Durable control-plane runtime for long-running AI agent missions",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
