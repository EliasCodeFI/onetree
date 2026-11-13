"""Synthetic local writers; only Git and WAL evidence can establish completion.

    python -m onetree.demo --output /tmp/onetree-demo

The output directory must be new. Evidence is retained on failure. Without
--commit-payload the demo admits onetree's standard gate in the sample repository.
"""

from __future__ import annotations

import argparse
import json
import math
import multiprocessing
import os
import shlex
import sys
import time
from dataclasses import asdict
from multiprocessing.connection import Connection, wait
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import Any, NoReturn, TextIO

from onetree.core import Coordinator
from onetree.errors import ClaimConflictError, OnetreeError
from onetree.git import Repository
from onetree.initialize import install_standard_gate

_SAMPLE_CHECK = """from pathlib import Path
import sys

root = Path.cwd()
valid = root.joinpath("shared.txt").read_text() == "shared\\n"
for number in range(int(sys.argv[1])):
    content = root.joinpath(f"worker-{number}.txt").read_text()
    valid = valid and content in ("initial\\n", f"worker {number}\\n")
raise SystemExit(0 if valid else 1)
"""


DEFAULT_MESSAGE = "onetree demo: a synthetic worker lands its file\n"


class DemoError(RuntimeError):
    pass


def _error(error: BaseException) -> dict[str, Any]:
    if isinstance(error, OnetreeError):
        return error.payload()
    return {"type": type(error).__name__, "message": str(error)}


def _write(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _setup(
    output: Path, gate: Path | None, workers: int, timeout: float
) -> tuple[Coordinator, Path]:
    repo_path = output / "repo"
    repo_path.mkdir()
    Repository._invoke(repo_path, ["init", "--initial-branch=main"], check=True)
    repo = Repository(repo_path)
    repo.git("config", "user.name", "OneTree synthetic demo")
    repo.git("config", "user.email", "demo@example.invalid")
    (repo_path / "shared.txt").write_text("shared\n", encoding="utf-8")
    names = ["shared.txt", "check_sample.py"]
    for number in range(workers):
        name = f"worker-{number}.txt"
        names.append(name)
        (repo_path / name).write_text("initial\n", encoding="utf-8")
    (repo_path / "check_sample.py").write_text(_SAMPLE_CHECK, encoding="utf-8")
    repo.git("add", "--", *names)
    tree = repo.git_text("write-tree")
    base = repo.git_text("commit-tree", tree, input_bytes=b"Synthetic sample baseline\n")
    repo.git("update-ref", "refs/heads/main", base)
    command = shlex.join([sys.executable, "check_sample.py", str(workers)])
    repo.git("config", "onetree.referee", command)
    repo.git("config", "onetree.refereeTimeout", str(timeout))
    if gate is None:
        gate, _ = install_standard_gate(repo)
    repo.admit_payload_gate(gate)
    coordinator = Coordinator(repo_path)
    coordinator.register_session("demo-controller", pid=os.getpid(), ttl=timeout + 60)
    coordinator.protect_workspace("demo-controller")
    return coordinator, gate


def _worker(
    connection: Connection,
    repo_path: Path,
    gate: Path,
    message: str,
    number: int,
    ttl: float,
) -> None:
    actor = f"demo-worker-{number}"
    coordinator = Coordinator(repo_path, commit_payload=gate)
    try:
        session = coordinator.register_session(actor, pid=os.getpid(), ttl=ttl)
        path = repo_path / f"worker-{number}.txt"
        coordinator.claim(actor, [str(path)], ttl=ttl)
        if number == 0:
            coordinator.claim(actor, [str(repo_path / "shared.txt")], ttl=ttl)
        connection.send({"phase": "ready", "actor": actor, "pid": os.getpid()})
        if connection.recv() != "conflict":
            raise DemoError("unexpected command before claim demonstration")
        refusal: dict[str, Any] | None = None
        if number:
            try:
                coordinator.claim(actor, [str(repo_path / "shared.txt")], ttl=ttl)
            except ClaimConflictError as error:
                refusal = error.payload()
            else:
                raise DemoError("the conflicting claim was unexpectedly accepted")
        connection.send({"phase": "conflict", "conflict": refusal})
        if connection.recv() != "land":
            raise DemoError("unexpected command before landing")
        path.write_text(f"worker {number}\n", encoding="utf-8")
        coordinator.repo.git("add", "--", path.name, index=session.index_path)
        result = coordinator.land(actor, message=message)
        coordinator.release(actor)
        connection.send({"phase": "landed", "land": asdict(result)})
    except BaseException as error:
        connection.send({"phase": "error", "error": _error(error)})
    finally:
        connection.close()


def _collect(
    connections: list[Connection],
    records: list[dict[str, Any]],
    phase: str,
    deadline: float,
    progress: TextIO,
) -> None:
    pending = set(range(len(connections)))
    while pending:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"demo deadline expired waiting for {phase}")
        ready = wait([connections[number] for number in pending], timeout=remaining)
        if not ready:
            raise TimeoutError(f"demo deadline expired waiting for {phase}")
        for number in sorted(pending):
            if connections[number] not in ready:
                continue
            try:
                result = connections[number].recv()
            except EOFError as error:
                raise DemoError(f"worker {number} exited without {phase} evidence") from error
            if not isinstance(result, dict) or result.get("phase") not in {phase, "error"}:
                raise DemoError(f"worker {number} returned an unexpected response")
            records[number].update(result)
            progress.write(f"worker {number}: {result['phase']}\n")
            progress.flush()
            if result["phase"] == "error":
                raise DemoError(f"worker {number} failed: {result['error']}")
            pending.remove(number)


def _verify(coordinator: Coordinator, records: list[dict[str, Any]]) -> dict[str, Any]:
    report = coordinator.inspect_wal()
    evidence = {
        "head": report.head,
        "consistent": report.consistent,
        "pending_intents": report.pending_intents,
        "active_claims": list(report.active_claims),
        "divergences": [item.payload() for item in report.divergences],
        "events": [item.payload() for item in report.events],
    }
    if not report.consistent or report.pending_intents or report.active_claims:
        raise DemoError("final WAL is incomplete or inconsistent")
    commits: set[str] = set()
    pids: set[int] = set()
    for number, record in enumerate(records):
        actor = f"demo-worker-{number}"
        if record.get("phase") != "landed" or record.get("exitcode") != 0:
            raise DemoError(f"worker {number} did not finish successfully")
        pid = record.get("pid")
        if record.get("actor") != actor or not isinstance(pid, int) or pid <= 0 or pid in pids:
            raise DemoError(f"worker {number} has inconsistent process evidence")
        pids.add(record["pid"])
        if number and record.get("conflict", {}).get("type") != "claim_conflict":
            raise DemoError(f"worker {number} lacks its conflict refusal")
        land = record["land"]
        path = f"worker-{number}.txt"
        if land["payload"] != [path] or land["commit"] in commits:
            raise DemoError(f"worker {number} has inconsistent payload evidence")
        commits.add(land["commit"])
        landed = [
            event
            for event in report.events
            if event.kind == "landed"
            and event.actor == actor
            and event.data.get("commit") == land["commit"]
            and event.data.get("intent") == land["intent"]
        ]
        intents = [
            event
            for event in report.events
            if event.kind == "land_intent"
            and event.actor == actor
            and event.event_id == land["intent"]
        ]
        if len(landed) != 1 or len(intents) != 1:
            raise DemoError(f"worker {number} lacks paired WAL evidence")
        content = coordinator.repo.git_text("show", f"{land['commit']}:{path}")
        final = coordinator.repo.git_text("show", f"HEAD:{path}")
        if content != f"worker {number}" or final != content:
            raise DemoError(f"worker {number} has incorrect committed content")
    return evidence


def run(
    output: Path,
    gate: Path | None,
    message: str,
    *,
    workers: int,
    timeout: float,
    progress: TextIO,
) -> dict[str, Any]:
    report: dict[str, Any] = {"ok": False, "synthetic": True, "workers": []}
    processes: list[BaseProcess] = []
    connections: list[Connection] = []
    owned = False
    coordinator: Coordinator | None = None
    try:
        if not 2 <= workers <= 16:
            raise ValueError("workers must be between 2 and 16")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        if not message.strip():
            raise ValueError("the operator's commit message must not be empty")
        if gate is not None and (not gate.is_absolute() or not gate.is_file()):
            raise ValueError("commit-payload must name an absolute gate file")
        # mkdir, without resolve or exist_ok, also refuses dangling symlinks.
        output.mkdir()
        owned = True
        output = output.resolve()
        report["output"] = str(output)
        _write(output / "demo.json", {"synthetic": True, "controller_pid": os.getpid()})
        (output / "workers").mkdir()
        coordinator, gate = _setup(output, gate, workers, timeout)
        admission = coordinator.repo.payload_gate_admission()
        report["gate"] = None if admission is None else admission.payload()
        context = multiprocessing.get_context("spawn")
        deadline = time.monotonic() + timeout
        for number in range(workers):
            parent, child = context.Pipe()
            process: BaseProcess = context.Process(
                target=_worker,
                args=(child, output / "repo", gate, message, number, timeout + 60),
            )
            try:
                process.start()
            except BaseException:
                parent.close()
                raise
            finally:
                child.close()
            connections.append(parent)
            processes.append(process)
            report["workers"].append({"number": number})
        records = report["workers"]
        _collect(connections, records, "ready", deadline, progress)
        for process, record in zip(processes, records, strict=True):
            if record.get("pid") != process.pid:
                raise DemoError("readiness does not identify the spawned worker")
        for phase in ("conflict", "land"):
            for connection in connections:
                connection.send(phase)
            _collect(
                connections, records, "landed" if phase == "land" else phase, deadline, progress
            )
        for process in processes:
            process.join(timeout=max(0, deadline - time.monotonic()))
            if process.is_alive():
                raise TimeoutError("worker did not exit before the demo deadline")
        for process, record in zip(processes, records, strict=True):
            record["exitcode"] = process.exitcode
            # Pipe preserves tuples; JSON evidence uses the same representation everywhere.
            record["land"]["payload"] = list(record["land"]["payload"])
        report["wal"] = _verify(coordinator, records)
        report["ok"] = True
    except (Exception, KeyboardInterrupt) as error:
        report["error"] = _error(error)
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
        for process, record in zip(processes, report["workers"], strict=True):
            process.join(timeout=2)
            if process.is_alive():
                process.kill()
                process.join()
            record["exitcode"] = process.exitcode
        for connection in connections:
            connection.close()
        if coordinator is not None and "wal" not in report:
            try:
                wal = coordinator.inspect_wal()
                report["wal"] = {
                    "head": wal.head,
                    "consistent": wal.consistent,
                    "pending_intents": wal.pending_intents,
                    "events": [event.payload() for event in wal.events],
                    "divergences": [item.payload() for item in wal.divergences],
                }
            except Exception as error:
                report["wal_error"] = _error(error)
        if owned:
            for number, record in enumerate(report["workers"]):
                _write(output / "workers" / f"{number}.json", record)
            _write(output / "report.json", report)
    return report


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ValueError(message)


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    json_mode = "--json" in arguments
    parser = _Parser(description="Run synthetic local writers in a new sample repository")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--commit-payload", type=Path)
    parser.add_argument("--message-file", type=Path)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--json", action="store_true")
    try:
        args = parser.parse_args(arguments)
        report = run(
            args.output,
            args.commit_payload,
            (
                DEFAULT_MESSAGE
                if args.message_file is None
                else args.message_file.read_text(encoding="utf-8")
            ),
            workers=args.workers,
            timeout=args.timeout,
            progress=sys.stderr if json_mode else sys.stdout,
        )
    except (Exception, KeyboardInterrupt) as error:
        report = {"ok": False, "synthetic": True, "workers": [], "error": _error(error)}
    if json_mode:
        sys.stdout.write(json.dumps(report, sort_keys=True) + "\n")
    else:
        summary = "Demo completed\n" if report["ok"] else f"Demo failed: {report['error']}\n"
        sys.stdout.write(summary)
        if "output" in report:
            sys.stdout.write(f"Evidence: {report['output']}/report.json\n")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
