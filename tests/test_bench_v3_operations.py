"""Schema v3 commit inclusion and byte removal as the harness's actual operations record them
(decisions b62102092057 and d680c759ca85)."""

from __future__ import annotations

import os
import shlex
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from conftest import _CLEAN_PAYLOAD_GATE_SOURCE, admit_gate
from test_bench import HOT, Bench

from onetree import git as git_module
from onetree.bench import events as events_module
from onetree.bench import metrics, runner, systems
from onetree.bench.corpus import Task
from onetree.core import Coordinator

ROOT = Path(__file__).resolve().parents[1]
FIRST = Task(0, 0, (HOT,), ("aaaaaaaaaaaa",), 0)
SECOND = Task(1, 0, (HOT,), ("bbbbbbbbbbbb",), 0)
THIRD = Task(2, 0, (HOT,), ("cccccccccccc",), 0)


def _scored(bench: Bench, arm: systems.Arm, harness: events_module.EventWriter) -> Any:
    for module in (metrics, runner, systems, events_module, git_module):
        assert Path(str(module.__file__)).resolve().is_relative_to(ROOT / "src"), module
    bench.finish(arm, harness)
    merged = events_module.read(bench.out / "events" / f"{arm.system}.jsonl")
    return metrics.collisions_v3(merged), metrics.collisions(merged), merged


def _pair(first: systems.Edit, second: systems.Edit, writers: list[int]) -> dict[str, Any]:
    return {"path": HOT, "writes": sorted([first.write_id, second.write_id]), "writers": writers}


def _write(
    arm: systems.Arm, task: Task, log: events_module.EventWriter, attempt: int = 1
) -> list[systems.Edit]:
    edits = systems.read_edits(arm.writer_checkout(task.writer), task, attempt)
    systems.write_edits(arm.writer_checkout(task.writer), edits, task, attempt, log)
    return edits


def _tokens(arm: systems.Arm, revision: str) -> list[str]:
    shown = systems.git(arm.checkout, "show", revision, check=False)
    return systems.corpus_module.tokens_in(shown.stdout.decode("utf-8"))


def _fail_naive_add(
    arm: systems.Arm, task: Task, log: events_module.EventWriter
) -> list[systems.Edit]:
    edits = _write(arm, task, log)
    lock = arm.checkout / ".git" / "index.lock"
    lock.write_bytes(b"fixture lock\n")
    try:
        assert systems.naive_submit(arm, task, 1, edits, log) == systems.RETRY
    finally:
        lock.unlink()
    return edits


def _commits_holding(checkout: Path, token: str) -> list[str]:
    listed = systems.git_text(
        checkout, "cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype)"
    )
    commits = [line.split()[0] for line in listed.splitlines() if line.endswith(" commit")]
    return sorted(
        commit
        for commit in commits
        if token in systems.git(checkout, "show", f"{commit}:{HOT}", check=False).stdout.decode()
    )


def _reachable(checkout: Path) -> set[str]:
    return set(systems.git_text(checkout, "rev-list", "--all", "--reflog").split())


def _kinds(log: events_module.EventWriter) -> list[str]:
    return [event["kind"] for event in events_module.read(log.path)]


def _found(events: list[dict[str, Any]]) -> list[str]:
    return sorted(
        event["commit"]
        for event in events
        if event["kind"] == "commit" and event["scope"] == "found"
    )


def _lock_ref(checkout: Path, ref: str) -> Path:
    lock = Path(systems.git_text(checkout, "rev-parse", "--git-path", f"{ref}.lock"))
    lock = lock if lock.is_absolute() else checkout / lock
    lock.write_bytes(b"fixture lock\n")
    return lock


def test_staged_tokens_read_the_same_index_git_show_reads(tmp_path: Path) -> None:
    bench = Bench(tmp_path, writers=2, files=3)
    arm, _ = bench.prepare("naive")
    edits = _write(arm, FIRST, bench.log("naive", 0))
    systems.git(arm.checkout, "add", "--", HOT)
    paths = [*bench.corpus.files, "corpus/absent.txt"]
    shown = {
        path: (_tokens(arm, f":{path}") if path != "corpus/absent.txt" else None) for path in paths
    }
    assert shown[HOT] == [edits[0].token]
    assert systems.staged_tokens(arm.checkout, paths) == shown
    private = tmp_path / "private-index"
    assert systems.staged_tokens(arm.checkout, paths, private) == dict.fromkeys(paths)
    environment = {**systems.git_environment(), "GIT_INDEX_FILE": str(private)}
    systems.subprocess.run(  # noqa: S603 - argv-only git
        [systems.GIT, "-C", str(arm.checkout), "read-tree", "HEAD"], env=environment, check=True
    )
    assert systems.staged_tokens(arm.checkout, paths, private) == {
        **dict.fromkeys(paths, []),
        "corpus/absent.txt": None,
    }


def test_staged_tokens_refuse_an_index_they_cannot_read(tmp_path: Path) -> None:
    bench = Bench(tmp_path, writers=1)
    arm, _ = bench.prepare("naive")
    corrupt = tmp_path / "corrupt-index"
    corrupt.write_bytes(b"garbage index bytes\n")
    with pytest.raises(systems.HarnessError, match="ls-files --stage exited 128"):
        systems.staged_tokens(arm.checkout, [HOT], corrupt)
    blob = systems.git_text(arm.checkout, "rev-parse", f":{HOT}")
    missing, unmerged = tmp_path / "missing-blob-index", tmp_path / "unmerged-index"
    absent = "0" * (len(blob) - 1) + "1"
    systems.subprocess.run(  # noqa: S603 - argv-only git
        [
            systems.GIT,
            "-C",
            str(arm.checkout),
            "update-index",
            "--add",
            "--cacheinfo",
            f"100644,{absent},{HOT}",
        ],
        env={**systems.git_environment(), "GIT_INDEX_FILE": str(missing)},
        check=True,
    )
    systems.subprocess.run(  # noqa: S603 - argv-only git
        [systems.GIT, "-C", str(arm.checkout), "update-index", "--index-info"],
        input=f"100644 {blob} 2\t{HOT}\n100644 {blob} 3\t{HOT}\n".encode(),
        env={**systems.git_environment(), "GIT_INDEX_FILE": str(unmerged)},
        check=True,
    )
    with pytest.raises(systems.HarnessError, match="did not return blob"):
        systems.staged_tokens(arm.checkout, [HOT], missing)
    with pytest.raises(systems.HarnessError, match="is unmerged in the index"):
        systems.staged_tokens(arm.checkout, [HOT], unmerged)
    assert systems.staged_tokens(arm.checkout, [HOT]) == {HOT: []}


def test_a_read_back_marks_what_it_cannot_read(tmp_path: Path) -> None:
    bench = Bench(tmp_path, writers=1, files=6)
    arm, _ = bench.prepare("naive")
    token = _write(arm, FIRST, bench.log("naive", 0))[0].token
    files = list(bench.corpus.files)
    torn = f"{systems.corpus_module.HEADER}tok:w0:t0:{token}\ntok:w1:t0:bbb"
    (arm.checkout / files[1]).unlink()
    (arm.checkout / files[2]).write_bytes(b"\xff\xfe\n")
    (arm.checkout / files[3]).write_text("", encoding="utf-8")
    (arm.checkout / files[4]).write_text(torn, encoding="utf-8")
    (arm.checkout / files[5]).unlink()
    (arm.checkout / files[5]).mkdir()
    assert files[0] == HOT
    assert systems.read_back(arm.checkout, files, index=True) == {
        files[0]: {"file": [token], "index": []},
        files[1]: {"file": None, "index": []},
        files[2]: {"file": [], "unreadable": True, "index": []},
        files[3]: {"file": [], "unreadable": True, "index": []},
        files[4]: {"file": [token], "unreadable": True, "index": []},
        files[5]: {"file": [], "unreadable": True, "index": []},
    }


@pytest.mark.parametrize(
    ("seen", "collisions", "causes"), [("", 1, []), (systems.corpus_module.HEADER, 0, [])]
)
def test_a_torn_read_back_ends_nothing_where_a_complete_one_sees_the_bytes_gone(
    tmp_path: Path, seen: str, collisions: int | None, causes: list[str]
) -> None:
    bench = Bench(tmp_path, writers=2)
    arm, harness = bench.prepare("naive")
    log = bench.log("naive", 0)
    _write(arm, FIRST, log)
    hot = arm.checkout / HOT
    written = hot.read_text(encoding="utf-8")
    hot.write_text(seen, encoding="utf-8")
    systems.observe(arm.checkout, [HOT], log, "write", FIRST, 1)
    hot.write_text(written, encoding="utf-8")
    _write(arm, SECOND, bench.log("naive", 1))
    v3, _, merged = _scored(bench, arm, harness)
    readings = [event for event in merged if event["kind"] == "observed" and event["writer"] == 0]
    assert readings[-1]["paths"][HOT].get("unreadable", False) is (seen == "")
    assert (v3["collisions"], [item["cause"] for item in v3["undetermined"]]) == (
        collisions,
        causes,
    )


# --- the task 200 lifecycle sequences ------------------------------------------------------


def test_an_errored_naive_write_stays_uncommitted_under_both_definitions(tmp_path: Path) -> None:
    bench = Bench(tmp_path, writers=2)
    arm, harness = bench.prepare("naive")
    first = _fail_naive_add(arm, FIRST, bench.log("naive", 0))
    second = _write(arm, SECOND, bench.log("naive", 1))
    v3, v2, _ = _scored(bench, arm, harness)
    pairs = [_pair(first[0], second[0], [0, 1])]
    assert v3 == {"collisions": 1, "pairs": pairs, "undetermined": []}
    assert v2["collisions"] == 0


def test_a_peer_commit_that_includes_an_errored_write_ends_it(tmp_path: Path) -> None:
    bench = Bench(tmp_path, writers=3)
    arm, harness = bench.prepare("naive")
    first = _fail_naive_add(arm, FIRST, bench.log("naive", 0))
    log = bench.log("naive", 1)
    second = _write(arm, SECOND, log)
    assert systems.naive_submit(arm, SECOND, 1, second, log) == systems.LANDED
    assert {first[0].token, second[0].token} <= set(_tokens(arm, f"HEAD:{HOT}"))
    _write(arm, THIRD, bench.log("naive", 2))
    v3, v2, merged = _scored(bench, arm, harness)
    pairs = [_pair(first[0], second[0], [0, 1])]
    assert v3 == {"collisions": 1, "pairs": pairs, "undetermined": []}
    assert v2["pairs"] == []
    commits = [event for event in merged if event["kind"] == "commit"]
    assert [(event["writer"], event["scope"]) for event in commits] == [(1, "target")]


def test_a_retry_keeps_the_earlier_tokens_own_interval(tmp_path: Path) -> None:
    bench = Bench(tmp_path, writers=2)
    arm, harness = bench.prepare("naive")
    log = bench.log("naive", 0)
    failed = _fail_naive_add(arm, FIRST, log)
    retried = _write(arm, FIRST, log, attempt=2)
    peer = _write(arm, SECOND, bench.log("naive", 1))
    assert failed[0].token in (arm.checkout / HOT).read_text(encoding="utf-8")
    v3, v2, _ = _scored(bench, arm, harness)
    assert v3["pairs"] == [
        _pair(failed[0], peer[0], [0, 1]),
        _pair(retried[0], peer[0], [0, 1]),
    ]
    assert (v3["collisions"], v3["undetermined"], v2["collisions"]) == (2, [], 1)


def test_an_onetree_restore_ends_the_write_before_the_peer_writes(tmp_path: Path) -> None:
    bench = Bench(tmp_path, writers=2)
    arm, harness = bench.prepare("onetree")
    coordinator = Coordinator(arm.checkout, commit_payload=arm.gate)
    log = bench.log("onetree", 0)
    actor, session = systems.onetree_begin(arm, coordinator, FIRST, 1, log)
    assert session is not None
    first = _write(arm, FIRST, log)
    lock = Path(str(session.index_path) + ".lock")
    lock.write_bytes(b"fixture lock\n")
    try:
        outcome = systems.onetree_submit(arm, coordinator, actor, session, FIRST, 1, first, log)
    finally:
        lock.unlink()
    assert outcome == systems.RETRY
    assert _kinds(log) == ["write", "observed", "observed", "error"]
    restored = events_module.read(log.path)[2]
    assert (restored["after"], restored["index"]) == ("restore", actor)
    assert restored["paths"][HOT] == {"file": [], "index": []}
    peer_log = bench.log("onetree", 1)
    assert systems.onetree_begin(arm, coordinator, SECOND, 1, peer_log)[1] is not None
    _write(arm, SECOND, peer_log)
    v3, v2, _ = _scored(bench, arm, harness)
    assert (v3["collisions"], v3["undetermined"], v2["collisions"]) == (0, [], 0)


def test_a_worktree_reset_ends_the_write_a_failed_local_commit_left(tmp_path: Path) -> None:
    bench = Bench(tmp_path, writers=2, files=2)
    arm, harness = bench.prepare("worktree")
    log = bench.log("worktree", 0)
    checkout = arm.writer_checkout(0)
    first = _write(arm, FIRST, log)
    lock = Path(systems.git_text(checkout, "rev-parse", "--git-path", "refs/heads/w0.lock"))
    lock = lock if lock.is_absolute() else checkout / lock
    lock.write_bytes(b"fixture lock\n")
    try:
        assert systems.worktree_commit(arm, FIRST, 1, first, log) is False
    finally:
        lock.unlink()
    unreferenced = sorted(set(_commits_holding(checkout, first[0].token)) - _reachable(checkout))
    later = Task(0, 1, ("corpus/f001.txt",), ("dddddddddddd",), 0)
    assert systems.worktree_attempt(arm, later, 1, log) == systems.LANDED
    reset = next(event for event in events_module.read(log.path) if event.get("after") == "reset")
    assert reset["paths"][HOT] == {"file": [], "index": []}
    _write(arm, SECOND, bench.log("worktree", 1))
    v3, v2, merged = _scored(bench, arm, harness)
    assert (v3["collisions"], v3["undetermined"], v2["collisions"]) == (0, [], 0)
    assert _found(merged) == unreferenced
    assert len(unreferenced) == 1


def test_a_commit_a_failed_worktree_commit_leaves_ends_the_write(tmp_path: Path) -> None:
    bench = Bench(tmp_path, writers=2, files=2)
    arm, harness = bench.prepare("worktree")
    log = bench.log("worktree", 0)
    checkout = arm.writer_checkout(0)
    first = _write(arm, FIRST, log)
    lock = Path(systems.git_text(checkout, "rev-parse", "--git-path", "refs/heads/w0.lock"))
    lock = lock if lock.is_absolute() else checkout / lock
    lock.write_bytes(b"fixture lock\n")
    try:
        assert systems.worktree_commit(arm, FIRST, 1, first, log) is False
    finally:
        lock.unlink()
    assert _kinds(log) == ["write", "observed", "commit", "error"]
    unreferenced = sorted(set(_commits_holding(checkout, first[0].token)) - _reachable(checkout))
    assert len(unreferenced) == 1
    peer = _write(arm, SECOND, bench.log("worktree", 1))
    later = Task(0, 1, ("corpus/f001.txt",), ("dddddddddddd",), 0)
    assert systems.worktree_attempt(arm, later, 1, log) == systems.LANDED
    v3, v2, merged = _scored(bench, arm, harness)
    found = [event for event in merged if event["kind"] == "commit" and event["scope"] == "found"]
    assert [event["commit"] for event in found] == unreferenced
    assert found[0]["payload"][0]["tokens"] == [first[0].token]
    assert found[0]["t"] < next(e["t"] for e in merged if e.get("write_id") == peer[0].write_id)
    assert v3 == {"collisions": 0, "pairs": [], "undetermined": []}
    assert v2["collisions"] == 0
    final = next(event for event in merged if event["kind"] == "final")
    assert final["unrecorded_commits"] == []


def test_a_local_commit_ends_the_write_before_the_peer_writes(tmp_path: Path) -> None:
    bench = Bench(tmp_path, writers=2)
    arm, harness = bench.prepare("worktree")
    log = bench.log("worktree", 0)
    first = _write(arm, FIRST, log)
    assert systems.worktree_commit(arm, FIRST, 1, first, log) is True
    local = events_module.read(log.path)[-1]
    assert (local["kind"], local["scope"]) == ("commit", "local")
    assert local["commit"] == systems.git_text(arm.writer_checkout(0), "rev-parse", "HEAD")
    assert first[0].token not in _tokens(arm, f"HEAD:{HOT}")
    _write(arm, SECOND, bench.log("worktree", 1))
    assert systems.worktree_integrate(arm, FIRST, 1, first, log) == systems.LANDED
    v3, v2, _ = _scored(bench, arm, harness)
    assert (v3["collisions"], v3["undetermined"], v2["collisions"]) == (0, [], 1)


# --- bytes left only in an index, and bytes that come back --------------------------------


def test_naive_bytes_left_only_in_the_index_stay_present_and_count(tmp_path: Path) -> None:
    bench = Bench(tmp_path, writers=3)
    arm, harness = bench.prepare("naive")
    logs = [bench.log("naive", writer) for writer in range(3)]
    stale_second = systems.read_edits(arm.checkout, SECOND, 1)
    stale_third = systems.read_edits(arm.checkout, THIRD, 1)
    first = _write(arm, FIRST, logs[0])
    hook = arm.checkout / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    try:
        assert systems.naive_submit(arm, FIRST, 1, first, logs[0]) == systems.RETRY
    finally:
        hook.unlink()
    assert first[0].token in _tokens(arm, f":{HOT}")
    systems.write_edits(arm.checkout, stale_second, SECOND, 1, logs[1])
    assert first[0].token not in (arm.checkout / HOT).read_text(encoding="utf-8")
    assert first[0].token in _tokens(arm, f":{HOT}")
    systems.write_edits(arm.checkout, stale_third, THIRD, 1, logs[2])
    assert systems.naive_submit(arm, SECOND, 1, stale_second, logs[1]) == systems.LANDED
    assert first[0].token not in _tokens(arm, f":{HOT}") + _tokens(arm, f"HEAD:{HOT}")
    v3, v2, _ = _scored(bench, arm, harness)
    assert v3 == {
        "collisions": 3,
        "pairs": [
            _pair(first[0], stale_second[0], [0, 1]),
            _pair(first[0], stale_third[0], [0, 2]),
            _pair(stale_second[0], stale_third[0], [1, 2]),
        ],
        "undetermined": [],
    }
    assert v2["collisions"] == 1


def test_a_refused_onetree_write_left_in_its_session_index_still_counts(
    tmp_path: Path,
) -> None:
    bench = Bench(tmp_path, writers=3)
    arm, harness = bench.prepare("onetree")
    coordinator = Coordinator(arm.checkout, commit_payload=arm.gate)
    logs = [bench.log("onetree", writer) for writer in range(3)]
    stale = coordinator.register_session(
        systems.onetree_actor(SECOND, 1), pid=os.getpid(), ttl=arm.ttl, target=systems.TARGET
    )
    assert systems.onetree_attempt(arm, coordinator, "bench-w0-watch", FIRST, 1, logs[0]) == (
        systems.LANDED
    )
    coordinator.claim(stale.actor, [str(arm.checkout / HOT)], ttl=arm.ttl)
    second = _write(arm, SECOND, logs[1])
    outcome = systems.onetree_submit(
        arm, coordinator, stale.actor, stale, SECOND, 1, second, logs[1]
    )
    assert outcome == systems.RETRY
    refused = events_module.read(logs[1].path)
    assert [event.get("reason") for event in refused if event["kind"] == "refusal"] == [
        "payload_conflict"
    ]
    assert refused[2]["paths"][HOT] == {
        "file": [FIRST.token(HOT)],
        "index": [FIRST.token(HOT), second[0].token],
    }
    assert _commits_holding(arm.checkout, second[0].token) == []
    assert systems.onetree_attempt(arm, coordinator, "bench-w2-watch", THIRD, 1, logs[2]) == (
        systems.LANDED
    )
    v3, v2, merged = _scored(bench, arm, harness)
    third = next(event for event in merged if event["kind"] == "write" and event["writer"] == 2)
    assert v3 == {
        "collisions": 1,
        "pairs": [
            {
                "path": HOT,
                "writes": sorted([second[0].write_id, third["write_id"]]),
                "writers": [1, 2],
            }
        ],
        "undetermined": [],
    }
    assert v2["collisions"] == 0


def test_bytes_that_return_after_disappearing_do_not_reopen_the_write(tmp_path: Path) -> None:
    bench = Bench(tmp_path, writers=3)
    arm, harness = bench.prepare("naive")
    logs = [bench.log("naive", writer) for writer in range(3)]
    stale_second = systems.read_edits(arm.checkout, SECOND, 1)
    first = _write(arm, FIRST, logs[0])
    carrying = systems.read_edits(arm.checkout, THIRD, 1)
    systems.write_edits(arm.checkout, stale_second, SECOND, 1, logs[1])
    assert first[0].token not in (arm.checkout / HOT).read_text(encoding="utf-8")
    systems.write_edits(arm.checkout, carrying, THIRD, 1, logs[2])
    assert first[0].token in (arm.checkout / HOT).read_text(encoding="utf-8")
    assert systems.naive_submit(arm, THIRD, 1, carrying, logs[2]) == systems.LANDED
    assert first[0].token in _tokens(arm, f"HEAD:{HOT}")
    v3, v2, _ = _scored(bench, arm, harness)
    assert v3 == {
        "collisions": 2,
        "pairs": [
            _pair(first[0], stale_second[0], [0, 1]),
            _pair(stale_second[0], carrying[0], [1, 2]),
        ],
        "undetermined": [],
    }
    assert v2["collisions"] == 3


# --- commits that refused, failed and retried operations leave ----------------------------


def _refusing_gate(tmp_path: Path, checkout: Path) -> Path:
    source = _CLEAN_PAYLOAD_GATE_SOURCE.replace(
        '"findings": [],', '"findings": ["fixture refusal"],'
    ).replace('"verdict": "CLEAN",', '"verdict": "REFUSE",')
    gate = tmp_path / "refusing_gate.py"
    gate.write_text(source + "raise SystemExit(1)\n", encoding="utf-8")
    _readmit(checkout, gate)
    return gate


def _readmit(checkout: Path, gate: Path) -> None:
    """Admit another gate in a protected fixture the way an operator must: then reconcile."""

    admit_gate(checkout, gate)
    coordinator = Coordinator(checkout)
    epoch = [
        event.event_id
        for event in coordinator.wal.read().events
        if event.kind in ("workspace_protect", "workspace_reconcile")
    ][-1]
    coordinator.reconcile_workspace(
        "bench-operator", expected_epoch=epoch, reason="fixture admits another gate"
    )


def _named_candidate(
    tmp_path: Path, arm: systems.Arm, gate: Path, log: events_module.EventWriter
) -> dict[str, Any]:
    coordinator = Coordinator(arm.checkout, commit_payload=gate)
    target = systems.git_text(arm.checkout, "rev-parse", systems.TARGET)
    outcome = systems.onetree_attempt(
        replace(arm, gate=gate), coordinator, "bench-w0-watch", FIRST, 1, log
    )
    assert outcome == systems.RETRY
    assert systems.git_text(arm.checkout, "rev-parse", systems.TARGET) == target
    assert _kinds(log) == ["write", "observed", "commit", "observed", "refusal"]
    commit = events_module.read(log.path)[2]
    assert commit["scope"] == "candidate"
    assert systems.git_text(arm.checkout, "cat-file", "-t", commit["commit"]) == "commit"
    assert commit["commit"] not in _reachable(arm.checkout)
    assert commit["payload"][0]["tokens"] == [FIRST.token(HOT)]
    assert _commits_holding(arm.checkout, FIRST.token(HOT)) == [commit["commit"]]
    return commit


def test_a_candidate_the_gate_refuses_is_recorded_as_a_commit(tmp_path: Path) -> None:
    bench = Bench(tmp_path, writers=2)
    arm, harness = bench.prepare("onetree")
    log = bench.log("onetree", 0)
    commit = _named_candidate(tmp_path, arm, _refusing_gate(tmp_path, arm.checkout), log)
    assert events_module.read(log.path)[-1]["reason"] == "payload_gate_refused"
    assert arm.gate is not None
    _readmit(arm.checkout, arm.gate)
    _write(arm, SECOND, bench.log("onetree", 1))
    v3, v2, merged = _scored(bench, arm, harness)
    first = next(event for event in merged if event["kind"] == "write" and event["writer"] == 0)
    restored = next(event for event in merged if event.get("after") == "restore")
    assert commit["t"] < restored["t"]
    assert first["write_id"] == "w0-t0-a1-p0"
    assert (v3["collisions"], v3["undetermined"], v2["collisions"]) == (0, [], 0)


def test_a_candidate_the_referee_fails_is_recorded_as_a_commit(tmp_path: Path) -> None:
    bench = Bench(tmp_path, writers=1, conflict=0.0)
    arm, _ = bench.prepare("onetree")
    bench.referee.write_text("raise SystemExit(3)\n", encoding="utf-8")
    log = bench.log("onetree", 0)
    assert arm.gate is not None
    _named_candidate(tmp_path, arm, arm.gate, log)
    assert events_module.read(log.path)[-1]["reason"] == "referee_red"


def test_a_workspace_refusal_names_no_candidate_and_leaves_none(tmp_path: Path) -> None:
    bench = Bench(tmp_path, writers=1, conflict=0.0)
    arm, _ = bench.prepare("onetree")
    red = tmp_path / "red_referee.py"
    red.write_text("raise SystemExit(3)\n", encoding="utf-8")
    systems.git(arm.checkout, "config", "onetree.referee", shlex.join([sys.executable, str(red)]))
    log = bench.log("onetree", 0)
    coordinator = Coordinator(arm.checkout, commit_payload=arm.gate)
    outcome = systems.onetree_attempt(arm, coordinator, "bench-w0-watch", FIRST, 1, log)
    assert outcome == systems.RETRY
    assert _kinds(log) == ["write", "observed", "observed", "refusal"]
    assert events_module.read(log.path)[-1]["reason"] == "workspace_changed"
    assert _commits_holding(arm.checkout, FIRST.token(HOT)) == []


def test_a_gate_error_candidate_is_found_before_the_restore(tmp_path: Path) -> None:
    bench = Bench(tmp_path, writers=1, conflict=0.0)
    arm, harness = bench.prepare("onetree")
    impostor = tmp_path / "impostor.py"
    impostor.write_text("raise SystemExit(0)\n", encoding="utf-8")
    log = bench.log("onetree", 0)
    coordinator = Coordinator(arm.checkout, commit_payload=impostor)
    outcome = systems.onetree_attempt(
        replace(arm, gate=impostor), coordinator, "bench-w0-watch", FIRST, 1, log
    )
    assert outcome == systems.RETRY
    assert _kinds(log) == ["write", "observed", "commit", "observed", "error"]
    recorded = events_module.read(log.path)
    assert "payload_gate_error" in recorded[-1]["detail"]
    unreferenced = sorted(
        set(_commits_holding(arm.checkout, FIRST.token(HOT))) - _reachable(arm.checkout)
    )
    found, restored = recorded[2], recorded[3]
    assert (found["scope"], [found["commit"]]) == ("found", unreferenced)
    assert found["payload"][0]["tokens"] == [FIRST.token(HOT)]
    assert restored["paths"][HOT] == {"file": [], "index": [FIRST.token(HOT)]}
    assert found["t"] < restored["t"]
    _, _, merged = _scored(bench, arm, harness)
    first = next(event for event in merged if event["kind"] == "write")
    peer = {
        **first,
        "writer": 1,
        "write_id": "w1-t0-a1-p0",
        "token": SECOND.token(HOT),
        "index": "bench-w1-t0-a1",
        "t": (found["t"] + restored["t"]) / 2,
    }
    interleaved = sorted([*merged, peer], key=lambda event: float(event["t"]))
    assert metrics.collisions_v3(interleaved) == {"collisions": 0, "pairs": [], "undetermined": []}
    unfound = [event for event in interleaved if event.get("scope") != "found"]
    assert metrics.collisions_v3(unfound) == {
        "collisions": 1,
        "pairs": [{"path": HOT, "writes": [first["write_id"], "w1-t0-a1-p0"], "writers": [0, 1]}],
        "undetermined": [],
    }


@pytest.mark.parametrize("branch_moves", [False, True])
def test_a_worktree_commit_ends_the_write_whether_or_not_its_branch_moves(
    tmp_path: Path, branch_moves: bool
) -> None:
    bench = Bench(tmp_path, writers=2, files=2)
    arm, harness = bench.prepare("worktree")
    log = bench.log("worktree", 0)
    checkout = arm.writer_checkout(0)
    first = _write(arm, FIRST, log)
    lock = None if branch_moves else _lock_ref(checkout, "refs/heads/w0")
    try:
        assert systems.worktree_commit(arm, FIRST, 1, first, log) is branch_moves
    finally:
        if lock is not None:
            lock.unlink()
    holding = _commits_holding(checkout, first[0].token)
    assert len(holding) == 1
    assert (holding[0] in _reachable(checkout)) is branch_moves
    peer = _write(arm, SECOND, bench.log("worktree", 1))
    v3, v2, merged = _scored(bench, arm, harness)
    commits = [event for event in merged if event["kind"] == "commit"]
    assert [(event["commit"], event["scope"]) for event in commits] == [
        (holding[0], "local" if branch_moves else "found")
    ]
    assert commits[0]["t"] < next(e["t"] for e in merged if e.get("write_id") == peer[0].write_id)
    assert v3 == {"collisions": 0, "pairs": [], "undetermined": []}
    assert v2["collisions"] == (1 if branch_moves else 0)


def test_a_failed_naive_commit_ends_a_peer_write_it_holds(tmp_path: Path) -> None:
    bench = Bench(tmp_path, writers=3, files=2)
    arm, harness = bench.prepare("naive")
    logs = [bench.log("naive", writer) for writer in range(3)]
    staged = _write(arm, SECOND, logs[1])
    hook = arm.checkout / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    try:
        assert systems.naive_submit(arm, SECOND, 1, staged, logs[1]) == systems.RETRY
    finally:
        hook.unlink()
    assert _commits_holding(arm.checkout, staged[0].token) == []
    other = Task(0, 0, ("corpus/f001.txt",), ("aaaaaaaaaaaa",), 0)
    own = _write(arm, other, logs[0])
    lock = _lock_ref(arm.checkout, "refs/heads/main")
    try:
        assert systems.naive_submit(arm, other, 1, own, logs[0]) == systems.RETRY
    finally:
        lock.unlink()
    left = sorted(set(_commits_holding(arm.checkout, staged[0].token)) - _reachable(arm.checkout))
    assert len(left) == 1
    assert _kinds(logs[0]) == ["write", "observed", "observed", "commit", "error"]
    found = events_module.read(logs[0].path)[3]
    assert (found["scope"], found["commit"]) == ("found", left[0])
    assert {entry["path"]: entry["tokens"] for entry in found["payload"]} == {
        HOT: [staged[0].token],
        "corpus/f001.txt": [own[0].token],
    }
    third = _write(arm, THIRD, logs[2])
    assert staged[0].token in (arm.checkout / HOT).read_text(encoding="utf-8")
    v3, v2, merged = _scored(bench, arm, harness)
    assert v3 == {"collisions": 0, "pairs": [], "undetermined": []}
    assert v2["collisions"] == 0
    unfound = [event for event in merged if event.get("scope") != "found"]
    assert metrics.collisions_v3(unfound)["pairs"] == [_pair(staged[0], third[0], [1, 2])]


def test_a_failed_target_merge_is_found_after_the_local_commit(tmp_path: Path) -> None:
    bench = Bench(tmp_path, writers=1, files=2)
    arm, harness = bench.prepare("worktree")
    log = bench.log("worktree", 0)
    first = _write(arm, FIRST, log)
    assert systems.worktree_commit(arm, FIRST, 1, first, log) is True
    lock = _lock_ref(arm.checkout, "refs/heads/main")
    try:
        assert systems.worktree_integrate(arm, FIRST, 1, first, log) == systems.RETRY
    finally:
        lock.unlink()
    recorded = events_module.read(log.path)
    assert _kinds(log) == ["write", "observed", "commit", "commit", "error"]
    local, found = recorded[2], recorded[3]
    assert (local["scope"], found["scope"]) == ("local", "found")
    parents = systems.git_text(arm.checkout, "rev-list", "--parents", "-n", "1", found["commit"])
    target = systems.git_text(arm.checkout, "rev-parse", "main")
    assert parents.split()[1:] == [target, local["commit"]]
    assert found["payload"][0]["tokens"] == [first[0].token]
    assert found["commit"] not in _reachable(arm.checkout)
    v3, _, merged = _scored(bench, arm, harness)
    assert v3 == {"collisions": 0, "pairs": [], "undetermined": []}
    assert next(event for event in merged if event["kind"] == "final")["unrecorded_commits"] == []


def test_a_landing_retried_after_wal_contention_leaves_a_candidate_only_the_census_sees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bench = Bench(tmp_path, writers=2, conflict=0.0)
    arm, harness = bench.prepare("onetree")
    coordinator = Coordinator(arm.checkout, commit_payload=arm.gate)
    land = Coordinator.land
    contended: list[str] = []

    def contending(self: Coordinator, actor: str, **options: Any) -> Any:
        def checkpoint(stage: str, context: dict[str, Any]) -> None:
            if stage == "before_intent" and not contended:
                contended.append(context["candidate"])
                # Reclaiming the payload paths gives them new claim generations, which the
                # tested candidate's fences no longer name, so the landing re-audits.
                self.release(actor)
                self.claim(actor, [str(arm.checkout / path) for path in context["payload"]])

        return land(self, actor, checkpoint=checkpoint, **options)

    monkeypatch.setattr(Coordinator, "land", contending)
    log = bench.log("onetree", 0)
    assert systems.onetree_attempt(arm, coordinator, "bench-w0-watch", FIRST, 1, log) == (
        systems.LANDED
    )
    monkeypatch.undo()
    recorded = events_module.read(log.path)
    landed = next(event for event in recorded if event["kind"] == "landed")
    assert [(e["scope"], e["commit"]) for e in recorded if e["kind"] == "commit"] == [
        ("target", landed["commit"])
    ]
    assert len(contended) == 1
    assert contended[0] != landed["commit"]
    assert _commits_holding(arm.checkout, FIRST.token(HOT)) == sorted(
        [contended[0], landed["commit"]]
    )
    assert contended[0] not in _reachable(arm.checkout)
    v3, _, merged = _scored(bench, arm, harness)
    census = [event for event in merged if event.get("scope") == "run_end"]
    assert [(event["writer"], event["commit"]) for event in census] == [(None, contended[0])]
    assert census[0]["payload"][0]["tokens"] == [FIRST.token(HOT)]
    final = next(event for event in merged if event["kind"] == "final")
    assert final["unrecorded_commits"] == contended
    assert v3 == {"collisions": 0, "pairs": [], "undetermined": []}
    first = next(event for event in merged if event["kind"] == "write")
    peer = {
        **first,
        "writer": 1,
        "write_id": "w1-t0-a1-p0",
        "token": SECOND.token(HOT),
        "index": "bench-w1-t0-a1",
        "t": (first["t"] + landed["t"]) / 2,
    }
    inside = sorted([*merged, peer], key=lambda event: float(event["t"]))
    pair = {"path": HOT, "writes": [first["write_id"], "w1-t0-a1-p0"], "writers": [0, 1]}
    assert metrics.collisions_v3(inside) == {
        "collisions": None,
        "pairs": [],
        "undetermined": [{"cause": "unobserved_inclusion", "pairs": [pair]}],
    }
    uncensused = [event for event in inside if event.get("scope") != "run_end"]
    assert metrics.collisions_v3(uncensused) == {
        "collisions": 1,
        "pairs": [pair],
        "undetermined": [],
    }
