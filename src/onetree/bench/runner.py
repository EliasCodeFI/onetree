"""One benchmark run: the same seeded corpus through each system, then receipts over raw data.

The output directory must not exist; nothing is written anywhere else. Per system the run
creates a fresh repository, performs the layer's setup, releases every writer process at once,
waits for them within the deadline, then records the harness referee verdicts, the target
branch's final tree and the layer's disk, and merges the per-process logs. ``metrics.json``
is computed from the merged logs only, and ``MANIFEST.sha256`` and ``FREEZE.json`` are
written last, over everything except the repositories and scratch space.

A run whose writers did not all exit cleanly before the deadline is recorded as incomplete,
never retried: the protocol reports a void, it does not re-run until one disappears.
"""

from __future__ import annotations

import hashlib
import multiprocessing
import platform
import sys
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from onetree import git as onetree_git
from onetree.bench import SCHEMA, SYSTEMS, receipts, systems
from onetree.bench import corpus as corpus_module
from onetree.bench import events as events_module
from onetree.bench import metrics as metrics_module
from onetree.bench.corpus import Corpus, Grid, Task
from onetree.bench.seeds import DERIVATION, SeedKey


@dataclass(frozen=True)
class Settings:
    """Everything a run depends on besides the machine; all of it is recorded in run.json."""

    seed: int
    grid: Grid
    systems: tuple[str, ...]
    gate: Path
    message: str
    max_attempts: int = 16
    backoff_ms: int = 5
    poll_ms: int = 20
    ttl: float = 660.0
    referee_timeout: float = 60.0
    deadline_s: float = 600.0

    def validate(self) -> None:
        if not self.systems or len(set(self.systems)) != len(self.systems):
            raise ValueError("systems must be a non-empty list without repeats")
        unknown = sorted(set(self.systems) - set(SYSTEMS))
        if unknown:
            raise ValueError(f"unknown systems: {', '.join(unknown)}")
        if "onetree" in self.systems and (not self.gate.is_absolute() or not self.gate.is_file()):
            raise ValueError("--commit-payload must name an absolute gate file")
        if "onetree" in self.systems and not self.message.strip():
            raise ValueError("the operator's landing message must not be empty")
        if self.max_attempts < 1 or self.backoff_ms < 0 or self.poll_ms < 1:
            raise ValueError("max_attempts and poll_ms must be positive, backoff_ms non-negative")
        for name in ("ttl", "referee_timeout", "deadline_s"):
            if not getattr(self, name) > 0:
                raise ValueError(f"{name} must be positive")
        if self.ttl <= self.deadline_s:
            raise ValueError("session ttl must outlive the run deadline")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _inputs(settings: Settings, referee_script: Path) -> dict[str, Any]:
    gate_sha256 = _sha256_file(settings.gate) if settings.gate.is_file() else None
    git_version = systems.git(Path.cwd(), "--version").stdout.decode().strip()
    return {
        "schema": SCHEMA,
        "seed": settings.seed,
        "grid": settings.grid.payload(),
        "systems": list(settings.systems),
        "seed_derivation": DERIVATION,
        "clock": events_module.CLOCK,
        "max_attempts": settings.max_attempts,
        "backoff_ms": settings.backoff_ms,
        "claim_check_poll_ms": settings.poll_ms,
        "session_ttl_s": settings.ttl,
        "referee_timeout_s": settings.referee_timeout,
        "deadline_s": settings.deadline_s,
        "gate": {
            "path": str(settings.gate),
            "sha256": gate_sha256,
            "admitted_pin_sha256": onetree_git.PAYLOAD_GATE_SOURCE_SHA256,
            "matches_admitted_pin": gate_sha256 == onetree_git.PAYLOAD_GATE_SOURCE_SHA256,
        },
        "referee_sha256": _sha256_file(referee_script),
        "message_sha256": hashlib.sha256(settings.message.encode("utf-8")).hexdigest(),
        "python": sys.version,
        "platform": platform.platform(),
        "git": git_version,
        "not_a_result": receipts.NOT_A_RESULT,
    }


def _writer_process(
    arm: systems.Arm,
    writer: int,
    tasks: Sequence[Task],
    part: Path,
    barrier: Any,
    deadline_s: float,
) -> None:
    """Runs only in a forked child, which the coverage collector does not measure."""

    events = events_module.EventWriter(part, arm.system, writer)
    barrier.wait(timeout=deadline_s)
    systems.run_writer(arm, tasks, events)


def execute(arm: systems.Arm, corpus: Corpus, parts: Path, deadline_s: float) -> dict[str, Any]:
    """Fork one process per writer, release them together, and wait within the deadline."""

    context = multiprocessing.get_context("fork")
    writers = sorted(corpus.tasks)
    barrier = context.Barrier(len(writers) + 1)
    processes = [
        context.Process(
            target=_writer_process,
            args=(
                arm,
                writer,
                corpus.tasks[writer],
                parts / f"{arm.system}-w{writer}.jsonl",
                barrier,
                deadline_s,
            ),
            name=f"onetree-bench-{arm.system}-w{writer}",
        )
        for writer in writers
    ]
    for process in processes:
        process.start()
    complete = True
    try:
        barrier.wait(timeout=deadline_s)
    except threading.BrokenBarrierError:
        complete = False
    started = events_module.now()
    for process in processes:
        process.join(timeout=max(0.0, started + deadline_s - events_module.now()))
    ended = events_module.now()
    exitcodes: list[int | None] = []
    for process in processes:
        if process.is_alive():
            complete = False
            process.terminate()
            process.join(timeout=5)
            if process.is_alive():
                process.kill()
                process.join()
        exitcodes.append(process.exitcode)
    complete = complete and all(code == 0 for code in exitcodes)
    return {"started": started, "ended": ended, "complete": complete, "exitcodes": exitcodes}


def prepare(
    out: Path, system: str, corpus: Corpus, settings: Settings, referee_script: Path
) -> tuple[systems.Arm, events_module.EventWriter]:
    root = out / "repos" / system
    checkout = root / "main"
    systems.init_repository(checkout, corpus)
    arm = systems.Arm(
        system=system,
        root=root,
        checkout=checkout,
        gate=settings.gate if system == "onetree" else None,
        message=settings.message,
        ttl=settings.ttl,
        max_attempts=settings.max_attempts,
        backoff_ms=settings.backoff_ms,
        wait_s=settings.deadline_s,
        poll_ms=settings.poll_ms,
    )
    harness = events_module.EventWriter(
        out / "events" / "parts" / f"{system}-harness.jsonl", system, None
    )
    started = events_module.now()
    detail = systems.setup(arm, settings.grid.writers, referee_script, settings.referee_timeout)
    duration = events_module.now() - started
    disk = systems.layer_disk(arm)
    harness.emit(
        "setup", duration_s=duration, disk_bytes=sum(disk.values()), disk_detail=disk, detail=detail
    )
    return arm, harness


def conclude(
    out: Path,
    arm: systems.Arm,
    corpus: Corpus,
    harness: events_module.EventWriter,
    referee_script: Path,
    referee_timeout: float,
) -> list[dict[str, Any]]:
    """Harness referee for systems without one, the final tree, the disk sample, the merge."""

    parts = out / "events" / "parts"
    if arm.system != "onetree":
        landed = [
            event
            for part in sorted(parts.glob(f"{arm.system}-w*.jsonl"))
            for event in events_module.read(part)
            if event["kind"] == "landed"
        ]
        scratch = out / "tmp" / arm.system
        scratch.mkdir(parents=True, exist_ok=True)
        for event in sorted(landed, key=lambda item: (float(item["t"]), str(item["commit"]))):
            started = events_module.now()
            verdict = systems.referee_commit(
                arm.checkout, str(event["commit"]), referee_script, scratch, referee_timeout
            )
            harness.emit(
                "referee",
                commit=event["commit"],
                duration_s=events_module.now() - started,
                **verdict,
            )
    disk = systems.layer_disk(arm)
    harness.emit(
        "final",
        tokens_by_path=systems.final_tokens(arm.checkout, corpus.files),
        head=systems.git_text(arm.checkout, "rev-parse", systems.TARGET),
        disk_bytes=sum(disk.values()),
        disk_detail=disk,
    )
    return events_module.merge(
        sorted(parts.glob(f"{arm.system}-*.jsonl")), out / "events" / f"{arm.system}.jsonl"
    )


def run(out: Path, settings: Settings) -> dict[str, Any]:
    settings.validate()
    out.mkdir()
    out = out.resolve()
    harness_dir = out / "harness"
    harness_dir.mkdir()
    referee_script = harness_dir / "referee_check.py"
    referee_script.write_text(systems.REFEREE_SOURCE, encoding="utf-8")
    corpus = corpus_module.build(settings.grid, SeedKey(settings.seed))
    receipts.write_json(out / "corpus.json", corpus.payload())
    inputs = _inputs(settings, referee_script)
    receipts.write_json(out / "run.json", inputs)
    complete = True
    metrics: dict[str, Any] = {}
    for system in settings.systems:
        arm, harness = prepare(out, system, corpus, settings, referee_script)
        window = execute(arm, corpus, out / "events" / "parts", settings.deadline_s)
        harness.emit(
            "run",
            started=window["started"],
            ended=window["ended"],
            complete=window["complete"],
            exitcodes=window["exitcodes"],
        )
        complete = complete and bool(window["complete"])
        merged = conclude(out, arm, corpus, harness, referee_script, settings.referee_timeout)
        metrics[system] = metrics_module.compute(merged)
    receipts.write_json(out / "metrics.json", metrics)
    record = receipts.freeze(out, receipts.freeze_fields(inputs, metrics, complete))
    return {
        "ok": complete,
        "run": str(out),
        "schema": SCHEMA,
        "manifest_sha256": record["manifest_sha256"],
        "complete": complete,
        "summary": record["summary"],
        "not_a_result": inputs["not_a_result"],
    }


__all__ = ["Settings", "conclude", "execute", "prepare", "run"]
