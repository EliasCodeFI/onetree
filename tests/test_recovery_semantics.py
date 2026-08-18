"""Round AF2: what recovery may touch, and what it is allowed to call clean.

Every test here pairs a guard with the known-bad input it was built for. Where
the bad input can be produced in-process the control lives in the test and is
asserted to produce the defect; where it cannot, the mutation is named in
docs/ROUND-AF2.md with the fixture it turns red.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from conftest import GitFixture, KillGit, install_kill_git

from onetree import core as core_module
from onetree import git as git_module
from onetree.cli import main
from onetree.core import Coordinator
from onetree.errors import (
    GitError,
    LandingRefusedError,
    RefCorruptError,
    RefLockedError,
    ReleaseRefusedError,
    WalAppendRefusedError,
    WalCorruptError,
    WalDivergedError,
)
from onetree.git import Repository
from onetree.proc import process_alive, process_start
from onetree.wal import WAL_REF

#: A pid no process on this machine holds, with a birth tick that cannot match.
DEAD_LANDER = {"pid": 999_999_999, "start": 1}
_GIT_EXECUTABLE = shutil.which("git") or "git"


def _repository_with_ref_backend(tmp_path: Path, ref_format: str) -> GitFixture:
    """Build the same committed repository over either supported ref backend."""

    root = tmp_path / ref_format
    subprocess.run(
        [
            _GIT_EXECUTABLE,
            "init",
            "--quiet",
            f"--ref-format={ref_format}",
            "--initial-branch=main",
            root,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    fixture = GitFixture(root=root, base="")
    fixture.git("config", "user.name", "Round AF3 Tests")
    fixture.git("config", "user.email", "round-af3@example.invalid")
    fixture.write("a.txt", "a0\n")
    fixture.write("b.txt", "b0\n")
    fixture.git("add", "--", "a.txt", "b.txt")
    fixture.git("commit", "--quiet", "-m", "baseline")
    return GitFixture(root=root, base=fixture.oid())


def _advance_with_ordinary_commits(
    coordinator: Coordinator,
    start: str,
    count: int,
) -> str:
    """Advance main with tree-identical, non-broker commits."""

    tree = coordinator.repo.git_text("show", "-s", "--format=%T", start)
    head = start
    for index in range(count):
        head = coordinator.repo.commit_tree(tree, f"ordinary {index}", (head,))
    assert coordinator.repo.update_ref("refs/heads/main", head, start)
    return head


def _register(coordinator: Coordinator, git_repo: GitFixture, actor: str, relative: str) -> Any:
    session = coordinator.register_session(actor, pid=os.getpid(), ttl=300)
    coordinator.claim(actor, [str(git_repo.root / relative)])
    return session


def _stage(git_repo: GitFixture, session: Any, relative: str, content: str) -> None:
    git_repo.write(relative, content)
    git_repo.stage(session.index_path, relative)


def _unrecorded_landing(coordinator: Coordinator, parent: str, operation: str) -> str:
    """Place a broker-trailered commit on the branch with no WAL record at all.

    This is the de-ordered crash shape: the branch moved before anything was
    written ahead of it.
    """

    repository = coordinator.repo
    tree = repository.git_text("show", "-s", "--format=%T", parent)
    commit = repository.commit_tree(
        tree,
        repository.landing_message("onetree land: ghost", operation, "e" * 32, "ghost"),
        (parent,),
    )
    assert repository.update_ref("refs/heads/main", commit, parent)
    return commit


def _pending_intent(
    git_repo: GitFixture,
    coordinator: Coordinator,
    session: Any,
    *,
    operation: str = "b" * 32,
    lander: Mapping[str, Any] | None = None,
    tagless: bool = False,
    generation: str | None = None,
    move_target: bool = False,
) -> str:
    """Leave the state a lander leaves when it dies after write-ahead step 1."""

    claim = coordinator.claim("alpha", [str(git_repo.root / "a.txt")]).claims[0]
    tree = coordinator.repo.write_tree(session.index_path)
    candidate = coordinator.repo.commit_tree(
        tree,
        coordinator.repo.landing_message("onetree land: alpha", operation, "c" * 32, "alpha"),
        (git_repo.base,),
    )
    data: dict[str, Any] = {
        "operation": operation,
        "attempt": 1,
        "target": "refs/heads/main",
        "expected_head": git_repo.base,
        "candidate": candidate,
        "tree": tree,
        "session_generation": session.generation if generation is None else generation,
        "lander": dict(DEAD_LANDER if lander is None else lander),
        "fences": [{"path": "a.txt", "claim_generation": claim.generation}],
        "payload": [
            {
                "path": "a.txt",
                "old": coordinator.repo.tree_entries(git_repo.base)["a.txt"].payload(),
                "new": coordinator.repo.tree_entries(tree)["a.txt"].payload(),
            }
        ],
    }
    if tagless:
        # Every intent written before Round AF2 looks like this.
        del data["lander"]
    intent = coordinator.wal.document("land_intent", "alpha", data)
    snapshot = coordinator.wal.read()
    assert coordinator.wal.try_append((intent,), snapshot.head) is not None
    if move_target:
        assert coordinator.repo.update_ref("refs/heads/main", candidate, git_repo.base)
    return candidate


def _cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


# --------------------------------------------------------------------------
# D1 -- a landing in flight is not a landing to refuse
# --------------------------------------------------------------------------


def test_recovery_run_during_a_live_landing_leaves_it_alone(
    git_repo: GitFixture,
) -> None:
    """Write-ahead made "intent appended, branch not moved" a normal state.

    An operator running ``onetree wal --recover`` in that window used to refuse
    the intent and report success while the lander went on to win its branch
    CAS -- after which every read of the WAL raised, forever.
    """

    lander = Coordinator(git_repo.root)
    operator = Coordinator(git_repo.root)  # a second process in spirit
    session = _register(lander, git_repo, "alpha", "a.txt")
    _stage(git_repo, session, "a.txt", "racing\n")
    seen: dict[str, Any] = {}

    def recover_at_the_window(point: str, _context: Mapping[str, Any]) -> None:
        if point == "after_intent" and "recovery" not in seen:
            seen["recovery"] = operator.recover()

    result = lander.land("alpha", message="racing land", checkpoint=recover_at_the_window)
    recovery = seen["recovery"]
    assert (recovery.consistent, recovery.pending) == (False, 1)
    assert (recovery.repaired, recovery.refused) == (0, 0)
    assert git_repo.oid() == result.commit

    # Every entry point still reads, which is the whole finding.
    assert operator.inspect_wal().consistent
    assert operator.recover().consistent
    assert operator.check("alpha", str(git_repo.root / "a.txt")).owned
    operator.gc("collector")
    operator.register_session("beta", pid=os.getpid(), ttl=300)


def test_a_recovery_that_ignores_liveness_is_stopped_at_the_wal_door(
    git_repo: GitFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED control for the pair: with the first guard removed, the second holds.

    Mutation M15 disables the in-flight bar, so the same interleave refuses a
    live landing's intent mid-flight. The lander then wins its branch CAS and
    goes to record the landing -- the exact write that used to brick the WAL.
    It is refused at the door and typed, and the resulting state is a named
    divergence rather than a WAL that raises from every read path forever.
    """

    lander = Coordinator(git_repo.root)
    operator = Coordinator(git_repo.root)
    session = _register(lander, git_repo, "alpha", "a.txt")
    _stage(git_repo, session, "a.txt", "racing\n")
    monkeypatch.setattr(Coordinator, "_landing_in_flight", staticmethod(lambda *_: False))

    def recover_at_the_window(point: str, _context: Mapping[str, Any]) -> None:
        if point == "after_intent":
            assert operator.recover().refused == 1

    with pytest.raises(WalAppendRefusedError, match="refused its landed mark"):
        lander.land("alpha", message="racing land", checkpoint=recover_at_the_window)

    report = operator.inspect_wal()
    assert [item.kind for item in report.divergences] == ["refused_candidate_on_target"]
    assert report.pending_intents == 0
    with pytest.raises(WalDivergedError):
        operator.recover()


def test_a_landing_recovery_completed_first_is_this_landings_own_outcome(
    git_repo: GitFixture,
) -> None:
    """The other half of the D1 window: recovery repairs while the mark is in flight.

    Recovery is allowed to finish a landing whose branch CAS has already won --
    that is the crash-window repair, and it cannot know the lander is alive
    after the branch moved. The lander's own mark is then a duplicate. An
    identical record is this call's outcome, not a conflict, so ``land``
    returns normally; a *different* record for the same operation still refuses.
    """

    lander = Coordinator(git_repo.root)
    operator = Coordinator(git_repo.root)
    session = _register(lander, git_repo, "alpha", "a.txt")
    _stage(git_repo, session, "a.txt", "repaired underneath\n")
    seen: dict[str, Any] = {}

    def repair_at_the_window(point: str, _context: Mapping[str, Any]) -> None:
        if point == "after_branch":
            seen["recovery"] = operator.recover()

    result = lander.land("alpha", message="repaired underneath", checkpoint=repair_at_the_window)

    assert seen["recovery"].repaired == 1
    assert git_repo.oid() == result.commit
    report = operator.inspect_wal()
    assert report.consistent
    assert report.divergences == ()
    # One landing, one mark: the lander's duplicate was refused and swallowed.
    landed = [event for event in report.events if event.kind == "landed"]
    assert len(landed) == 1
    assert landed[0].data["recovered"] is True
    assert landed[0].data["commit"] == result.commit

    # ...and only an identical record counts as this call's outcome. A mark that
    # disagrees on any pinned field is someone else's landing, or a forgery.
    forged = lander.wal.document("landed", "alpha", {**landed[0].data, "commit": git_repo.base})
    assert not lander._landing_recorded(result.operation, forged)


def test_a_landed_mark_with_no_matching_intent_is_refused_at_write_time(
    git_repo: GitFixture,
) -> None:
    """The brick shape, reproduced both ways: refused as a write, fatal as a read."""

    coordinator = Coordinator(git_repo.root)
    session = _register(coordinator, git_repo, "alpha", "a.txt")
    _stage(git_repo, session, "a.txt", "pending\n")
    candidate = _pending_intent(git_repo, coordinator, session)
    assert coordinator.recover().refused == 1  # the intent is now closed as refused

    intent = next(
        event
        for event in coordinator.wal.read().events
        if event.kind == "land_intent" and event.data.get("operation") == "b" * 32
    )
    mark = coordinator.wal.document(
        "landed",
        "alpha",
        {
            "operation": "b" * 32,
            "intent": intent.event_id,
            "target": "refs/heads/main",
            "expected_head": git_repo.base,
            "commit": candidate,
            "tree": intent.data["tree"],
            "payload": ["a.txt"],
            "recovered": False,
        },
    )
    snapshot = coordinator.wal.read()
    state = coordinator._fold(snapshot)
    with pytest.raises(WalAppendRefusedError, match="cannot replay"):
        coordinator._try_append(state, snapshot, (mark,))
    assert coordinator.inspect_wal().pending_intents == 0

    # RED control: the same document through the un-validated primitive -- the
    # only path a forger has, and the path the broker itself used to take.
    assert coordinator.wal.try_append((mark,), snapshot.head) is not None
    for probe in (
        coordinator.inspect_wal,
        coordinator.recover,
        lambda: coordinator.check("alpha", str(git_repo.root / "a.txt")),
        lambda: coordinator.gc("collector"),
        lambda: coordinator.register_session("beta", pid=os.getpid(), ttl=300),
    ):
        with pytest.raises(WalCorruptError, match="landed event has no matching intent"):
            probe()


# --------------------------------------------------------------------------
# D2 -- an unreadable ref is not an absent one
# --------------------------------------------------------------------------


@pytest.mark.parametrize("shape", ["eacces", "eisdir"])
def test_an_unreadable_wal_ref_is_never_read_as_an_empty_healthy_wal(
    git_repo: GitFixture,
    shape: str,
) -> None:
    coordinator = Coordinator(git_repo.root)
    _register(coordinator, git_repo, "alpha", "a.txt")
    assert coordinator.check("beta", str(git_repo.root / "a.txt")).blocked
    reference = coordinator.repo.git_path(WAL_REF)

    if shape == "eacces":
        reference.chmod(0o000)
        if os.access(reference, os.R_OK):  # pragma: no cover - only true for root
            reference.chmod(0o644)
            pytest.skip("EACCES cannot be fixtured for a user that ignores the mode bits")
    else:
        reference.unlink()
        reference.mkdir()

    try:
        # RED control: both probes exit 1, and neither one's stderr names the
        # ref, so the shipped discriminator -- exit status plus a search for the
        # ref name in git's message -- had nothing to separate this from absent.
        for revision in (f"{WAL_REF}^{{commit}}", WAL_REF):
            probe = git_repo.git(
                "rev-parse", "--verify", "--quiet", "--end-of-options", revision, check=False
            )
            assert probe.returncode == 1
            assert WAL_REF not in probe.stderr

        for call in (
            lambda: coordinator.check("beta", str(git_repo.root / "a.txt")),
            coordinator.inspect_wal,
            coordinator.recover,
            lambda: coordinator.claim("alpha", [str(git_repo.root / "b.txt")]),
            lambda: coordinator.gc("collector"),
        ):
            with pytest.raises(WalCorruptError, match="cannot be read"):
                call()
    finally:
        if shape == "eacces":
            reference.chmod(0o644)
        else:
            reference.rmdir()


def test_ref_storage_answers_present_or_absent_from_storage_not_from_git_text(
    git_repo: GitFixture,
) -> None:
    """The discriminator reads ref storage, in both of Git's two backends.

    ``ref_storage_state`` is asked only after Git has declined to resolve the
    ref, and its answer is "nothing is stored here" or a reason. A ref that is
    stored -- loose or packed -- must never answer absent, because absent is the
    one answer that lets an unreadable WAL read as a healthy empty one.
    """

    repository = Repository(git_repo.root)
    assert repository.ref_storage_state("refs/heads/nowhere") is None
    assert repository.ref_storage_state("refs/heads/main") == (
        "the ref file is present but Git cannot resolve it"
    )

    git_repo.git("pack-refs", "--all")
    assert not repository.git_path("refs/heads/main").exists()
    assert repository.ref_storage_state("refs/heads/main") == (
        "packed-refs names the ref but Git cannot resolve it"
    )
    assert repository.ref_storage_state("refs/heads/nowhere") is None
    assert repository.resolve_ref("refs/heads/main") == git_repo.base

    packed = repository.git_path("packed-refs")
    packed.chmod(0o000)
    try:
        if os.access(packed, os.R_OK):  # pragma: no cover - only true for root
            pytest.skip("EACCES cannot be fixtured for a user that ignores the mode bits")
        state = repository.ref_storage_state("refs/heads/gone")
        assert state is not None
        assert "packed-refs cannot be read" in state
    finally:
        packed.chmod(0o644)


@pytest.mark.parametrize("ref_format", ["files", "reftable"])
def test_both_ref_backends_refuse_unreadable_wal_storage_and_report_their_lock(
    tmp_path: Path,
    ref_format: str,
) -> None:
    """The files arm is the control; reftable must carry the same guarantees."""

    git_repo = _repository_with_ref_backend(tmp_path, ref_format)
    coordinator = Coordinator(git_repo.root)
    repository = coordinator.repo
    assert repository.resolve_ref("refs/heads/unborn", missing_ok=True) is None
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    coordinator.claim("alpha", [str(git_repo.root / "a.txt")])

    if ref_format == "files":
        storage = [repository.git_path(WAL_REF)]
        expected_lock = repository.git_path(f"{WAL_REF}.lock")
    else:
        reftable = repository.git_dir / "reftable"
        storage = [reftable / "tables.list", *sorted(reftable.glob("*.ref"))]
        expected_lock = reftable / "tables.list.lock"
    for path in storage:
        path.chmod(0o000)
    try:
        assert all(not os.access(path, os.R_OK) for path in storage)
        with pytest.raises(RefCorruptError, match="cannot be read"):
            repository.resolve_ref(WAL_REF, missing_ok=True)
        for call in (
            coordinator.inspect_wal,
            coordinator.recover,
            lambda: coordinator.check("beta", str(git_repo.root / "a.txt")),
        ):
            with pytest.raises(WalCorruptError, match="cannot be read"):
                call()
    finally:
        for path in storage:
            path.chmod(0o644)

    assert repository.ref_lock_path(WAL_REF) == expected_lock
    expected_lock.write_text("", encoding="utf-8")
    try:
        wal_head = repository.resolve_ref(WAL_REF)
        assert wal_head is not None
        with pytest.raises(RefLockedError, match="locked") as refused:
            repository.update_ref(WAL_REF, wal_head, wal_head)
        assert refused.value.details["lock"] == str(expected_lock)
        assert str(expected_lock) in coordinator.inspect_wal().ref_locks
    finally:
        expected_lock.unlink()


def test_a_ref_created_after_both_probes_is_retried_not_called_corrupt(
    git_repo: GitFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A healthy peer may create the WAL between rev-parse and storage inspection."""

    repository = Repository(git_repo.root)
    reference = "refs/onetree/racing"
    original = repository.ref_storage_state

    def create_before_storage_observation(observed: str) -> str | None:
        assert observed == reference
        git_repo.git("update-ref", reference, git_repo.base)
        return original(observed)

    monkeypatch.setattr(repository, "ref_storage_state", create_before_storage_observation)
    assert repository.resolve_ref(reference, missing_ok=True) == git_repo.base


def test_readable_but_malformed_reftable_storage_is_not_read_as_absent(
    tmp_path: Path,
) -> None:
    git_repo = _repository_with_ref_backend(tmp_path, "reftable")
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    table_list = coordinator.repo.git_dir / "reftable" / "tables.list"
    table_name = table_list.read_text(encoding="utf-8").splitlines()[0]
    table = table_list.parent / table_name
    original = table.read_bytes()
    table.write_bytes(b"garbage")
    try:
        with pytest.raises(RefCorruptError, match="cannot be read"):
            coordinator.repo.resolve_ref(WAL_REF, missing_ok=True)
        with pytest.raises(WalCorruptError, match="cannot be read"):
            coordinator.inspect_wal()
    finally:
        table.write_bytes(original)


# --------------------------------------------------------------------------
# D3 -- the branch-side audit is bounded by the WAL, not retired by use
# --------------------------------------------------------------------------


@pytest.mark.parametrize("order", ["first_sight", "later_registration", "later_landing"])
def test_an_unrecorded_landing_survives_the_routine_act_that_used_to_mask_it(
    git_repo: GitFixture,
    order: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Found by both verification seats, in two independent reproductions.

    The scan excluded every position the WAL had ever seen, and a crash-left
    commit becomes such a position the moment anyone registers on top of it or
    lands above it. Detection therefore expired through ordinary use.
    """

    coordinator = Coordinator(git_repo.root)
    if order != "first_sight":
        coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    unrecorded = _unrecorded_landing(coordinator, git_repo.base, "f" * 32)
    if order != "first_sight":
        assert [item.kind for item in coordinator.inspect_wal().divergences] == [
            "unrecorded_landing"
        ]

    if order == "later_landing":
        session = _register(coordinator, git_repo, "beta", "b.txt")
        _stage(git_repo, session, "b.txt", "legit\n")
        landed = coordinator.land("beta", message="one more legit landing")
        assert git_repo.git(
            "merge-base", "--is-ancestor", unrecorded, landed.commit, check=False
        ).returncode == 0
    else:
        coordinator.register_session("beta", pid=os.getpid(), ttl=300)

    report = coordinator.inspect_wal()
    assert [item.kind for item in report.divergences] == ["unrecorded_landing"]
    assert report.divergences[0].commit == unrecorded
    assert not report.consistent
    with pytest.raises(WalDivergedError):
        coordinator.recover()

    # RED control (mutation M16): restore the shipped floor -- every position
    # the WAL has ever seen -- and the same on-disk state reads clean. This is
    # also what keeps the empty floor falsifiable: a floor is one monkeypatch
    # away, and installing one loses the finding.
    monkeypatch.setattr(
        Coordinator,
        "_scan_floor",
        lambda _self, state, target: tuple(state.target_positions[target]),
    )
    assert coordinator.inspect_wal().consistent
    assert coordinator.recover().consistent


def test_an_unrecorded_landing_beyond_one_scan_page_is_still_reported(
    git_repo: GitFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A page boundary bounds one Git result, never the audit's correctness."""

    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    unrecorded = _unrecorded_landing(coordinator, git_repo.base, "f" * 32)
    for index in range(3):
        git_repo.external_commit("b.txt", f"hand {index}\n", f"hand commit {index}")
    monkeypatch.setattr(git_module, "DIVERGENCE_SCAN_LIMIT", 2)

    report = coordinator.inspect_wal()
    kinds = [item.kind for item in report.divergences]
    assert kinds == ["unrecorded_landing"]
    assert not report.consistent
    assert report.divergences[0].commit == unrecorded
    with pytest.raises(WalDivergedError):
        coordinator.recover()

    # The marked commit is below the first page and really is on the target.
    assert git_repo.git(
        "merge-base", "--is-ancestor", unrecorded, git_repo.oid(), check=False
    ).returncode == 0


def test_honest_history_past_the_scan_page_never_becomes_inconsistent(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    _advance_with_ordinary_commits(
        coordinator,
        git_repo.base,
        git_module.DIVERGENCE_SCAN_LIMIT + 4,
    )

    assert coordinator.inspect_wal().consistent
    assert coordinator.recover().consistent


def test_the_commit_immediately_below_first_sight_stays_inside_the_audit(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    unrecorded = _unrecorded_landing(coordinator, git_repo.base, "f" * 32)
    git_repo.external_commit("b.txt", "hand\n", "permitted hand commit")
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)

    report = coordinator.inspect_wal()
    assert [item.kind for item in report.divergences] == ["unrecorded_landing"]
    assert report.divergences[0].commit == unrecorded


# --------------------------------------------------------------------------
# D4/D5/D6 -- what recovery and inspection are allowed to claim
# --------------------------------------------------------------------------


def test_recovery_reports_the_target_branch_locks_it_must_not_delete(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    session = _register(coordinator, git_repo, "alpha", "a.txt")
    _stage(git_repo, session, "a.txt", "locked\n")
    _pending_intent(git_repo, coordinator, session)
    branch_lock = coordinator.repo.ref_lock_path("refs/heads/main")
    branch_lock.write_text("", encoding="utf-8")

    # RED control: the gate recovery used to run -- locks on the WAL ref alone
    # -- cannot see this at all, which is why it reported an all-clear.
    assert [lock for lock in coordinator.repo.ref_lock_paths(WAL_REF) if lock.exists()] == []

    recovery = coordinator.recover()
    assert recovery.locks == (str(branch_lock),)
    assert branch_lock.exists()  # report, never delete
    assert (recovery.repaired, recovery.refused, recovery.pending) == (0, 1, 0)

    code, out, _ = _cli("wal", "--repo", str(git_repo.root), "--recover", "--json")
    assert code == 0
    assert json.loads(out)["recovery"]["locks"] == [str(branch_lock)]

    # A second run over the fully settled state still names it: the lock is what
    # refuses the next landing whether or not anything was left to recover.
    code, human, _ = _cli("wal", "--repo", str(git_repo.root), "--recover")
    assert code == 0
    assert "ref lock(s) held" in human
    assert str(branch_lock) in human
    assert "repaired=0, refused=0, pending=0" in human

    branch_lock.unlink()
    assert coordinator.recover().locks == ()


def test_the_wal_human_line_agrees_with_its_exit_code_and_its_json(
    git_repo: GitFixture,
) -> None:
    """A pending intent is the commonest post-crash state and used to print clean."""

    coordinator = Coordinator(git_repo.root)
    session = _register(coordinator, git_repo, "alpha", "a.txt")
    _stage(git_repo, session, "a.txt", "pending\n")

    code, human, _ = _cli("wal", "--repo", str(git_repo.root))
    assert code == 0
    assert "consistent" in human
    assert "INCONSISTENT" not in human

    _pending_intent(git_repo, coordinator, session)
    code, human, _ = _cli("wal", "--repo", str(git_repo.root))
    assert code == 1
    assert "INCONSISTENT" in human
    assert "1 pending intent(s)" in human
    code, out, _ = _cli("wal", "--repo", str(git_repo.root), "--json")
    payload = json.loads(out)["wal"]
    assert (code, payload["consistent"], payload["pending_intents"]) == (1, False, 1)


def test_recovery_counters_are_measurements_and_reach_the_human_line(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    session = _register(coordinator, git_repo, "alpha", "a.txt")
    _stage(git_repo, session, "a.txt", "rolled forward\n")
    _pending_intent(git_repo, coordinator, session, move_target=True)

    code, human, _ = _cli("wal", "--repo", str(git_repo.root), "--recover")
    assert code == 0
    # A recovery that completed a landing must not print like a no-op.
    assert "repaired=1" in human
    assert coordinator.inspect_wal().consistent


def test_recovery_reports_an_in_flight_landing_as_pending_and_exits_one(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    session = _register(coordinator, git_repo, "alpha", "a.txt")
    _stage(git_repo, session, "a.txt", "in flight\n")
    _pending_intent(
        git_repo,
        coordinator,
        session,
        lander={"pid": os.getpid(), "start": None},
    )

    code, human, _ = _cli("wal", "--repo", str(git_repo.root), "--recover")
    assert code == 1
    assert "pending=1" in human
    assert "still in flight" in human
    code, out, err = _cli("wal", "--repo", str(git_repo.root), "--recover", "--json")
    assert err == ""
    payload = json.loads(out)
    assert (code, payload["ok"], payload["recovery"]["divergences"]) == (1, False, [])
    assert coordinator.inspect_wal().pending_intents == 1


def test_a_live_landing_defers_repair_but_never_defers_the_divergence_audit(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    session = _register(coordinator, git_repo, "alpha", "a.txt")
    _stage(git_repo, session, "a.txt", "landed then removed\n")
    landed = coordinator.land("alpha", message="recorded landing")
    git_repo.git("update-ref", "refs/heads/main", git_repo.base, landed.commit)
    _pending_intent(
        git_repo,
        coordinator,
        session,
        operation="d" * 32,
        lander={"pid": os.getpid(), "start": None},
    )

    report = coordinator.inspect_wal()
    assert report.pending_intents == 1
    assert [item.kind for item in report.divergences] == ["landed_not_on_target"]
    with pytest.raises(WalDivergedError) as refused:
        coordinator.recover()
    assert refused.value.details["pending"] == 1
    assert refused.value.details["divergences"][0]["kind"] == "landed_not_on_target"

    code, out, err = _cli("wal", "--repo", str(git_repo.root), "--recover")
    assert (code, out) == (2, "")
    assert "branch and the WAL disagree" in err
    assert "pending=1 landing(s) still in flight" in err

    code, out, err = _cli(
        "wal",
        "--repo",
        str(git_repo.root),
        "--recover",
        "--json",
    )
    assert err == ""
    payload = json.loads(out)
    assert code == 2
    assert payload["ok"] is False
    assert payload["recovery"]["pending"] == 1
    assert payload["recovery"]["divergences"][0]["kind"] == "landed_not_on_target"


def test_lander_birth_tick_is_part_of_the_in_flight_bar(
    git_repo: GitFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = Coordinator(git_repo.root)
    session = _register(coordinator, git_repo, "alpha", "a.txt")
    _stage(git_repo, session, "a.txt", "birth tick\n")
    _pending_intent(
        git_repo,
        coordinator,
        session,
        lander={"pid": os.getpid(), "start": 12345},
    )
    observed: list[tuple[int, int | None]] = []

    def alive_only_for_the_recorded_incarnation(pid: int, start: int | None) -> bool:
        observed.append((pid, start))
        return pid == os.getpid() and start == 12345

    monkeypatch.setattr(core_module, "_pid_alive", alive_only_for_the_recorded_incarnation)
    result = coordinator.recover()
    assert result.pending == 1
    assert observed == [(os.getpid(), 12345)]

    # RED control: the bare-pid mutation revives the wrong process incarnation
    # as live and changes recovery's measured outcome.
    monkeypatch.setattr(
        Coordinator,
        "_landing_in_flight",
        staticmethod(
            lambda _state, intent, _now: core_module._pid_alive(
                int(intent.data["lander"]["pid"]), None
            )
        ),
    )
    mutated = coordinator.recover()
    assert mutated.pending == 0
    assert mutated.refused == 1


def test_a_degraded_lander_surfaces_the_same_warning_as_a_degraded_session(
    git_repo: GitFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = Coordinator(git_repo.root)
    session = _register(coordinator, git_repo, "alpha", "a.txt")
    _stage(git_repo, session, "a.txt", "degraded lander\n")
    monkeypatch.setattr(core_module, "_pid_start", lambda _pid: None)

    landed = coordinator.land("alpha")
    assert landed.warnings == (core_module.DEGRADED_LIVENESS,)
    intent = next(event for event in coordinator.wal.read().events if event.kind == "land_intent")
    assert intent.data["lander"]["warnings"] == [core_module.DEGRADED_LIVENESS]


def test_a_pruned_landed_object_names_the_restore_path_before_recovery_can_audit(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    session = _register(coordinator, git_repo, "alpha", "a.txt")
    _stage(git_repo, session, "a.txt", "pruned object\n")
    landed = coordinator.land("alpha")
    git_repo.git("update-ref", "refs/heads/main", git_repo.base, landed.commit)
    object_path = git_repo.root / ".git" / "objects" / landed.commit[:2] / landed.commit[2:]
    compressed = object_path.read_bytes()
    object_path.unlink()

    for call in (coordinator.inspect_wal, coordinator.recover):
        with pytest.raises(WalCorruptError, match="restore") as refused:
            call()
        assert refused.value.details["commit"] == landed.commit

    object_path.write_bytes(compressed)
    assert [item.kind for item in coordinator.inspect_wal().divergences] == [
        "landed_not_on_target"
    ]


# --------------------------------------------------------------------------
# D8 -- one refusal per divergence, not per probe
# --------------------------------------------------------------------------


def test_probing_a_diverged_repository_records_one_refusal_per_divergence(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    first = _unrecorded_landing(coordinator, git_repo.base, "f" * 32)

    def refusals() -> list[Any]:
        return [
            event
            for event in coordinator.wal.read().events
            if event.kind == "refused" and event.data.get("reason") == "branch_diverged"
        ]

    for _ in range(3):
        with pytest.raises(WalDivergedError):
            coordinator.recover()
    assert len(refusals()) == 1

    # A different divergence set is a different fact and is recorded again.
    _unrecorded_landing(coordinator, first, "a" * 32)
    with pytest.raises(WalDivergedError):
        coordinator.recover()
    assert len(refusals()) == 2
    with pytest.raises(WalDivergedError):
        coordinator.recover()
    assert len(refusals()) == 2


# --------------------------------------------------------------------------
# D9 -- a landing that cannot move the branch does not leave its intent behind
# --------------------------------------------------------------------------


def test_a_locked_target_branch_closes_the_intent_written_ahead_of_it(
    git_repo: GitFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = Coordinator(git_repo.root)
    session = _register(coordinator, git_repo, "alpha", "a.txt")
    _stage(git_repo, session, "a.txt", "locked out\n")
    branch_lock = coordinator.repo.ref_lock_path("refs/heads/main")
    branch_lock.write_text("", encoding="utf-8")

    with pytest.raises(RefLockedError, match="locked"):
        coordinator.land("alpha")
    report = coordinator.inspect_wal()
    assert report.pending_intents == 0
    assert [
        event.data.get("reason") for event in report.events if event.kind == "refused"
    ] == ["target_ref_locked"]
    # The claim is free again: a stranded intent fences its own session's claims.
    assert coordinator.release("alpha", [str(git_repo.root / "a.txt")]).released == ("a.txt",)

    # RED control (mutation M18): leave the intent open on the way out and the
    # session is fenced against releasing the claim it holds -- visible, at
    # least, on the human line the same round taught to name pending intents.
    coordinator.claim("alpha", [str(git_repo.root / "a.txt")])
    monkeypatch.setattr(Coordinator, "_close_intent", lambda *args, **kwargs: None)
    with pytest.raises(RefLockedError):
        coordinator.land("alpha")
    assert coordinator.inspect_wal().pending_intents == 1
    with pytest.raises(ReleaseRefusedError) as fenced:
        coordinator.release("alpha", [str(git_repo.root / "a.txt")])
    assert fenced.value.details["reason"] == "pending_land"
    code, human, _ = _cli("wal", "--repo", str(git_repo.root))
    assert code == 1
    assert "1 pending intent(s)" in human
    branch_lock.unlink()


# --------------------------------------------------------------------------
# Suspicions carried by the verification seats
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("target", "refs/tags/not-a-branch"),
        ("expected_head", "not-an-oid"),
        ("candidate", "not-an-oid"),
        ("tree", "not-an-oid"),
    ],
)
def test_all_four_git_validators_refuse_an_unreplayable_append_at_the_wal_door(
    git_repo: GitFixture,
    field: str,
    value: str,
) -> None:
    coordinator = Coordinator(git_repo.root)
    session = coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    claim = coordinator.claim("alpha", [str(git_repo.root / "a.txt")]).claims[0]
    index = coordinator.repo.ensure_index("alpha", git_repo.base)
    tree = coordinator.repo.write_tree(index)
    candidate = coordinator.repo.commit_tree(tree, "candidate", (git_repo.base,))
    entry = coordinator.repo.tree_entries(git_repo.base)["a.txt"].payload()
    data: dict[str, Any] = {
        "operation": "9" * 32,
        "attempt": 1,
        "target": "refs/heads/main",
        "expected_head": git_repo.base,
        "candidate": candidate,
        "tree": tree,
        "session_generation": session.generation,
        "lander": dict(DEAD_LANDER),
        "fences": [{"path": "a.txt", "claim_generation": claim.generation}],
        "payload": [{"path": "a.txt", "old": entry, "new": entry}],
    }
    data[field] = value
    document = coordinator.wal.document("land_intent", "alpha", data)
    snapshot = coordinator.wal.read()

    with pytest.raises(WalAppendRefusedError, match="cannot replay"):
        coordinator._try_append(coordinator._fold(snapshot), snapshot, (document,))


def _flaky_git(directory: Path, failures: int) -> Path:
    """A git shim that fails the first N ``update-ref`` calls without a lock file."""

    executable = directory / "git"
    executable.write_text(
        "#!/bin/bash\n"
        "set -u\n"
        'for argument in "$@"; do\n'
        '  if [ "$argument" = update-ref ]; then\n'
        "    seen=0\n"
        '    if [ -f "$ONETREE_TEST_FLAKY_STATE" ]; then seen=$(<"$ONETREE_TEST_FLAKY_STATE"); fi\n'
        "    seen=$((seen + 1))\n"
        '    printf \'%s\' "$seen" > "$ONETREE_TEST_FLAKY_STATE"\n'
        '    if [ "$seen" -le "$ONETREE_TEST_FLAKY_FAILURES" ]; then\n'
        "      echo \"fatal: cannot lock ref: Unable to create lock file: File exists\" >&2\n"
        "      exit 1\n"
        "    fi\n"
        "  fi\n"
        "done\n"
        'exec "$ONETREE_TEST_REAL_GIT" "$@"\n',
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def _flaky_worker(root: str, wrapper: str, environment: dict[str, str], results: Any) -> None:
    install_kill_git(wrapper, environment)
    repository = Repository(Path(root))
    head = repository.require_commit("refs/heads/main")
    tree = repository.git_text("show", "-s", "--format=%T", head)
    candidate = repository.commit_tree(tree, "contended candidate", (head,))
    try:
        results.put({"won": repository.update_ref("refs/heads/main", candidate, head)})
    except Exception as error:  # noqa: BLE001 - reported through the child result
        results.put({"error": type(error).__name__, "message": str(error)})


@pytest.mark.parametrize(
    ("failures", "expected"),
    [(2, {"won": True}), (99, {"error": "GitError"})],
)
def test_lock_contention_that_clears_is_retried_rather_than_misattributed(
    git_repo: GitFixture,
    tmp_path: Path,
    failures: int,
    expected: dict[str, Any],
) -> None:
    """A peer that releases its lock between the failure and the probe left no lock.

    ``update_ref`` decided "genuine Git failure" from the absence of a lock file
    at probe time, so contention that cleared in that window was reported as an
    unretryable error. Every non-moving failure is retried within the bounded
    window instead; a failure that is genuinely permanent still ends as
    ``GitError``, which is the second parameter here.
    """

    import multiprocessing

    directory = tmp_path / "flakygit"
    directory.mkdir()
    wrapper = _flaky_git(directory, failures)
    environment = {
        "ONETREE_TEST_REAL_GIT": os.environ.get("ONETREE_TEST_REAL_GIT") or _real_git(),
        "ONETREE_TEST_FLAKY_STATE": str(tmp_path / "flaky.count"),
        "ONETREE_TEST_FLAKY_FAILURES": str(failures),
    }
    context = multiprocessing.get_context("fork")
    results = context.Queue()
    process = context.Process(
        target=_flaky_worker,
        args=(str(git_repo.root), str(wrapper), environment, results),
    )
    process.start()
    process.join(timeout=30)
    assert process.exitcode == 0
    outcome = results.get(timeout=5)
    assert {key: outcome.get(key) for key in expected} == expected
    # No lock file was ever present: the shim refuses without taking one, which
    # is exactly the window the old discrimination could not see.
    assert not coordinator_lock_exists(git_repo)


def _real_git() -> str:
    import shutil

    return shutil.which("git") or "git"


def coordinator_lock_exists(git_repo: GitFixture) -> bool:
    repository = Repository(git_repo.root)
    return any(lock.exists() for lock in repository.ref_lock_paths("refs/heads/main"))


def test_infrastructure_failure_while_verifying_a_landing_is_not_called_wal_corruption(
    git_repo: GitFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = Coordinator(git_repo.root)
    session = _register(coordinator, git_repo, "alpha", "a.txt")
    _stage(git_repo, session, "a.txt", "verified\n")
    coordinator.land("alpha", message="verified landing")

    def broken(self: Repository, reference: str) -> str:
        raise GitError("could not execute git: injected", operation="rev-parse")

    monkeypatch.setattr(Repository, "require_commit", broken)
    # RED control: the shipped except-arm caught GitError alongside
    # RefCorruptError, so a broken toolchain was reported as a corrupt WAL and
    # sent the reader to repair the wrong thing.
    with pytest.raises(GitError, match="injected"):
        coordinator.inspect_wal()


@pytest.mark.parametrize(
    "temp_indexes",
    ["index-1-1-abc", [1], [None], [["index-1-1-abc"]], {"path": "index-1-1-abc"}],
)
def test_a_gc_event_whose_temp_index_list_is_the_wrong_shape_is_corruption(
    git_repo: GitFixture,
    temp_indexes: Any,
) -> None:
    """The fold validated every other list in the gc event, and not this one.

    A string is iterable, so ``temp_indexes: "index-1-1-abc"`` folded silently
    into fifteen single-character removals; the other shapes reached the
    comparison as unhashable or non-string values.
    """

    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    document = coordinator.wal.document(
        "gc",
        "alpha",
        {"claim_generations": [], "session_generations": [], "temp_indexes": temp_indexes},
    )
    snapshot = coordinator.wal.read()
    state = coordinator._fold(snapshot)
    with pytest.raises(WalAppendRefusedError, match="temp_indexes"):
        coordinator._try_append(state, snapshot, (document,))

    # RED control: written through the un-validated primitive, the shape has to
    # be refused on the way back out -- a fold that accepts it reports removals
    # that never happened, or raises an untyped TypeError from the comparison.
    assert coordinator.wal.try_append((document,), snapshot.head) is not None
    with pytest.raises(WalCorruptError, match="temp_indexes"):
        coordinator.inspect_wal()


def test_recovery_resolves_more_stranded_intents_than_the_cas_retry_budget(
    git_repo: GitFixture,
) -> None:
    """One CAS per intent spent the retry budget on progress rather than contention."""

    coordinator = Coordinator(git_repo.root)
    session = coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    coordinator.claim("alpha", [str(git_repo.root / "a.txt")])
    _stage(git_repo, session, "a.txt", "many\n")
    tree = coordinator.repo.write_tree(session.index_path)
    entry = coordinator.repo.tree_entries(git_repo.base)["a.txt"].payload()
    new_entry = coordinator.repo.tree_entries(tree)["a.txt"].payload()
    claim = coordinator._fold(coordinator.wal.read()).active_claims(0.0)[0]
    intents = tuple(
        coordinator.wal.document(
            "land_intent",
            "alpha",
            {
                "operation": f"{index:032x}",
                "attempt": 1,
                "target": "refs/heads/main",
                "expected_head": git_repo.base,
                "candidate": coordinator.repo.commit_tree(
                    tree,
                    coordinator.repo.landing_message(
                        "onetree land: alpha", f"{index:032x}", "c" * 32, "alpha"
                    ),
                    (git_repo.base,),
                ),
                "tree": tree,
                "session_generation": session.generation,
                "lander": dict(DEAD_LANDER),
                "fences": [{"path": "a.txt", "claim_generation": claim.generation}],
                "payload": [{"path": "a.txt", "old": entry, "new": new_entry}],
            },
        )
        for index in range(40)
    )
    snapshot = coordinator.wal.read()
    assert coordinator.wal.try_append(intents, snapshot.head) is not None
    assert coordinator.inspect_wal().pending_intents == 40

    recovery = coordinator.recover()
    assert (recovery.consistent, recovery.pending, recovery.refused) == (True, 0, 40)


def test_a_dead_session_still_bars_recovery_from_touching_a_live_landers_intent(
    git_repo: GitFixture,
) -> None:
    """Liveness is asked of the landing, not of the session that started it.

    The two come apart in both directions: a CLI invocation dies while its
    session body keeps running (the crash windows), and a session body dies
    while the process it launched is still mid-landing (this one).
    """

    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    coordinator = Coordinator(git_repo.root)
    session = coordinator.register_session("alpha", pid=sleeper.pid, ttl=300)
    coordinator.claim("alpha", [str(git_repo.root / "a.txt")])
    _stage(git_repo, session, "a.txt", "outliving its session\n")
    _pending_intent(
        git_repo,
        coordinator,
        session,
        lander={"pid": os.getpid(), "start": None},
    )
    sleeper.kill()
    sleeper.wait(timeout=5)

    # The session body is gone; the process performing the landing is not, and
    # it is the one that can still move the branch.
    assert coordinator.recover().pending == 1
    assert coordinator.recover().refused == 0


def test_an_intent_with_no_lander_recorded_falls_back_to_its_session(
    git_repo: GitFixture,
) -> None:
    """Every intent written before this round looks like this, so it must work."""

    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    coordinator = Coordinator(git_repo.root)
    session = coordinator.register_session("alpha", pid=sleeper.pid, ttl=300)
    coordinator.claim("alpha", [str(git_repo.root / "a.txt")])
    _stage(git_repo, session, "a.txt", "no lander tag\n")
    _pending_intent(git_repo, coordinator, session, operation="b" * 32, tagless=True)

    # A live session is the only evidence such an intent carries, so it stands.
    assert (coordinator.recover().pending, coordinator.recover().refused) == (1, 0)

    # An intent naming a generation this session no longer has cannot be the
    # work of anything running, whatever the session is doing.
    _pending_intent(
        git_repo,
        coordinator,
        session,
        operation="d" * 32,
        tagless=True,
        generation="0" * 32,
    )
    recovery = coordinator.recover()
    assert (recovery.pending, recovery.refused) == (1, 1)

    sleeper.kill()
    sleeper.wait(timeout=5)
    assert coordinator.recover().refused == 1
    assert coordinator.inspect_wal().pending_intents == 0


# --------------------------------------------------------------------------
# Round AF4 -- a falsifiable landing bar, and crash states that classify
# --------------------------------------------------------------------------


def _arm_kill_git(
    monkeypatch: pytest.MonkeyPatch,
    kill_git: KillGit,
    *,
    reference: str,
    nth: int,
    mode: str,
) -> None:
    """Point this process's Git layer at the shim, reversibly.

    ``install_kill_git`` edits the interpreter for good, which is why the
    PPID-kill tests spend a forked child on it. ``self_after`` kills the git
    process and leaves the broker running, so it is observed in-process and
    every mutation has to be undone afterwards.
    """

    for name, value in kill_git.environment(reference, nth, mode).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PATH", f"{kill_git.executable.parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(git_module, "_GIT_EXECUTABLE", str(kill_git.executable))


def test_a_healthy_landing_records_the_birth_tick_it_will_be_judged_by(
    git_repo: GitFixture,
) -> None:
    """The write half of the in-flight bar, which nothing observed.

    ``test_lander_birth_tick_is_part_of_the_in_flight_bar`` pins the read half:
    recovery must ask liveness with the tick the intent recorded. No fixture
    watched a healthy landing *record* one, so ``lander_start = None`` at the
    write site left all 134 tests green while every landing in production
    silently degraded to the bare-pid bar -- under which a recycled pid keeps a
    dead lander's intent in flight forever, which is the hazard the whole
    in-flight guarantee rests on.
    """

    coordinator = Coordinator(git_repo.root)
    session = _register(coordinator, git_repo, "alpha", "a.txt")
    _stage(git_repo, session, "a.txt", "healthy lander\n")
    # proc.py reads Linux's birth tick and the suite's liveness model needs it;
    # its absence is a failure to report, never a case to skip past.
    expected_start = process_start(os.getpid())
    assert expected_start is not None

    landed = coordinator.land("alpha", message="healthy landing")

    assert landed.warnings == ()
    intent = next(
        event
        for event in coordinator.wal.read().events
        if event.kind == "land_intent" and event.data.get("operation") == landed.operation
    )
    lander = intent.data["lander"]
    assert lander["pid"] == os.getpid()
    assert lander["start"] == expected_start
    assert lander["warnings"] == []
    # ...and what was recorded is the tick liveness is actually decided by, not
    # merely an integer: the next incarnation of this pid must not answer for
    # this one.
    assert process_alive(lander["pid"], lander["start"])
    assert not process_alive(lander["pid"], lander["start"] + 1)


@pytest.mark.parametrize("ref_format", ["files", "reftable"])
def test_a_git_death_after_the_branch_cas_applied_completes_the_landing(
    tmp_path: Path,
    ref_format: str,
    kill_git: KillGit,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The third crash state ``land()``'s own docstring said could not exist.

    ``git update-ref`` still has work to do after the rename that commits a
    single-ref update. A death in that window (OOM kill, SIGKILL) returns a
    non-zero status over an update that already applied, and the ref then holds
    this invocation's own candidate. Reading that as a lost race refuses an
    intent whose candidate is on the branch, which leaves the WAL diverged on
    ``refused_candidate_on_target`` permanently -- over data that is correct.
    """

    git_repo = _repository_with_ref_backend(tmp_path, ref_format)
    coordinator = Coordinator(git_repo.root)
    session = _register(coordinator, git_repo, "alpha", "a.txt")
    _stage(git_repo, session, "a.txt", "killed mid update-ref\n")
    _arm_kill_git(
        monkeypatch, kill_git, reference="refs/heads/main", nth=1, mode="self_after"
    )

    result = coordinator.land("alpha", message="killed mid update-ref")

    assert result.attempts == 1
    assert git_repo.oid() == result.commit
    assert git_repo.git("show", "refs/heads/main:a.txt").stdout == "killed mid update-ref\n"
    events = coordinator.wal.read().events
    landed = [event for event in events if event.kind == "landed"]
    assert [event.data["operation"] for event in landed] == [result.operation]
    assert landed[0].data["recovered"] is False
    # No refusal of any kind: the landing succeeded and is recorded as such.
    assert [event.data.get("reason") for event in events if event.kind == "refused"] == []
    report = coordinator.inspect_wal()
    assert report.consistent
    assert report.divergences == ()
    assert coordinator.recover().consistent


def test_the_shipped_cas_reading_turns_that_death_into_a_false_payload_conflict(
    git_repo: GitFixture,
    kill_git: KillGit,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED control for the pair: restore the conflation, the harm comes back.

    ``update_ref`` classified every failed invocation whose ref had moved as a
    lost race, without asking whether it had moved to this call's own candidate.
    """

    coordinator = Coordinator(git_repo.root)
    session = _register(coordinator, git_repo, "alpha", "a.txt")
    _stage(git_repo, session, "a.txt", "killed mid update-ref\n")
    monkeypatch.setattr(Repository, "_reapplied", lambda _self, _new, _observed: False)
    _arm_kill_git(
        monkeypatch, kill_git, reference="refs/heads/main", nth=1, mode="self_after"
    )

    with pytest.raises(LandingRefusedError) as refused:
        coordinator.land("alpha", message="killed mid update-ref")

    assert refused.value.details["reason"] == "payload_conflict"
    # The landing the caller was told was refused is on the branch, with the
    # right bytes -- that is what makes the refusal false rather than merely
    # unlucky.
    intent = next(
        event for event in coordinator.wal.read().events if event.kind == "land_intent"
    )
    assert git_repo.oid() == intent.data["candidate"]
    assert git_repo.git("show", "refs/heads/main:a.txt").stdout == "killed mid update-ref\n"
    assert [item.kind for item in coordinator.inspect_wal().divergences] == [
        "refused_candidate_on_target"
    ]
    # Nothing retires a `refused` event, so this is permanent: recovery raises
    # on every call, exit 2, over a repository whose data is fine.
    for _ in range(2):
        with pytest.raises(WalDivergedError):
            coordinator.recover()


@pytest.mark.parametrize("ref_format", ["files", "reftable"])
@pytest.mark.parametrize("hand_commits", [0, 1, 2, 3, 5])
def test_a_crash_left_landing_is_found_under_any_number_of_hand_commits(
    tmp_path: Path,
    ref_format: str,
    hand_commits: int,
) -> None:
    """The scan floor's depth was never the defect; the floor was.

    Lead (c) measured the mask at one permitted hand commit and the floor moved
    down one generation; AF3b then measured the same mask at two and at three.
    A floor a fixed genealogical distance below first sight is defeated by that
    many hand commits, so the class only closes when the audit stops excluding
    history it has never read.
    """

    git_repo = _repository_with_ref_backend(tmp_path, ref_format)
    coordinator = Coordinator(git_repo.root)
    unrecorded = _unrecorded_landing(coordinator, git_repo.base, "f" * 32)
    for index in range(hand_commits):
        git_repo.external_commit("b.txt", f"hand {index}\n", f"permitted hand commit {index}")
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)

    report = coordinator.inspect_wal()
    assert [item.kind for item in report.divergences] == ["unrecorded_landing"]
    assert report.divergences[0].commit == unrecorded
    assert not report.consistent
    with pytest.raises(WalDivergedError):
        coordinator.recover()


@pytest.mark.parametrize("hand_commits", [1, 2, 3, 4, 5, 6])
def test_page_boundaries_never_drop_the_commit_they_land_on(
    git_repo: GitFixture,
    hand_commits: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contiguity across ``--skip`` pages, swept over every boundary offset.

    With a page size of two the walk crosses a boundary after rows 2, 4 and 6,
    so a seam that double-counted or skipped one row would lose the marked
    commit at exactly one of these offsets and report a clean branch.
    """

    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    unrecorded = _unrecorded_landing(coordinator, git_repo.base, "f" * 32)
    for index in range(hand_commits):
        git_repo.external_commit("b.txt", f"hand {index}\n", f"hand commit {index}")
    monkeypatch.setattr(git_module, "DIVERGENCE_SCAN_LIMIT", 2)

    report = coordinator.inspect_wal()
    assert [item.kind for item in report.divergences] == ["unrecorded_landing"]
    assert report.divergences[0].commit == unrecorded


@pytest.mark.parametrize(
    ("start", "warnings"),
    [
        (4242, [core_module.DEGRADED_LIVENESS]),
        (None, []),
        (4242, ["something_else"]),
        (4242, "degraded_liveness"),
    ],
)
def test_a_lander_whose_warnings_contradict_its_birth_tick_is_refused(
    git_repo: GitFixture,
    start: int | None,
    warnings: Any,
) -> None:
    """The warnings validator had no observed-failing input in the suite.

    ``(None, [])`` is the harmful direction: an intent that degraded to bare-pid
    liveness while reporting itself healthy, which is exactly what an operator
    reading the WAL would have to trust.
    """

    coordinator = Coordinator(git_repo.root)
    session = coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    claim = coordinator.claim("alpha", [str(git_repo.root / "a.txt")]).claims[0]
    index = coordinator.repo.ensure_index("alpha", git_repo.base)
    tree = coordinator.repo.write_tree(index)
    candidate = coordinator.repo.commit_tree(tree, "candidate", (git_repo.base,))
    entry = coordinator.repo.tree_entries(git_repo.base)["a.txt"].payload()
    document = coordinator.wal.document(
        "land_intent",
        "alpha",
        {
            "operation": "9" * 32,
            "attempt": 1,
            "target": "refs/heads/main",
            "expected_head": git_repo.base,
            "candidate": candidate,
            "tree": tree,
            "session_generation": session.generation,
            "lander": {"pid": os.getpid(), "start": start, "warnings": warnings},
            "fences": [{"path": "a.txt", "claim_generation": claim.generation}],
            "payload": [{"path": "a.txt", "old": entry, "new": entry}],
        },
    )
    snapshot = coordinator.wal.read()

    with pytest.raises(WalAppendRefusedError, match="lander.warnings is invalid"):
        coordinator._try_append(coordinator._fold(snapshot), snapshot, (document,))

    # RED control: through the un-validated primitive -- the only path a forger
    # has -- the same document has to be refused on the way back out.
    assert coordinator.wal.try_append((document,), snapshot.head) is not None
    with pytest.raises(WalCorruptError, match="lander.warnings is invalid"):
        coordinator.inspect_wal()


def test_a_zero_byte_reftable_table_list_refuses_instead_of_answering_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The next D1 variant, and the one reftable can actually decide.

    ``show-ref --exists`` answers "no table names this ref" with the same status
    for a truncated table list as for a ref that was never written, while the
    WAL's bytes sit in the orphaned ``*.ref`` tables beside it. Trusting that
    status without looking at storage answers every claim question over an
    empty log at exit 0.
    """

    git_repo = _repository_with_ref_backend(tmp_path, "reftable")
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    coordinator.claim("alpha", [str(git_repo.root / "a.txt")])
    assert coordinator.check("beta", str(git_repo.root / "a.txt")).blocked

    table_list = coordinator.repo.git_dir / "reftable" / "tables.list"
    original = table_list.read_bytes()
    # A fresh reftable repository always writes a non-empty list, so zero bytes
    # is not a state Git ever leaves behind -- it is decidable, not ambiguous.
    assert original
    table_list.write_bytes(b"")
    try:
        # Git's own verdict on this state, measured rather than assumed: the ref
        # reads absent and both integrity checks bless the repository, so the
        # refusal below is strictly stronger than anything Git will say.
        assert git_repo.git("show-ref", "--exists", WAL_REF, check=False).returncode == 2
        assert git_repo.git("fsck", check=False).returncode == 0
        assert git_repo.git("refs", "verify", check=False).returncode == 0
        assert sorted(table_list.parent.glob("*.ref"))

        with pytest.raises(RefCorruptError, match="cannot be read"):
            coordinator.repo.resolve_ref(WAL_REF, missing_ok=True)
        for call in (
            coordinator.inspect_wal,
            coordinator.recover,
            lambda: coordinator.check("beta", str(git_repo.root / "a.txt")),
        ):
            with pytest.raises(WalCorruptError, match="cannot be read"):
                call()

        # RED control (B-F5): gut `_reftable_storage_state` to `return None` --
        # the shape that left its whole body decision-dead -- and the live WAL
        # reads as an empty healthy one, at exit 0, with the claim gone.
        monkeypatch.setattr(Repository, "_reftable_storage_state", lambda _self: None)
        assert coordinator.repo.resolve_ref(WAL_REF, missing_ok=True) is None
        assert coordinator.inspect_wal().events == ()
        assert not coordinator.check("beta", str(git_repo.root / "a.txt")).blocked
    finally:
        table_list.write_bytes(original)
    assert coordinator.check("beta", str(git_repo.root / "a.txt")).blocked


def test_a_deleted_loose_wal_ref_still_answers_absent_on_the_files_backend(
    tmp_path: Path,
) -> None:
    """The files control: there the same question is genuinely undecidable.

    Nothing on the files backend distinguishes "this loose ref was deleted" from
    "it was never written", so absent remains the only honest answer and the
    reftable refusal must not leak across.
    """

    git_repo = _repository_with_ref_backend(tmp_path, "files")
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    coordinator.repo.git_path(WAL_REF).unlink()

    assert coordinator.repo.resolve_ref(WAL_REF, missing_ok=True) is None
    assert coordinator.inspect_wal().events == ()


@pytest.mark.parametrize("ref_format", ["files", "reftable"])
def test_registering_on_an_unborn_branch_refuses_the_same_on_both_backends(
    tmp_path: Path,
    ref_format: str,
) -> None:
    """Absent must stay absent on reftable now that it is taken from storage."""

    git_repo = _repository_with_ref_backend(tmp_path, ref_format)
    coordinator = Coordinator(git_repo.root)

    assert coordinator.repo.resolve_ref("refs/heads/unborn", missing_ok=True) is None
    with pytest.raises(GitError, match="landing target is unborn"):
        coordinator.register_session(
            "alpha", pid=os.getpid(), ttl=300, target="refs/heads/unborn"
        )


def test_land_warnings_reach_both_cli_output_modes(
    git_repo: GitFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Degradation the operator never sees is degradation that was not surfaced.

    Both output modes carried the warning and neither was asserted anywhere, so
    dropping the JSON key and the human suffix together left the suite green.
    """

    coordinator = Coordinator(git_repo.root)
    alpha = _register(coordinator, git_repo, "alpha", "a.txt")
    _stage(git_repo, alpha, "a.txt", "degraded through the cli\n")
    beta = _register(coordinator, git_repo, "beta", "b.txt")
    _stage(git_repo, beta, "b.txt", "degraded through the cli as well\n")
    monkeypatch.setattr(core_module, "_pid_start", lambda _pid: None)

    code, out, err = _cli("land", "--repo", str(git_repo.root), "--session", "alpha", "--json")
    assert (code, err) == (0, "")
    assert len(out.strip().splitlines()) == 1  # exactly one JSON object on stdout
    assert json.loads(out)["land"]["warnings"] == [core_module.DEGRADED_LIVENESS]

    code, human, err = _cli("land", "--repo", str(git_repo.root), "--session", "beta")
    assert (code, err) == (0, "")
    assert human.rstrip("\n").endswith(f"[warning: {core_module.DEGRADED_LIVENESS}]")
