"""Exploratory same-target landing-contention instrument (not the preregistered benchmark).

N real lander processes, each the registered body of its own session, land disjoint
claimed files on one target of a disposable repository through the real ``onetree land``
CLI, with an admitted CLEAN stub payload gate and a referee that sleeps T seconds. A
lander resubmits after a ``train_pending`` refusal or a landing retry-limit error, as an
agent following the Gate3 workflow would, up to a bound; any other refusal is final.

An optional control session appends unrelated WAL events (claim and release of a file no
lander touches) throughout the run. This is not ``docs/PREREGISTRATION.md``'s protocol and
none of its numbers are benchmark results; see ``docs/ROUND-CONTENTION.md``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shlex
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

import onetree
from onetree.core import Coordinator

#: Refusals a Gate3 lander answers by landing again (AGENTS.md / the dogfood brief).
RESUBMIT_REASONS = frozenset({"train_pending", "retry_limit"})
DEFAULT_MAX_SUBMISSIONS = 50
TARGET = "refs/heads/main"

_GATE_SOURCE = """\
import json, subprocess, sys
candidate, repository = sys.argv[2], sys.argv[4]
listed = subprocess.run(
    [{git!r}, "-C", repository, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z",
     candidate], check=True, capture_output=True).stdout
paths = sorted(p.decode() for p in listed.split(b"\\0") if p)
print(json.dumps({{"candidate": candidate, "repository": repository, "staged_files": paths,
                   "findings": [], "degraded": [], "verdict": "CLEAN"}}))
"""

_REFEREE_SOURCE = """\
import json, os, sys, time
log, seconds = sys.argv[1], float(sys.argv[2])
start = time.time()
time.sleep(seconds)
record = {"candidate": os.environ["ONETREE_CANDIDATE"],
          "baseline": os.environ["ONETREE_BASELINE"], "start": start, "end": time.time()}
with open(log, "a", encoding="utf-8") as stream:
    stream.write(json.dumps(record) + "\\n")
"""


@dataclass(frozen=True)
class Cell:
    landers: int
    referee_seconds: float
    control: bool
    repetition: int
    #: Seconds the control session sleeps between claim/release pairs.
    control_interval: float = 0.02


def _git(root: Path, *arguments: str, env: Mapping[str, str] | None = None) -> str:
    git = shutil.which("git") or "git"
    completed = subprocess.run(  # noqa: S603 - fixed argv
        [git, "-C", str(root), *arguments], check=True, capture_output=True, text=True, env=env,
    )
    return completed.stdout


def _package_root() -> str:
    return str(Path(onetree.__file__).resolve().parents[1])


def _child_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = _package_root()
    environment.pop("ONETREE_COMMIT_PAYLOAD", None)
    return environment


def build_repository(root: Path, landers: int, referee_seconds: float) -> dict[str, Path]:
    """A disposable protected repository with one file per lander, a gate and a referee."""

    root.mkdir(parents=True)
    tooling = root.parent / f"{root.name}-tooling"
    tooling.mkdir()
    _git(root, "init", "--quiet", "--initial-branch=main")
    _git(root, "config", "user.name", "onetree contention bench")
    _git(root, "config", "user.email", "contention@example.invalid")
    for index in range(landers):
        (root / f"f{index}.txt").write_text("base\n", encoding="utf-8")
    (root / "noise.txt").write_text("noise\n", encoding="utf-8")
    _git(root, "add", "--", ".")
    _git(root, "commit", "--quiet", "-m", "baseline")
    gate = tooling / "clean_gate.py"
    gate.write_text(_GATE_SOURCE.format(git=shutil.which("git") or "git"), encoding="utf-8")
    _git(root, "config", "onetree.payloadGateSha256",
         hashlib.sha256(gate.read_bytes()).hexdigest())
    _git(root, "config", "onetree.payloadGate", str(gate.resolve()))
    referee = tooling / "referee.py"
    referee.write_text(_REFEREE_SOURCE, encoding="utf-8")
    referee_log = tooling / "referee.jsonl"
    _git(root, "config", "onetree.referee", shlex.join(
        [sys.executable, "-I", "-S", str(referee), str(referee_log), repr(referee_seconds)]
    ))
    _git(root, "config", "onetree.refereeTimeout", str(max(60.0, referee_seconds * 10)))
    return {"gate": gate, "referee_log": referee_log, "tooling": tooling}


def _submit(root: str, actor: str, gate: str, python: str) -> dict[str, Any]:
    started = time.time()
    completed = subprocess.run(  # noqa: S603 - fixed argv
        [python, "-m", "onetree.cli", "land", "--repo", root, "--session", actor,
         "--target", TARGET, "--commit-payload", gate, "-m", f"contention {actor}", "--json"],
        check=False, capture_output=True, text=True, env=_child_environment(),
    )
    ended = time.time()
    try:
        payload = json.loads(completed.stdout)
    except ValueError:
        payload = {"ok": False, "error": {"type": "unparsed", "message": completed.stderr[-400:]}}
    record: dict[str, Any] = {"start": started, "end": ended, "exit": completed.returncode}
    if payload.get("ok"):
        record.update(outcome="landed", attempts=payload["land"]["attempts"],
                      commit=payload["land"]["commit"])
        return record
    error = payload.get("error", {})
    reason = error.get("reason")
    if reason is None and "retry limit" in str(error.get("message", "")):
        reason = "retry_limit"
        record["attempts"] = error.get("attempts")
    record.update(outcome="refused", reason=reason or error.get("type", "unknown"),
                  message=str(error.get("message", ""))[:200])
    return record


def lander_main(arguments: Sequence[str]) -> int:
    """Body of one session: wait for the start signal, then land until done or refused."""

    root, actor, gate, go, out, max_submissions = arguments
    while not Path(go).exists():
        time.sleep(0.005)
    submissions: list[dict[str, Any]] = []
    for _ in range(int(max_submissions)):
        record = _submit(root, actor, gate, sys.executable)
        submissions.append(record)
        if record["outcome"] == "landed" or record.get("reason") not in RESUBMIT_REASONS:
            break
    Path(out).write_text(json.dumps({"actor": actor, "submissions": submissions}),
                         encoding="utf-8")
    return 0


def control_main(arguments: Sequence[str]) -> int:
    """Body of the unrelated-append session: claim and release noise.txt until told to stop."""

    root, actor, stop, out, interval = arguments
    coordinator = Coordinator(Path(root))
    appended = 0
    while not Path(stop).exists():
        coordinator.claim(actor, [str(Path(root) / "noise.txt")])
        coordinator.release(actor, [str(Path(root) / "noise.txt")])
        appended += 2
        time.sleep(float(interval))
    Path(out).write_text(json.dumps({"appended": appended}), encoding="utf-8")
    return 0


def _spawn(mode: str, *arguments: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(  # noqa: S603 - fixed argv
        [sys.executable, "-m", "onetree.bench.contention", mode, *arguments],
        env=_child_environment(), stdin=subprocess.DEVNULL,
    )


def jain(values: Sequence[float]) -> float | None:
    """Jain's fairness index: 1.0 when all values are equal, 1/n at maximal skew."""

    if not values or not any(values):
        return None
    return sum(values) ** 2 / (len(values) * sum(value * value for value in values))


def run_cell(cell: Cell, workdir: Path, max_submissions: int = DEFAULT_MAX_SUBMISSIONS,
             deadline: float = 1200.0) -> dict[str, Any]:
    """Run one grid cell and return its per-landing rows and per-run summary."""

    label = (f"n{cell.landers}-t{cell.referee_seconds}-c{int(cell.control)}"
             f"{cell.control_interval if cell.control else ''}-r{cell.repetition}")
    root = workdir / label
    paths = build_repository(root, cell.landers, cell.referee_seconds)
    tooling = paths["tooling"]
    coordinator = Coordinator(root)
    coordinator.protect_workspace("bench")
    go, stop = tooling / "go", tooling / "stop"
    landers: list[tuple[str, subprocess.Popen[bytes], Path]] = []
    control: subprocess.Popen[bytes] | None = None
    try:
        for index in range(cell.landers):
            actor = f"lander{index}"
            out = tooling / f"{actor}.json"
            process = _spawn("lander", str(root), actor, str(paths["gate"]), str(go), str(out),
                             str(max_submissions))
            landers.append((actor, process, out))
            session = coordinator.register_session(actor, pid=process.pid, ttl=deadline + 600)
            coordinator.claim(actor, [str(root / f"f{index}.txt")], ttl=deadline + 600)
            (root / f"f{index}.txt").write_text(f"{actor}\n", encoding="utf-8")
            environment = dict(os.environ, GIT_INDEX_FILE=str(session.index_path))
            _git(root, "add", "--", f"f{index}.txt", env=environment)
        if cell.control:
            noise = coordinator.register_session("noise", pid=os.getpid(), ttl=deadline + 600)
            del noise
            control = _spawn("control", str(root), "noise", str(stop),
                             str(tooling / "control.json"), repr(cell.control_interval))
        load = os.getloadavg()[0]
        started = time.time()
        go.write_text("go", encoding="utf-8")
        for _, process, _ in landers:
            process.wait(timeout=max(1.0, started + deadline - time.time()))
        finished = time.time()
    finally:
        stop.write_text("stop", encoding="utf-8")
        for _, process, _ in landers:
            if process.poll() is None:
                process.kill()
                process.wait()
        if control is not None:
            control.wait(timeout=60)
    referee_runs = [
        json.loads(line)
        for line in paths["referee_log"].read_text(encoding="utf-8").splitlines() if line
    ] if paths["referee_log"].exists() else []
    owner: dict[str, str] = {}
    for run in referee_runs:
        changed = _git(root, "diff-tree", "--no-commit-id", "--name-only", "-r",
                       run["baseline"], run["candidate"]).split()
        owner[run["candidate"]] = next(
            (f"lander{name[1:-4]}" for name in changed if name.startswith("f")), "unknown"
        )
    events = coordinator.inspect_wal().events
    rows: list[dict[str, Any]] = []
    for actor, process, out in landers:
        report = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {
            "submissions": []}
        submissions = report["submissions"]
        landed = [item for item in submissions if item["outcome"] == "landed"]
        last = submissions[-1] if submissions else {"outcome": "missing"}
        refusals: dict[str, int] = {}
        for event in events:
            if event.kind == "refused" and event.actor == actor:
                reason = str(event.data.get("reason"))
                refusals[reason] = refusals.get(reason, 0) + 1
        rows.append({
            "cell": label, "landers": cell.landers, "referee_seconds": cell.referee_seconds,
            "control": cell.control, "repetition": cell.repetition, "actor": actor,
            "exit": process.returncode,
            "outcome": "landed" if landed else str(last.get("reason", last["outcome"])),
            "submissions": len(submissions),
            "attempts": sum(int(item.get("attempts") or 0) for item in submissions),
            "referee_runs": sum(1 for run in referee_runs if owner[run["candidate"]] == actor),
            "intents": sum(1 for event in events
                           if event.kind == "land_intent" and event.actor == actor),
            "wal_refusals": refusals,
            "wall_seconds": (landed[0]["end"] - started) if landed else None,
            "submission_reasons": [item.get("reason", item["outcome"]) for item in submissions],
        })
    landed_rows = [row for row in rows if row["outcome"] == "landed"]
    walls = [float(row["wall_seconds"]) for row in landed_rows]
    makespan = finished - started
    control_report = (json.loads((tooling / "control.json").read_text(encoding="utf-8"))
                      if (tooling / "control.json").exists() else None)
    summary = {
        "cell": label, "landers": cell.landers, "referee_seconds": cell.referee_seconds,
        "control": cell.control,
        "control_interval": cell.control_interval if cell.control else None,
        "repetition": cell.repetition, "loadavg_1m": load,
        "landed": len(landed_rows), "makespan_seconds": makespan,
        "throughput_per_minute": 60.0 * len(landed_rows) / makespan if makespan > 0 else None,
        "referee_runs": len(referee_runs),
        "referee_runs_per_landing": (len(referee_runs) / len(landed_rows)
                                     if landed_rows else None),
        "max_referee_runs": max((row["referee_runs"] for row in rows), default=0),
        "max_submissions": max((row["submissions"] for row in rows), default=0),
        "outcomes": {outcome: sum(1 for row in rows if row["outcome"] == outcome)
                     for outcome in sorted({row["outcome"] for row in rows})},
        "jain_wall": jain(walls) if len(walls) == len(rows) else None,
        "jain_referee_runs": jain([float(row["referee_runs"]) for row in rows]),
        "median_wall_seconds": statistics.median(walls) if walls else None,
        "max_wall_seconds": max(walls) if walls else None,
        "control_appends": None if control_report is None else control_report["appended"],
        "branch_commits": int(_git(root, "rev-list", "--count", TARGET).strip()) - 1,
        "wal_consistent": coordinator.inspect_wal().consistent,
    }
    return {"summary": summary, "rows": rows}


def grid(landers: Sequence[int], seconds: Sequence[float], repetitions: int,
         controls: Sequence[bool], seed: int, control_interval: float = 0.02) -> list[Cell]:
    cells = [Cell(n, t, c, r, control_interval) for n in landers for t in seconds
             for c in controls for r in range(repetitions)]
    random.Random(seed).shuffle(cells)  # noqa: S311 - run order, not secrecy
    return cells


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ValueError(message)


def _numbers(kind: type[float] | type[int], value: str) -> list[Any]:
    return [kind(part) for part in value.split(",") if part.strip()]


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "lander":
        return lander_main(argv[1:])
    if argv and argv[0] == "control":
        return control_main(argv[1:])
    parser = _Parser(prog="python -m onetree.bench.contention")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--landers", default="2,4,8")
    parser.add_argument("--referee-seconds", default="0.2,2,10")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--controls", default="0", help="comma list of 0/1")
    parser.add_argument("--seed", type=int, default=20261108)
    parser.add_argument("--control-interval", type=float, default=0.02)
    parser.add_argument("--max-submissions", type=int, default=DEFAULT_MAX_SUBMISSIONS)
    parser.add_argument("--json", action="store_true")
    try:
        args = parser.parse_args(argv)
        cells = grid(_numbers(int, args.landers), _numbers(float, args.referee_seconds),
                     args.repetitions, [bool(c) for c in _numbers(int, args.controls)],
                     args.seed, args.control_interval)
    except ValueError as error:
        sys.stdout.write(json.dumps({"ok": False, "error": str(error)}) + "\n")
        return 2
    args.out.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="onetree-contention-"))
    rows_path, summary_path = args.out / "landings.jsonl", args.out / "runs.jsonl"
    summaries = []
    for cell in cells:
        result = run_cell(cell, workdir, args.max_submissions)
        with rows_path.open("a", encoding="utf-8") as stream:
            for row in result["rows"]:
                stream.write(json.dumps(row) + "\n")
        with summary_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(result["summary"]) + "\n")
        summaries.append(result["summary"])
        if not args.json:
            sys.stderr.write(json.dumps(result["summary"]) + "\n")
    report = {"ok": True, "cells": len(cells), "workdir": str(workdir),
              "landings": str(rows_path), "runs": str(summary_path),
              "package": _package_root(), "seed": args.seed}
    sys.stdout.write(json.dumps(report) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
