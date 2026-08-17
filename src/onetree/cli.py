"""Command-line entry point.

Every subcommand supports --json; machine output is a single JSON object on
stdout and nothing else, so agent sessions can parse without scraping.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from onetree import __version__


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="onetree", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    status = subparsers.add_parser("status", help="Report tool version and build phase.")
    status.add_argument("--json", action="store_true", help="Emit a single JSON object.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "status":
        payload = {"name": "onetree", "version": __version__, "phase": "gate-0"}
        if args.json:
            sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
        else:
            sys.stdout.write(f"onetree {__version__} (phase: gate-0)\n")
        return 0
    raise AssertionError("unreachable: subparser is required")


if __name__ == "__main__":
    raise SystemExit(main())
