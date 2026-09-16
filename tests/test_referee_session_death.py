"""Gate 2 task 16, remaining axes: session-body death and death inside commit_tree.

``test_referee_crash_windows.py`` kills a landing incarnation whose session
body is the test process, so every window there ends with a live session and
a dead lander. Two axes were left open. First, the submitter that dies may be
the registered session body itself: then the session, its claims and the
fences on its intent all belong to a dead process, and what recovery, the
claim table and a live peer do with that is a different question from the
lander-only case. Second, death between the tree seam and the referee was
bounded by ``after_tree`` and the referee marker but never produced: the
lander is inside ``Repository.commit_tree`` there, blocked in
``subprocess.run``, and no Python seam exists.

Instruments. Every lander is a real forked process running
``Coordinator.land`` and SIGKILLed from outside, with ``-SIGKILL`` as the
exit status and a phase log naming the reached seam, the shape the existing
fixtures use. The session-body cases register the session on the lander's
own pid from inside the lander, so the WAL's ``session_register`` and the
intent's ``lander`` block name the same process, and the test reads both
back. The commit-tree cases point the lander's Git layer at a script that,
for ``commit-tree`` only, writes a marker from inside the invocation and
blocks: before running Git, so no object is written, or after running it, so
the commit object exists and its id is held only by a process about to die.
Both are deaths at the wrapper boundary, while the lander is blocked in
``subprocess.run``; neither is a death inside Git's own object write, which
no marker outside Git can place and which this module does not claim.

Controls. The ``-SIGKILL`` exit and the phase log distinguish the kill from a
refusal or return. The dead session's own actor is asked to land afterwards
and is refused ``session_inactive``, recorded, so "the session is dead" is a
broker observation, not the test's. Where the commit object must be absent,
the same object-store count that finds it in the after-write mode finds
nothing in the before-write mode. The zombie-landing red control for the
no-trace assertions stands in ``test_referee_crash_windows.py`` and is not
repeated.
"""

from __future__ import annotations

import contextlib
import json
import multiprocessing
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from conftest import _GIT_EXECUTABLE, GitFixture
from test_referee import _alive
from test_referee_crash_windows import (
    _append,
    _assert_no_landing_trace,
    _kill,
    _kinds,
    _object_type,
    _reachable,
    _reap_referee,
    _records,
    _Referee,
    _wait_for,
)

from onetree import git
from onetree.core import Coordinator
from onetree.errors import LandingRefusedError

_PAYLOAD = "a-body-death\n"
_PEER_PAYLOAD = "a-peer-after-death\n"


def _start_tick(pid: int) -> int:
    """Field 22 of ``/proc/<pid>/stat``: the process start tick, the gc bar's birth tick."""
    stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    return int(stat.rsplit(")", 1)[1].split()[19])


def _phases(log: Path) -> list[str]:
    return [str(record["phase"]) for record in _records(log) if record["phase"] != "registered"]


def _body_lander(
    root: str, base: str, actor: str, stop_at: str | None, log: str, scratch: str,
    git_executable: str | None,
) -> None:
    """One process that is both the session body and the lander, killed from outside.

    Registration, claim and staging happen here, on this pid, so the death
    under test takes the session with it. ``git_executable`` redirects only
    this process's Git layer, for the commit-tree cases.
    """
    tempfile.tempdir = scratch
    if git_executable is not None:
        git._GIT_EXECUTABLE = git_executable
    repo = GitFixture(root=Path(root), base=base)
    coordinator = Coordinator(repo.root)
    session = coordinator.register_session(actor, pid=os.getpid(), ttl=300)
    claimed = coordinator.claim(actor, [str(repo.root / "a.txt")])
    repo.write("a.txt", _PAYLOAD)
    repo.stage(session.index_path, "a.txt")
    _append(Path(log), {
        "phase": "registered", "pid": os.getpid(), "start": _start_tick(os.getpid()),
        "session_generation": session.generation,
        "claim_generations": [claim.generation for claim in claimed.claims],
    })

    def checkpoint(point: str, context: Mapping[str, Any]) -> None:
        _append(Path(log), {
            "phase": point, "pid": os.getpid(), "candidate": context.get("candidate"),
            "tree": context.get("tree"), "expected_head": context.get("expected_head"),
            "operation": context.get("operation"), "intent": context.get("intent"),
        })
        if point == stop_at:
            while True:
                time.sleep(0.05)

    coordinator.land(actor, message="session-body death fixture", checkpoint=checkpoint)
    _append(Path(log), {"phase": "returned", "pid": os.getpid()})


def _fork_body_lander(
    git_repo: GitFixture, actor: str, stop_at: str | None, log: Path, scratch: Path,
    git_executable: str | None = None,
) -> Any:
    scratch.mkdir(exist_ok=True)
    context = multiprocessing.get_context("fork")
    process = context.Process(
        target=_body_lander,
        args=(str(git_repo.root), git_repo.base, actor, stop_at, str(log), str(scratch),
              git_executable),
    )
    process.start()
    return process


def _stop(process: Any) -> None:
    if process.is_alive():
        process.kill()
        process.join(timeout=5)


def _events(coordinator: Coordinator) -> list[Any]:
    return list(coordinator.inspect_wal().events)


def _registered(log: Path) -> dict[str, Any]:
    return next(record for record in _records(log) if record["phase"] == "registered")


def _assert_session_identity(coordinator: Coordinator, log: Path, process: Any) -> None:
    """The WAL's session body is the process that died, by pid and birth tick."""
    registered = _registered(log)
    assert registered["pid"] == process.pid
    sessions = [event for event in _events(coordinator) if event.kind == "session_register"]
    assert len(sessions) == 1
    assert sessions[0].actor == "alpha"
    assert sessions[0].data["pid"] == process.pid
    assert sessions[0].data["pid_start"] == registered["start"]
    assert sessions[0].data["generation"] == registered["session_generation"]
    assert not _alive(process.pid)


def _assert_dead_session_refused(coordinator: Coordinator) -> None:
    """The broker finds the session dead: its actor cannot land, and the refusal is recorded."""
    with pytest.raises(LandingRefusedError) as refused:
        coordinator.land("alpha", message="from the dead session's actor")
    assert refused.value.details["reason"] == "session_inactive"
    assert [
        event.data["reason"] for event in _events(coordinator)
        if event.kind == "refused" and event.actor == "alpha"
    ][-1] == "session_inactive"


def _peer(git_repo: GitFixture, coordinator: Coordinator) -> None:
    """A live session on the test process claims the dead session's path and stages a payload."""
    session = coordinator.register_session("peer", pid=os.getpid(), ttl=300)
    coordinator.claim("peer", [str(git_repo.root / "a.txt")])
    git_repo.write("a.txt", _PEER_PAYLOAD)
    git_repo.stage(session.index_path, "a.txt")


#: Every referee-era window, with what the WAL holds when the body dies there
#: and what recovery does about it (INV7 rows for the two recorded windows).
_WINDOWS = [
    pytest.param("after_audit", 0, 0, id="after-audit"),
    pytest.param("after_tree", 0, 0, id="after-tree"),
    pytest.param("referee", 0, 0, id="during-referee"),
    pytest.param("before_intent", 0, 0, id="green-before-intent"),
    pytest.param("after_intent", 0, 1, id="intent-recorded-branch-unmoved"),
    pytest.param("after_branch", 1, 0, id="branch-moved-landed-unrecorded"),
]


@pytest.mark.parametrize(("window", "repaired", "refused"), _WINDOWS)
def test_session_body_death_in_each_window_leaves_a_dead_session_the_broker_names(
    git_repo: GitFixture, tmp_path: Path, window: str, repaired: int, refused: int,
) -> None:
    coordinator = Coordinator(git_repo.root)
    referee = _Referee(git_repo, tmp_path)
    if window != "referee":
        referee.let_pass()
    log, scratch = tmp_path / "phases.jsonl", tmp_path / "scratch"
    kinds_before = _kinds(coordinator)
    stop_at = None if window == "referee" else window

    process = _fork_body_lander(git_repo, "alpha", stop_at, log, scratch)
    running: dict[str, Any] | None = None
    referee_pid: int | None = None
    try:
        if window == "referee":
            _wait_for(lambda: referee.running() is not None, "the referee to start")
            running = referee.running()
            assert running is not None
            referee_pid = int(running["pid"])
            assert _alive(referee_pid)
            assert _phases(log) == ["after_audit", "after_tree"]
        else:
            _wait_for(lambda: window in _phases(log), f"the body to reach {window}")
        _kill(process)
        if window == "referee":
            # Production cleanup (task 171) owns the referee once its lander is gone.
            _wait_for(lambda: not _alive(int(running["pid"])), "referee cleanup", timeout=5)
            _wait_for(lambda: not list(scratch.glob("onetree-referee-*")),
                      "scratch cleanup", timeout=5)
    finally:
        _stop(process)
        _reap_referee(running if running is not None else referee.running())
    if referee_pid is not None:
        assert not _alive(referee_pid)

    # Reached phase and identity: the session body and the lander are one dead process.
    expected = {
        "after_audit": ["after_audit"],
        "after_tree": ["after_audit", "after_tree"],
        "referee": ["after_audit", "after_tree"],
        "before_intent": ["after_audit", "after_tree", "before_intent"],
        "after_intent": ["after_audit", "after_tree", "before_intent", "after_intent"],
        "after_branch": ["after_audit", "after_tree", "before_intent", "after_intent",
                         "after_branch"],
    }[window]
    assert _phases(log) == expected
    _assert_session_identity(coordinator, log, process)
    registered = _registered(log)
    last = _records(log)[-1]
    candidate = last.get("candidate")
    assert referee.executions == (0 if window in {"after_audit", "after_tree"} else 1)

    if window in {"after_audit", "after_tree", "referee", "before_intent"}:
        # Nothing recorded past the session's own rows: the dead session holds
        # claims on a payload that never became an intent.
        assert [event.kind for event in _events(coordinator)] == [
            *kinds_before, "session_register", "claim",
        ]
        _assert_no_landing_trace(
            git_repo, coordinator, [*kinds_before, "session_register", "claim"],
        )
        if candidate is not None:
            assert _object_type(git_repo, str(candidate)) == "commit"
            assert not _reachable(git_repo, str(candidate))
        _assert_dead_session_refused(coordinator)
        # The dead session's claim no longer fences the path: a live peer claims
        # it at once and lands its own payload, with the referee run for it.
        _peer(git_repo, coordinator)
        landed = coordinator.land("peer", message="peer after body death")
        assert git_repo.oid() == landed.commit
        assert git_repo.git("show", f"{landed.commit}:a.txt").stdout == _PEER_PAYLOAD
        assert referee.executions == (1 if window in {"after_audit", "after_tree"} else 2)
        kinds = [event.kind for event in _events(coordinator)]
        assert kinds[-2:] == ["land_intent", "landed"]
        assert kinds.count("landed") == 1
        return

    # Recorded windows: the intent names the dead process as its lander, and
    # the same pid and birth tick as the session body.
    intents = [event for event in _events(coordinator) if event.kind == "land_intent"]
    assert len(intents) == 1
    intent = intents[0]
    assert intent.actor == "alpha"
    assert intent.data["lander"] == {"pid": process.pid, "start": registered["start"],
                                     "warnings": []}
    assert intent.data["session_generation"] == registered["session_generation"]
    assert [fence["claim_generation"] for fence in intent.data["fences"]] == (
        registered["claim_generations"]
    )
    assert intent.data["candidate"] == candidate
    report = coordinator.inspect_wal()
    assert report.pending_intents == 1
    assert report.divergences == ()
    assert git_repo.oid() == (candidate if window == "after_branch" else git_repo.base)
    _assert_dead_session_refused(coordinator)

    # Claim disposition before recovery: the dead session's claim is inactive, so
    # the peer's claim is granted; the pending intent of a dead lander still holds
    # the train, so the peer's landing is refused until recovery resolves it.
    _peer(git_repo, coordinator)
    with pytest.raises(LandingRefusedError) as held:
        coordinator.land("peer", message="peer before recovery")
    assert held.value.details["reason"] == "train_pending"
    assert git_repo.oid() == (candidate if window == "after_branch" else git_repo.base)

    recovery = coordinator.recover()
    assert recovery.consistent
    assert recovery.pending == 0
    assert (recovery.repaired, recovery.refused) == (repaired, refused)
    operation = intent.data["operation"]
    resolutions = [
        event for event in _events(coordinator)
        if event.kind in {"landed", "refused"} and event.data.get("operation") == operation
    ]
    assert [event.kind for event in resolutions][-1] == ("landed" if repaired else "refused")
    if refused:
        assert resolutions[-1].data["reason"] == "incomplete_intent"
        assert git_repo.oid() == git_repo.base
        assert not _reachable(git_repo, str(candidate))
    else:
        assert resolutions[-1].data["recovered"] is True
        assert git_repo.oid() == candidate
        assert git_repo.git("show", f"{candidate}:a.txt").stdout == _PAYLOAD

    # Fresh valid work after recovery: the peer lands on whatever recovery left.
    landed = coordinator.land("peer", message="peer after recovery")
    assert git_repo.oid() == landed.commit
    assert git_repo.git("show", f"{landed.commit}:a.txt").stdout == _PEER_PAYLOAD
    counted = git_repo.git("rev-list", "--count", f"{git_repo.base}..{landed.commit}")
    assert counted.stdout.strip() == ("2" if repaired else "1")
    final = coordinator.inspect_wal()
    assert final.consistent
    assert final.pending_intents == 0
    assert final.divergences == ()
    assert [event.kind for event in final.events].count("landed") == (2 if repaired else 1)
    assert subprocess.run(
        [_GIT_EXECUTABLE, "-C", str(git_repo.root), "fsck", "--no-dangling"],
        check=False, capture_output=True, text=True,
    ).returncode == 0


#: A Git front that, for the ``commit-tree`` of the candidate tree only (the
#: WAL writes its own events with commit-tree too), writes a marker from
#: inside the invocation and blocks, either before Git runs or after it has
#: returned. The candidate tree is the one the ``after_tree`` seam logged.
_COMMIT_TREE_FRONT = """\
#!{python}
import json, os, pathlib, subprocess, sys, time
real = {real!r}
def candidate_trees():
    log = pathlib.Path({log!r})
    if not log.exists():
        return set()
    records = [json.loads(line) for line in log.read_text().splitlines() if line]
    return {{record["tree"] for record in records if record.get("phase") == "after_tree"}}
if "commit-tree" not in sys.argv[1:]:
    os.execv(real, [real, *sys.argv[1:]])
tree = sys.argv[sys.argv.index("commit-tree") + 1]
if tree not in candidate_trees():
    os.execv(real, [real, *sys.argv[1:]])
record = {{"pid": os.getpid(), "ppid": os.getppid(), "argv": sys.argv[1:],
          "cwd": os.getcwd(), "mode": {mode!r}, "oid": None}}
if {mode!r} == "after":
    completed = subprocess.run([real, *sys.argv[1:]], stdin=sys.stdin, capture_output=True)
    record["oid"] = completed.stdout.decode().strip()
    record["returncode"] = completed.returncode
marker = pathlib.Path({marker!r})
staging = marker.with_suffix(".tmp")
staging.write_text(json.dumps(record))
staging.replace(marker)
time.sleep(60)
"""


def _dangling_commits(git_repo: GitFixture) -> set[str]:
    """Commit objects in the store that no ref, WAL included, reaches."""
    listed = git_repo.git("cat-file", "--batch-all-objects", "--batch-check").stdout.splitlines()
    commits = {line.split()[0] for line in listed if line.split()[1] == "commit"}
    return commits - set(git_repo.git("rev-list", "--all").stdout.split())


@pytest.mark.parametrize("mode", ["before", "after"])
def test_death_inside_the_commit_tree_wrapper_leaves_no_trace(
    git_repo: GitFixture, tmp_path: Path, mode: str,
) -> None:
    """The lander dies while blocked in ``Repository.commit_tree``.

    ``before``: Git never ran; the object store holds no new commit.
    ``after``: Git returned the commit id to a process that died holding it;
    the object exists, reachable from no ref, and is not what lands."""
    coordinator = Coordinator(git_repo.root)
    referee = _Referee(git_repo, tmp_path)
    referee.let_pass()
    log, scratch = tmp_path / "phases.jsonl", tmp_path / "scratch"
    marker = tmp_path / "commit-tree.marker"
    front = tmp_path / "git-front"
    front.write_text(_COMMIT_TREE_FRONT.format(
        python=sys.executable, real=_GIT_EXECUTABLE, mode=mode, marker=str(marker),
        log=str(log),
    ), encoding="utf-8")
    front.chmod(0o700)
    kinds_before = _kinds(coordinator)
    assert _dangling_commits(git_repo) == set()

    process = _fork_body_lander(git_repo, "alpha", None, log, scratch, str(front))
    inside: dict[str, Any] | None = None
    try:
        _wait_for(marker.exists, "the lander to enter commit-tree")
        inside = json.loads(marker.read_text(encoding="utf-8"))
        assert inside is not None
        # Reached phase: past the tree seam, inside the wrapper, no candidate seam.
        assert _phases(log) == ["after_audit", "after_tree"]
        assert inside["ppid"] == process.pid
        assert _alive(int(inside["pid"]))
        argv = list(inside["argv"])
        assert argv[argv.index("commit-tree") + 1] == str(_records(log)[-1]["tree"])
        _kill(process)
    finally:
        _stop(process)
        if inside is None and marker.exists():
            inside = json.loads(marker.read_text(encoding="utf-8"))
        if inside is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(inside["pid"]), signal.SIGKILL)
    assert inside is not None
    _wait_for(lambda: not _alive(int(inside["pid"])), "the git front to be reaped", timeout=5)

    assert _phases(log) == ["after_audit", "after_tree"]
    tree = str(_records(log)[-1]["tree"])
    assert _object_type(git_repo, tree) == "tree"
    assert referee.executions == 0
    # The object store, not the log, says whether the dead attempt wrote a commit:
    # the same census that finds one in the after-write mode finds none here.
    dangling = _dangling_commits(git_repo)
    if mode == "before":
        assert inside["oid"] is None
        assert dangling == set()
    else:
        assert inside["returncode"] == 0
        dead_candidate = str(inside["oid"])
        assert dangling == {dead_candidate}
        assert _object_type(git_repo, dead_candidate) == "commit"
        assert git_repo.git("rev-parse", f"{dead_candidate}^{{tree}}").stdout.strip() == tree
        assert git_repo.git("rev-parse", f"{dead_candidate}^").stdout.strip() == git_repo.base
        assert not _reachable(git_repo, dead_candidate)
    _assert_session_identity(coordinator, log, process)
    _assert_no_landing_trace(git_repo, coordinator, [*kinds_before, "session_register", "claim"])
    assert sorted(scratch.glob("onetree-referee-*")) == []
    _assert_dead_session_refused(coordinator)

    _peer(git_repo, coordinator)
    landed = coordinator.land("peer", message="peer after commit-tree death")
    assert git_repo.oid() == landed.commit
    assert landed.commit not in dangling
    assert git_repo.git("show", f"{landed.commit}:a.txt").stdout == _PEER_PAYLOAD
    assert referee.executions == 1
    assert _dangling_commits(git_repo) == dangling
    assert subprocess.run(
        [_GIT_EXECUTABLE, "-C", str(git_repo.root), "fsck", "--no-dangling"],
        check=False, capture_output=True, text=True,
    ).returncode == 0


def test_commit_tree_front_reports_from_inside_the_invocation(tmp_path: Path) -> None:
    """Instrument control: the front passes every other command, and a commit-tree
    of any other tree, to Git unchanged; for the logged candidate tree it records
    from inside, with this process as parent."""
    marker = tmp_path / "marker"
    log = tmp_path / "phases.jsonl"
    front = tmp_path / "git-front"
    front.write_text(_COMMIT_TREE_FRONT.format(
        python=sys.executable, real=_GIT_EXECUTABLE, mode="before", marker=str(marker),
        log=str(log),
    ), encoding="utf-8")
    front.chmod(0o700)
    version = subprocess.run([str(front), "--version"], check=True, capture_output=True, text=True)
    assert version.stdout.startswith("git version")
    other = subprocess.run([str(front), "commit-tree", "1" * 40], check=False,
                           capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert other.returncode != 0
    assert not marker.exists()
    _append(log, {"phase": "after_tree", "tree": "0" * 40})
    blocked = subprocess.Popen(
        [str(front), "--no-pager", "-C", str(tmp_path), "commit-tree", "0" * 40, "-p", "1" * 40],
        stdin=subprocess.DEVNULL,
    )
    try:
        _wait_for(marker.exists, "the front to record", timeout=10)
        inside = json.loads(marker.read_text(encoding="utf-8"))
        assert inside["ppid"] == os.getpid()
        assert inside["pid"] == blocked.pid
        assert inside["mode"] == "before"
        assert blocked.poll() is None
    finally:
        blocked.kill()
        blocked.wait(timeout=5)
