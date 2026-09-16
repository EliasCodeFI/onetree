"""Gate 2 task 16: real submitter death in the referee-era windows.

Every case forks a lander that runs ``Coordinator.land`` for real and is
SIGKILLed from outside, by this test, while it sits in one named window. The
reached phase is established two ways at once: a phase log the lander appends
before it blocks (and, for the referee window, a marker the referee process
writes from inside its own execution), and the child's exit status, which is
``-SIGKILL`` when the kernel took it there and something else when it left by
an exception.

The windows the mission names are candidate construction, the referee, and the
gap between a green referee and the intent append. Two further seam deaths,
after the intent and after the branch move, re-observe the INV7 ref-update
matrix with this second instrument; their expected recovery outcomes are the
matrix's own rows, not new claims.

Controls. The ``-SIGKILL`` exit and the phase log distinguish a kill in the
named window from a lander that refused or returned. The red control in the
before-intent case puts the dead lander's green candidate on the branch by
hand, the zombie landing that window could produce, and checks that
``inspect_wal`` and ``recover`` both name it; without that, the consistency
assertions here could not fail.
"""

from __future__ import annotations

import contextlib
import json
import multiprocessing
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
from conftest import _GIT_EXECUTABLE, GitFixture
from test_land import _register_and_stage
from test_referee import _alive

from onetree.core import Coordinator
from onetree.errors import UnrecordedLandingError

_PAYLOAD = "a-after-death\n"


def _append(path: Path, record: Mapping[str, Any]) -> None:
    """Append one durable JSON line: the reader may be a process that survives the writer."""
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _phases(path: Path) -> list[str]:
    return [str(record["phase"]) for record in _records(path)]


def _dying_lander(root: str, actor: str, stop_at: str | None, log: str, scratch: str) -> None:
    """Run one real landing; block at ``stop_at`` (or never) until killed from outside.

    The referee's scratch clone is created under ``scratch`` so that whatever a
    death leaves behind is observable by the test rather than lost in the
    system temporary directory.
    """
    tempfile.tempdir = scratch
    coordinator = Coordinator(Path(root))

    def checkpoint(point: str, context: Mapping[str, Any]) -> None:
        _append(Path(log), {
            "phase": point, "pid": os.getpid(), "candidate": context.get("candidate"),
            "tree": context.get("tree"), "expected_head": context.get("expected_head"),
            "intent": context.get("intent"),
        })
        if point == stop_at:
            while True:
                time.sleep(0.05)

    coordinator.land(actor, message="death fixture", checkpoint=checkpoint)
    _append(Path(log), {"phase": "returned", "pid": os.getpid()})


_REFEREE_SCRIPT = """\
import json, os, pathlib, time
marker = pathlib.Path(sys.argv[1]); release = pathlib.Path(sys.argv[2])
counter = pathlib.Path(sys.argv[3])
count = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(count))
if not release.exists():
    record = {"phase": "referee_running", "pid": os.getpid(), "pgid": os.getpgid(0),
              "candidate": os.environ["ONETREE_CANDIDATE"],
              "baseline": os.environ["ONETREE_BASELINE"], "cwd": os.getcwd()}
    staging = marker.with_suffix(".tmp")
    staging.write_text(json.dumps(record))
    staging.replace(marker)
    time.sleep(60)
"""


class _Referee:
    """A counting referee that blocks inside its own execution until released."""

    def __init__(self, git_repo: GitFixture, tmp_path: Path) -> None:
        self.marker = tmp_path / "referee.marker"
        self.release = tmp_path / "referee.release"
        self.counter = tmp_path / "referee.count"
        script = tmp_path / "referee.py"
        script.write_text("import sys\n" + _REFEREE_SCRIPT, encoding="utf-8")
        git_repo.git("config", "onetree.referee", shlex.join([
            sys.executable, "-I", "-S", str(script),
            str(self.marker), str(self.release), str(self.counter),
        ]))

    @property
    def executions(self) -> int:
        return int(self.counter.read_text()) if self.counter.exists() else 0

    def running(self) -> dict[str, Any] | None:
        if not self.marker.exists():
            return None
        return dict(json.loads(self.marker.read_text(encoding="utf-8")))

    def let_pass(self) -> None:
        self.release.write_text("release\n", encoding="utf-8")


def _wait_for(condition: Callable[[], bool], what: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.02)


def _fork_lander(
    git_repo: GitFixture, actor: str, stop_at: str | None, log: Path, scratch: Path,
) -> Any:
    scratch.mkdir(exist_ok=True)
    context = multiprocessing.get_context("fork")
    process = context.Process(
        target=_dying_lander,
        args=(str(git_repo.root), actor, stop_at, str(log), str(scratch)),
    )
    process.start()
    return process


def _kill(process: Any) -> None:
    """SIGKILL the lander from outside and prove that is how it died."""
    os.kill(process.pid, signal.SIGKILL)
    process.join(timeout=20)
    assert process.exitcode == -signal.SIGKILL


def _reap_referee(running: Mapping[str, Any] | None, timeout: float = 5.0) -> None:
    """Kill a started referee's whole group and wait, bounded, without asserting.

    Runs from cleanup, so it must not raise over the failure it is cleaning up
    after; whether the reap took is asserted by the caller on the success path.
    """
    if running is None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(int(running["pgid"]), signal.SIGKILL)
    deadline = time.monotonic() + timeout
    while _alive(int(running["pid"])) and time.monotonic() < deadline:
        time.sleep(0.02)


def _kinds(coordinator: Coordinator) -> list[str]:
    return [event.kind for event in coordinator.inspect_wal().events]


def _object_type(git_repo: GitFixture, oid: str) -> str:
    return git_repo.git("cat-file", "-t", oid).stdout.strip()


def _reachable(git_repo: GitFixture, commit: str) -> bool:
    listed = git_repo.git("rev-list", "--all").stdout.split()
    return commit in listed


def _assert_no_landing_trace(
    git_repo: GitFixture, coordinator: Coordinator, kinds_before: list[str],
) -> None:
    """The branch, the WAL and recovery all say this landing never happened."""
    assert git_repo.oid() == git_repo.base
    report = coordinator.inspect_wal()
    assert report.consistent
    assert report.pending_intents == 0
    assert report.divergences == ()
    assert [event.kind for event in report.events] == kinds_before
    recovery = coordinator.recover()
    assert recovery.consistent
    assert (recovery.pending, recovery.repaired, recovery.refused) == (0, 0, 0)
    assert recovery.locks == ()


def _prepare(git_repo: GitFixture, tmp_path: Path) -> tuple[Coordinator, _Referee, Path, Path]:
    """One staged session whose body is this test process, so the lander dies alone.

    The session is registered on the test's own pid: the death under test is the
    landing incarnation's, not the session's, which is the case the intent's
    ``lander`` block exists for.
    """
    coordinator = Coordinator(git_repo.root)
    _register_and_stage(git_repo, coordinator, "alpha", "a.txt", _PAYLOAD)
    referee = _Referee(git_repo, tmp_path)
    return coordinator, referee, tmp_path / "phases.jsonl", tmp_path / "scratch"


@pytest.mark.parametrize("point", ["after_audit", "after_tree"])
def test_death_during_candidate_construction_leaves_no_trace_and_the_referee_unrun(
    git_repo: GitFixture, tmp_path: Path, point: str,
) -> None:
    coordinator, referee, log, scratch = _prepare(git_repo, tmp_path)
    kinds_before = _kinds(coordinator)

    process = _fork_lander(git_repo, "alpha", point, log, scratch)
    try:
        _wait_for(lambda: point in _phases(log), f"the lander to reach {point}")
        _kill(process)
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=5)

    expected_phases = ["after_audit"] if point == "after_audit" else ["after_audit", "after_tree"]
    assert _phases(log) == expected_phases
    last = _records(log)[-1]
    assert last["candidate"] is None
    if point == "after_tree":
        # The candidate tree object exists; no commit was ever written over it.
        assert _object_type(git_repo, str(last["tree"])) == "tree"
    assert referee.executions == 0

    _assert_no_landing_trace(git_repo, coordinator, kinds_before)
    assert sorted(scratch.glob("onetree-referee-*")) == []

    # The staged session and its claims are intact: the same actor lands the same
    # payload on a fresh attempt, and only then does the referee run.
    referee.let_pass()
    landed = coordinator.land("alpha", message="retry after construction death")
    assert git_repo.oid() == landed.commit
    assert landed.attempts == 1
    assert git_repo.git("show", f"{landed.commit}:a.txt").stdout == _PAYLOAD
    assert referee.executions == 1
    assert _kinds(coordinator) == [*kinds_before, "land_intent", "landed"]


def test_death_during_the_referee_leaves_no_trace_and_orphans_the_referee(
    git_repo: GitFixture, tmp_path: Path,
) -> None:
    coordinator, referee, log, scratch = _prepare(git_repo, tmp_path)
    kinds_before = _kinds(coordinator)

    # One cleanup scope spans the whole lifetime of both spawned processes: the
    # lander from its fork, and the referee from the moment its marker can
    # exist. Any assertion that fails in between exits through the reap of both,
    # with its own failure preserved (task 172).
    process = _fork_lander(git_repo, "alpha", None, log, scratch)
    running: dict[str, Any] | None = None
    try:
        _wait_for(lambda: referee.running() is not None, "the referee to start")
        running = referee.running()
        assert running is not None
        # Reached phase: the tree seam was crossed, the referee is executing the
        # candidate, and the before-intent seam has not been reached.
        assert _phases(log) == ["after_audit", "after_tree"]
        assert _alive(int(running["pid"]))
        _kill(process)

        candidate = str(running["candidate"])
        referee_pid = int(running["pid"])
        assert running["baseline"] == git_repo.base
        assert _phases(log) == ["after_audit", "after_tree"]
        assert referee.executions == 1

        # The candidate the referee was testing is a real commit over the audited
        # payload, reachable from no ref: an object, not a landing.
        assert _object_type(git_repo, candidate) == "commit"
        assert git_repo.git("show", f"{candidate}:a.txt").stdout == _PAYLOAD
        assert not _reachable(git_repo, candidate)
        _assert_no_landing_trace(git_repo, coordinator, kinds_before)

        # Observed shape of this gap, pinned so a later change flips it visibly:
        # the referee runs in its own session (``start_new_session=True``), so
        # the broker's death does not reach it. It outlives its broker, and the
        # scratch clone it runs in is never removed, because the cleanup that
        # would do both lives in the dead process. No contract in ROUND-B.md
        # speaks to submitter death; the timeout contract's process-group kill
        # is a different path and is not exercised here (task 171).
        assert _alive(referee_pid)
        residue = sorted(path.name for path in scratch.glob("onetree-referee-*"))
        assert len(residue) == 1
        assert Path(str(running["cwd"])).is_relative_to(scratch / residue[0])
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=5)
        # The marker may have appeared after a failed wait; read it again so a
        # referee that started is reaped on every exit path.
        _reap_referee(running if running is not None else referee.running())
    assert not _alive(referee_pid)

    # A fresh attempt builds and tests a new candidate; the dead attempt's
    # candidate is not what lands.
    referee.let_pass()
    landed = coordinator.land("alpha", message="retry after referee death")
    assert git_repo.oid() == landed.commit
    assert landed.commit != candidate
    assert landed.referee_receipt["result"] == "pass"
    assert landed.referee_receipt["tested"] == [landed.commit]
    assert referee.executions == 2
    assert git_repo.git("show", f"{landed.commit}:a.txt").stdout == _PAYLOAD
    assert _kinds(coordinator) == [*kinds_before, "land_intent", "landed"]


def test_death_after_green_before_intent_loses_the_green_and_leaves_no_trace(
    git_repo: GitFixture, tmp_path: Path,
) -> None:
    coordinator, referee, log, scratch = _prepare(git_repo, tmp_path)
    referee.let_pass()
    kinds_before = _kinds(coordinator)

    process = _fork_lander(git_repo, "alpha", "before_intent", log, scratch)
    try:
        _wait_for(lambda: "before_intent" in _phases(log), "the lander to pass the referee")
        _kill(process)
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=5)

    assert _phases(log) == ["after_audit", "after_tree", "before_intent"]
    record = _records(log)[-1]
    candidate = str(record["candidate"])
    assert record["expected_head"] == git_repo.base
    # The referee ran once and passed; its receipt existed only in the dead
    # process, because the intent that would have carried it was never appended.
    assert referee.executions == 1
    assert _object_type(git_repo, candidate) == "commit"
    assert git_repo.git("show", f"{candidate}:a.txt").stdout == _PAYLOAD
    assert not _reachable(git_repo, candidate)
    assert sorted(scratch.glob("onetree-referee-*")) == []
    _assert_no_landing_trace(git_repo, coordinator, kinds_before)

    # RED control: the zombie landing this window could produce is the dead
    # lander's green candidate on the branch with no WAL record. Put it there by
    # hand and check that the instruments above would have named it.
    git_repo.git("update-ref", "refs/heads/main", candidate, git_repo.base)
    diverged = coordinator.inspect_wal()
    assert not diverged.consistent
    assert [item.kind for item in diverged.divergences] == ["unrecorded_landing"]
    assert diverged.divergences[0].commit == candidate
    with pytest.raises(UnrecordedLandingError):
        coordinator.recover()
    git_repo.git("update-ref", "refs/heads/main", git_repo.base, candidate)
    _assert_no_landing_trace(git_repo, coordinator, kinds_before)

    # The green is not reused: a fresh attempt runs the referee again over a
    # new candidate before anything is recorded.
    landed = coordinator.land("alpha", message="retry after pre-intent death")
    assert git_repo.oid() == landed.commit
    assert landed.commit != candidate
    assert referee.executions == 2
    assert landed.referee_receipt["tested"] == [landed.commit]
    assert _kinds(coordinator) == [*kinds_before, "land_intent", "landed"]


#: The two INV7 rows a Python-seam death can reach, with the recovery outcomes
#: the ref-update matrix in test_land.py already establishes for them.
_RECORDED_WINDOWS = [
    pytest.param("after_intent", 0, 1, id="intent-recorded-branch-unmoved"),
    pytest.param("after_branch", 1, 0, id="branch-moved-landed-unrecorded"),
]


@pytest.mark.parametrize(("point", "repaired", "refused"), _RECORDED_WINDOWS)
def test_seam_death_after_the_intent_corroborates_the_inv7_matrix(
    git_repo: GitFixture, tmp_path: Path, point: str, repaired: int, refused: int,
) -> None:
    coordinator, referee, log, scratch = _prepare(git_repo, tmp_path)
    referee.let_pass()
    kinds_before = _kinds(coordinator)

    process = _fork_lander(git_repo, "alpha", point, log, scratch)
    try:
        _wait_for(lambda: point in _phases(log), f"the lander to reach {point}")
        _kill(process)
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=5)

    expected_phases = ["after_audit", "after_tree", "before_intent", "after_intent"]
    if point == "after_branch":
        expected_phases.append("after_branch")
    assert _phases(log) == expected_phases
    candidate = str(_records(log)[-1]["candidate"])
    assert referee.executions == 1

    # Before recovery: the intent is on the WAL, the branch is exactly where the
    # window says, and nothing is diverged -- the write-ahead order's invariant.
    report = coordinator.inspect_wal()
    assert report.pending_intents == 1
    assert report.divergences == ()
    assert [event.kind for event in report.events] == [*kinds_before, "land_intent"]
    assert git_repo.oid() == (candidate if point == "after_branch" else git_repo.base)

    recovery = coordinator.recover()
    assert recovery.consistent
    assert recovery.pending == 0
    assert (recovery.repaired, recovery.refused) == (repaired, refused)

    if point == "after_intent":
        # Refused, not completed: the branch never moved, so the same actor lands
        # a fresh candidate, and the referee runs for it.
        assert git_repo.oid() == git_repo.base
        landed = coordinator.land("alpha", message="retry after refused intent")
        assert git_repo.oid() == landed.commit
        assert landed.commit != candidate
        assert referee.executions == 2
    else:
        # Completed by recovery: the branch already holds the candidate and the
        # landed mark is appended for it, with no second referee run.
        assert git_repo.oid() == candidate
        assert referee.executions == 1

    final = coordinator.inspect_wal()
    assert final.consistent
    assert final.pending_intents == 0
    assert final.divergences == ()
    assert [event.kind for event in final.events].count("landed") == 1
    counted = git_repo.git("rev-list", "--count", f"{git_repo.base}..{git_repo.oid()}")
    assert counted.stdout.strip() == "1"
    assert git_repo.git("show", f"{git_repo.oid()}:a.txt").stdout == _PAYLOAD
    assert subprocess.run(  # the repository is sound after every window
        [_GIT_EXECUTABLE, "-C", str(git_repo.root), "fsck", "--no-dangling"],
        check=False, capture_output=True, text=True,
    ).returncode == 0
