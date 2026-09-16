"""``python -m onetree.bench``: run the hermetic leg, verify a run, or compare two runs.

``run`` takes every grid value explicitly, because the preregistered grid is the task 25
amendment's to fix and no default here may pass for it. ``--commit-payload`` names the
operator's admitted gate and ``--message-file`` the operator's landing message, which carries
the operator's own identity trailers; the onetree arm lands every task with it verbatim. With
``--json`` stdout carries exactly one JSON object and the exit status is 0 only for a complete
run, a void-free verification or a void-free comparison.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, NoReturn

from onetree.bench import SYSTEMS, receipts, runner
from onetree.bench.corpus import Grid


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ValueError(message)


def _systems(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _parser() -> _Parser:
    parser = _Parser(prog="python -m onetree.bench", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    run = commands.add_parser("run", help="run the hermetic leg into a new directory")
    run.add_argument("--out", required=True, type=Path)
    run.add_argument("--commit-payload", required=True, type=Path)
    run.add_argument("--message-file", required=True, type=Path)
    run.add_argument("--seed", required=True, type=int)
    run.add_argument("--writers", required=True, type=int)
    run.add_argument("--tasks", required=True, type=int)
    run.add_argument("--files", required=True, type=int)
    run.add_argument("--conflict", required=True, type=float)
    run.add_argument("--paths-per-task", type=int, default=1)
    run.add_argument("--jitter-ms", type=int, default=20)
    run.add_argument("--systems", type=_systems, default=SYSTEMS)
    run.add_argument("--max-attempts", type=int, default=16)
    run.add_argument("--deadline", type=float, default=600.0)
    run.add_argument("--json", action="store_true")
    verify = commands.add_parser("verify", help="recompute a run's receipts and report voids")
    verify.add_argument("--run", required=True, type=Path)
    verify.add_argument("--json", action="store_true")
    compare = commands.add_parser("compare", help="compare two runs of one seed and grid")
    compare.add_argument("--first", required=True, type=Path)
    compare.add_argument("--second", required=True, type=Path)
    compare.add_argument("--json", action="store_true")
    return parser


def _error(error: BaseException) -> dict[str, Any]:
    return {"type": type(error).__name__, "message": str(error)}


def _dispatch(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "verify":
        return receipts.verify(args.run)
    if args.command == "compare":
        return receipts.compare(args.first, args.second)
    grid = Grid(
        writers=args.writers,
        tasks=args.tasks,
        files=args.files,
        conflict=args.conflict,
        paths_per_task=args.paths_per_task,
        jitter_ms=args.jitter_ms,
    )
    settings = runner.Settings(
        seed=args.seed,
        grid=grid,
        systems=tuple(args.systems),
        gate=args.commit_payload,
        message=args.message_file.read_text(encoding="utf-8"),
        max_attempts=args.max_attempts,
        deadline_s=args.deadline,
        ttl=args.deadline + 60,
    )
    return runner.run(args.out, settings)


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    json_mode = "--json" in arguments
    try:
        report = _dispatch(_parser().parse_args(arguments))
    except (Exception, KeyboardInterrupt) as error:
        report = {"ok": False, "error": _error(error)}
    if json_mode:
        sys.stdout.write(json.dumps(report, sort_keys=True) + "\n")
    elif report.get("ok"):
        sys.stdout.write(f"ok: {report.get('run') or report.get('first') or ''}\n")
    else:
        detail = report.get("error") or report.get("voids")
        sys.stdout.write(f"failed: {json.dumps(detail, sort_keys=True)}\n")
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
