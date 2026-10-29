"""Task 26 hermetic benchmark harness.

The metric tests hand each frozen definition a known-bad event log beside a control that
differs only in the property under test. The receipt tests forge a run, including forgeries
that re-freeze their own manifest. The system tests script adverse interleavings against real
Git and the real broker, and the end-to-end tests fork real writers through the CLI. The
payload gate is the suite's exact-output clean gate (``tests/conftest.py``), so nothing here
needs a live session identity.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import GitFixture

from onetree.bench import (
    SCHEMA,
    SCHEMA_V2,
    SYSTEMS,
    legacy_corpus,
    metrics,
    receipts,
    runner,
    systems,
)
from onetree.bench import __main__ as cli
from onetree.bench import corpus as corpus_module
from onetree.bench import events as events_module
from onetree.bench.corpus import Grid, Task
from onetree.bench.legacy_seeds import DERIVATION as LEGACY_DERIVATION
from onetree.bench.legacy_seeds import SeedKey
from onetree.bench.seeds import DERIVATION
from onetree.core import Coordinator

HOT = "corpus/f000.txt"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _event(kind: str, writer: int | None = 0, t: float = 0.0, **fields: Any) -> dict[str, Any]:
    return {"system": "fixture", "writer": writer, "kind": kind, "t": t, **fields}


def _write(
    writer: int, write_id: str, path: str, token: str, t: float, content: str = "c"
) -> dict[str, Any]:
    return _event(
        "write",
        writer,
        t,
        task=0,
        attempt=1,
        write_id=write_id,
        path=path,
        token=token,
        content_sha256=_sha(content),
    )


def _landed(
    writer: int,
    write_ids: list[str],
    payload: list[dict[str, Any]],
    t: float,
    commit: str = "c1",
    green: bool | None = None,
) -> dict[str, Any]:
    return _event(
        "landed",
        writer,
        t,
        task=0,
        attempt=1,
        write_ids=write_ids,
        commit=commit,
        payload=payload,
        green=green,
    )


def _entry(path: str, content: str, tokens: list[str]) -> dict[str, Any]:
    return {"path": path, "blob": "b", "content_sha256": _sha(content), "tokens": tokens}


def _gate() -> Path:
    return Path(os.environ["ONETREE_COMMIT_PAYLOAD"])


# --- seeds and corpus ---------------------------------------------------------------------


def test_seed_keys_derive_from_the_standard_library_alone() -> None:
    key = SeedKey(7).spawn("writer", "0")
    assert key.digest() == hashlib.sha256(b"7/writer/0").hexdigest()
    assert key.digest() == "6fb917fa7a6a833a2c779b9b78f275ea60a281b4e23da830b40aecab2d35cc1f"
    stranger = random.Random(int(key.digest()[:16], 16))  # noqa: S311 - recomputation
    generator = key.rng()
    drawn = [generator.getrandbits(16) for _ in range(3)]
    assert drawn == [stranger.getrandbits(16) for _ in range(3)] == [19999, 34654, 43888]
    assert SeedKey(7).spawn("writer", "1").digest() != key.digest()
    assert (key.seed, key.path) == (7, ("writer", "0"))
    assert "writer/0" in repr(key)


@pytest.mark.parametrize(
    ("seed", "path", "message"),
    [
        (True, (), "non-negative"),
        (-1, (), "non-negative"),
        (1, ("",), "non-empty"),
        (1, ("a/b",), "contain no"),
    ],
)
def test_seed_keys_refuse_ambiguous_paths(seed: int, path: tuple[str, ...], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        SeedKey(seed, path)


def test_corpus_is_fixed_by_seed_and_grid() -> None:
    grid = Grid(writers=2, tasks=2, files=3, conflict=0.5, jitter_ms=10)
    corpus = legacy_corpus.build(grid, SeedKey(7))
    assert corpus.payload() == legacy_corpus.build(grid, SeedKey(7)).payload()
    assert corpus.payload() != legacy_corpus.build(grid, SeedKey(8)).payload()
    assert corpus.hot == ("corpus/f000.txt", "corpus/f001.txt")
    assert corpus.tasks[0][0].tokens == ("6e61ea99f3ed",)
    assert corpus.all_tokens()["7307d3955e06"] == (1, 0, "corpus/f001.txt")
    assert grid.payload()["status"] == "PLACEHOLDER: not amended (task 25)"
    assert replace(grid, amended=True).payload()["status"] == "amended by task 25"


def test_conflict_extremes_partition_the_paths() -> None:
    disjoint = corpus_module.build(Grid(writers=3, tasks=20, files=9, conflict=0.0), 1)
    pools = [set(disjoint.private[writer]) for writer in range(3)]
    assert disjoint.hot == ()
    assert all(pools[a].isdisjoint(pools[b]) for a in range(3) for b in range(a + 1, 3))
    for writer, tasks in disjoint.tasks.items():
        assert corpus_module.paths_of(tasks) <= pools[writer]
    shared = corpus_module.build(Grid(writers=3, tasks=20, files=9, conflict=1.0), 1)
    assert set(shared.hot) == set(shared.files)
    assert all(corpus_module.paths_of(tasks) <= set(shared.hot) for tasks in shared.tasks.values())
    crowded = corpus_module.build(
        Grid(writers=3, tasks=5, files=3, conflict=0.0, paths_per_task=2), 1
    )
    assert all(len(set(task.paths)) == 2 for tasks in crowded.tasks.values() for task in tasks)


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"writers": 0, "tasks": 1, "files": 1, "conflict": 0.0}, "must be positive"),
        ({"writers": 1, "tasks": 1, "files": 1, "conflict": 1.5}, "within"),
        ({"writers": 1, "tasks": 1, "files": 1, "conflict": 0.0, "jitter_ms": -1}, "jitter"),
        ({"writers": 1, "tasks": 1, "files": 1, "conflict": 0.0, "paths_per_task": 2}, "exceed"),
    ],
)
def test_grid_refuses_impossible_values(fields: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        Grid(**fields)


def test_every_attempt_writes_its_own_token() -> None:
    task = Task(0, 3, (HOT,), ("0123456789ab",), 0)
    assert task.token(HOT) == "0123456789ab"
    assert len({task.token(HOT, attempt) for attempt in range(1, 6)}) == 5
    assert corpus_module.TOKEN_PATTERN.match(task.token_line(HOT, 4))
    with pytest.raises(ValueError, match="numbered from 1"):
        task.token(HOT, 0)
    line = "tok:w0:t3:0123456789ab\n"
    assert corpus_module.rewrite("garbage", task, HOT) == corpus_module.HEADER + line
    unterminated = corpus_module.HEADER + "tok:w1:t0:aaaaaaaaaaaa"
    assert corpus_module.rewrite(unterminated, task, HOT) == unterminated + "\n" + line


@pytest.mark.parametrize(
    ("content", "valid"),
    [
        (corpus_module.HEADER, True),
        (corpus_module.HEADER + "tok:w0:t1:0123456789ab\n", True),
        (corpus_module.HEADER + "tok:w0:t1:0123456789ab", True),
        ("wrong header\n", False),
        ("", False),
        (corpus_module.HEADER + "tok:w0:t1:0123456789AB\n", False),
        (corpus_module.HEADER + "stray line\n", False),
    ],
)
def test_shipped_referee_and_corpus_invariant_agree(
    tmp_path: Path, content: str, valid: bool
) -> None:
    tree = tmp_path / "tree"
    (tree / "corpus").mkdir(parents=True)
    (tree / "corpus" / "f000.txt").write_text(content, encoding="utf-8")
    script = tmp_path / "referee.py"
    script.write_text(systems.REFEREE_SOURCE, encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(script)], cwd=tree, capture_output=True, check=False
    )
    assert (completed.returncode == 0) is valid
    assert corpus_module.well_formed(content) is valid


def test_shipped_referee_refuses_a_tree_without_corpus(tmp_path: Path) -> None:
    script = tmp_path / "referee.py"
    script.write_text(systems.REFEREE_SOURCE, encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(script)], cwd=tmp_path, capture_output=True, check=False
    )
    assert completed.returncode == 1
    assert b"missing or empty" in completed.stderr


def test_event_logs_merge_by_time_and_refuse_malformed_lines(tmp_path: Path) -> None:
    first = events_module.EventWriter(tmp_path / "a.jsonl", "naive", 0)
    second = events_module.EventWriter(tmp_path / "b.jsonl", "naive", 1)
    first.emit("write", t=2.0, write_id="late")
    second.emit("write", t=1.0, write_id="early")
    first.emit("write", t=1.0, write_id="tie")
    merged = events_module.merge([tmp_path / "a.jsonl", tmp_path / "b.jsonl"], tmp_path / "m.jsonl")
    assert [event["write_id"] for event in merged] == ["tie", "early", "late"]
    assert events_module.read(tmp_path / "m.jsonl") == merged
    with pytest.raises(ValueError, match="unknown event kind"):
        first.emit("bogus")
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"kind": "write"}\n\n', encoding="utf-8")
    with pytest.raises(ValueError, match="blank line"):
        events_module.read(bad)
    bad.write_text('["not", "an", "event"]\n', encoding="utf-8")
    with pytest.raises(ValueError, match="not an event object"):
        events_module.read(bad)


# --- metric definitions against known-bad logs ----------------------------------------------


def test_lost_work_counts_only_writes_without_a_landing_or_a_receipt() -> None:
    write = _write(0, "w0", "p", "tok0", 1.0)
    assert metrics.lost_work([write])["lost"] == 1
    refused = _event("refusal", 0, 2.0, task=0, attempt=1, write_ids=["w0"], reason="x")
    assert metrics.lost_work([write, refused])["lost"] == 0
    landed = _landed(0, ["w0"], [_entry("p", "c", ["tok0"])], 2.0)
    assert metrics.lost_work([write, landed])["lost"] == 0
    final = _event("final", None, 3.0, tokens_by_path={"p": ["tok0"]})
    assert metrics.lost_work([write, final])["lost"] == 0
    errored = _event("error", 0, 2.0, task=0, attempt=1, write_ids=["w0"], detail="git add")
    assert metrics.lost_work([write, errored])["lost"] == 1
    elsewhere = _landed(0, ["w0"], [_entry("q", "c", ["tok0"])], 2.0)
    assert metrics.lost_work([write, elsewhere])["events"][0]["write_id"] == "w0"


def test_collision_needs_two_writers_with_overlapping_uncommitted_writes() -> None:
    first = _write(0, "a", "p", "t1", 1.0)
    second = _write(1, "b", "p", "t2", 2.0)
    first_done = _landed(0, ["a"], [], 3.0)
    second_done = _landed(1, ["b"], [], 4.0, commit="c2")
    assert metrics.collisions([first, second, first_done, second_done])["collisions"] == 1
    landed_early = _landed(0, ["a"], [], 1.5)
    assert metrics.collisions([first, landed_early, second, second_done])["collisions"] == 0
    same_writer = _write(0, "c", "p", "t3", 2.0)
    assert metrics.collisions([first, same_writer, first_done])["collisions"] == 0
    other_path = _write(1, "d", "q", "t4", 2.0)
    assert metrics.collisions([first, other_path, first_done])["collisions"] == 0
    window = _event("run", None, 0.0, started=0.0, ended=10.0)
    assert metrics.collisions([window, first, second])["pairs"] == [
        {"path": "p", "writes": ["a", "b"], "writers": [0, 1]}
    ]


def test_integrity_fails_foreign_and_missing_hunks_per_commit() -> None:
    write = _write(0, "w0", "p", "t", 1.0, content="mine")
    exact = _landed(0, ["w0"], [_entry("p", "mine", ["t"])], 2.0)
    assert metrics.integrity([write, exact])["per_commit"][0]["pass"] is True
    extra = _landed(0, ["w0"], [_entry("p", "mine", ["t"]), _entry("q", "theirs", [])], 2.0)
    result = metrics.integrity([write, extra])["per_commit"][0]
    assert (result["pass"], result["foreign"], result["missing"]) == (False, ["q"], [])
    overwritten = _landed(0, ["w0"], [_entry("p", "theirs", [])], 2.0)
    assert metrics.integrity([write, overwritten])["per_commit"][0]["foreign"] == ["p"]
    missing = _landed(0, ["w0"], [], 2.0)
    assert metrics.integrity([write, missing])["per_commit"][0]["missing"] == ["p"]


def test_throughput_counts_only_green_landings_in_the_window() -> None:
    window = _event("run", None, 0.0, started=0.0, ended=1800.0)
    landings = [
        _landed(0, [], [], 1.0, commit="own-green", green=True),
        _landed(0, [], [], 1.0, commit="own-red", green=False),
        _landed(0, [], [], 2.0, commit="refereed"),
        _landed(0, [], [], 3.0, commit="red"),
        _landed(0, [], [], 4.0, commit="unverified"),
    ]
    verdicts = [
        _event("referee", None, 5.0, commit="refereed", result="pass"),
        _event("referee", None, 5.0, commit="red", result="red"),
    ]
    result = metrics.throughput([window, *landings, *verdicts])
    assert (result["green"], result["red"], result["unverified"], result["landed"]) == (2, 2, 1, 5)
    assert result["green_per_hour"] == 4.0
    assert metrics.throughput(landings)["green_per_hour"] is None


def test_throughput_window_waits_for_the_last_harness_referee_verdict() -> None:
    window = _event("run", None, 0.0, started=0.0, ended=10.0)
    landings = [_landed(0, [], [], 1.0, commit="a"), _landed(0, [], [], 2.0, commit="b")]
    fast = [
        _event("referee", None, 10.5, commit="a", result="pass", duration_s=0.25),
        _event("referee", None, 11.0, commit="b", result="pass", duration_s=0.25),
    ]
    slow = [
        _event("referee", None, 30.0, commit="a", result="pass", duration_s=20.0),
        _event("referee", None, 50.0, commit="b", result="pass", duration_s=20.0),
    ]
    quick = metrics.throughput([window, *landings, *fast])
    late = metrics.throughput([window, *landings, *slow])
    assert (quick["writers_window_seconds"], quick["window_seconds"]) == (10.0, 11.0)
    assert (late["window_seconds"], late["referee_seconds"]) == (50.0, 40.0)
    assert late["window_end"] == "last harness referee verdict"
    assert late["green"] == quick["green"] == 2
    assert late["green_per_hour"] == 2 / (50.0 / 3600.0)
    assert quick["green_per_hour"] == 2 / (11.0 / 3600.0)
    own = metrics.throughput([window, _landed(0, [], [], 1.0, commit="a", green=True)])
    assert (own["window_seconds"], own["window_end"], own["referee_seconds"]) == (
        10.0,
        "writers' window",
        0.0,
    )


def test_refusals_are_grouped_by_reason_and_errors_stay_separate() -> None:
    events = [
        _event("refusal", 0, 1.0, reason="claim_conflict", write_ids=[]),
        _event("refusal", 1, 2.0, reason="claim_conflict", write_ids=[]),
        _event("refusal", 1, 3.0, reason="merge_conflict", write_ids=[]),
        _event("error", 0, 4.0, detail="git commit: nothing to commit", write_ids=[]),
    ]
    assert metrics.refusals(events) == {
        "refusals": 3,
        "by_reason": {"claim_conflict": 2, "merge_conflict": 1},
        "errors": 1,
        "error_details": {"git commit: nothing to commit": 1},
    }


def test_overhead_takes_setup_duration_and_the_larger_disk_sample() -> None:
    events = [
        _event("setup", None, 0.0, duration_s=1.5, disk_bytes=100),
        _event("final", None, 9.0, disk_bytes=40, tokens_by_path={}),
    ]
    assert metrics.overhead(events) == {
        "setup_seconds": 1.5,
        "peak_disk_bytes": 100,
        "disk_samples": [100, 40],
        "sampling": "setup end and run end only",
    }


# --- receipts against forgery ---------------------------------------------------------------


_MANIFEST_FIELDS = {"schema", "frozen_utc", "file_count", "manifest_sha256", "excluded_dirs"}


def _synthetic_run(
    root: Path,
    *,
    seed: int = 11,
    system: str = "naive",
    shift: float = 0.0,
    setup_seconds: float = 0.0,
    lose: bool = False,
    schema: str = SCHEMA_V2,
) -> Path:
    """A small run shaped exactly as ``runner.run`` writes one, with scripted events."""

    v3 = schema == SCHEMA
    grid = Grid(writers=2, tasks=1, files=2, conflict=1.0, jitter_ms=0)
    corpus = corpus_module.build(grid, seed) if v3 else legacy_corpus.build(grid, SeedKey(seed))
    root.mkdir()
    referee = root / "harness" / "referee_check.py"
    referee.parent.mkdir()
    referee.write_text(systems.REFEREE_SOURCE, encoding="utf-8")
    receipts.write_json(root / "corpus.json", corpus.payload())
    parts = root / "events" / "parts"
    harness = events_module.EventWriter(parts / f"{system}-harness.jsonl", system, None)
    harness.emit("setup", t=0.0, duration_s=setup_seconds, disk_bytes=0, disk_detail={}, detail={})
    held: dict[tuple[str, str], list[str]] = {}
    for writer, tasks in corpus.tasks.items():
        log = events_module.EventWriter(parts / f"{system}-w{writer}.jsonl", system, writer)
        task = tasks[0]
        path = task.paths[0]
        checkout = systems.checkout_label(system, writer)
        held.setdefault((checkout, path), []).append(task.token(path))
        content = corpus_module.rewrite(corpus_module.HEADER, task, path)
        write_id = f"w{writer}-t0-a1-p0"
        log.emit(
            "write",
            t=1.0 + writer + shift,
            task=0,
            attempt=1,
            write_id=write_id,
            path=path,
            token=task.token(path),
            content_sha256=_sha(content),
            **({"checkout": checkout, "index": systems.index_label(system, task, 1)} if v3 else {}),
        )
        if lose and writer == 1:
            log.emit(
                "error", t=5.0 + writer + shift, task=0, attempt=1, write_ids=[write_id], detail="x"
            )
            continue
        payload = [_entry(path, content, [task.token(path)])]
        if v3:
            log.emit(
                "commit",
                t=5.0 + writer + shift,
                task=0,
                attempt=1,
                write_ids=[write_id],
                commit=f"c{writer}",
                scope="target",
                payload=payload,
            )
        log.emit(
            "landed",
            t=5.0 + writer + shift,
            task=0,
            attempt=1,
            write_ids=[write_id],
            commit=f"c{writer}",
            green=None,
            payload=payload,
        )
    harness.emit("run", t=9.0 + shift, started=0.5, ended=9.0 + shift, complete=True, exitcodes=[0])
    for writer in (sorted(corpus.tasks) if system == "worktree" else [None]) if v3 else []:
        checkout = systems.checkout_label(system, writer)
        index = None if system == "onetree" else checkout
        harness.emit(
            "observed",
            t=9.5 + shift,
            checkout=checkout,
            index=index,
            after="run_end",
            paths={
                name: {"file": held.get((checkout, name), []), **({"index": []} if index else {})}
                for name in corpus.files
            },
        )
    for tasks in corpus.tasks.values() if v3 and system == "onetree" else []:
        harness.emit(
            "observed",
            t=9.5 + shift,
            checkout=None,
            index=systems.index_label(system, tasks[0], 1),
            after="run_end",
            paths={name: {"index": []} for name in corpus.files},
        )
    harness.emit(
        "final",
        t=10.0 + shift,
        tokens_by_path={},
        head="c1",
        disk_bytes=0,
        disk_detail={},
        **({"unrecorded_commits": []} if v3 else {}),
    )
    merged = events_module.merge(
        sorted(parts.glob(f"{system}-*.jsonl")), root / "events" / f"{system}.jsonl"
    )
    computed = {system: (metrics.compute if v3 else metrics.compute_v2)(merged)}
    receipts.write_json(root / "metrics.json", computed)
    inputs = {
        "schema": schema,
        "seed": seed,
        "grid": grid.payload(),
        "systems": [system],
        "seed_derivation": DERIVATION if v3 else LEGACY_DERIVATION,
        "clock": events_module.CLOCK,
        "gate": {
            "path": None,
            "sha256": None,
            "admitted_pin_sha256": "0" * 64,
            "matches_admitted_pin": False,
        },
        "referee_sha256": receipts.digest_file(referee),
        "not_a_result": receipts.NOT_A_RESULT,
    }
    receipts.write_json(root / "run.json", inputs)
    _freeze(root, receipts.freeze_fields(inputs, computed, True), schema)
    return root


def _freeze(run: Path, fields: dict[str, Any], schema: str) -> None:
    if schema == SCHEMA:
        receipts.freeze(run, fields)
        return
    text = receipts.manifest_text(receipts.manifest_entries(run, schema))
    (run / receipts.MANIFEST).write_text(text, encoding="utf-8")
    record = {
        "schema": schema,
        "frozen_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "file_count": text.count("\n"),
        "manifest_sha256": receipts.digest_text(text),
        "excluded_dirs": list(receipts.EXCLUDED_DIRS),
        **fields,
    }
    receipts.write_json(run / receipts.FREEZE, record)


def _edit_json(path: Path, change: Callable[[dict[str, Any]], object]) -> None:
    value = receipts.read_json(path)
    change(value)
    receipts.write_json(path, value)


def _refreeze(run: Path) -> None:
    """Re-freeze the files as they now are, keeping every claim FREEZE.json already makes."""

    record = receipts.read_json(run / receipts.FREEZE)
    _freeze(
        run,
        {key: value for key, value in record.items() if key not in _MANIFEST_FIELDS},
        record["schema"],
    )


def _rederive(run: Path, system: str = "naive") -> None:
    """A consistent forgery of every derived file: merged log, metrics, summary and manifest."""

    parts = sorted((run / "events" / "parts").glob(f"{system}-*.jsonl"))
    merged = events_module.merge(parts, run / "events" / f"{system}.jsonl")
    inputs = receipts.read_json(run / "run.json")
    compute = metrics.compute if inputs["schema"] == SCHEMA else metrics.compute_v2
    computed = {system: compute(merged)}
    receipts.write_json(run / "metrics.json", computed)
    complete = bool(receipts.read_json(run / receipts.FREEZE)["complete"])
    _freeze(run, receipts.freeze_fields(inputs, computed, complete), inputs["schema"])


def _rewrite_events(part: Path, change: Callable[[dict[str, Any]], object]) -> None:
    rows = events_module.read(part)
    for row in rows:
        change(row)
    part.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8"
    )


def _voids(run: Path) -> list[str]:
    return [str(void["what"]) for void in receipts.verify(run)["voids"]]


def test_verify_accepts_an_untouched_run_and_voids_an_edited_log(tmp_path: Path) -> None:
    run = _synthetic_run(tmp_path / "run")
    assert receipts.verify(run)["ok"] is True
    part = run / "events" / "parts" / "naive-w0.jsonl"
    original = part.read_text(encoding="utf-8")
    part.write_text(original.replace('"t": 1.0', '"t": 1.25'), encoding="utf-8")
    assert _voids(run) == ["manifest_sha256", "file", "events"]
    part.write_text(original, encoding="utf-8")
    assert receipts.verify(run)["ok"] is True


def test_a_refrozen_metric_forgery_is_still_void(tmp_path: Path) -> None:
    run = _synthetic_run(tmp_path / "run")
    _edit_json(run / "metrics.json", lambda m: m["naive"]["throughput"].__setitem__("green", 99))
    assert _voids(run) == ["manifest_sha256", "file", "metrics"]
    _refreeze(run)
    assert _voids(run) == ["metrics"]


@pytest.mark.parametrize("valid", [True, False])
@pytest.mark.parametrize("operation", ["verify", "compare"])
@pytest.mark.parametrize("scratch_shape", ["existing", "missing", "read_only", "file", "symlink"])
def test_verify_preserves_input_files_and_directory_mtimes(
    tmp_path: Path, valid: bool, operation: str, scratch_shape: str
) -> None:
    run = _synthetic_run(tmp_path / "run")
    scratch = run / "tmp"
    outside = tmp_path / "outside.txt"
    outside.write_text("outside scratch evidence\n", encoding="utf-8")
    if scratch_shape == "existing":
        scratch.mkdir()
        (scratch / "naive.recomputed.jsonl").write_text(
            "retained scratch evidence\n", encoding="utf-8"
        )
    elif scratch_shape == "file":
        scratch.write_text("retained scratch file\n", encoding="utf-8")
    elif scratch_shape == "symlink":
        scratch.mkdir()
        (scratch / "naive.recomputed.jsonl").symlink_to(outside)
    if not valid:
        _edit_json(run / "metrics.json", lambda m: m["naive"]["lost_work"].update(lost=999))

    def snapshot() -> dict[Path, tuple[int, int, bytes | None, str | None]]:
        entries = {}
        for path in (run, *run.rglob("*"), outside):
            info = path.lstat()
            entries[path] = (
                info.st_mode,
                info.st_mtime_ns,
                path.read_bytes() if path.is_file() else None,
                os.readlink(path) if path.is_symlink() else None,
            )
        return entries

    mode = stat.S_IMODE(run.stat().st_mode)
    try:
        if scratch_shape == "read_only":
            run.chmod(0o555)
            if os.access(run, os.W_OK):
                pytest.skip("requires enforced directory write permissions")
        before = snapshot()
        result = receipts.verify(run) if operation == "verify" else receipts.compare(run, run)
        assert result["ok"] is valid
        assert snapshot() == before
    finally:
        if scratch_shape == "read_only":
            run.chmod(mode)


def test_a_refrozen_corpus_forgery_is_still_void(tmp_path: Path) -> None:
    run = _synthetic_run(tmp_path / "run")
    _edit_json(run / "corpus.json", lambda c: c["tasks"]["0"][0].__setitem__("tokens", ["f" * 12]))
    _refreeze(run)
    assert _voids(run) == ["corpus"]


@pytest.mark.parametrize(
    ("metric", "forged", "recomputed"),
    [("lost_work.lost", 999999, 0), ("integrity.failed", False, 0)],
)
def test_verify_reconciles_the_freeze_summary_with_the_events(
    tmp_path: Path, metric: str, forged: object, recomputed: int
) -> None:
    run = _synthetic_run(tmp_path / "run")
    _edit_json(run / receipts.FREEZE, lambda r: r["summary"]["naive"].__setitem__(metric, forged))
    assert receipts.verify(run)["voids"] == [
        {
            "what": "summary",
            "system": "naive",
            "metric": metric,
            "recorded": forged,
            "recomputed": recomputed,
        }
    ]


@pytest.mark.parametrize(
    ("declared", "details"),
    [
        (
            [],
            {
                "systems must be a non-empty list",
                "evidence present for a system the run does not declare",
            },
        ),
        (["bogus"], {"unsupported systems: bogus"}),
        (["naive", "naive"], {"systems repeat"}),
        ("naive", {"systems must be a non-empty list"}),
    ],
)
def test_verify_refuses_an_empty_unsupported_or_repeated_scope(
    tmp_path: Path, declared: object, details: set[str]
) -> None:
    run = _synthetic_run(tmp_path / "run")
    _edit_json(run / receipts.FREEZE, lambda r: r.__setitem__("systems", declared))
    result = receipts.verify(run)
    scope_details = {void["detail"] for void in result["voids"] if void["what"] == "scope"}
    assert result["ok"] is False
    assert details <= scope_details
    assert {
        "what": "inputs",
        "field": "systems",
        "detail": "FREEZE.json and run.json disagree",
    } in (result["voids"])


def test_a_scope_forged_consistently_in_both_records_is_still_void(tmp_path: Path) -> None:
    run = _synthetic_run(tmp_path / "run")
    _edit_json(run / "run.json", lambda r: r.__setitem__("systems", []))
    _edit_json(run / receipts.FREEZE, lambda r: r.__setitem__("systems", []))
    _refreeze(run)
    result = receipts.verify(run)
    assert result["scope"] == []
    assert result["recomputed"] == {}
    assert [(void["what"], void.get("system")) for void in result["voids"]] == [
        ("scope", None),
        ("scope", "naive"),
        ("summary", "naive"),
    ]


@pytest.mark.parametrize(
    ("target", "field", "value", "refreeze", "expected"),
    [
        ("run.json", "seed", 12, True, [("inputs", "seed")]),
        ("FREEZE.json", "gate_sha256", "f" * 64, False, [("inputs", "gate_sha256")]),
        ("FREEZE.json", "referee_sha256", "0" * 64, False, [("inputs", "referee_sha256")]),
        ("run.json", "clock", "wall clock", True, [("inputs", "clock")]),
        (
            "FREEZE.json",
            "seed_derivation",
            "numpy SeedSequence",
            False,
            [("seed_derivation", None)],
        ),
        ("FREEZE.json", "complete", False, False, [("complete", None)]),
        ("FREEZE.json", "file_count", 999, False, [("file_count", None)]),
        ("FREEZE.json", "excluded_dirs", ["repos"], False, [("excluded_dirs", None)]),
        ("run.json", "not_a_result", "a result", True, [("label", None)]),
    ],
)
def test_verify_reconciles_every_checkable_claim(
    tmp_path: Path,
    target: str,
    field: str,
    value: object,
    refreeze: bool,
    expected: list[tuple[str, str | None]],
) -> None:
    run = _synthetic_run(tmp_path / "run")
    _edit_json(run / target, lambda r: r.__setitem__(field, value))
    if refreeze:
        _refreeze(run)
    voids = receipts.verify(run)["voids"]
    assert [(void["what"], void.get("field")) for void in voids] == expected


def test_verify_checks_the_gate_record_grid_label_referee_and_manifest_file(tmp_path: Path) -> None:
    gate = _synthetic_run(tmp_path / "gate")
    _edit_json(gate / "run.json", lambda r: r["gate"].__setitem__("matches_admitted_pin", True))
    _refreeze(gate)
    assert _voids(gate) == ["gate"]

    grid = _synthetic_run(tmp_path / "grid")
    for name in ("run.json", receipts.FREEZE):
        _edit_json(grid / name, lambda r: r["grid"].__setitem__("status", "amended by task 25"))
    _refreeze(grid)
    assert _voids(grid) == ["grid"]

    referee = _synthetic_run(tmp_path / "referee")
    script = referee / "harness" / "referee_check.py"
    script.write_text("raise SystemExit(0)\n", encoding="utf-8")
    digest = receipts.digest_file(script)
    for name in ("run.json", receipts.FREEZE):
        _edit_json(referee / name, lambda r: r.__setitem__("referee_sha256", digest))
    _refreeze(referee)
    assert receipts.verify(referee)["voids"] == [
        {
            "what": "referee",
            "detail": "harness/referee_check.py is not the referee this schema ships",
        }
    ]

    manifest = _synthetic_run(tmp_path / "manifest")
    text = (manifest / receipts.MANIFEST).read_text(encoding="utf-8")
    (manifest / receipts.MANIFEST).write_text("0" * 64 + text[64:], encoding="utf-8")
    assert _voids(manifest) == ["manifest_file", "file"]


def test_verify_checks_every_write_against_the_seeded_corpus(tmp_path: Path) -> None:
    token = _synthetic_run(tmp_path / "token")
    part = token / "events" / "parts" / "naive-w0.jsonl"

    def forge_token(row: dict[str, Any]) -> None:
        if row["kind"] == "write":
            row["token"] = "f" * 12
        for entry in row.get("payload", ()):
            entry["tokens"] = ["f" * 12]

    _rewrite_events(part, forge_token)
    _rederive(token)
    assert receipts.verify(token)["voids"] == [
        {
            "what": "events",
            "system": "naive",
            "detail": "writes the seeded corpus does not produce",
            "write_ids": ["w0-t0-a1-p0"],
            "count": 1,
        }
    ]

    system = _synthetic_run(tmp_path / "system")
    _rewrite_events(
        system / "events" / "parts" / "naive-w1.jsonl",
        lambda r: r.__setitem__("system", "worktree"),
    )
    _rederive(system)
    assert receipts.verify(system)["voids"] == [
        {
            "what": "events",
            "system": "naive",
            "detail": "events disagree with the system or writer their log names",
            "parts": ["naive-w1.jsonl"],
        }
    ]


def test_verify_names_added_missing_and_mislabelled_files(tmp_path: Path) -> None:
    run = _synthetic_run(tmp_path / "run")
    (run / "notes.txt").write_text("unlisted\n", encoding="utf-8")
    added = receipts.verify(run)["voids"]
    assert {"what": "file", "path": "notes.txt", "detail": "present but not in manifest"} in added
    (run / "notes.txt").unlink()
    (run / "events" / "naive.jsonl").unlink()
    assert _voids(run) == ["manifest_sha256", "file", "events"]
    _edit_json(run / receipts.FREEZE, lambda r: r.__setitem__("schema", "onetree-bench-v1"))
    assert _voids(run)[0] == "schema"
    (run / receipts.FREEZE).write_text("[]\n", encoding="utf-8")
    assert receipts.verify(run)["voids"] == [
        {"what": "unreadable", "path": receipts.FREEZE, "detail": "not a JSON object"}
    ]


def test_receipts_void_what_cannot_be_recomputed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="malformed line"):
        receipts.parse_manifest("not-a-digest  name\n")
    run = _synthetic_run(tmp_path / "run")
    _edit_json(run / receipts.FREEZE, lambda r: r["grid"].__setitem__("writers", 0))
    assert _voids(run) == ["inputs", "grid", "corpus"]
    run = _synthetic_run(tmp_path / "parts-gone")
    for part in (run / "events" / "parts").iterdir():
        part.unlink()
    _refreeze(run)
    assert _voids(run) == ["events"]


def test_compare_certifies_nothing_about_a_run_that_does_not_verify(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    valid = _synthetic_run(tmp_path / "valid")
    forged = _synthetic_run(tmp_path / "forged")
    _edit_json(
        forged / "metrics.json", lambda m: m["naive"]["lost_work"].__setitem__("lost", 999999)
    )
    itself = receipts.compare(forged, forged)
    assert (itself["ok"], itself["comparable"], itself["counts"]) == (False, None, {})
    assert itself["verified"] == {"first": False, "second": False}
    assert [(void["what"], void["run"]) for void in itself["voids"]] == [
        ("unverified", "first"),
        ("unverified", "second"),
    ]
    mixed = receipts.compare(valid, forged)
    assert mixed["verified"] == {"first": True, "second": False}
    assert [void["run"] for void in mixed["voids"]] == ["second"]
    assert cli.main(["compare", "--first", str(forged), "--second", str(forged)]) == 1
    assert capsys.readouterr().out.startswith("failed: ")


def test_compare_voids_differing_counts_but_never_timing(tmp_path: Path) -> None:
    first = _synthetic_run(tmp_path / "a")
    timing = _synthetic_run(tmp_path / "b", shift=0.5, setup_seconds=3.0)
    same = receipts.compare(first, timing)
    assert (same["ok"], same["comparable"], same["voids"]) == (True, True, [])
    assert "throughput.window_seconds" in same["not_compared"]
    lossy = _synthetic_run(tmp_path / "c", lose=True)
    result = receipts.compare(first, lossy)
    assert {void["metric"] for void in result["voids"]} == {
        "lost_work.lost",
        "refusal.errors",
        "integrity.commits",
        "throughput.landed",
    }
    other = receipts.compare(first, _synthetic_run(tmp_path / "d", seed=12))
    assert other["comparable"] is False
    assert other["voids"][0] == {
        "what": "inputs",
        "fields": ["seed"],
        "detail": "seed, grid or systems differ; runs are not reproductions",
    }


def test_compare_voids_a_system_missing_from_either_run(tmp_path: Path) -> None:
    naive = _synthetic_run(tmp_path / "naive")
    worktree = _synthetic_run(tmp_path / "worktree", system="worktree")
    result = receipts.compare(naive, worktree)
    assert result["verified"] == {"first": True, "second": True}
    assert [(void["what"], void.get("system")) for void in result["voids"]] == [
        ("inputs", None),
        ("system", "worktree"),
        ("system", "naive"),
    ]


# --- the real systems, scripted ------------------------------------------------------------


class Bench:
    """A run directory prepared the way ``runner.run`` prepares one, driven step by step."""

    def __init__(
        self, tmp_path: Path, *, writers: int, tasks: int = 1, files: int = 2, conflict: float = 1.0
    ) -> None:
        self.out = tmp_path / "run"
        (self.out / "harness").mkdir(parents=True)
        self.referee = self.out / "harness" / "referee_check.py"
        self.referee.write_text(systems.REFEREE_SOURCE, encoding="utf-8")
        self.grid = Grid(writers=writers, tasks=tasks, files=files, conflict=conflict, jitter_ms=0)
        self.corpus = corpus_module.build(self.grid, 5)
        self.settings = runner.Settings(
            seed=5,
            grid=self.grid,
            systems=SYSTEMS,
            gate=_gate(),
            message="bench landing\n",
            backoff_ms=1,
            deadline_s=60.0,
            ttl=120.0,
        )
        self.started = events_module.now()

    def prepare(self, system: str) -> tuple[systems.Arm, events_module.EventWriter]:
        return runner.prepare(self.out, system, self.corpus, self.settings, self.referee)

    def log(self, system: str, writer: int) -> events_module.EventWriter:
        part = self.out / "events" / "parts" / f"{system}-w{writer}.jsonl"
        return events_module.EventWriter(part, system, writer)

    def finish(
        self, arm: systems.Arm, harness: events_module.EventWriter, referee: Path | None = None
    ) -> dict[str, Any]:
        harness.emit(
            "run", started=self.started, ended=events_module.now(), complete=True, exitcodes=[0]
        )
        script = self.referee if referee is None else referee
        merged = runner.conclude(self.out, arm, self.corpus, harness, script, 60.0)
        assert receipts._outside_contract(merged) == []
        return metrics.compute(merged)

    def final_tokens(self, system: str) -> dict[str, list[str]]:
        merged = events_module.read(self.out / "events" / f"{system}.jsonl")
        return dict(next(e for e in merged if e["kind"] == "final")["tokens_by_path"])


@pytest.mark.parametrize("system", SYSTEMS)
def test_a_lone_writer_lands_every_task_cleanly(tmp_path: Path, system: str) -> None:
    bench = Bench(tmp_path, writers=1, tasks=2, conflict=0.0)
    arm, harness = bench.prepare(system)
    tasks = bench.corpus.tasks[0]
    summary = systems.run_writer(arm, tasks, bench.log(system, 0))
    result = bench.finish(arm, harness)
    assert summary == {"landed": 2, "abandoned": 0, "attempts": 2}
    assert result["lost_work"]["lost"] == 0
    assert result["collision"]["collisions"] == 0
    assert (result["refusal"]["refusals"], result["refusal"]["errors"]) == (0, 0)
    assert (result["integrity"]["commits"], result["integrity"]["failed"]) == (2, 0)
    assert (result["throughput"]["green"], result["throughput"]["unverified"]) == (2, 0)
    landed = {token for tokens in bench.final_tokens(system).values() for token in tokens}
    assert {task.token(task.paths[0]) for task in tasks} <= landed
    window = result["throughput"]
    if system == "onetree":
        assert (window["window_end"], window["referee_seconds"]) == ("writers' window", 0.0)
    else:
        assert window["window_end"] == "last harness referee verdict"
        assert (
            window["window_seconds"] >= window["writers_window_seconds"] + window["referee_seconds"]
        )


def test_a_slow_harness_referee_is_inside_the_baseline_throughput_window(tmp_path: Path) -> None:
    slow = tmp_path / "slow_referee.py"
    slow.write_text("import time\ntime.sleep(0.4)\n" + systems.REFEREE_SOURCE, encoding="utf-8")
    bench = Bench(tmp_path, writers=1, tasks=2, conflict=0.0)
    arm, harness = bench.prepare("worktree")
    assert systems.run_writer(arm, bench.corpus.tasks[0], bench.log("worktree", 0))["landed"] == 2
    result = bench.finish(arm, harness, referee=slow)["throughput"]
    assert result["green"] == 2
    assert result["referee_seconds"] >= 0.8
    assert result["window_end"] == "last harness referee verdict"
    assert result["window_seconds"] >= result["writers_window_seconds"] + result["referee_seconds"]
    assert result["green_per_hour"] == 2 / (result["window_seconds"] / 3600.0)


def test_naive_interleaving_loses_a_write_and_lands_foreign_bytes(tmp_path: Path) -> None:
    bench = Bench(tmp_path, writers=2)
    arm, harness = bench.prepare("naive")
    first, second = (
        Task(0, 0, (HOT,), ("aaaaaaaaaaaa",), 0),
        Task(1, 0, (HOT,), ("bbbbbbbbbbbb",), 0),
    )
    log_first, log_second = bench.log("naive", 0), bench.log("naive", 1)
    edits_first = systems.read_edits(arm.checkout, first, 1)
    edits_second = systems.read_edits(arm.checkout, second, 1)
    systems.write_edits(arm.checkout, edits_first, first, 1, log_first)
    systems.write_edits(arm.checkout, edits_second, second, 1, log_second)
    assert systems.naive_submit(arm, first, 1, edits_first, log_first) == systems.LANDED
    assert systems.naive_submit(arm, second, 1, edits_second, log_second) == systems.STOP
    result = bench.finish(arm, harness)
    assert [event["token"] for event in result["lost_work"]["events"]] == ["aaaaaaaaaaaa"]
    assert (result["integrity"]["commits"], result["integrity"]["failed"]) == (1, 1)
    assert result["integrity"]["per_commit"][0]["foreign"] == [HOT]
    assert result["collision"]["collisions"] == 1
    assert (result["refusal"]["refusals"], result["refusal"]["errors"]) == (0, 1)
    assert bench.final_tokens("naive")[HOT] == ["bbbbbbbbbbbb"]


def test_onetree_claims_turn_the_same_interleaving_into_a_receipted_wait(tmp_path: Path) -> None:
    bench = Bench(tmp_path, writers=2)
    arm, harness = bench.prepare("onetree")
    coordinator = Coordinator(arm.checkout, commit_payload=arm.gate)
    first, second = (
        Task(0, 0, (HOT,), ("aaaaaaaaaaaa",), 0),
        Task(1, 0, (HOT,), ("bbbbbbbbbbbb",), 0),
    )
    log_first, log_second = bench.log("onetree", 0), bench.log("onetree", 1)
    actor, session = systems.onetree_begin(arm, coordinator, first, 1, log_first)
    assert session is not None
    assert systems.onetree_begin(arm, coordinator, second, 1, log_second)[1] is None
    impatient = replace(arm, wait_s=0.05, poll_ms=10)
    assert systems.onetree_wait(impatient, coordinator, "bench-w1-watch", second) is False
    edits = systems.read_edits(arm.checkout, first, 1)
    systems.write_edits(arm.checkout, edits, first, 1, log_first)
    outcome = systems.onetree_submit(arm, coordinator, actor, session, first, 1, edits, log_first)
    assert outcome == systems.LANDED
    assert systems.onetree_wait(impatient, coordinator, "bench-w1-watch", second) is True
    assert (
        systems.onetree_attempt(arm, coordinator, "bench-w1-watch", second, 2, log_second)
        == systems.LANDED
    )
    result = bench.finish(arm, harness)
    assert result["lost_work"]["lost"] == 0
    assert result["collision"]["collisions"] == 0
    assert result["refusal"]["by_reason"] == {"claim_conflict": 1}
    assert (result["integrity"]["commits"], result["integrity"]["failed"]) == (2, 0)
    assert result["throughput"]["green"] == 2
    assert bench.final_tokens("onetree")[HOT] == ["aaaaaaaaaaaa", second.token(HOT, 2)]


def test_onetree_refuses_a_stale_base_and_the_bytes_are_restored(tmp_path: Path) -> None:
    bench = Bench(tmp_path, writers=2)
    arm, harness = bench.prepare("onetree")
    coordinator = Coordinator(arm.checkout, commit_payload=arm.gate)
    first, second = (
        Task(0, 0, (HOT,), ("aaaaaaaaaaaa",), 0),
        Task(1, 0, (HOT,), ("bbbbbbbbbbbb",), 0),
    )
    log_first, log_second = bench.log("onetree", 0), bench.log("onetree", 1)
    stale = coordinator.register_session(
        "bench-w1-t0-a1", pid=os.getpid(), ttl=arm.ttl, target=systems.TARGET
    )
    assert (
        systems.onetree_attempt(arm, coordinator, "bench-w0-watch", first, 1, log_first)
        == systems.LANDED
    )
    coordinator.claim("bench-w1-t0-a1", [str(arm.checkout / HOT)], ttl=arm.ttl)
    edits = systems.read_edits(arm.checkout, second, 1)
    systems.write_edits(arm.checkout, edits, second, 1, log_second)
    outcome = systems.onetree_submit(
        arm, coordinator, "bench-w1-t0-a1", stale, second, 1, edits, log_second
    )
    assert outcome == systems.RETRY
    assert (arm.checkout / HOT).read_text(encoding="utf-8") == edits[0].before
    result = bench.finish(arm, harness)
    assert result["refusal"]["by_reason"] == {"payload_conflict": 1}
    assert result["lost_work"]["lost"] == 0
    assert (result["integrity"]["commits"], result["integrity"]["failed"]) == (1, 0)


def test_a_claim_held_past_the_wait_budget_abandons_with_its_reason(tmp_path: Path) -> None:
    bench = Bench(tmp_path, writers=2)
    arm, _ = bench.prepare("onetree")
    holder = Coordinator(arm.checkout, commit_payload=arm.gate)
    holder.register_session("peer", pid=os.getpid(), ttl=arm.ttl, target=systems.TARGET)
    holder.claim("peer", [str(arm.checkout / HOT)], ttl=arm.ttl)
    log = bench.log("onetree", 1)
    task = Task(1, 0, (HOT,), ("bbbbbbbbbbbb",), 0)
    summary = systems.run_writer(replace(arm, wait_s=0.1, poll_ms=10), [task], log)
    assert summary == {"landed": 0, "abandoned": 1, "attempts": 1}
    assert [(event["kind"], event.get("reason")) for event in events_module.read(log.path)] == [
        ("abandon", "claim_wait_deadline")
    ]


def test_a_deleted_path_is_a_foreign_hunk(tmp_path: Path) -> None:
    bench = Bench(tmp_path, writers=1, conflict=0.0)
    arm, _ = bench.prepare("naive")
    systems.git(arm.checkout, "rm", "--quiet", "--", HOT)
    systems.git(arm.checkout, "commit", "--quiet", "-m", "delete a corpus file")
    head = systems.git_text(arm.checkout, "rev-parse", "HEAD")
    payload = systems.landed_payload(arm.checkout, head)
    assert payload == [{"path": HOT, "blob": None, "content_sha256": None, "tokens": []}]
    landing = _landed(0, [], payload, 1.0, commit=head)
    assert metrics.integrity([landing])["per_commit"][0]["foreign"] == [HOT]


def test_an_unadmitted_gate_never_lands_and_leaves_the_tree_as_it_was(tmp_path: Path) -> None:
    bench = Bench(tmp_path, writers=1, conflict=0.0)
    arm, _ = bench.prepare("onetree")
    impostor = tmp_path / "impostor.py"
    impostor.write_text("raise SystemExit(0)\n", encoding="utf-8")
    task = bench.corpus.tasks[0][0]
    before = (arm.checkout / task.paths[0]).read_text(encoding="utf-8")
    head = systems.git_text(arm.checkout, "rev-parse", systems.TARGET)
    log = bench.log("onetree", 0)
    summary = systems.run_writer(replace(arm, gate=impostor, max_attempts=1), [task], log)
    kinds = [event["kind"] for event in events_module.read(log.path)]
    assert summary == {"landed": 0, "abandoned": 1, "attempts": 1}
    assert "landed" not in kinds
    assert {"refusal", "error"} & set(kinds)
    assert (arm.checkout / task.paths[0]).read_text(encoding="utf-8") == before
    assert systems.git_text(arm.checkout, "rev-parse", systems.TARGET) == head


def test_onetree_operational_failures_are_errors_not_refusals(tmp_path: Path) -> None:
    bench = Bench(tmp_path, writers=1, conflict=0.0)
    arm, _ = bench.prepare("onetree")
    coordinator = Coordinator(arm.checkout, commit_payload=arm.gate)
    log = bench.log("onetree", 0)
    outside = Task(0, 1, ("../outside.txt",), ("ffffffffffff",), 0)
    assert systems.onetree_begin(arm, coordinator, outside, 1, log)[1] is None
    absent = Task(0, 2, ("corpus/absent.txt",), ("eeeeeeeeeeee",), 0)
    assert (
        systems.onetree_attempt(arm, coordinator, "bench-w0-watch", absent, 1, log) == systems.RETRY
    )
    assert [event["kind"] for event in events_module.read(log.path)] == ["error", "error"]


def test_worktree_conflict_is_refused_at_merge_and_leaves_the_target_clean(tmp_path: Path) -> None:
    bench = Bench(tmp_path, writers=2)
    arm, harness = bench.prepare("worktree")
    first, second = (
        Task(0, 0, (HOT,), ("aaaaaaaaaaaa",), 0),
        Task(1, 0, (HOT,), ("bbbbbbbbbbbb",), 0),
    )
    logs = {0: bench.log("worktree", 0), 1: bench.log("worktree", 1)}
    pending = {}
    for task in (first, second):
        checkout = arm.writer_checkout(task.writer)
        edits = systems.read_edits(checkout, task, 1)
        systems.write_edits(checkout, edits, task, 1, logs[task.writer])
        assert systems.worktree_commit(arm, task, 1, edits, logs[task.writer]) is True
        pending[task.writer] = edits
    assert systems.worktree_integrate(arm, first, 1, pending[0], logs[0]) == systems.LANDED
    assert systems.layer_disk(arm)["objects only on writer branches"] > 0
    assert systems.worktree_integrate(arm, second, 1, pending[1], logs[1]) == systems.RETRY
    assert systems.git(arm.checkout, "status", "--porcelain").stdout == b""
    result = bench.finish(arm, harness)
    assert result["collision"]["collisions"] == 0
    merged = events_module.read(bench.out / "events" / "worktree.jsonl")
    assert metrics.compute_v2(merged)["collision"]["collisions"] == 1
    assert result["refusal"]["by_reason"] == {"merge_conflict": 1}
    assert result["lost_work"]["lost"] == 0
    assert (result["integrity"]["commits"], result["integrity"]["failed"]) == (1, 0)
    assert result["throughput"]["green"] == 1


def test_git_failures_without_a_layer_are_errors(tmp_path: Path) -> None:
    bench = Bench(tmp_path, writers=1, conflict=0.0)
    worktree_arm, _ = bench.prepare("worktree")
    naive_arm, _ = bench.prepare("naive")
    unchanged = Task(0, 0, (HOT,), ("cccccccccccc",), 0)
    worktree_log = bench.log("worktree", 0)
    assert systems.worktree_commit(worktree_arm, unchanged, 1, [], worktree_log) is False
    ghost = Task(9, 0, (HOT,), ("dddddddddddd",), 0)
    assert systems.worktree_integrate(worktree_arm, ghost, 1, [], worktree_log) == systems.RETRY
    naive_log = bench.log("naive", 0)
    absent = Task(0, 0, ("corpus/absent.txt",), ("eeeeeeeeeeee",), 0)
    assert systems.naive_submit(naive_arm, absent, 1, [], naive_log) == systems.RETRY
    for log in (worktree_log, naive_log):
        assert {event["kind"] for event in events_module.read(log.path)} == {"error"}


def test_naive_waits_out_a_held_index_lock_and_gives_up_on_a_stuck_one(tmp_path: Path) -> None:
    bench = Bench(tmp_path, writers=1, tasks=2, conflict=0.0)
    arm, _ = bench.prepare("naive")
    log = bench.log("naive", 0)
    lock = arm.checkout / ".git" / "index.lock"
    first, second = bench.corpus.tasks[0]
    edits = systems.read_edits(arm.checkout, first, 1)
    systems.write_edits(arm.checkout, edits, first, 1, log)
    lock.write_bytes(b"")
    releaser = threading.Timer(0.2, lock.unlink)
    releaser.start()
    try:
        assert (
            systems.naive_submit(replace(arm, backoff_ms=5), first, 1, edits, log) == systems.LANDED
        )
    finally:
        releaser.join()
    edits = systems.read_edits(arm.checkout, second, 1)
    systems.write_edits(arm.checkout, edits, second, 1, log)
    lock.write_bytes(b"")
    try:
        assert (
            systems.naive_submit(replace(arm, backoff_ms=1), second, 1, edits, log) == systems.RETRY
        )
    finally:
        lock.unlink()
    failure = [event for event in events_module.read(log.path) if event["kind"] == "error"]
    assert len(failure) == 1
    assert "index.lock" in failure[0]["detail"]


def test_harness_referee_passes_good_trees_and_reddens_broken_ones(tmp_path: Path) -> None:
    bench = Bench(tmp_path, writers=1, conflict=0.0)
    arm, _ = bench.prepare("naive")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    good = systems.git_text(arm.checkout, "rev-parse", "HEAD")
    assert (
        systems.referee_commit(arm.checkout, good, bench.referee, scratch, 30.0)["result"] == "pass"
    )
    (arm.checkout / HOT).write_text(corpus_module.HEADER + "not a token line\n", encoding="utf-8")
    systems.git(arm.checkout, "commit", "--quiet", "-am", "break the corpus invariant")
    broken = systems.git_text(arm.checkout, "rev-parse", "HEAD")
    verdict = systems.referee_commit(arm.checkout, broken, bench.referee, scratch, 30.0)
    assert (verdict["result"], verdict["returncode"]) == ("red", 1)
    assert "token line" in verdict["detail"]
    sleeper = tmp_path / "sleeper.py"
    sleeper.write_text("import time\ntime.sleep(10)\n", encoding="utf-8")
    assert systems.referee_commit(arm.checkout, good, sleeper, scratch, 0.3)["result"] == "timeout"


def test_layer_disk_is_attributed_to_each_layer_only(tmp_path: Path) -> None:
    bench = Bench(tmp_path, writers=2)
    onetree_arm, _ = bench.prepare("onetree")
    worktree_arm, _ = bench.prepare("worktree")
    naive_arm, _ = bench.prepare("naive")
    assert systems.layer_disk(naive_arm) == {}
    worktree = systems.layer_disk(worktree_arm)
    assert worktree["wt"] > 0
    assert worktree[".git/worktrees"] > 0
    assert worktree["objects only on writer branches"] == 0
    onetree = systems.layer_disk(onetree_arm)
    assert onetree[".git/onetree"] > 0
    assert onetree["objects only on refs/onetree"] > 0


# --- the whole run, forked -----------------------------------------------------------------


def _run_arguments(out: Path, message: Path, *extra: str) -> list[str]:
    return [
        "run",
        "--out",
        str(out),
        "--commit-payload",
        str(_gate()),
        "--message-file",
        str(message),
        "--seed",
        "3",
        "--writers",
        "2",
        "--tasks",
        "2",
        "--files",
        "3",
        "--conflict",
        "0.5",
        "--jitter-ms",
        "5",
        "--deadline",
        "120",
        *extra,
    ]


def test_cli_runs_all_three_systems_and_its_receipts_verify(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    message = tmp_path / "message.txt"
    message.write_text("bench end-to-end landing\n", encoding="utf-8")
    out = tmp_path / "run"
    code = cli.main(_run_arguments(out, message, "--json"))
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    report = json.loads(lines[0])
    assert (code, report["ok"], report["complete"]) == (0, True, True)
    assert sorted(report["summary"]) == sorted(SYSTEMS)
    onetree = report["summary"]["onetree"]
    assert (onetree["lost_work.lost"], onetree["integrity.failed"]) == (0, 0)
    assert (onetree["throughput.landed"], onetree["throughput.green"]) == (4, 4)
    worktree = report["summary"]["worktree"]
    assert (worktree["lost_work.lost"], worktree["integrity.failed"]) == (0, 0)
    inputs = receipts.read_json(out / "run.json")
    setup = [
        e for e in events_module.read(out / "events" / "onetree.jsonl") if e["kind"] == "setup"
    ]
    observed = setup[0]["detail"]["gate_admission"]["sha256"]
    assert inputs["gate"]["admitted_pin_sha256"] == observed == _sha256_path(_gate())
    assert inputs["gate"]["matches_admitted_pin"] is True
    assert inputs["grid"]["status"] == "PLACEHOLDER: not amended (task 25)"
    assert cli.main(["verify", "--run", str(out), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["voids"] == []
    assert cli.main(["compare", "--first", str(out), "--second", str(out)]) == 0
    assert capsys.readouterr().out == f"ok: {out}\n"

    # run.json claiming an admission other than the one setup observed is void, even when the
    # record is self-consistent and freshly frozen.
    forged = tmp_path / "forged"
    shutil.copytree(out, forged, symlinks=True)
    _edit_json(
        forged / "run.json",
        lambda r: r["gate"].update(admitted_pin_sha256="f" * 64, matches_admitted_pin=False),
    )
    _refreeze(forged)
    voids = receipts.verify(forged)["voids"]
    assert voids == [
        {
            "what": "gate",
            "detail": "the admission the onetree setup observed differs from run.json",
            "observed": observed,
            "recorded": "f" * 64,
        }
    ]
    part = out / "events" / "parts" / "onetree-w0.jsonl"
    part.write_text(part.read_text(encoding="utf-8") * 2, encoding="utf-8")
    assert cli.main(["verify", "--run", str(out)]) == 1
    assert capsys.readouterr().out.startswith("failed: ")
    assert cli.main(["compare", "--first", str(out), "--second", str(out)]) == 1
    assert capsys.readouterr().out.startswith("failed: ")


def _sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_the_recorded_admission_is_read_back_and_can_disagree(
    tmp_path: Path, git_repo: GitFixture
) -> None:
    gate = _gate()
    out = tmp_path / "out"
    out.mkdir()
    arm = SimpleNamespace(checkout=git_repo.root)
    inputs: dict[str, Any] = {
        "gate": {
            "path": str(gate),
            "sha256": "e" * 64,
            "admitted_pin_sha256": None,
            "matches_admitted_pin": False,
        }
    }
    runner._observe_admission(out, arm, inputs)  # type: ignore[arg-type]
    assert inputs["gate"]["admitted_pin_sha256"] == _sha256_path(gate)
    assert inputs["gate"]["matches_admitted_pin"] is False
    assert receipts.read_json(out / "run.json") == inputs

    inputs["gate"]["sha256"] = _sha256_path(gate)
    runner._observe_admission(out, arm, inputs)  # type: ignore[arg-type]
    assert inputs["gate"]["matches_admitted_pin"] is True

    for key in ("onetree.payloadGate", "onetree.payloadGateSha256"):
        git_repo.git("config", "--local", "--unset-all", key)
    runner._observe_admission(out, arm, inputs)  # type: ignore[arg-type]
    assert inputs["gate"]["admitted_pin_sha256"] is None
    assert inputs["gate"]["matches_admitted_pin"] is False


def test_cli_refuses_bad_invocations_without_touching_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    message = tmp_path / "message.txt"
    message.write_text("m\n", encoding="utf-8")
    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "keep.txt").write_text("keep\n", encoding="utf-8")
    assert cli.main(_run_arguments(existing, message, "--json")) == 1
    assert json.loads(capsys.readouterr().out)["error"]["type"] == "FileExistsError"
    assert [path.name for path in existing.iterdir()] == ["keep.txt"]
    fresh = tmp_path / "fresh"
    assert cli.main(_run_arguments(fresh, message, "--systems", "onetree,bogus", "--json")) == 1
    assert "unknown systems" in json.loads(capsys.readouterr().out)["error"]["message"]
    assert not fresh.exists()
    assert cli.main(["run", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["type"] == "ValueError"
    assert cli.main(["verify", "--run", str(tmp_path / "missing")]) == 1
    assert capsys.readouterr().out.startswith("failed: ")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"systems": ()}, "non-empty"),
        ({"systems": ("naive", "naive")}, "without repeats"),
        ({"gate": Path("relative.py")}, "absolute gate"),
        ({"message": "  "}, "must not be empty"),
        ({"max_attempts": 0}, "must be positive"),
        ({"deadline_s": 0.0}, "deadline_s must be positive"),
        ({"ttl": 10.0}, "outlive"),
    ],
)
def test_settings_refuse_unusable_configurations(change: dict[str, Any], message: str) -> None:
    settings = runner.Settings(
        seed=1,
        grid=Grid(writers=1, tasks=1, files=1, conflict=0.0),
        systems=SYSTEMS,
        gate=_gate(),
        message="m",
        deadline_s=60.0,
        ttl=120.0,
    )
    with pytest.raises(ValueError, match=message):
        replace(settings, **change).validate()


def _naive_settings(deadline_s: float) -> runner.Settings:
    return runner.Settings(
        seed=1,
        grid=Grid(writers=1, tasks=1, files=1, conflict=0.0, jitter_ms=0),
        systems=("naive",),
        gate=_gate(),
        message="m\n",
        deadline_s=deadline_s,
        ttl=deadline_s + 60,
    )


def test_a_dead_writer_makes_the_run_incomplete_and_the_record_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def dies(arm: systems.Arm, tasks: Any, events: events_module.EventWriter) -> dict[str, int]:
        raise RuntimeError("writer died")

    monkeypatch.setattr(systems, "run_writer", dies)
    report = runner.run(tmp_path / "run", _naive_settings(30.0))
    assert (report["ok"], report["complete"]) == (False, False)
    assert receipts.read_json(tmp_path / "run" / receipts.FREEZE)["complete"] is False
    merged = events_module.read(tmp_path / "run" / "events" / "naive.jsonl")
    assert next(e for e in merged if e["kind"] == "run")["exitcodes"] == [1]
    assert receipts.verify(tmp_path / "run")["ok"] is True


def test_a_writer_past_the_deadline_is_stopped_and_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def stalls(arm: systems.Arm, tasks: Any, events: events_module.EventWriter) -> dict[str, int]:
        time.sleep(30)
        return {}

    monkeypatch.setattr(systems, "run_writer", stalls)
    started = time.monotonic()
    report = runner.run(tmp_path / "run", _naive_settings(0.5))
    assert time.monotonic() - started < 20
    assert report["complete"] is False
    merged = events_module.read(tmp_path / "run" / "events" / "naive.jsonl")
    run_event = next(e for e in merged if e["kind"] == "run")
    assert run_event["complete"] is False
    assert run_event["exitcodes"] != [0]
