"""Round A landing invariants, including real races and process death."""

from __future__ import annotations

import multiprocessing
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from conftest import GitFixture

from onetree.core import Coordinator
from onetree.errors import LandingRefusedError

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

    def checkpoint(point: str) -> None:
        nonlocal waited
        if point == "before_transaction" and not waited:
            waited = True
            barrier.wait(timeout=10)

    try:
        result = coordinator.land(actor, message=f"land {actor}", checkpoint=checkpoint)
    except Exception as error:  # pragma: no cover - asserted through the child result
        results.put({"error": f"{type(error).__name__}: {error}"})
    else:
        results.put({"actor": actor, "attempts": result.attempts, "commit": result.commit})


def _kill_lander(root: str, actor: str, point_to_kill: str) -> None:
    coordinator = Coordinator(Path(root))

    def checkpoint(point: str) -> None:
        if point == point_to_kill:
            os.kill(os.getpid(), signal.SIGKILL)

    coordinator.land(actor, message="kill fixture", checkpoint=checkpoint)


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

    def release_at_fence(point: str) -> None:
        nonlocal released
        if point == "before_transaction" and not released:
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


@pytest.mark.parametrize(
    "kill_point",
    ["after_audit", "after_tree", "before_transaction", "after_transaction"],
)
def test_inv7_kill_at_each_land_phase_recovers_to_consistent_refs(
    git_repo: GitFixture,
    kill_point: str,
) -> None:
    coordinator = Coordinator(git_repo.root)
    index = _register_and_stage(git_repo, coordinator, "alpha", "a.txt", "a1\n")

    if kill_point == "after_audit":
        # RED control: the old split-ref shape can expose the branch update with
        # no corresponding WAL landing event.
        unsafe = git_repo.raw_commit(index, git_repo.base, "unsafe split refs")
        git_repo.git("update-ref", "refs/heads/main", unsafe, git_repo.base)
        assert git_repo.oid() == unsafe
        assert not any(event.kind == "landed" for event in coordinator.inspect_wal().events)
        git_repo.git("update-ref", "refs/heads/main", git_repo.base, unsafe)

    context = multiprocessing.get_context("fork")
    process = context.Process(
        target=_kill_lander,
        args=(str(git_repo.root), "alpha", kill_point),
    )
    process.start()
    process.join(timeout=10)
    assert process.exitcode == -signal.SIGKILL

    recovery = coordinator.recover()
    assert recovery.consistent
    assert recovery.pending == 0
    if git_repo.oid() == git_repo.base:
        coordinator.land("alpha", message="recovered retry")

    report = coordinator.inspect_wal()
    kinds = [event.kind for event in report.events]
    assert report.consistent
    assert report.pending_intents == 0
    assert kinds.count("land_intent") == kinds.count("landed") == 1
    assert git_repo.git("show", f"{git_repo.oid()}:a.txt").stdout == "a1\n"
