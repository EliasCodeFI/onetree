"""Round A landing invariants, including real races and process death."""

from __future__ import annotations

import multiprocessing
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from conftest import GitFixture, KillGit, install_kill_git

from onetree import core
from onetree.core import Coordinator
from onetree.errors import LandingRefusedError, UnrecordedLandingError

_GIT_EXECUTABLE = shutil.which("git") or "git"


def _raw_cas_worker(
    root: str,
    candidate: str,
    expected: str,
    barrier: Any,
    results: Any,
) -> None:
    barrier.wait(timeout=10)
    completed = subprocess.run(
        [_GIT_EXECUTABLE, "-C", root, "update-ref", "refs/heads/main", candidate, expected],
        check=False,
        capture_output=True,
        text=True,
    )
    results.put(completed.returncode)


def _land_worker(root: str, actor: str, barrier: Any, results: Any) -> None:
    coordinator = Coordinator(Path(root))
    waited = False

    def checkpoint(point: str, _context: Mapping[str, Any]) -> None:
        nonlocal waited
        if point == "before_intent" and not waited:
            waited = True
            barrier.wait(timeout=10)

    try:
        result = coordinator.land(actor, message=f"land {actor}", checkpoint=checkpoint)
    except Exception as error:  # pragma: no cover - asserted through the child result
        results.put({"error": f"{type(error).__name__}: {error}"})
    else:
        results.put({"actor": actor, "attempts": result.attempts, "commit": result.commit})


def _crash_lander(root: str, actor: str, wrapper: str, environment: dict[str, str]) -> None:
    install_kill_git(wrapper, environment)
    Coordinator(Path(root)).land(actor, message="crash fixture")


def _deordered_lander(root: str, actor: str) -> None:
    coordinator = Coordinator(Path(root))

    def checkpoint(point: str, context: Mapping[str, Any]) -> None:
        if point != "before_intent":
            return
        subprocess.run(
            [
                _GIT_EXECUTABLE,
                "-C",
                root,
                "update-ref",
                str(context["target"]),
                str(context["candidate"]),
                str(context["expected_head"]),
            ],
            check=True,
            capture_output=True,
        )
        os.kill(os.getpid(), signal.SIGKILL)

    coordinator.land(actor, message="de-ordered control", checkpoint=checkpoint)


def _register_and_stage(
    git_repo: GitFixture,
    coordinator: Coordinator,
    actor: str,
    relative: str,
    content: str,
) -> Path:
    session = coordinator.register_session(actor, pid=os.getpid(), ttl=300)
    coordinator.claim(actor, [str(git_repo.root / relative)])
    git_repo.write(relative, content)
    git_repo.stage(session.index_path, relative)
    return session.index_path


def test_inv1_unclaimed_payload_refuses_whole_land_then_claimed_payload_lands(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    session = coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    coordinator.claim("alpha", [str(git_repo.root / "a.txt")])
    git_repo.write("a.txt", "a1\n")
    git_repo.write("b.txt", "b1\n")
    git_repo.stage(session.index_path, "a.txt", "b.txt")

    # RED control: the raw write-tree/commit-tree/update-ref sequence lands both
    # paths even though only a.txt is claimed.
    unsafe = git_repo.raw_commit(session.index_path, git_repo.base, "unsafe INV1")
    git_repo.git("update-ref", "refs/heads/main", unsafe, git_repo.base)
    assert git_repo.changed_paths(unsafe) == {"a.txt", "b.txt"}
    git_repo.git("update-ref", "refs/heads/main", git_repo.base, unsafe)

    with pytest.raises(LandingRefusedError) as refused:
        coordinator.land("alpha")
    assert refused.value.details["reason"] == "unclaimed_paths"
    assert refused.value.details["paths"] == ["b.txt"]
    assert git_repo.oid() == git_repo.base

    coordinator.claim("alpha", [str(git_repo.root / "b.txt")])
    landed = coordinator.land("alpha", message="claimed payload")
    assert git_repo.oid() == landed.commit
    assert git_repo.changed_paths(landed.commit) == {"a.txt", "b.txt"}


def test_inv2_release_between_audit_and_cas_breaks_fence_then_reclaim_allows(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    index = _register_and_stage(git_repo, coordinator, "alpha", "a.txt", "a1\n")
    unsafe = git_repo.raw_commit(index, git_repo.base, "unsafe INV2")
    released = False

    def release_at_fence(point: str, _context: Mapping[str, Any]) -> None:
        nonlocal released
        if point == "before_intent" and not released:
            released = True
            coordinator.release("alpha", [str(git_repo.root / "a.txt")])

    with pytest.raises(LandingRefusedError) as refused:
        coordinator.land("alpha", checkpoint=release_at_fence)
    assert refused.value.details["reason"] == "unclaimed_paths"
    assert git_repo.oid() == git_repo.base

    # RED control: a target-only CAS has no WAL fence and still succeeds after release.
    git_repo.git("update-ref", "refs/heads/main", unsafe, git_repo.base)
    assert git_repo.oid() == unsafe
    git_repo.git("update-ref", "refs/heads/main", git_repo.base, unsafe)

    coordinator.claim("alpha", [str(git_repo.root / "a.txt")])
    assert coordinator.land("alpha").commit == git_repo.oid()


def test_inv2_dead_session_refuses_then_live_session_allows(
    git_repo: GitFixture,
) -> None:
    sleeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    coordinator = Coordinator(git_repo.root)
    try:
        dead = coordinator.register_session("doomed", pid=sleeper.pid, ttl=300)
        coordinator.claim("doomed", [str(git_repo.root / "a.txt")])
        git_repo.write("a.txt", "dead payload\n")
        git_repo.stage(dead.index_path, "a.txt")
    finally:
        sleeper.kill()
        sleeper.wait(timeout=5)

    with pytest.raises(LandingRefusedError) as refused:
        coordinator.land("doomed")
    assert refused.value.details["reason"] == "session_inactive"
    assert git_repo.oid() == git_repo.base

    live = coordinator.register_session("live", pid=os.getpid(), ttl=300)
    coordinator.claim("live", [str(git_repo.root / "a.txt")])
    git_repo.stage(live.index_path, "a.txt")
    assert coordinator.land("live").commit == git_repo.oid()


def test_inv3_real_race_has_one_first_attempt_winner_and_both_payloads_land(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    alpha_index = _register_and_stage(git_repo, coordinator, "alpha", "a.txt", "a1\n")
    beta_index = _register_and_stage(git_repo, coordinator, "beta", "b.txt", "b1\n")

    # RED control: two raw one-shot CAS landers produce one winner and one lost payload.
    alpha_candidate = git_repo.raw_commit(alpha_index, git_repo.base, "raw alpha")
    beta_candidate = git_repo.raw_commit(beta_index, git_repo.base, "raw beta")
    context = multiprocessing.get_context("fork")
    raw_barrier = context.Barrier(2)
    raw_results = context.Queue()
    raw_processes = [
        context.Process(
            target=_raw_cas_worker,
            args=(str(git_repo.root), candidate, git_repo.base, raw_barrier, raw_results),
        )
        for candidate in (alpha_candidate, beta_candidate)
    ]
    for process in raw_processes:
        process.start()
    for process in raw_processes:
        process.join(timeout=10)
        assert process.exitcode == 0
    raw_codes = [raw_results.get(timeout=2) for _ in raw_processes]
    assert raw_codes.count(0) == 1
    assert sum(code != 0 for code in raw_codes) == 1
    raw_head = git_repo.oid()
    assert raw_head in {alpha_candidate, beta_candidate}
    git_repo.git("update-ref", "refs/heads/main", git_repo.base, raw_head)

    barrier = context.Barrier(2)
    results = context.Queue()
    processes = [
        context.Process(
            target=_land_worker,
            args=(str(git_repo.root), actor, barrier, results),
        )
        for actor in ("alpha", "beta")
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0

    outcomes = [results.get(timeout=2) for _ in processes]
    assert all("error" not in outcome for outcome in outcomes), outcomes
    attempts = sorted(int(outcome["attempts"]) for outcome in outcomes)
    assert attempts[0] == 1
    assert attempts[1] >= 2
    head = git_repo.oid()
    commits = {str(outcome["commit"]) for outcome in outcomes}
    assert all(
        git_repo.git("merge-base", "--is-ancestor", commit, head, check=False).returncode == 0
        for commit in commits
    )
    assert git_repo.git("rev-list", "--count", f"{git_repo.base}..{head}").stdout.strip() == "2"
    assert git_repo.git("show", f"{head}:a.txt").stdout == "a1\n"
    assert git_repo.git("show", f"{head}:b.txt").stdout == "b1\n"


def test_inv5_stale_full_tree_has_foreign_hunk_but_overlay_is_exact_and_conflict_refuses(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    index = _register_and_stage(git_repo, coordinator, "alpha", "a.txt", "a1\n")
    external = git_repo.external_commit("b.txt", "b-external\n", "external b")

    # RED control: merely re-parenting the stale session tree reverts b.txt.
    unsafe = git_repo.raw_commit(index, external, "unsafe stale reparent")
    assert git_repo.changed_paths(unsafe) == {"a.txt", "b.txt"}

    landed = coordinator.land("alpha", message="exact overlay")
    assert git_repo.changed_paths(landed.commit) == {"a.txt"}
    assert git_repo.git("show", f"{landed.commit}:b.txt").stdout == "b-external\n"
    assert landed.payload == ("a.txt",)

    coordinator.release("alpha")
    beta = coordinator.register_session("beta", pid=os.getpid(), ttl=300)
    coordinator.claim("beta", [str(git_repo.root / "a.txt")])
    git_repo.write("a.txt", "a-beta\n")
    git_repo.stage(beta.index_path, "a.txt")
    conflicting = git_repo.external_commit("a.txt", "a-external\n", "external a")
    with pytest.raises(LandingRefusedError) as refused:
        coordinator.land("beta")
    assert refused.value.details["reason"] == "payload_conflict"
    assert refused.value.details["paths"] == ["a.txt"]
    assert git_repo.oid() == conflicting


def test_inv2_session_death_between_audit_and_fence_refuses(
    git_repo: GitFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The landing-moment liveness re-sample is the only thing that can catch this.

    The session is alive when the top-of-loop audit reads it and dies before the
    fence, so a refusal here can come from nowhere else. The unsafe control at
    the end disables exactly that sub-term and lands a dead session's payload.
    """

    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    coordinator = Coordinator(git_repo.root)
    session = coordinator.register_session("doomed", pid=sleeper.pid, ttl=300)
    coordinator.claim("doomed", [str(git_repo.root / "a.txt")])
    git_repo.write("a.txt", "fence payload\n")
    git_repo.stage(session.index_path, "a.txt")

    def kill_session(point: str, _context: Mapping[str, Any]) -> None:
        if point == "before_intent" and sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait(timeout=5)

    with pytest.raises(LandingRefusedError) as refused:
        coordinator.land("doomed", checkpoint=kill_session)
    assert refused.value.details["reason"] == "session_died_at_fence"
    assert git_repo.oid() == git_repo.base
    reasons = [
        event.data.get("reason")
        for event in coordinator.inspect_wal().events
        if event.kind == "refused"
    ]
    assert reasons == ["session_died_at_fence"]

    # RED control (mutation M1): with the landing-moment liveness sub-term
    # force-disabled and only the expiry sub-term left, the same dead session
    # lands its payload on the branch.
    monkeypatch.setattr(
        core,
        "_session_alive",
        lambda session, now: bool(session.expires_at > now),
    )
    unsafe = coordinator.land("doomed", message="M1 control")
    assert git_repo.oid() == unsafe.commit
    assert git_repo.git("show", f"{unsafe.commit}:a.txt").stdout == "fence payload\n"


#: Every window in which SIGKILL can land during the write-ahead protocol's ref
#: updates, with the recovery outcome each one must produce. ``nth``/``mode``
#: select the matched invocation and whether its rename completed first.
_CRASH_WINDOWS = [
    pytest.param("refs/onetree/wal", 1, "before", 0, 0, id="intent-not-appended"),
    pytest.param("refs/onetree/wal", 1, "after", 0, 1, id="intent-appended"),
    pytest.param("refs/heads/main", 1, "before", 0, 1, id="branch-not-moved"),
    pytest.param("refs/heads/main", 1, "after", 1, 0, id="branch-moved"),
    pytest.param("refs/onetree/wal", 2, "before", 1, 0, id="landed-not-appended"),
    pytest.param("refs/onetree/wal", 2, "after", 0, 0, id="landed-appended"),
]


@pytest.mark.parametrize(("reference", "nth", "mode", "repaired", "refused"), _CRASH_WINDOWS)
def test_inv7_kill_inside_each_ref_update_recovers_to_consistent_refs(
    git_repo: GitFixture,
    kill_git: KillGit,
    reference: str,
    nth: int,
    mode: str,
    repaired: int,
    refused: int,
) -> None:
    coordinator = Coordinator(git_repo.root)
    _register_and_stage(git_repo, coordinator, "alpha", "a.txt", "a1\n")

    context = multiprocessing.get_context("fork")
    process = context.Process(
        target=_crash_lander,
        args=(
            str(git_repo.root),
            "alpha",
            str(kill_git.executable),
            kill_git.environment(reference, nth, mode),
        ),
    )
    process.start()
    process.join(timeout=20)
    assert process.exitcode == -signal.SIGKILL

    # The invariant the write-ahead order buys: no crash window can put a broker
    # landing on the branch that the WAL has no record of.
    assert coordinator.inspect_wal().divergences == ()

    recovery = coordinator.recover()
    assert recovery.consistent
    assert recovery.pending == 0
    assert (recovery.repaired, recovery.refused) == (repaired, refused)

    if git_repo.oid() == git_repo.base:
        coordinator.land("alpha", message="recovered retry")

    report = coordinator.inspect_wal()
    kinds = [event.kind for event in report.events]
    assert report.consistent
    assert report.pending_intents == 0
    assert report.divergences == ()
    assert kinds.count("landed") == 1
    counted = git_repo.git("rev-list", "--count", f"{git_repo.base}..{git_repo.oid()}")
    assert counted.stdout.strip() == "1"
    assert git_repo.git("show", f"{git_repo.oid()}:a.txt").stdout == "a1\n"


def test_inv7_de_ordered_protocol_leaves_an_unrecorded_landing_that_is_detected(
    git_repo: GitFixture,
) -> None:
    """RED control: move the branch before recording the intent, then die.

    This is the shape Round A's two-ref transaction produced when SIGKILL fell
    between Git's two loose-ref renames. The shipped order cannot reach it --
    every window in the matrix above reports no divergence -- and when it is
    reached anyway, inspection and recovery must both name it.
    """

    coordinator = Coordinator(git_repo.root)
    _register_and_stage(git_repo, coordinator, "alpha", "a.txt", "a1\n")

    context = multiprocessing.get_context("fork")
    process = context.Process(target=_deordered_lander, args=(str(git_repo.root), "alpha"))
    process.start()
    process.join(timeout=20)
    assert process.exitcode == -signal.SIGKILL

    assert git_repo.oid() != git_repo.base
    report = coordinator.inspect_wal()
    assert report.pending_intents == 0
    assert not report.consistent
    assert [item.kind for item in report.divergences] == ["unrecorded_landing"]
    assert report.divergences[0].commit == git_repo.oid()

    wal_before = coordinator.wal.head()
    with pytest.raises(UnrecordedLandingError) as diverged:
        coordinator.recover()
    assert diverged.value.details["divergences"][0]["kind"] == "unrecorded_landing"
    assert coordinator.wal.head() == wal_before
    recorded = [
        event
        for event in coordinator.inspect_wal().events
        if event.kind == "refused" and event.data.get("reason") == "branch_diverged"
    ]
    assert recorded == []
