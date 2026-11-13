"""What recovery may touch, and what it is allowed to call clean.

Every test here pairs a guard with the known-bad input it was built for. Where
the bad input can be produced in-process the control lives in the test and is
asserted to produce the defect.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from conftest import GitFixture, KillGit, admit_gate, install_kill_git

from onetree import core as core_module
from onetree import git as git_module
from onetree.cli import main
from onetree.core import Coordinator
from onetree.errors import (
    GitError,
    LandingRefusedError,
    OnetreeError,
    RefCorruptError,
    RefLockedError,
    ReleaseRefusedError,
    UnsupportedWorktreeError,
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


def _admit_fixture_gate(root: Path) -> None:
    """Admit the autouse fixture's gate, as `git_repo` does for its own repository."""

    admit_gate(root, Path(os.environ["ONETREE_COMMIT_PAYLOAD"]))


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
    fixture.git("config", "user.name", "Recovery Tests")
    fixture.git("config", "user.email", "recovery@example.invalid")
    _admit_fixture_gate(root)
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


def _marked_commit(
    coordinator: Coordinator,
    reference: str,
    operation: str,
    parents: Sequence[str],
    *,
    tree_from: str | None = None,
    extra: str = "",
) -> str:
    """Put one broker-marked commit on ``reference`` without touching the WAL."""

    repository = coordinator.repo
    source = tree_from or (parents[0] if parents else repository.require_commit("HEAD"))
    tree = repository.git_text("show", "-s", "--format=%T", source)
    commit = repository.commit_tree(
        tree,
        f"unrecorded landing\n\nOnetree-Operation: {operation}{extra}\n",
        parents,
    )
    previous = repository.resolve_ref(reference, missing_ok=True)
    assert repository.update_ref(reference, commit, previous)
    return commit


def _make_commit_graph_entry_parentless(commit_graph: Path, commit: str) -> None:
    """Make one SHA-1 commit-graph row a checksum-valid false root."""

    content = bytearray(commit_graph.read_bytes())
    assert content[:6] == b"CGPH\x01\x01"
    chunks: dict[bytes, int] = {}
    for index in range(content[6] + 1):
        start = 8 + index * 12
        chunk_id = bytes(content[start : start + 4])
        chunks[chunk_id] = struct.unpack(">Q", content[start + 4 : start + 12])[0]
    fanout = chunks[b"OIDF"]
    commit_count = struct.unpack(">I", content[fanout + 255 * 4 : fanout + 256 * 4])[0]
    oid_lookup = chunks[b"OIDL"]
    commit_data = chunks[b"CDAT"]
    oids = [
        bytes(content[oid_lookup + index * 20 : oid_lookup + (index + 1) * 20])
        for index in range(commit_count)
    ]
    row = commit_data + oids.index(bytes.fromhex(commit)) * 36
    parent_slots = row + 20
    assert content[parent_slots : parent_slots + 4] != b"\x70\x00\x00\x00"
    content[parent_slots : parent_slots + 8] = struct.pack(">II", 0x70000000, 0x70000000)
    content[-20:] = hashlib.sha1(content[:-20], usedforsecurity=False).digest()
    commit_graph.chmod(0o644)
    commit_graph.write_bytes(content)


def _make_midx_entry_point_to(midx: Path, victim: str, decoy: str) -> None:
    """Make one MIDX row return another object's bytes, preserving its checksum."""

    content = bytearray(midx.read_bytes())
    assert content[:5] == b"MIDX\x01"
    oid_version = content[5]
    if oid_version == 1:
        hash_length = 20
        digest = hashlib.sha1  # noqa: S324 - Git's repository format requires SHA-1
    elif oid_version == 2:
        hash_length = 32
        digest = hashlib.sha256
    else:  # pragma: no cover - Repository rejects unknown object formats first
        raise AssertionError(f"unsupported MIDX object format {oid_version}")

    chunks: dict[bytes, int] = {}
    for index in range(content[6] + 1):
        start = 12 + index * 12
        chunk_id = bytes(content[start : start + 4])
        chunks[chunk_id] = struct.unpack(">Q", content[start + 4 : start + 12])[0]
    fanout = chunks[b"OIDF"]
    object_count = struct.unpack(">I", content[fanout + 255 * 4 : fanout + 256 * 4])[0]
    oid_lookup = chunks[b"OIDL"]
    oids = [
        bytes(content[oid_lookup + index * hash_length : oid_lookup + (index + 1) * hash_length])
        for index in range(object_count)
    ]
    victim_index = oids.index(bytes.fromhex(victim))
    decoy_index = oids.index(bytes.fromhex(decoy))
    offsets = chunks[b"OOFF"]
    decoy_entry = bytes(content[offsets + decoy_index * 8 : offsets + (decoy_index + 1) * 8])
    assert decoy_entry[4] & 0x80 == 0, "fixture requires a directly encoded pack offset"
    content[offsets + victim_index * 8 : offsets + (victim_index + 1) * 8] = decoy_entry
    content[-hash_length:] = digest(content[:-hash_length], usedforsecurity=False).digest()
    midx.write_bytes(content)


def _published_broker_repository(
    tmp_path: Path,
    *,
    hand_commits: int = 0,
) -> tuple[GitFixture, Path, str]:
    """Publish broker history through ordinary Git, which deliberately omits the WAL."""

    source = _repository_with_ref_backend(tmp_path / "source", "files")
    coordinator = Coordinator(source.root)
    session = _register(coordinator, source, "alpha", "a.txt")
    _stage(source, session, "a.txt", "broker landing before clone\n")
    landed = coordinator.land("alpha", message="broker landing before clone").commit
    for index in range(hand_commits):
        source.external_commit("b.txt", f"hand {index}\n", f"hand commit {index}")
    source.git("branch", "clean", source.base)

    remote = tmp_path / "published.git"
    subprocess.run(
        [_GIT_EXECUTABLE, "init", "--bare", "--quiet", "--initial-branch=main", remote],
        check=True,
        capture_output=True,
        text=True,
    )
    source.git("remote", "add", "published", str(remote))
    source.git("push", "--quiet", "published", "refs/heads/main", "refs/heads/clean")
    published_refs = subprocess.run(
        [_GIT_EXECUTABLE, "-C", str(remote), "for-each-ref", "--format=%(refname)"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert published_refs == ["refs/heads/clean", "refs/heads/main"]
    return source, remote, landed


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
        # Every intent written before landers were recorded looks like this.
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


def test_a_persisted_refusal_stops_a_landed_mark_at_the_wal_door(
    git_repo: GitFixture,
) -> None:
    lander = Coordinator(git_repo.root)
    operator = Coordinator(git_repo.root)
    session = _register(lander, git_repo, "alpha", "a.txt")
    _stage(git_repo, session, "a.txt", "racing\n")
    refused_head: list[str | None] = []

    def persist_refusal_at_the_window(point: str, context: Mapping[str, Any]) -> None:
        if point == "after_intent":
            recovery = operator.recover()
            assert recovery.pending == 1
            assert recovery.refused == 0
            snapshot = operator.wal.read()
            intent = next(
                event for event in snapshot.events
                if event.kind == "land_intent" and event.data["operation"] == context["operation"]
            )
            refused = operator.wal.document("refused", "alpha", {
                "action": "land", "reason": "incomplete_intent", "paths": [],
                "operation": intent.data["operation"], "intent": intent.event_id,
            })
            assert operator.wal.try_append((refused,), snapshot.head) is not None
            refused_head.append(operator.wal.read().head)

    with pytest.raises(WalAppendRefusedError, match="refused its landed mark"):
        lander.land("alpha", message="racing land", checkpoint=persist_refusal_at_the_window)

    report = operator.inspect_wal()
    assert [operator.wal.read().head] == refused_head
    assert not any(event.kind == "landed" for event in report.events)
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


def test_an_unrelated_landing_after_branch_cas_does_not_relabel_success(
    git_repo: GitFixture,
) -> None:
    """An irreversible target move still receives its terminal WAL event."""

    coordinator = Coordinator(git_repo.root)
    session = _register(coordinator, git_repo, "alpha", "a.txt")
    _stage(git_repo, session, "a.txt", "landed before unrelated audit failure\n")
    interleaved: dict[str, str] = {}

    def plant_unrelated(point: str, _context: Mapping[str, Any]) -> None:
        if point == "after_branch":
            interleaved["commit"] = _marked_commit(
                coordinator,
                "refs/heads/topic",
                "9" * 32,
                (git_repo.base,),
            )

    result = coordinator.land(
        "alpha",
        message="completed landing",
        checkpoint=plant_unrelated,
    )

    assert git_repo.oid() == result.commit
    snapshot = coordinator.wal.read()
    assert not coordinator._fold(snapshot).pending
    assert [
        event.data["commit"]
        for event in snapshot.events
        if event.kind == "landed" and event.data.get("operation") == result.operation
    ] == [result.commit]
    report = coordinator.inspect_wal()
    assert [(item.kind, item.target, item.commit) for item in report.divergences] == [
        ("unrecorded_landing", "refs/heads/topic", interleaved["commit"])
    ]
    with pytest.raises(OnetreeError) as refused:
        coordinator.register_session("beta", pid=os.getpid(), ttl=300)
    assert refused.value.kind == "unrecorded_landing"


def test_a_recovered_duplicate_mark_survives_an_unrelated_divergence(
    git_repo: GitFixture,
) -> None:
    """The duplicate-success check reads the WAL, not the mutation audit."""

    lander = Coordinator(git_repo.root)
    operator = Coordinator(git_repo.root)
    session = _register(lander, git_repo, "alpha", "a.txt")
    _stage(git_repo, session, "a.txt", "recovered with unrelated divergence\n")
    interleaved: dict[str, str] = {}

    def repair_then_diverge(point: str, _context: Mapping[str, Any]) -> None:
        if point == "after_branch":
            assert operator.recover().repaired == 1
            interleaved["commit"] = _marked_commit(
                operator,
                "refs/heads/topic",
                "8" * 32,
                (git_repo.base,),
            )

    result = lander.land(
        "alpha",
        message="recovered first",
        checkpoint=repair_then_diverge,
    )

    assert git_repo.oid() == result.commit
    landed = [event for event in lander.wal.read().events if event.kind == "landed"]
    assert len(landed) == 1
    assert landed[0].data["commit"] == result.commit
    assert lander.inspect_wal().divergences[0].commit == interleaved["commit"]


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
) -> None:
    """Found in two independent reproductions.

    The scan excluded every position the WAL had ever seen, and a crash-left
    commit becomes such a position the moment anyone registers on top of it or
    lands above it. Detection therefore expired through ordinary use. onetree
    refuses those acts until explicit adoption, so they cannot mask the fact.
    """

    coordinator = Coordinator(git_repo.root)
    if order == "later_registration":
        coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    elif order == "later_landing":
        session = _register(coordinator, git_repo, "beta", "b.txt")
        _stage(git_repo, session, "b.txt", "legit\n")
    unrecorded = _unrecorded_landing(coordinator, git_repo.base, "f" * 32)

    def routine_act() -> object:
        if order == "later_landing":
            return coordinator.land("beta", message="one more legit landing")
        return coordinator.register_session("beta", pid=os.getpid(), ttl=300)

    wal_before = coordinator.wal.head()
    with pytest.raises(OnetreeError) as refused:
        routine_act()
    assert refused.value.kind == "unrecorded_landing"
    assert coordinator.wal.head() == wal_before
    if order == "later_landing":
        assert coordinator.check("gamma", str(git_repo.root / "b.txt")).blocked

    report = coordinator.inspect_wal()
    assert [item.kind for item in report.divergences] == ["unrecorded_landing"]
    assert report.divergences[0].commit == unrecorded
    assert not report.consistent
    with pytest.raises(OnetreeError) as recovery_refused:
        coordinator.recover()
    assert recovery_refused.value.kind == "unrecorded_landing"

    adopted = coordinator.adopt_landings()
    assert [event.data["commit"] for event in adopted.adopted] == [unrecorded]
    assert adopted.report.consistent
    routine_act()
    assert coordinator.inspect_wal().consistent


def test_an_unrecorded_landing_beyond_one_scan_page_is_still_reported(
    git_repo: GitFixture,
) -> None:
    """A deep unrecorded landing remains visible in the complete stream."""

    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    unrecorded = _unrecorded_landing(coordinator, git_repo.base, "f" * 32)
    for index in range(3):
        git_repo.external_commit("b.txt", f"hand {index}\n", f"hand commit {index}")
    report = coordinator.inspect_wal()
    kinds = [item.kind for item in report.divergences]
    assert kinds == ["unrecorded_landing"]
    assert not report.consistent
    assert report.divergences[0].commit == unrecorded
    with pytest.raises(OnetreeError) as refused:
        coordinator.recover()
    assert refused.value.kind == "unrecorded_landing"

    # The marked commit is below the ordinary hand history and really is on the target.
    assert git_repo.git(
        "merge-base", "--is-ancestor", unrecorded, git_repo.oid(), check=False
    ).returncode == 0


@pytest.mark.parametrize(
    "shape",
    [
        "wal_unknown_branch",
        "root",
        "merge",
        "copied_operation",
        "detached",
        "detached_only",
    ],
)
def test_repository_wide_audit_and_adoption_cover_every_landing_shape(
    git_repo: GitFixture,
    shape: str,
) -> None:
    """Every local branch plus HEAD is audited, and every refusal has an escape."""

    coordinator = Coordinator(git_repo.root)
    session = _register(coordinator, git_repo, "alpha", "a.txt")
    _stage(git_repo, session, "a.txt", "recorded landing\n")
    landed = coordinator.land("alpha", message="recorded landing")
    main = git_repo.oid()
    operation = "f" * 32

    if shape == "wal_unknown_branch":
        target = "refs/heads/release"
        git_repo.git("branch", "release", main)
        candidate = _marked_commit(coordinator, target, operation, (main,))
        expected_head = main
    elif shape == "root":
        target = "refs/heads/orphan"
        candidate = _marked_commit(
            coordinator,
            target,
            operation,
            (),
            tree_from=main,
        )
        expected_head = coordinator.repo.zero_oid
    elif shape == "merge":
        target = "refs/heads/main"
        tree = coordinator.repo.git_text("show", "-s", "--format=%T", main)
        side = coordinator.repo.commit_tree(tree, "ordinary side", (main,))
        assert coordinator.repo.update_ref("refs/heads/feature", side, None)
        candidate = _marked_commit(coordinator, target, operation, (main, side))
        expected_head = main
    elif shape == "copied_operation":
        target = "refs/heads/release"
        operation = landed.operation
        (expected_head,) = coordinator.repo.commit_parents(landed.commit)
        candidate = _marked_commit(
            coordinator,
            target,
            operation,
            (expected_head,),
            tree_from=landed.commit,
        )
    elif shape == "detached":
        target = "refs/heads/main"
        candidate = _marked_commit(coordinator, target, operation, (main,))
        expected_head = main
        git_repo.git("checkout", "--quiet", "--detach", candidate)
    else:
        target = "HEAD"
        tree = coordinator.repo.git_text("show", "-s", "--format=%T", main)
        candidate = coordinator.repo.commit_tree(
            tree,
            coordinator.repo.landing_message(
                "detached-only landing", operation, "e" * 32, "ghost"
            ),
            (main,),
        )
        expected_head = main
        git_repo.git("checkout", "--quiet", "--detach", candidate)

    report = coordinator.inspect_wal()
    assert [(item.kind, item.target, item.commit) for item in report.divergences] == [
        ("unrecorded_landing", target, candidate)
    ]
    explicit_target = "refs/heads/main" if shape == "detached_only" else None
    with pytest.raises(OnetreeError) as refused:
        coordinator.register_session(
            "beta",
            pid=os.getpid(),
            ttl=300,
            target=explicit_target,
        )
    assert refused.value.kind == "unrecorded_landing"

    adoption = coordinator.adopt_landings()
    assert len(adoption.adopted) == 1
    adopted = adoption.adopted[0]
    assert frozenset(adopted.data) == {
        "operation",
        "target",
        "expected_head",
        "commit",
        "tree",
    }
    assert adopted.data["operation"] == operation
    assert adopted.data["target"] == target
    assert adopted.data["expected_head"] == expected_head
    assert adopted.data["commit"] == candidate
    assert adoption.report.consistent
    registered = coordinator.register_session(
        "beta",
        pid=os.getpid(),
        ttl=300,
        target=explicit_target,
    )
    if shape == "detached":
        coordinator.claim("beta", [str(git_repo.root / "nested/c.txt")])
        _stage(git_repo, registered, "nested/c.txt", "post-adoption landing\n")
        assert coordinator.land(
            "beta", message="post-adoption landing", target=target
        ).target == target


def test_targeted_adoption_records_only_the_named_branch(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    wanted = _marked_commit(
        coordinator,
        "refs/heads/main",
        "1" * 32,
        (git_repo.base,),
    )
    unwanted = _marked_commit(
        coordinator,
        "refs/heads/topic",
        "2" * 32,
        (git_repo.base,),
    )

    adoption = coordinator.adopt_landings("refs/heads/main")

    assert [event.data["commit"] for event in adoption.adopted] == [wanted]
    assert not adoption.report.consistent
    assert [(item.target, item.commit) for item in adoption.report.divergences] == [
        ("refs/heads/topic", unwanted)
    ]


def test_targeted_adoption_attributes_a_shared_commit_to_the_named_branch(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    candidate = _marked_commit(
        coordinator,
        "refs/heads/main",
        "3" * 32,
        (git_repo.base,),
    )
    assert coordinator.repo.update_ref("refs/heads/aaa", candidate, None)

    adoption = coordinator.adopt_landings("refs/heads/main")

    assert len(adoption.adopted) == 1
    assert adoption.adopted[0].data["target"] == "refs/heads/main"
    assert adoption.adopted[0].data["commit"] == candidate
    assert adoption.report.consistent


def test_adoption_is_safe_to_run_twice(git_repo: GitFixture) -> None:
    """A second `wal --adopt` adopts nothing and exits 0; it is not an error."""

    coordinator = Coordinator(git_repo.root)
    candidate = _marked_commit(
        coordinator,
        "refs/heads/main",
        "4" * 32,
        (git_repo.base,),
    )

    first_code, first_out, _ = _cli("wal", "--repo", str(git_repo.root), "--adopt", "--json")
    second_code, second_out, _ = _cli("wal", "--repo", str(git_repo.root), "--adopt", "--json")

    assert first_code == 0
    assert json.loads(first_out)["adoption"]["commits"] == [candidate]
    assert second_code == 0
    second = json.loads(second_out)["adoption"]
    assert second["commits"] == []
    assert second["consistent"] is True
    assert second["divergences"] == []


def test_adoption_of_a_clean_branch_still_reports_the_other_branch(
    git_repo: GitFixture,
) -> None:
    """Nothing to adopt here is not nothing to adopt anywhere; the report is repo-wide."""

    coordinator = Coordinator(git_repo.root)
    elsewhere = _marked_commit(
        coordinator,
        "refs/heads/topic",
        "5" * 32,
        (git_repo.base,),
    )

    adoption = coordinator.adopt_landings("refs/heads/main")

    assert adoption.adopted == ()
    assert not adoption.report.consistent
    assert [(item.target, item.commit) for item in adoption.report.divergences] == [
        ("refs/heads/topic", elsewhere)
    ]


def test_land_refuses_detached_head_without_an_explicit_target(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    session = _register(coordinator, git_repo, "alpha", "a.txt")
    _stage(git_repo, session, "a.txt", "detached payload\n")
    git_repo.git("checkout", "--quiet", "--detach", git_repo.base)
    wal_before = coordinator.wal.head()

    with pytest.raises(GitError, match="HEAD is detached; pass a branch ref"):
        coordinator.land("alpha", message="must name the target")

    assert coordinator.wal.head() == wal_before
    assert coordinator.repo.resolve_ref("HEAD") == git_repo.base
    assert coordinator.repo.resolve_ref("refs/heads/main") == git_repo.base
    result = coordinator.land(
        "alpha",
        message="explicit detached landing",
        target="refs/heads/main",
    )
    assert result.target == "refs/heads/main"
    assert coordinator.repo.resolve_ref("refs/heads/main") == result.commit
    assert coordinator.repo.resolve_ref("HEAD") == git_repo.base


def test_a_malformed_loose_ref_does_not_hide_valid_audit_heads(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    candidate = _marked_commit(
        coordinator,
        "refs/heads/topic",
        "4" * 32,
        (git_repo.base,),
    )
    malformed = coordinator.repo.git_dir / "refs" / "heads" / "bad..name"
    malformed.write_text(git_repo.base + "\n", encoding="ascii")

    report = coordinator.inspect_wal()

    assert [(item.target, item.commit) for item in report.divergences] == [
        ("refs/heads/topic", candidate)
    ]
    adoption = coordinator.adopt_landings("refs/heads/topic")
    assert [event.data["commit"] for event in adoption.adopted] == [candidate]
    assert adoption.report.consistent

    code, output, error = _cli("wal", "--repo", str(git_repo.root), "--json")
    assert (code, error) == (0, "")
    assert json.loads(output)["wal"]["consistent"] is True
    code, output, error = _cli(
        "session",
        "--repo",
        str(git_repo.root),
        "--session",
        "beta",
        "--pid",
        str(os.getpid()),
        "--json",
    )
    assert (code, error) == (0, "")
    assert json.loads(output)["session"]["actor"] == "beta"
    code, output, error = _cli(
        "claim",
        str(git_repo.root / "b.txt"),
        "--repo",
        str(git_repo.root),
        "--session",
        "beta",
        "--json",
    )
    assert (code, error) == (0, "")
    assert len(json.loads(output)["claim"]["claims"]) == 1


def test_a_malformed_out_of_scope_ref_name_does_not_brick_the_audit(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    malformed = coordinator.repo.git_dir / "refs" / "tags" / "bad..name"
    malformed.write_text(git_repo.base + "\n", encoding="ascii")

    assert coordinator.repo.audit_heads() == {
        "HEAD": git_repo.base,
        "refs/heads/main": git_repo.base,
    }
    assert coordinator.inspect_wal().consistent


def test_a_control_character_in_a_malformed_ref_name_does_not_brick_the_audit(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    malformed = coordinator.repo.git_dir / "refs" / "heads" / "bad\n..name"
    malformed.write_text(git_repo.base + "\n", encoding="ascii")

    assert coordinator.repo.audit_heads() == {
        "HEAD": git_repo.base,
        "refs/heads/main": git_repo.base,
    }
    assert coordinator.inspect_wal().consistent


def test_warning_shaped_non_utf8_bytes_in_a_malformed_name_do_not_brick_audit(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    relative = (
        Path("bad\nwarning: ignoring broken ref refs")
        / "heads"
        / os.fsdecode(b"\xff..")
    )
    malformed = coordinator.repo.git_dir / "refs" / "heads" / relative
    malformed.parent.mkdir(parents=True)
    malformed.write_text(git_repo.base + "\n", encoding="ascii")

    assert coordinator.repo.audit_heads() == {
        "HEAD": git_repo.base,
        "refs/heads/main": git_repo.base,
    }
    assert coordinator.inspect_wal().consistent


def test_a_validly_named_broken_ref_still_refuses_the_audit(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    broken = coordinator.repo.git_dir / "refs" / "heads" / "broken"
    broken.write_text("not-an-object-id\n", encoding="ascii")

    with pytest.raises(GitError, match="cannot list audit refs"):
        coordinator.inspect_wal()


def test_a_malformed_name_cannot_mask_a_validly_named_broken_branch(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    malformed = coordinator.repo.git_dir / "refs" / "tags" / "bad..name"
    malformed.write_text(git_repo.base + "\n", encoding="ascii")
    broken = coordinator.repo.git_dir / "refs" / "heads" / "broken"
    broken.write_text("not-an-object-id\n", encoding="ascii")

    with pytest.raises(GitError, match="cannot list audit refs"):
        coordinator.inspect_wal()


def test_a_malformed_name_cannot_mask_a_non_utf8_broken_branch(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    malformed = coordinator.repo.git_dir / "refs" / "heads" / "00..bad"
    malformed.write_text(git_repo.base + "\n", encoding="ascii")
    non_utf8_name = os.fsdecode(b"\xff")
    broken = coordinator.repo.git_dir / "refs" / "heads" / non_utf8_name
    broken.write_text("not-an-object-id\n", encoding="ascii")

    with pytest.raises(GitError, match="cannot list audit refs"):
        coordinator.inspect_wal()


def test_a_dangling_symbolic_branch_is_not_an_audit_head(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    git_repo.git("symbolic-ref", "refs/heads/current", "refs/heads/missing")

    assert coordinator.repo.audit_heads() == {
        "HEAD": git_repo.base,
        "refs/heads/main": git_repo.base,
    }
    assert coordinator.inspect_wal().consistent
    assert coordinator.register_session("after-symref", pid=os.getpid(), ttl=300).actor == (
        "after-symref"
    )


def test_a_dangling_symbolic_branch_targeting_a_namespace_is_not_an_audit_head(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    git_repo.git("branch", "group/child", git_repo.base)
    git_repo.git("symbolic-ref", "refs/heads/current", "refs/heads/group")

    assert coordinator.repo.audit_heads() == {
        "HEAD": git_repo.base,
        "refs/heads/group/child": git_repo.base,
        "refs/heads/main": git_repo.base,
    }
    assert coordinator.inspect_wal().consistent
    assert coordinator.register_session("after-namespace", pid=os.getpid(), ttl=300).actor == (
        "after-namespace"
    )


def test_a_dangling_symbolic_branch_ending_at_a_dangling_symlink_is_not_an_audit_head(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    ghost = coordinator.repo.git_dir / "refs" / "heads" / "ghost"
    ghost.symlink_to("nowhere")
    git_repo.git("symbolic-ref", "refs/heads/current", "refs/heads/ghost")

    assert coordinator.repo.audit_heads() == {
        "HEAD": git_repo.base,
        "refs/heads/main": git_repo.base,
    }
    assert coordinator.inspect_wal().consistent
    assert coordinator.register_session("after-alias-chain", pid=os.getpid(), ttl=300).actor == (
        "after-alias-chain"
    )


def test_a_filesystem_namespace_symlink_exposes_its_resolving_child_as_an_audit_head(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    git_repo.git("branch", "group/child", git_repo.base)
    alias = coordinator.repo.git_dir / "refs" / "heads" / "alias"
    alias.symlink_to("group")

    assert coordinator.repo.audit_heads() == {
        "HEAD": git_repo.base,
        "refs/heads/alias/child": git_repo.base,
        "refs/heads/group/child": git_repo.base,
        "refs/heads/main": git_repo.base,
    }
    assert coordinator.inspect_wal().consistent


def test_a_corrupt_child_below_a_filesystem_namespace_symlink_refuses_the_audit(
    git_repo: GitFixture,
    tmp_path: Path,
) -> None:
    coordinator = Coordinator(git_repo.root)
    external = tmp_path / "external-refs"
    external.mkdir()
    child = external / "child"
    child.write_text(git_repo.base + "\n", encoding="ascii")
    alias = coordinator.repo.git_dir / "refs" / "heads" / "alias"
    alias.symlink_to(external, target_is_directory=True)

    assert coordinator.repo.audit_heads()["refs/heads/alias/child"] == git_repo.base

    child.write_text("not-an-object-id\n", encoding="ascii")
    listing = git_repo.git("for-each-ref", "refs/heads", check=False)
    assert listing.returncode == 0
    assert "warning: ignoring broken ref refs/heads/alias/child" in listing.stderr
    with pytest.raises(GitError, match="cannot list audit refs"):
        coordinator.repo.audit_heads()


def test_a_filesystem_ref_namespace_symlink_cycle_refuses_the_audit(
    git_repo: GitFixture,
    tmp_path: Path,
) -> None:
    coordinator = Coordinator(git_repo.root)
    external = tmp_path / "cyclic-refs"
    external.mkdir()
    (external / "loop").symlink_to(external, target_is_directory=True)
    alias = coordinator.repo.git_dir / "refs" / "heads" / "alias"
    alias.symlink_to(external, target_is_directory=True)

    with pytest.raises(GitError, match="namespace symlink cycle"):
        coordinator.repo.audit_heads()


def test_a_symbolic_branch_targeting_a_corrupt_exact_ref_refuses_the_audit(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    broken = coordinator.repo.git_dir / "refs" / "heads" / "broken"
    broken.write_text("not-an-object-id\n", encoding="ascii")
    git_repo.git("symbolic-ref", "refs/heads/current", "refs/heads/broken")

    with pytest.raises(GitError, match="cannot list audit refs"):
        coordinator.repo.audit_heads()


def test_a_dangling_filesystem_symlink_is_not_an_audit_head(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    ghost = coordinator.repo.git_dir / "refs" / "heads" / "ghost"
    ghost.symlink_to("nowhere")
    assert git_repo.git("fsck", "--no-dangling", check=False).returncode == 0

    assert coordinator.repo.audit_heads() == {
        "HEAD": git_repo.base,
        "refs/heads/main": git_repo.base,
    }
    assert coordinator.inspect_wal().consistent
    assert coordinator.register_session("after-symlink", pid=os.getpid(), ttl=300).actor == (
        "after-symlink"
    )


def test_a_resolving_filesystem_symlink_remains_an_audit_head(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    alias = coordinator.repo.git_dir / "refs" / "heads" / "alias"
    alias.symlink_to("main")

    assert coordinator.repo.audit_heads() == {
        "HEAD": git_repo.base,
        "refs/heads/alias": git_repo.base,
        "refs/heads/main": git_repo.base,
    }
    assert coordinator.inspect_wal().consistent


def test_a_resolving_symbolic_branch_remains_an_audit_head(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    git_repo.git("symbolic-ref", "refs/heads/alias", "refs/heads/main")

    assert coordinator.repo.audit_heads() == {
        "HEAD": git_repo.base,
        "refs/heads/alias": git_repo.base,
        "refs/heads/main": git_repo.base,
    }
    assert coordinator.inspect_wal().consistent


def test_a_dangling_loose_alias_cannot_hide_a_same_name_packed_branch(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    git_repo.git("branch", "alias", git_repo.base)
    git_repo.git("pack-refs", "--all")
    git_repo.git("symbolic-ref", "refs/heads/alias", "refs/heads/missing")

    with pytest.raises(GitError, match="cannot list audit refs"):
        coordinator.repo.audit_heads()


def test_unrelated_rc_zero_git_stderr_does_not_reframe_the_ref_listing(
    git_repo: GitFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = Coordinator(git_repo.root)
    original = coordinator.repo.git

    def add_unrelated_diagnostic(*arguments: str, **kwargs: Any) -> git_module.GitResult:
        result = original(*arguments, **kwargs)
        if arguments[0] == "for-each-ref":
            assert result.returncode == 0
            return git_module.GitResult(
                result.returncode,
                result.stdout,
                b"trace: built-in: git for-each-ref\n",
            )
        return result

    monkeypatch.setattr(coordinator.repo, "git", add_unrelated_diagnostic)

    assert coordinator.repo.audit_heads() == {
        "HEAD": git_repo.base,
        "refs/heads/main": git_repo.base,
    }
    assert coordinator.inspect_wal().consistent


def test_reftable_broken_branch_diagnostic_remains_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git_repo = _repository_with_ref_backend(tmp_path, "reftable")
    coordinator = Coordinator(git_repo.root)
    original = coordinator.repo.git

    def add_broken_branch_diagnostic(*arguments: str, **kwargs: Any) -> git_module.GitResult:
        result = original(*arguments, **kwargs)
        if arguments[0] == "for-each-ref":
            assert result.returncode == 0
            return git_module.GitResult(
                result.returncode,
                result.stdout,
                b"warning: ignoring broken ref refs/heads/broken\n",
            )
        return result

    monkeypatch.setattr(coordinator.repo, "git", add_broken_branch_diagnostic)

    with pytest.raises(GitError, match="Git ignored a broken branch"):
        coordinator.repo.audit_heads()


def test_a_malformed_symbolic_head_cannot_disappear_from_the_audit(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    malformed = coordinator.repo.git_dir / "refs" / "heads" / "bad..name"
    malformed.write_text(git_repo.base + "\n", encoding="ascii")
    (coordinator.repo.git_dir / "HEAD").write_text(
        "ref: refs/heads/bad..name\n", encoding="ascii"
    )

    with pytest.raises(RefCorruptError):
        coordinator.repo.audit_heads()


def test_git_plumbing_pins_diagnostic_locale(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")

    assert Repository._environment()["LC_ALL"] == "C"


def test_adoption_can_share_an_id_with_an_independent_pending_intent(
    git_repo: GitFixture,
) -> None:
    """Adoption records the commit pair without cancelling a live lander."""

    lander = Coordinator(git_repo.root)
    operator = Coordinator(git_repo.root)
    session = _register(lander, git_repo, "alpha", "a.txt")
    _stage(git_repo, session, "a.txt", "live landing\n")
    adopted: dict[str, str] = {}

    def adopt_during_intent(point: str, context: Mapping[str, Any]) -> None:
        if point != "after_intent":
            return
        operation = str(context["operation"])
        commit = _marked_commit(
            operator,
            "refs/heads/topic",
            operation,
            (git_repo.base,),
        )
        result = operator.adopt_landings("refs/heads/topic")
        assert [(event.data["commit"], event.data["operation"]) for event in result.adopted] == [
            (commit, operation)
        ]
        assert result.report.pending_intents == 1
        adopted["commit"] = commit

    landed = lander.land(
        "alpha",
        message="complete after peer adoption",
        checkpoint=adopt_during_intent,
    )

    report = lander.inspect_wal()
    assert adopted
    assert report.consistent
    assert report.pending_intents == 0
    assert {
        (event.data["commit"], event.data["operation"])
        for event in report.events
        if event.kind in {"adopted", "landed"}
    } >= {
        (adopted["commit"], landed.operation),
        (landed.commit, landed.operation),
    }


def test_a_completed_peer_landing_restarts_the_audit_snapshot(
    git_repo: GitFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A complete peer interleave is recorded, not misreported as unrecorded."""

    coordinator = Coordinator(git_repo.root)
    peer = _register(coordinator, git_repo, "peer", "b.txt")
    _stage(git_repo, peer, "b.txt", "peer landing\n")
    original = Coordinator._audit_heads
    fired: list[bool] = []

    def interleave(
        self: Coordinator,
        state: Any,
        target: str | None = None,
    ) -> Any:
        if not fired:
            fired.append(True)
            coordinator.land("peer", message="complete peer landing")
        return original(self, state, target)

    monkeypatch.setattr(Coordinator, "_audit_heads", interleave)

    registered = coordinator.register_session("beta", pid=os.getpid(), ttl=300)

    assert fired == [True]
    assert registered.actor == "beta"
    assert coordinator.inspect_wal().consistent


def test_inspection_restarts_when_a_peer_lands_before_head_capture(
    git_repo: GitFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = Coordinator(git_repo.root)
    peer = _register(coordinator, git_repo, "peer", "b.txt")
    _stage(git_repo, peer, "b.txt", "peer landing during inspection\n")
    original = Coordinator._audit_heads
    fired: list[bool] = []

    def interleave(
        self: Coordinator,
        state: Any,
        target: str | None = None,
    ) -> Any:
        if not fired:
            fired.append(True)
            coordinator.land("peer", message="complete peer landing")
        return original(self, state, target)

    monkeypatch.setattr(Coordinator, "_audit_heads", interleave)

    report = coordinator.inspect_wal()

    assert fired == [True]
    assert report.consistent
    assert report.divergences == ()


def test_inspection_rechecks_heads_after_computing_a_clean_result(
    git_repo: GitFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    main = git_repo.oid()
    tree = coordinator.repo.git_text("show", "-s", "--format=%T", main)
    candidate = coordinator.repo.commit_tree(
        tree,
        coordinator.repo.landing_message(
            "unrecorded during inspection", "5" * 32, "6" * 32, "ghost"
        ),
        (main,),
    )
    original = Coordinator._divergences
    fired: list[bool] = []

    def move_after_comparison(
        self: Coordinator,
        state: Any,
        heads: Any,
    ) -> Any:
        result = original(self, state, heads)
        if not fired:
            fired.append(True)
            assert coordinator.repo.update_ref("refs/heads/main", candidate, main)
        return result

    monkeypatch.setattr(Coordinator, "_divergences", move_after_comparison)

    report = coordinator.inspect_wal()

    assert fired == [True]
    assert [(item.kind, item.commit) for item in report.divergences] == [
        ("unrecorded_landing", candidate)
    ]


def test_a_branch_move_without_a_wal_write_restarts_the_audit_snapshot(
    git_repo: GitFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changed captured ref is retried even when the WAL head did not move."""

    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    main = git_repo.oid()
    tree = coordinator.repo.git_text("show", "-s", "--format=%T", main)
    candidate = coordinator.repo.commit_tree(
        tree,
        coordinator.repo.landing_message(
            "unrecorded interleave", "d" * 32, "e" * 32, "ghost"
        ),
        (main,),
    )
    original = Coordinator._unrecorded_landings
    fired: list[bool] = []

    def interleave(
        self: Coordinator,
        state: Any,
        targets: Any,
    ) -> Any:
        if not fired:
            fired.append(True)
            assert coordinator.repo.update_ref("refs/heads/main", candidate, main)
        return original(self, state, targets)

    monkeypatch.setattr(Coordinator, "_unrecorded_landings", interleave)

    with pytest.raises(OnetreeError) as refused:
        coordinator.register_session("beta", pid=os.getpid(), ttl=300)

    assert fired == [True]
    assert refused.value.kind == "unrecorded_landing"
    assert coordinator.wal.read().events[-1].actor == "alpha"


def test_recovery_rechecks_heads_after_computing_a_clean_result(
    git_repo: GitFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = Coordinator(git_repo.root)
    session = _register(coordinator, git_repo, "alpha", "a.txt")
    _stage(git_repo, session, "a.txt", "land before rollback\n")
    landed = coordinator.land("alpha", message="land before rollback")
    original = Coordinator._divergences
    fired: list[bool] = []

    def rollback_after_comparison(
        self: Coordinator,
        state: Any,
        heads: Any,
    ) -> Any:
        result = original(self, state, heads)
        if not fired:
            fired.append(True)
            assert coordinator.repo.update_ref(
                "refs/heads/main", git_repo.base, landed.commit
            )
        return result

    monkeypatch.setattr(Coordinator, "_divergences", rollback_after_comparison)

    with pytest.raises(WalDivergedError) as refused:
        coordinator.recover()

    assert fired == [True]
    assert [item["kind"] for item in refused.value.details["divergences"]] == [
        "landed_not_on_target"
    ]


def test_recovery_rechecks_the_wal_after_computing_a_clean_result(
    git_repo: GitFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = Coordinator(git_repo.root)
    peer = _register(coordinator, git_repo, "peer", "b.txt")
    _stage(git_repo, peer, "b.txt", "intent during recovery\n")
    original = Coordinator._divergences
    fired: list[bool] = []

    class StopAfterIntentError(RuntimeError):
        pass

    def append_intent_after_comparison(
        self: Coordinator,
        state: Any,
        heads: Any,
    ) -> Any:
        result = original(self, state, heads)
        if not fired:
            fired.append(True)

            def stop(point: str, _context: Mapping[str, Any]) -> None:
                if point == "after_intent":
                    raise StopAfterIntentError

            with pytest.raises(StopAfterIntentError):
                coordinator.land("peer", message="paused peer", checkpoint=stop)
        return result

    monkeypatch.setattr(Coordinator, "_divergences", append_intent_after_comparison)

    recovery = coordinator.recover()

    assert fired == [True]
    assert not recovery.consistent
    assert recovery.pending == 1


@pytest.mark.parametrize(
    ("hidden_by", "expected_refusal"),
    [("replace", "unrecorded_landing"), ("grafts", "legacy_grafts")],
)
def test_hidden_history_cannot_launder_an_unrecorded_landing(
    git_repo: GitFixture,
    hidden_by: str,
    expected_refusal: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The audit sees the object graph that exists below local history rewrites."""

    monkeypatch.delenv("GIT_NO_REPLACE_OBJECTS", raising=False)
    monkeypatch.delenv("GIT_GRAFT_FILE", raising=False)
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    hidden = _unrecorded_landing(coordinator, git_repo.base, "a" * 32)
    tip = git_repo.external_commit("b.txt", "ordinary tip\n", "ordinary tip")
    real_parent = git_repo.git("rev-parse", f"{hidden}^").stdout.strip()

    if hidden_by == "replace":
        git_repo.git("replace", "--graft", tip, real_parent)
    else:
        info = coordinator.repo.git_dir / "info"
        info.mkdir(exist_ok=True)
        info.joinpath("grafts").write_text(f"{tip} {real_parent}\n", encoding="ascii")

    # Known-bad control: ordinary Git really does hide the broker landing.
    visible = set(git_repo.git("log", "--format=%H", "refs/heads/main", "--").stdout.split())
    assert hidden not in visible

    report = coordinator.inspect_wal()
    reported = [item.commit for item in report.divergences if item.commit is not None]
    assert reported == [hidden]
    assert not report.consistent
    if hidden_by == "grafts":
        assert "legacy_grafts" in {item.kind for item in report.divergences}

    with pytest.raises(OnetreeError) as refused:
        coordinator.register_session("beta", pid=os.getpid(), ttl=300)
    assert refused.value.kind == expected_refusal


def test_a_nonempty_legacy_graft_file_is_an_explicit_mutation_boundary(
    git_repo: GitFixture,
) -> None:
    """The graft fail-safe is independent of Git's undocumented environment override."""

    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    tip = git_repo.external_commit("b.txt", "ordinary tip\n", "ordinary tip")
    parent = git_repo.git("rev-parse", f"{tip}^").stdout.strip()
    info = coordinator.repo.git_dir / "info"
    info.mkdir(exist_ok=True)
    info.joinpath("grafts").write_text(f"{tip} {parent}\n", encoding="ascii")

    report = coordinator.inspect_wal()
    assert not report.consistent
    assert {item.kind for item in report.divergences} == {"legacy_grafts"}
    with pytest.raises(OnetreeError) as refused:
        coordinator.register_session("beta", pid=os.getpid(), ttl=300)
    assert refused.value.kind == "legacy_grafts"
    assert refused.value.details["graft_file"] == str(info / "grafts")
    with pytest.raises(OnetreeError) as adoption_refused:
        coordinator.adopt_landings()
    assert adoption_refused.value.kind == "legacy_grafts"


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b"\n", id="blank"),
        pytest.param(b" \t\n", id="whitespace"),
        pytest.param(b"# first comment\n# second comment\n", id="comments"),
    ],
)
def test_a_graft_file_without_effective_entries_is_not_a_boundary(
    git_repo: GitFixture,
    content: bytes,
) -> None:
    """Git ignores blank and comment-only graft files, so the guard does too."""

    coordinator = Coordinator(git_repo.root)
    graft_file = coordinator.repo.git_dir / "info" / "grafts"
    graft_file.parent.mkdir(exist_ok=True)
    graft_file.write_bytes(content)

    raw_history = git_repo.git("log", "--format=%H", "refs/heads/main", "--").stdout.split()
    assert raw_history == [git_repo.oid()]
    assert coordinator.repo.nonempty_graft_file() is None
    assert coordinator.inspect_wal().consistent
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)


def test_an_unreadable_graft_path_is_a_typed_git_failure(git_repo: GitFixture) -> None:
    coordinator = Coordinator(git_repo.root)
    graft_file = coordinator.repo.git_dir / "info" / "grafts"
    graft_file.mkdir(parents=True)

    with pytest.raises(GitError, match="cannot inspect the legacy graft file"):
        coordinator.repo.nonempty_graft_file()


def test_the_empty_graft_override_does_not_add_deprecation_advice(
    git_repo: GitFixture,
) -> None:
    result = Repository(git_repo.root).git("log", "-1", "--format=%H", "HEAD", "--")

    assert result.stderr == b""


@pytest.mark.parametrize(
    ("config_route", "config_key", "config_value"),
    [
        pytest.param("local", "trailer.separators", "=", id="local-separator"),
        pytest.param(
            "local",
            "trailer.Onetree-Operation.key",
            "X",
            id="local-exact-key-rewrite",
        ),
        pytest.param(
            "local",
            "trailer.hostile.key",
            "Onetree-OperationX",
            id="local-key-prefix-rewrite",
        ),
        pytest.param("global", "trailer.separators", "=", id="global-separator"),
        pytest.param("system", "trailer.separators", "=", id="system-separator"),
        pytest.param("worktree", "trailer.separators", "=", id="worktree-separator"),
        pytest.param(
            "command-environment",
            "trailer.separators",
            "=",
            id="command-environment-separator",
        ),
        pytest.param(
            "command-parameters",
            "trailer.separators",
            "=",
            id="command-parameters-separator",
        ),
    ],
)
def test_trailer_configuration_cannot_hide_a_landing(
    git_repo: GitFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config_route: str,
    config_key: str,
    config_value: str,
) -> None:
    """Git's configurable trailer parser is not onetree's parser contract."""

    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    operation = "7" * 32
    hidden = _unrecorded_landing(coordinator, git_repo.base, operation)

    for name in (
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_CONFIG_NOSYSTEM",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    if config_route == "local":
        git_repo.git("config", config_key, config_value)
    elif config_route == "worktree":
        git_repo.git("config", "extensions.worktreeConfig", "true")
        git_repo.git("config", "--worktree", config_key, config_value)
    elif config_route == "global":
        global_config = tmp_path / "global.config"
        global_config.write_text("[trailer]\n\tseparators = =\n", encoding="utf-8")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    elif config_route == "system":
        system_config = tmp_path / "system.config"
        system_config.write_text("[trailer]\n\tseparators = =\n", encoding="utf-8")
        monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(system_config))
    elif config_route == "command-environment":
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", config_key)
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", config_value)
    else:
        monkeypatch.setenv("GIT_CONFIG_PARAMETERS", f"'{config_key}'='{config_value}'")

    format_argument = (
        "--format=%H%x00%(trailers:key=Onetree-Operation,valueonly,separator=%x2C)"
    )
    unpinned_fields = git_repo.git(
        "log", "-z", format_argument, "refs/heads/main", "--"
    ).stdout.split("\0")
    hidden_position = unpinned_fields.index(hidden)
    assert unpinned_fields[hidden_position + 1] == ""

    marked = {item.commit: item.operations for item in coordinator.repo.marked_commits("HEAD")}
    assert marked[hidden] == frozenset({operation})
    assert coordinator.repo.landing_operations(hidden) == frozenset({operation})
    report = coordinator.inspect_wal()
    assert [(item.kind, item.commit) for item in report.divergences] == [
        ("unrecorded_landing", hidden)
    ]
    with pytest.raises(OnetreeError) as refused:
        coordinator.register_session("beta", pid=os.getpid(), ttl=300)
    assert refused.value.kind == "unrecorded_landing"


@pytest.mark.parametrize(
    "raw_message",
    [
        pytest.param(
            b"subject\n\nOnetree-Operation: "
            + b"a" * 32
            + b"\n(cherry picked from commit "
            + b"1" * 40
            + b")\n",
            id="git-generated-line",
        ),
        pytest.param(
            b"subject\n\nOnetree-Operation: " + b"a" * 32 + b"\n# fixed comment\n",
            id="fixed-comment-line",
        ),
        pytest.param(
            b"subject\n\nOnetree-Operation: " + b"a" * 32 + b"\nordinary prose\n",
            id="ordinary-prose-union",
        ),
        pytest.param(
            b"subject\n\nOnetree-Operation: "
            + b"a" * 32
            + b"\n(cherry picked from commit "
            + b"1" * 40
            + b")\n"
            + b"\n".join(f"prose {index}".encode() for index in range(6))
            + b"\n",
            id="exactly-25-percent-trailers",
        ),
        pytest.param(
            b"subject\n\nOnetree-Operation: "
            + b"a" * 32
            + b"\n(cherry picked from commit "
            + b"1" * 40
            + b")\n"
            + b"\n".join(f"prose {index}".encode() for index in range(7))
            + b"\n",
            id="below-25-percent-union",
        ),
        pytest.param(
            b"subject\n\nOnetree-Operation: " + b"a" * 32 + b"\n---\npatch text\n",
            id="divider-ends-message",
        ),
        pytest.param(
            b"Onetree-Operation: " + b"a" * 32 + b"\n",
            id="title-marker-union",
        ),
        pytest.param(
            b"subject\n\nOnetree-Operation: " + b"a" * 32 + b"\n",
            id="all-trailers-control",
        ),
    ],
)
def test_line_union_does_not_inherit_git_trailer_block_boundaries(
    raw_message: bytes,
) -> None:
    assert git_module.landing_ids(raw_message) == frozenset({"a" * 32})


def test_configured_log_encoding_cannot_reframe_audit_messages(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    operation = "6" * 32
    hidden = _unrecorded_landing(coordinator, git_repo.base, operation)
    git_repo.git("config", "i18n.logOutputEncoding", "UTF-16")

    unpinned = coordinator.repo.git(
        "log", "-1", "-z", "--format=%H%x00%B", hidden, "--"
    ).stdout
    assert unpinned.startswith(b"\xff\xfe")
    assert unpinned.count(b"\x00") > 40

    marked = {item.commit: item.operations for item in coordinator.repo.marked_commits("HEAD")}
    assert marked[hidden] == frozenset({operation})
    assert coordinator.repo.landing_operations(hidden) == frozenset({operation})


def test_a_nul_in_a_commit_message_cannot_hide_its_final_trailer(
    git_repo: GitFixture,
) -> None:
    """Raw object framing must not inherit pretty-format NUL truncation."""

    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    operation = "4" * 32
    hidden = _unrecorded_landing(coordinator, git_repo.base, operation)
    raw_object = coordinator.repo.git("cat-file", "commit", hidden).stdout
    headers, separator, message = raw_object.partition(b"\n\n")
    body, trailer_separator, trailers = message.partition(b"\n\n")
    assert separator == b"\n\n"
    assert trailer_separator == b"\n\n"
    nul_object = (
        headers
        + separator
        + body
        + b"\x00pretty output stops here"
        + trailer_separator
        + trailers
    )
    nul_hidden = coordinator.repo.git_text(
        "hash-object",
        "--literally",
        "-t",
        "commit",
        "-w",
        "--stdin",
        input_bytes=nul_object,
    )
    assert coordinator.repo.update_ref("refs/heads/main", nul_hidden, hidden)

    pretty = coordinator.repo.git("show", "-s", "--format=%B", nul_hidden).stdout
    assert b"Onetree-Operation" not in pretty
    assert b"\x00" in coordinator.repo.git("cat-file", "commit", nul_hidden).stdout

    marked = {item.commit: item.operations for item in coordinator.repo.marked_commits("HEAD")}
    assert marked[nul_hidden] == frozenset({operation})
    assert coordinator.repo.landing_operations(nul_hidden) == frozenset({operation})
    report = coordinator.inspect_wal()
    assert [(item.kind, item.commit) for item in report.divergences] == [
        ("unrecorded_landing", nul_hidden)
    ]
    with pytest.raises(OnetreeError) as refused:
        coordinator.register_session("beta", pid=os.getpid(), ttl=300)
    assert refused.value.kind == "unrecorded_landing"


def test_signature_display_configuration_cannot_precede_audit_records(
    git_repo: GitFixture,
    tmp_path: Path,
) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    operation = "5" * 32
    hidden = _unrecorded_landing(coordinator, git_repo.base, operation)
    raw_commit = coordinator.repo.git("cat-file", "commit", hidden).stdout
    headers, divider, message = raw_commit.partition(b"\n\n")
    assert divider == b"\n\n"
    signed_content = (
        headers
        + b"\ngpgsig -----BEGIN PGP SIGNATURE-----\n"
        + b" deliberately invalid\n"
        + b" -----END PGP SIGNATURE-----\n\n"
        + message
    )
    signed = coordinator.repo.git_text(
        "hash-object",
        "-t",
        "commit",
        "-w",
        "--stdin",
        input_bytes=signed_content,
    )
    assert coordinator.repo.update_ref("refs/heads/main", signed, hidden)

    marker = tmp_path / "gpg-invoked"
    fake_gpg = tmp_path / "fake-gpg"
    fake_gpg.write_text(
        f"#!{sys.executable}\n"
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('invoked', encoding='utf-8')\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    fake_gpg.chmod(0o755)
    git_repo.git("config", "log.showSignature", "true")
    git_repo.git("config", "gpg.program", str(fake_gpg))

    unpinned = git_repo.git("log", "-1", "--format=%H", signed, "--", check=False)
    assert unpinned.returncode == 0
    assert marker.is_file()
    marker.unlink()

    marked = {item.commit: item.operations for item in coordinator.repo.marked_commits("HEAD")}
    assert marked[signed] == frozenset({operation})
    assert coordinator.repo.landing_operations(signed) == frozenset({operation})
    assert not marker.exists()


@pytest.mark.parametrize("call_site", ["adopt", "verify"])
def test_wal_audit_never_executes_the_repositorys_signature_program(
    git_repo: GitFixture,
    tmp_path: Path,
    call_site: str,
) -> None:
    """Both tree-verification paths are read-only even for a signed branch commit."""

    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    operation = "3" * 32
    hidden = _unrecorded_landing(coordinator, git_repo.base, operation)
    raw_commit = coordinator.repo.git("cat-file", "commit", hidden).stdout
    headers, divider, message = raw_commit.partition(b"\n\n")
    assert divider == b"\n\n"
    signed_content = (
        headers
        + b"\ngpgsig -----BEGIN PGP SIGNATURE-----\n"
        + b" deliberately invalid\n"
        + b" -----END PGP SIGNATURE-----\n\n"
        + message
    )
    signed = coordinator.repo.git_text(
        "hash-object",
        "--literally",
        "-t",
        "commit",
        "-w",
        "--stdin",
        input_bytes=signed_content,
    )
    assert coordinator.repo.update_ref("refs/heads/main", signed, hidden)

    if call_site == "verify":
        coordinator.adopt_landings()

    marker = tmp_path / f"gpg-{call_site}-invoked"
    fake_gpg = tmp_path / f"fake-gpg-{call_site}"
    fake_gpg.write_text(
        f"#!{sys.executable}\n"
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('invoked', encoding='utf-8')\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    fake_gpg.chmod(0o755)
    git_repo.git("config", "log.showSignature", "true")
    git_repo.git("config", "gpg.program", str(fake_gpg))

    unpinned = git_repo.git("show", "-s", "--format=%T", signed, check=False)
    assert unpinned.returncode == 0
    assert marker.is_file()
    marker.unlink()

    if call_site == "adopt":
        coordinator.adopt_landings()
    else:
        coordinator.inspect_wal()

    assert not marker.exists()


def test_a_midx_cannot_substitute_landing_bytes(
    git_repo: GitFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The audit ignores a checksum-valid MIDX whose OID-to-offset row lies."""

    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    operation = "8" * 32
    hidden = _unrecorded_landing(coordinator, git_repo.base, operation)
    git_repo.external_commit("b.txt", "ordinary tip\n", "ordinary tip")
    parent = coordinator.repo.git_text("rev-parse", f"{hidden}^")
    tree = coordinator.repo.git_text("show", "-s", "--format=%T", hidden)
    decoy = coordinator.repo.commit_tree(tree, "ordinary work", (parent,))
    git_repo.git("update-ref", "refs/heads/midx-decoy", decoy)
    git_repo.git("repack", "-a", "-d", "-q")
    git_repo.git("update-ref", "-d", "refs/heads/midx-decoy")
    git_repo.git("multi-pack-index", "write")
    midx = coordinator.repo.common_git_dir / "objects" / "pack" / "multi-pack-index"
    assert midx.is_file()

    format_argument = (
        "--format=%H%x00%(trailers:key=Onetree-Operation,valueonly,separator=%x2C)"
    )
    control_fields = git_repo.git(
        "log", "-z", format_argument, "refs/heads/main", "--"
    ).stdout.split("\0")
    assert control_fields[control_fields.index(hidden) + 1] == operation
    assert git_repo.git("multi-pack-index", "verify", check=False).returncode == 0

    _make_midx_entry_point_to(midx, hidden, decoy)

    assert git_repo.git("multi-pack-index", "verify", check=False).returncode == 1
    blinded_fields = git_repo.git(
        "log", "-z", format_argument, "refs/heads/main", "--"
    ).stdout.split("\0")
    assert blinded_fields[blinded_fields.index(hidden) + 1] == ""
    substituted = git_repo.git("cat-file", "commit", hidden).stdout
    assert "ordinary work" in substituted
    assert "Onetree-Operation" not in substituted

    # Command-scope config is lower precedence than onetree's explicit -c, but Git's
    # test-only override is applied after config and must be removed from the child.
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.multiPackIndex")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    monkeypatch.setenv("GIT_TEST_MULTI_PACK_INDEX", "1")

    marked = {item.commit: item.operations for item in coordinator.repo.marked_commits("HEAD")}
    assert marked[hidden] == frozenset({operation})
    assert coordinator.repo.landing_operations(hidden) == frozenset({operation})
    report = coordinator.inspect_wal()
    assert [(item.kind, item.commit) for item in report.divergences] == [
        ("unrecorded_landing", hidden)
    ]
    with pytest.raises(OnetreeError) as refused:
        coordinator.register_session("beta", pid=os.getpid(), ttl=300)
    assert refused.value.kind == "unrecorded_landing"


def test_an_ambient_object_directory_cannot_redirect_audit_reads(
    git_repo: GitFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    hidden = _unrecorded_landing(coordinator, git_repo.base, "a" * 32)
    ambient_objects = tmp_path / "ambient-objects"
    ambient_objects.mkdir()
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(ambient_objects))

    assert git_repo.git("log", "--format=%H", "HEAD", "--", check=False).returncode != 0
    report = coordinator.inspect_wal()
    assert [(item.kind, item.commit) for item in report.divergences] == [
        ("unrecorded_landing", hidden)
    ]
    assert coordinator.repo.landing_operations(hidden) == frozenset({"a" * 32})


def test_an_ambient_alternate_cannot_make_an_incomplete_repository_look_complete(
    git_repo: GitFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    hidden = _unrecorded_landing(coordinator, git_repo.base, "b" * 32)
    git_repo.external_commit("b.txt", "ordinary tip\n", "ordinary tip")
    git_repo.git("repack", "-a", "-d", "-q")

    pack_directory = coordinator.repo.common_git_dir / "objects" / "pack"
    alternate = tmp_path / "alternate-objects"
    alternate_pack = alternate / "pack"
    alternate_pack.mkdir(parents=True)
    packed_files = list(pack_directory.glob("pack-*"))
    assert packed_files
    for packed_file in packed_files:
        packed_file.replace(alternate_pack / packed_file.name)

    assert git_repo.git("log", "--format=%H", "HEAD", "--", check=False).returncode != 0
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", str(alternate))
    ambient_visible = git_repo.git("log", "--format=%H", "HEAD", "--").stdout.split()
    assert hidden in ambient_visible

    with pytest.raises(GitError, match="git log failed"):
        list(coordinator.repo.marked_commits("HEAD"))


@pytest.mark.parametrize("variable", ["GIT_DIR", "GIT_COMMON_DIR"])
def test_ambient_repository_directories_cannot_change_audit_reads(
    git_repo: GitFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    operation = "c" * 32
    hidden = _unrecorded_landing(coordinator, git_repo.base, operation)
    alternate = _repository_with_ref_backend(tmp_path / "alternate", "files")
    monkeypatch.setenv(variable, str(alternate.root / ".git"))

    redirected = git_repo.git("log", "--format=%H", "HEAD", "--", check=False)
    if variable == "GIT_DIR":
        assert redirected.returncode == 0
        assert redirected.stdout.split() == [alternate.base]
    else:
        assert redirected.returncode != 0
        assert "bad object HEAD" in redirected.stderr

    marked = {item.commit: item.operations for item in coordinator.repo.marked_commits("HEAD")}
    assert marked[hidden] == frozenset({operation})
    assert coordinator.repo.landing_operations(hidden) == frozenset({operation})


@pytest.mark.parametrize(
    "suffix",
    [
        pytest.param("\x1e" + "b" * 40 + "\x1f" + "c" * 32, id="forged-record"),
        pytest.param("\x1enot-an-oid", id="malformed-record"),
    ],
)
def test_trailer_control_bytes_cannot_forge_or_break_audit_records(
    git_repo: GitFixture,
    suffix: str,
) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    unrecorded = _unrecorded_landing(
        coordinator,
        git_repo.base,
        "a" * 32 + suffix,
    )
    raw_commit = coordinator.repo.git("cat-file", "commit", unrecorded).stdout
    assert b"\x1e" in raw_commit

    report = coordinator.inspect_wal()

    assert [item.commit for item in report.divergences] == [unrecorded]
    assert not report.consistent


def test_honest_history_past_the_scan_page_never_becomes_inconsistent(
    git_repo: GitFixture,
) -> None:
    """A real log larger than one read chunk retains every complete record."""

    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    _advance_with_ordinary_commits(
        coordinator,
        git_repo.base,
        4_100,
    )

    head = git_repo.oid()
    raw_log = coordinator.repo.git(
        "log",
        "-z",
        "--format=%H%x00",
        head,
        "--",
    ).stdout
    expected = git_repo.git("log", "--format=%H", head, "--").stdout.splitlines()
    marked = list(coordinator.repo.marked_commits(head))

    assert len(raw_log) > 64 * 1024
    assert [item.commit for item in marked] == expected
    assert coordinator.inspect_wal().consistent
    assert coordinator.recover().consistent


def test_a_malformed_stream_record_is_refused_before_it_can_become_an_object_id(
    git_repo: GitFixture,
) -> None:
    repository = Repository(git_repo.root)

    with pytest.raises(GitError, match="malformed target object ID"):
        repository._parse_walked_commit(
            b"truncated-object-id",
            b"f" * 32,
            "refs/heads/main",
        )


def test_a_non_ascii_stream_object_id_is_a_typed_git_failure(
    git_repo: GitFixture,
) -> None:
    repository = Repository(git_repo.root)

    with pytest.raises(GitError, match="commit listing object ID is not ASCII"):
        repository._parse_walked_commit(
            b"\xff" * 40,
            b"f" * 32,
            "refs/heads/main",
        )


@pytest.mark.parametrize(
    ("returncode", "expected_message"),
    [
        pytest.param(0, "malformed commit listing stream", id="successful-child"),
        pytest.param(23, "git log failed with exit 23: deliberate log failure", id="failed-child"),
    ],
)
def test_an_incomplete_stream_defers_to_the_child_exit_status(
    git_repo: GitFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    expected_message: str,
) -> None:
    repository = Repository(git_repo.root)
    real_git = git_module._GIT_EXECUTABLE
    assert real_git is not None
    head = git_repo.oid()
    shim = tmp_path / "git"
    shim.write_text(
        f"#!{sys.executable}\n"
        "import os\n"
        "import sys\n"
        "if 'log' in sys.argv:\n"
        f"    os.write(1, b'{head}\\x00')\n"
        "    os.write(2, b'deliberate log failure')\n"
        f"    raise SystemExit({returncode})\n"
        f"os.execv({str(real_git)!r}, [{str(real_git)!r}, *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setattr(git_module, "_GIT_EXECUTABLE", str(shim))

    with pytest.raises(GitError, match=expected_message):
        list(repository.marked_commits("refs/heads/main"))


def test_a_failed_git_log_cannot_return_a_partial_healthy_audit(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    unrecorded = _unrecorded_landing(coordinator, git_repo.base, "f" * 32)
    tree = coordinator.repo.git_text("show", "-s", "--format=%T", unrecorded)
    first = coordinator.repo.commit_tree(tree, "ordinary one", (unrecorded,))
    second = coordinator.repo.commit_tree(tree, "ordinary two", (first,))
    assert coordinator.repo.update_ref("refs/heads/main", second, unrecorded)

    loose_object = coordinator.repo.git_dir / "objects" / first[:2] / first[2:]
    assert loose_object.is_file()
    loose_object.chmod(0o644)
    loose_object.write_bytes(b"not a git object")
    failed_log = git_repo.git("log", "--format=%H", "refs/heads/main", "--", check=False)
    assert failed_log.returncode != 0

    with pytest.raises(GitError, match="git log failed with exit"):
        coordinator.inspect_wal()


def test_the_streamed_walk_cannot_deadlock_on_a_full_stderr_pipe(
    git_repo: GitFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = Repository(git_repo.root)
    real_git = git_module._GIT_EXECUTABLE
    assert real_git is not None
    head = git_repo.oid()
    shim = tmp_path / "git"
    shim.write_text(
        f"#!{sys.executable}\n"
        "import os\n"
        "import sys\n"
        "if 'log' in sys.argv:\n"
        "    os.write(2, b'w' * 200_000)\n"
        f"    os.write(1, b'{head}\\x00\\x00')\n"
        "    raise SystemExit(0)\n"
        f"os.execv({str(real_git)!r}, [{str(real_git)!r}, *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setattr(git_module, "_GIT_EXECUTABLE", str(shim))

    original_popen = git_module.subprocess.Popen
    children: list[subprocess.Popen[bytes]] = []

    def recording_popen(arguments: Sequence[str], *args: Any, **kwargs: Any) -> Any:
        child = original_popen(arguments, *args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(git_module.subprocess, "Popen", recording_popen)
    finished = threading.Event()
    result: list[object] = []

    def walk() -> None:
        try:
            result.extend(repository.marked_commits("refs/heads/main"))
        except BaseException as error:  # noqa: BLE001 - the worker reports every failure
            result.append(error)
        finally:
            finished.set()

    worker = threading.Thread(target=walk, daemon=True)
    worker.start()
    completed_without_cleanup = finished.wait(timeout=5)
    if not completed_without_cleanup:
        assert children
        children[0].kill()
        worker.join(timeout=5)

    assert completed_without_cleanup, "marked_commits deadlocked on a full stderr pipe"
    assert result == [git_module.MarkedCommit(head, frozenset())]


def test_a_partially_consumed_walk_retains_no_git_process_descriptors(
    git_repo: GitFixture,
) -> None:
    repository = Repository(git_repo.root)
    descriptor_directory = Path("/proc/self/fd")
    assert descriptor_directory.is_dir()
    baseline = len(tuple(descriptor_directory.iterdir()))

    walks = [repository.marked_commits("refs/heads/main") for _ in range(30)]
    assert all(next(walk).commit == git_repo.oid() for walk in walks)

    assert len(tuple(descriptor_directory.iterdir())) <= baseline


def test_the_commit_immediately_below_first_sight_stays_inside_the_audit(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    unrecorded = _unrecorded_landing(coordinator, git_repo.base, "f" * 32)
    git_repo.external_commit("b.txt", "hand\n", "permitted hand commit")
    with pytest.raises(OnetreeError) as refused:
        coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    assert refused.value.kind == "unrecorded_landing"

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
    """Historical name: onetree now refuses before writing even one probe event."""

    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    first = _unrecorded_landing(coordinator, git_repo.base, "f" * 32)

    def refusals() -> list[Any]:
        return [
            event
            for event in coordinator.wal.read().events
            if event.kind == "refused" and event.data.get("reason") == "branch_diverged"
        ]

    wal_before = coordinator.wal.head()
    for _ in range(3):
        with pytest.raises(OnetreeError) as refused:
            coordinator.recover()
        assert refused.value.kind == "unrecorded_landing"
    assert coordinator.wal.head() == wal_before
    assert refusals() == []

    # A different divergence set is still reported without growing the WAL.
    _unrecorded_landing(coordinator, first, "a" * 32)
    with pytest.raises(OnetreeError) as refused:
        coordinator.recover()
    assert refused.value.kind == "unrecorded_landing"
    assert len(coordinator.inspect_wal().divergences) == 2
    assert refusals() == []
    with pytest.raises(OnetreeError):
        coordinator.recover()
    assert coordinator.wal.head() == wal_before


# --------------------------------------------------------------------------
# D9 -- a landing that cannot move the branch does not leave its intent behind
# --------------------------------------------------------------------------


def test_a_locked_target_branch_closes_the_intent_written_ahead_of_it(
    git_repo: GitFixture,
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

    _pending_intent(git_repo, coordinator, session)
    assert coordinator.inspect_wal().pending_intents == 1
    with pytest.raises(ReleaseRefusedError) as fenced:
        coordinator.release("alpha", [str(git_repo.root / "a.txt")])
    assert fenced.value.details["reason"] == "pending_land"
    code, human, _ = _cli("wal", "--repo", str(git_repo.root))
    assert code == 1
    assert "1 pending intent(s)" in human
    branch_lock.unlink()
    assert coordinator.recover().refused == 1
    assert coordinator.release("alpha", [str(git_repo.root / "a.txt")]).released == ("a.txt",)
    assert coordinator.inspect_wal().consistent


# --------------------------------------------------------------------------
# Suspicions carried by independent review
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
    landed = coordinator.land("alpha", message="verified landing").commit
    # A real plumbing failure: the loose object Git must inflate to verify the
    # landing is cut in half, so every reader of it fails inside Git rather
    # than answering "not a commit". The shipped except-arm once caught
    # GitError alongside RefCorruptError, so a broken store was reported as a
    # corrupt WAL and sent the reader to repair the wrong thing; the earlier
    # control injected the failure by replacing require_commit, which the
    # batched read no longer consults for an object Git hands back.
    loose = git_repo.root / ".git" / "objects" / landed[:2] / landed[2:]
    assert loose.exists()
    raw = loose.read_bytes()
    loose.chmod(0o644)
    loose.write_bytes(raw[: len(raw) // 2])
    with pytest.raises(GitError, match="corrupt loose object") as failure:
        coordinator.inspect_wal()
    assert not isinstance(failure.value, WalCorruptError)


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
    """Every intent written before landers were recorded looks like this, so it must work."""

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
# A falsifiable landing bar, and crash states that classify
# --------------------------------------------------------------------------


def _arm_kill_git(
    monkeypatch: pytest.MonkeyPatch,
    kill_git: KillGit,
    *,
    reference: str,
    nth: int,
    mode: str,
    hand_commits: int = 0,
) -> None:
    """Point this process's Git layer at the shim, reversibly.

    ``install_kill_git`` edits the interpreter for good, which is why the
    PPID-kill tests spend a forked child on it. ``self_after`` kills the git
    process and leaves the broker running, so it is observed in-process and
    every mutation has to be undone afterwards.
    """

    for name, value in kill_git.environment(
        reference, nth, mode, hand_commits=hand_commits
    ).items():
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


def test_a_real_competing_commit_causes_a_payload_conflict_without_landing_the_candidate(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    session = _register(coordinator, git_repo, "alpha", "a.txt")
    _stage(git_repo, session, "a.txt", "candidate bytes\n")
    peer_index = coordinator.repo.git_dir / "peer.index"
    peer_commits: list[str] = []

    def peer_wins(point: str, _context: Mapping[str, Any]) -> None:
        if point != "after_intent":
            return
        git_repo.git("read-tree", git_repo.base, index=peer_index)
        git_repo.write("a.txt", "competing bytes\n")
        git_repo.stage(peer_index, "a.txt")
        peer = git_repo.raw_commit(peer_index, git_repo.base, "ordinary peer commit")
        git_repo.git("update-ref", "refs/heads/main", peer, git_repo.base)
        peer_commits.append(peer)

    with pytest.raises(LandingRefusedError) as refused:
        coordinator.land("alpha", message="competing landing", checkpoint=peer_wins)

    assert refused.value.details["reason"] == "payload_conflict"
    intent = next(
        event for event in coordinator.wal.read().events if event.kind == "land_intent"
    )
    assert [git_repo.oid()] == peer_commits
    assert git_repo.oid() != intent.data["candidate"]
    assert git_repo.git("show", "refs/heads/main:a.txt").stdout == "competing bytes\n"
    report = coordinator.inspect_wal()
    assert not any(event.kind == "landed" for event in report.events)
    assert report.pending_intents == 0
    assert report.consistent
    assert coordinator.recover().consistent


@pytest.mark.parametrize("ref_format", ["files", "reftable"])
@pytest.mark.parametrize("hand_commits", [0, 1, 2, 3, 5])
def test_a_crash_left_landing_is_found_under_any_number_of_hand_commits(
    tmp_path: Path,
    ref_format: str,
    hand_commits: int,
) -> None:
    """The scan floor's depth was never the defect; the floor was.

    The mask was measured at one permitted hand commit and the floor moved down
    one generation; the same mask was then measured at two and at three.
    A floor a fixed genealogical distance below first sight is defeated by that
    many hand commits, so the class only closes when the audit stops excluding
    history it has never read.
    """

    git_repo = _repository_with_ref_backend(tmp_path, ref_format)
    coordinator = Coordinator(git_repo.root)
    git_repo.git("remote", "add", "origin", str(tmp_path / "ordinary-remote.git"))
    unrecorded = _unrecorded_landing(coordinator, git_repo.base, "f" * 32)
    for index in range(hand_commits):
        git_repo.external_commit("b.txt", f"hand {index}\n", f"permitted hand commit {index}")

    report = coordinator.inspect_wal()
    assert [item.kind for item in report.divergences] == ["unrecorded_landing"]
    assert report.divergences[0].commit == unrecorded
    assert not report.consistent
    with pytest.raises(OnetreeError) as refused:
        coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    assert refused.value.kind == "unrecorded_landing"
    with pytest.raises(OnetreeError) as recovery_refused:
        coordinator.recover()
    assert recovery_refused.value.kind == "unrecorded_landing"

    adoption = coordinator.adopt_landings()
    assert [event.data["commit"] for event in adoption.adopted] == [unrecorded]
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    assert coordinator.inspect_wal().consistent


@pytest.mark.parametrize("hand_commits", [1, 2, 3, 4, 5, 6])
def test_page_boundaries_never_drop_the_commit_they_land_on(
    git_repo: GitFixture,
    hand_commits: int,
) -> None:
    """The former pagination offsets remain inside the complete audit."""

    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    unrecorded = _unrecorded_landing(coordinator, git_repo.base, "f" * 32)
    for index in range(hand_commits):
        git_repo.external_commit("b.txt", f"hand {index}\n", f"hand commit {index}")
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


@pytest.mark.parametrize(
    ("start", "warnings"),
    [
        (None, [core_module.DEGRADED_LIVENESS]),
        (4242, []),
    ],
)
def test_a_lander_whose_warnings_agree_with_its_birth_tick_is_accepted(
    git_repo: GitFixture,
    start: int | None,
    warnings: list[str],
) -> None:
    """The acceptance half of the pair above, without which it proves nothing.

    A validator fixture that only ever shows refusals cannot tell a correct
    guard apart from one that refuses everything: ``raise`` on the first line of
    ``_validate_lander`` would satisfy every case in
    ``test_a_lander_whose_warnings_contradict_its_birth_tick_is_refused`` and all
    four of its parametrisations. These are the only two agreeing pairs the
    cross-check admits -- degraded reports the warning, healthy reports none --
    and both must survive the append and the read back out.

    Real landings exercise this incidentally, but incidentally is not stated.
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

    appended = coordinator._try_append(coordinator._fold(snapshot), snapshot, (document,))

    assert appended is not None
    # Written, and still readable: the validator runs again on every fold, so an
    # append the door accepted must not become corruption on the way back out.
    report = coordinator.inspect_wal()
    intent = next(event for event in report.events if event.kind == "land_intent")
    assert intent.data["lander"] == {"pid": os.getpid(), "start": start, "warnings": warnings}


@pytest.mark.parametrize("empty_table_list", [b"", b"\n", b"\n\n"])
def test_a_zero_byte_reftable_table_list_refuses_instead_of_answering_absent(
    tmp_path: Path,
    empty_table_list: bytes,
) -> None:
    """A table list naming no table is the reftable state onetree can decide.

    ``show-ref --exists`` answers "no table names this ref" with the same status
    for an empty table list as for a ref that was never written, while the
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
    # A fresh reftable repository always names at least one table, so contents
    # naming none are not a state Git leaves behind -- decidable, not ambiguous.
    assert original
    table_list.write_bytes(empty_table_list)
    try:
        # Git's own verdict on this state, measured rather than assumed: the ref
        # reads absent and both integrity checks bless the repository, so the
        # refusal below is strictly stronger than anything Git will say.
        assert git_repo.git("show-ref", "--exists", WAL_REF, check=False).returncode in {1, 2}
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

    finally:
        table_list.write_bytes(original)
    assert coordinator.inspect_wal().consistent
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


# --------------------------------------------------------------------------
# Predicates sized to the defect, not the old fixture
# --------------------------------------------------------------------------


@pytest.mark.parametrize("ref_format", ["files", "reftable"])
@pytest.mark.parametrize("hand_commits", [0, 1, 2, 3, 5, 10, 40])
def test_a_status_lost_branch_cas_stays_a_win_above_any_hand_commits(
    tmp_path: Path,
    ref_format: str,
    hand_commits: int,
    kill_git: KillGit,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A candidate is on its target when the target contains it, not just at its tip."""

    git_repo = _repository_with_ref_backend(tmp_path, ref_format)
    coordinator = Coordinator(git_repo.root)
    session = _register(coordinator, git_repo, "alpha", "a.txt")
    _stage(git_repo, session, "a.txt", "status lost above candidate\n")
    _arm_kill_git(
        monkeypatch,
        kill_git,
        reference="refs/heads/main",
        nth=1,
        mode="self_after_hand_commits",
        hand_commits=hand_commits,
    )

    result = coordinator.land("alpha", message="status lost above candidate")

    assert git_repo.git(
        "merge-base", "--is-ancestor", result.commit, "refs/heads/main", check=False
    ).returncode == 0
    assert git_repo.git("show", f"{result.commit}:a.txt").stdout == "status lost above candidate\n"
    events = coordinator.wal.read().events
    assert [event.kind for event in events if event.kind == "land_intent"]
    assert [event.data.get("reason") for event in events if event.kind == "refused"] == []
    assert coordinator.inspect_wal().consistent
    assert coordinator.recover().consistent


def test_a_status_lost_wal_intent_append_is_not_retried_under_a_new_operation(
    git_repo: GitFixture,
    kill_git: KillGit,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The WAL append uses the same lost-status predicate as the branch CAS."""

    coordinator = Coordinator(git_repo.root)
    session = _register(coordinator, git_repo, "alpha", "a.txt")
    _stage(git_repo, session, "a.txt", "lost WAL intent status\n")
    _arm_kill_git(
        monkeypatch, kill_git, reference=WAL_REF, nth=1, mode="self_after"
    )

    result = coordinator.land("alpha", message="lost WAL intent status")

    report = coordinator.inspect_wal()
    intents = [event for event in report.events if event.kind == "land_intent"]
    landed = [event for event in report.events if event.kind == "landed"]
    assert [event.data["operation"] for event in intents] == [result.operation]
    assert [event.data["operation"] for event in landed] == [result.operation]
    assert report.pending_intents == 0
    assert report.consistent
    assert coordinator.recover().consistent


def test_reftable_table_replacement_during_an_absent_probe_is_not_corruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid concurrent ``tables.list`` replacements are traffic, not damage."""

    git_repo = _repository_with_ref_backend(tmp_path, "reftable")
    repository = Repository(git_repo.root)
    table_list = repository.git_dir / "reftable" / "tables.list"
    original_read_bytes = Path.read_bytes
    original = original_read_bytes(table_list)
    reads = 0

    def changing_read_bytes(path: Path) -> bytes:
        nonlocal reads
        if path == table_list:
            reads += 1
            return original if reads % 2 else original + b"\n"
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", changing_read_bytes)

    assert repository.resolve_ref("refs/onetree/absent", missing_ok=True) is None
    assert reads >= 2


def test_reftable_table_open_race_cannot_turn_an_absent_ref_into_corruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ref resolution never opens a listed table, so compaction cannot corrupt it."""

    git_repo = _repository_with_ref_backend(tmp_path, "reftable")
    repository = Repository(git_repo.root)
    table_list = repository.git_dir / "reftable" / "tables.list"
    table = table_list.parent / table_list.read_text(encoding="utf-8").splitlines()[0]
    original_open = Path.open
    opened_table = 0

    def vanishing_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        nonlocal opened_table
        if path == table:
            opened_table += 1
            raise FileNotFoundError("compaction removed the old table")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", vanishing_open)

    assert repository.resolve_ref("refs/onetree/absent", missing_ok=True) is None
    assert opened_table == 0


@pytest.mark.parametrize("ref_format", ["files", "reftable"])
def test_an_absent_wal_stays_absent_under_two_thousand_ordinary_ref_writes(
    tmp_path: Path,
    ref_format: str,
) -> None:
    """Historical name; the measured sweep is now 2,500 attempts."""

    git_repo = _repository_with_ref_backend(tmp_path, ref_format)
    repository = Repository(git_repo.root)
    stop = threading.Event()
    writer_error: list[BaseException] = []

    def writer() -> None:
        index = 0
        try:
            while not stop.is_set():
                reference = f"refs/heads/peer{index % 3}"
                git_repo.git("update-ref", reference, git_repo.base)
                git_repo.git("update-ref", "-d", reference)
                index += 1
        except BaseException as error:  # the test thread must report, not vanish
            writer_error.append(error)

    peer = threading.Thread(target=writer, daemon=True)
    peer.start()
    errors: list[RefCorruptError] = []
    try:
        for _ in range(2_500):
            try:
                assert repository.resolve_ref(WAL_REF, missing_ok=True) is None
            except RefCorruptError as error:
                errors.append(error)
    finally:
        stop.set()
        peer.join(timeout=10)

    assert not peer.is_alive()
    assert writer_error == []
    assert errors == []


def test_the_branch_audit_uses_one_log_without_skip_pagination(
    git_repo: GitFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The full audit is one history walk, not repeatedly re-walked pages."""

    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    unrecorded = _unrecorded_landing(coordinator, git_repo.base, "f" * 32)
    for index in range(6):
        git_repo.external_commit("b.txt", f"hand {index}\n", f"hand commit {index}")

    original_popen = git_module.subprocess.Popen
    log_calls: list[tuple[str, ...]] = []

    def recording_popen(arguments: Sequence[str], *args: Any, **kwargs: Any) -> Any:
        if "log" in arguments:
            log_calls.append(tuple(arguments))
        return original_popen(arguments, *args, **kwargs)

    monkeypatch.setattr(git_module.subprocess, "Popen", recording_popen)

    report = coordinator.inspect_wal()

    assert [item.commit for item in report.divergences] == [unrecorded]
    assert len(log_calls) == 1
    assert all(not argument.startswith("--skip=") for argument in log_calls[0])
    assert all(not argument.startswith("--max-count=") for argument in log_calls[0])


def test_a_plain_clone_missing_the_wal_is_quarantined_before_it_can_append(
    tmp_path: Path,
) -> None:
    """An ordinary push omits the WAL; local adoption is the explicit escape."""

    _, remote, landed = _published_broker_repository(tmp_path)

    clone = tmp_path / "clone"
    subprocess.run(
        [_GIT_EXECUTABLE, "clone", "--quiet", str(remote), str(clone)],
        check=True,
        capture_output=True,
        text=True,
    )
    coordinator = Coordinator(clone)
    assert coordinator.wal.head() is None

    report = coordinator.inspect_wal()
    assert not report.consistent
    assert [item.kind for item in report.divergences] == ["unrecorded_landing"]
    assert report.divergences[0].commit == landed
    clear = coordinator.check("beta", str(clone / "a.txt"))
    assert not clear.blocked

    with pytest.raises(OnetreeError) as refused:
        coordinator.register_session("beta", pid=os.getpid(), ttl=300)
    assert refused.value.kind == "unrecorded_landing"
    assert coordinator.wal.read().events == ()

    with pytest.raises(OnetreeError) as recovery_refused:
        coordinator.recover()
    assert recovery_refused.value.kind == "unrecorded_landing"
    code, output, error = _cli("wal", "--repo", str(clone), "--json")
    assert (code, error) == (1, "")
    assert json.loads(output)["wal"]["divergences"][0]["kind"] == "unrecorded_landing"

    code, output, error = _cli("wal", "--repo", str(clone), "--adopt", "--json")
    assert (code, error) == (0, "")
    adoption = json.loads(output)["adoption"]
    assert adoption["consistent"] is True
    assert adoption["commits"] == [landed]
    coordinator.register_session("beta", pid=os.getpid(), ttl=300)
    assert coordinator.recover().consistent


@pytest.mark.parametrize(
    "clone_shape",
    ["upstream", "renamed", "detached", "non_head_target"],
)
def test_missing_wal_detection_and_adoption_do_not_depend_on_clone_shape(
    tmp_path: Path,
    clone_shape: str,
) -> None:
    _, remote, landed = _published_broker_repository(tmp_path)
    clone = tmp_path / "clone"
    clone_command = [_GIT_EXECUTABLE, "clone", "--quiet"]
    if clone_shape == "upstream":
        clone_command.extend(("--origin", "upstream"))
    clone_command.extend((str(remote), str(clone)))
    subprocess.run(clone_command, check=True, capture_output=True, text=True)
    fixture = GitFixture(clone, landed)
    target = "refs/heads/main"
    if clone_shape == "renamed":
        fixture.git("remote", "rename", "origin", "upstream")
    elif clone_shape == "detached":
        fixture.git("checkout", "--quiet", "--detach", target)
    elif clone_shape == "non_head_target":
        fixture.git("checkout", "--quiet", "clean")

    coordinator = Coordinator(clone)
    report = coordinator.inspect_wal(target=target)
    assert [(item.kind, item.commit) for item in report.divergences] == [
        ("unrecorded_landing", landed)
    ]
    with pytest.raises(OnetreeError) as refused:
        coordinator.register_session("beta", pid=os.getpid(), ttl=300, target=target)
    assert refused.value.kind == "unrecorded_landing"

    code, output, error = _cli(
        "wal", "--repo", str(clone), "--target", target, "--adopt", "--json"
    )
    assert (code, error) == (0, "")
    assert json.loads(output)["adoption"]["commits"] == [landed]
    coordinator.register_session("beta", pid=os.getpid(), ttl=300, target=target)


def test_a_landing_on_another_local_branch_blocks_the_first_wal_append(
    tmp_path: Path,
) -> None:
    _, remote, landed = _published_broker_repository(tmp_path)
    clone = tmp_path / "clone"
    subprocess.run(
        [_GIT_EXECUTABLE, "clone", "--quiet", str(remote), str(clone)],
        check=True,
        capture_output=True,
        text=True,
    )
    fixture = GitFixture(clone, landed)
    fixture.git("checkout", "--quiet", "clean")
    coordinator = Coordinator(clone)
    with pytest.raises(OnetreeError) as refused:
        coordinator.register_session(
            "clean-session", pid=os.getpid(), ttl=300, target="refs/heads/clean"
        )
    assert refused.value.kind == "unrecorded_landing"
    assert coordinator.wal.head() is None

    adoption = coordinator.adopt_landings()
    assert [event.data["commit"] for event in adoption.adopted] == [landed]
    coordinator.register_session(
        "clean-session", pid=os.getpid(), ttl=300, target="refs/heads/clean"
    )


def test_an_ordinary_message_mention_has_an_adoption_escape(
    git_repo: GitFixture,
) -> None:
    git_repo.git("remote", "add", "origin", "unused://ordinary-remote")
    commit = git_repo.external_commit(
        "a.txt",
        "ordinary\n",
        "ordinary discussion\n\n"
        f"Onetree-Operation: {'d' * 32}\n\n"
        "The exact line above is documentation, not a final trailer.",
    )
    coordinator = Coordinator(git_repo.root)

    report = coordinator.inspect_wal()
    assert [(item.kind, item.commit) for item in report.divergences] == [
        ("unrecorded_landing", commit)
    ]
    adoption = coordinator.adopt_landings()
    assert [event.data["commit"] for event in adoption.adopted] == [commit]
    assert adoption.report.consistent
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)


@pytest.mark.parametrize(("depth", "landing_visible"), [(3, False), (4, True)])
def test_shallow_history_is_an_explicit_audit_boundary(
    tmp_path: Path,
    depth: int,
    landing_visible: bool,
) -> None:
    _, remote, landed = _published_broker_repository(tmp_path, hand_commits=3)
    clone = tmp_path / "clone"
    subprocess.run(
        [
            _GIT_EXECUTABLE,
            "clone",
            "--quiet",
            f"--depth={depth}",
            f"file://{remote}",
            str(clone),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    coordinator = Coordinator(clone)
    visible = {item.commit for item in coordinator.repo.marked_commits("refs/heads/main")}
    assert (landed in visible) is landing_visible

    report = coordinator.inspect_wal()
    kinds = [item.kind for item in report.divergences]
    assert "shallow_history" in kinds
    assert ("unrecorded_landing" in kinds) is landing_visible
    with pytest.raises(OnetreeError) as refused:
        coordinator.register_session("beta", pid=os.getpid(), ttl=300)
    assert refused.value.kind == "shallow_history"


def test_a_non_root_shallow_marker_refuses_with_its_parent_object_present(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    hidden = _unrecorded_landing(coordinator, git_repo.base, "a" * 32)
    boundary = git_repo.external_commit("b.txt", "ordinary tip\n", "ordinary tip")
    marker = coordinator.repo.git_path("shallow")
    marker.write_text(boundary + "\n", encoding="ascii")

    raw_boundary = coordinator.repo.git("cat-file", "commit", boundary).stdout
    assert f"parent {hidden}\n".encode() in raw_boundary
    assert coordinator.repo.object_type(hidden) == "commit"
    assert git_repo.git("rev-parse", f"{boundary}^", check=False).returncode == 128
    visible = set(git_repo.git("log", "--format=%H", "HEAD", "--").stdout.split())
    assert hidden not in visible

    report = coordinator.inspect_wal()
    assert not report.consistent
    assert {item.kind for item in report.divergences} == {"shallow_history"}
    with pytest.raises(OnetreeError) as refused:
        coordinator.register_session("beta", pid=os.getpid(), ttl=300)
    assert refused.value.kind == "shallow_history"


def test_an_ambient_shallow_file_cannot_make_the_guard_and_walk_disagree(
    git_repo: GitFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    hidden = _unrecorded_landing(coordinator, git_repo.base, "b" * 32)
    boundary = git_repo.external_commit("b.txt", "ordinary tip\n", "ordinary tip")
    ambient_marker = tmp_path / "ambient-shallow"
    ambient_marker.write_text(boundary + "\n", encoding="ascii")
    monkeypatch.setenv("GIT_SHALLOW_FILE", str(ambient_marker))

    assert not coordinator.repo.git_path("shallow").exists()
    ambient_visible = set(
        git_repo.git("log", "--format=%H", "refs/heads/main", "--").stdout.split()
    )
    assert hidden not in ambient_visible

    report = coordinator.inspect_wal()
    assert [(item.kind, item.commit) for item in report.divergences] == [
        ("unrecorded_landing", hidden)
    ]
    with pytest.raises(OnetreeError) as refused:
        coordinator.register_session("beta", pid=os.getpid(), ttl=300)
    assert refused.value.kind == "unrecorded_landing"


@pytest.mark.parametrize("boundary_kind", ["blob", "missing"])
def test_a_non_commit_shallow_boundary_fails_closed(
    git_repo: GitFixture,
    boundary_kind: str,
) -> None:
    repository = Repository(git_repo.root)
    boundary = (
        git_repo.git("rev-parse", "HEAD:a.txt").stdout.strip()
        if boundary_kind == "blob"
        else "1" * repository.oid_length
    )
    repository.git_path("shallow").write_text(boundary + "\n", encoding="ascii")

    assert repository.git_text("rev-parse", "--is-shallow-repository") == "true"
    assert repository.is_shallow() is True


def test_an_unreadable_shallow_marker_is_a_typed_git_failure(
    git_repo: GitFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = Repository(git_repo.root)
    repository.git_path("shallow").mkdir()
    monkeypatch.setattr(repository, "git_text", lambda *_args, **_kwargs: "true")

    with pytest.raises(GitError, match="cannot read the shallow marker"):
        repository.is_shallow()


def test_a_non_ascii_shallow_marker_is_a_typed_git_failure(
    git_repo: GitFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = Repository(git_repo.root)
    repository.git_path("shallow").write_bytes(b"\xff" * repository.oid_length + b"\n")
    monkeypatch.setattr(repository, "git_text", lambda *_args, **_kwargs: "true")

    with pytest.raises(GitError, match="shallow marker contains a non-ASCII object ID"):
        repository.is_shallow()


def test_a_root_shallow_marker_does_not_truncate_complete_history(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    session = _register(coordinator, git_repo, "alpha", "a.txt")
    _stage(git_repo, session, "a.txt", "broker landing\n")
    coordinator.land("alpha", message="broker landing")

    root = git_repo.git("rev-list", "--max-parents=0", "HEAD").stdout.split()[0]
    marker = coordinator.repo.git_path("shallow")
    marker.write_text(root + "\n", encoding="ascii")

    # Git reports a shallow repository, but grafting a genuine root to zero
    # parents cannot remove any history because its raw commit already has none.
    assert git_repo.git("rev-parse", "--is-shallow-repository").stdout.strip() == "true"
    assert coordinator.repo.is_shallow() is False
    assert coordinator.inspect_wal().consistent
    coordinator.register_session("beta", pid=os.getpid(), ttl=300)


def test_a_valid_commit_graph_cannot_hide_an_unrecorded_landing(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    hidden = _unrecorded_landing(coordinator, git_repo.base, "c" * 32)
    git_repo.external_commit("b.txt", "ordinary tip\n", "ordinary tip")
    git_repo.git("commit-graph", "write", "--reachable")
    commit_graph = coordinator.repo.common_git_dir / "objects" / "info" / "commit-graph"

    assert commit_graph.is_file()
    assert git_repo.git("commit-graph", "verify", check=False).returncode == 0
    assert hidden in git_repo.git("log", "--format=%H", "HEAD", "--").stdout.split()
    report = coordinator.inspect_wal()
    assert [(item.kind, item.commit) for item in report.divergences] == [
        ("unrecorded_landing", hidden)
    ]
    with pytest.raises(OnetreeError) as refused:
        coordinator.register_session("beta", pid=os.getpid(), ttl=300)
    assert refused.value.kind == "unrecorded_landing"


@pytest.mark.parametrize("graph_storage", ["monolithic", "split-chain"])
def test_a_checksum_valid_commit_graph_cannot_hide_an_unrecorded_landing(
    git_repo: GitFixture,
    graph_storage: str,
) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    hidden = _unrecorded_landing(coordinator, git_repo.base, "d" * 32)
    tip = git_repo.external_commit("b.txt", "ordinary tip\n", "ordinary tip")
    info = coordinator.repo.common_git_dir / "objects" / "info"
    if graph_storage == "monolithic":
        git_repo.git("commit-graph", "write", "--reachable")
        commit_graph = info / "commit-graph"
    else:
        graph_directory = info / "commit-graphs"
        git_repo.git("commit-graph", "write", "--reachable", "--split=no-merge")
        first_hash = (graph_directory / "commit-graph-chain").read_text(
            encoding="ascii"
        ).strip()
        git_repo.external_commit("c.txt", "new chain tip\n", "new chain tip")
        git_repo.git("commit-graph", "write", "--reachable", "--split=no-merge")
        graph_hashes = (graph_directory / "commit-graph-chain").read_text(
            encoding="ascii"
        ).splitlines()
        assert len(graph_hashes) == 2
        assert graph_hashes[0] == first_hash
        commit_graph = graph_directory / f"graph-{first_hash}.graph"
    _make_commit_graph_entry_parentless(commit_graph, tip)

    raw_tip = coordinator.repo.git("cat-file", "commit", tip).stdout
    assert f"parent {hidden}\n".encode() in raw_tip
    assert git_repo.git("commit-graph", "verify", check=False).returncode == 1
    ordinary_visible = git_repo.git("log", "--format=%H", "HEAD", "--").stdout.split()
    raw_visible = git_repo.git(
        "-c", "core.commitGraph=false", "log", "--format=%H", "HEAD", "--"
    ).stdout.split()
    assert hidden not in ordinary_visible
    assert hidden in raw_visible

    report = coordinator.inspect_wal()
    assert [(item.kind, item.commit) for item in report.divergences] == [
        ("unrecorded_landing", hidden)
    ]
    with pytest.raises(OnetreeError) as refused:
        coordinator.register_session("beta", pid=os.getpid(), ttl=300)
    assert refused.value.kind == "unrecorded_landing"


def test_an_ambient_git_test_override_cannot_reenable_commit_graphs(
    git_repo: GitFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git's test override must not outrank the disabled commit-graph reader."""

    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    hidden = _unrecorded_landing(coordinator, git_repo.base, "9" * 32)
    tip = git_repo.external_commit("b.txt", "ordinary tip\n", "ordinary tip")
    git_repo.git("commit-graph", "write", "--reachable")
    commit_graph = coordinator.repo.common_git_dir / "objects" / "info" / "commit-graph"
    _make_commit_graph_entry_parentless(commit_graph, tip)
    monkeypatch.setenv("GIT_TEST_COMMIT_GRAPH", "1")

    overridden = git_repo.git(
        "-c", "core.commitGraph=false", "log", "--format=%H", "HEAD", "--"
    ).stdout.split()
    assert hidden not in overridden

    report = coordinator.inspect_wal()
    assert [(item.kind, item.commit) for item in report.divergences] == [
        ("unrecorded_landing", hidden)
    ]
    with pytest.raises(OnetreeError) as refused:
        coordinator.register_session("beta", pid=os.getpid(), ttl=300)
    assert refused.value.kind == "unrecorded_landing"


def test_git_log_z_terminates_every_audit_record(git_repo: GitFixture) -> None:
    first = git_repo.oid()
    second = git_repo.external_commit("b.txt", "ordinary tip\n", "ordinary tip")

    raw = git_repo.git("log", "-z", "--format=%H%x00", "HEAD", "--").stdout.encode()

    assert raw == second.encode() + b"\x00\x00" + first.encode() + b"\x00\x00"


def test_linked_worktrees_are_refused_before_they_can_split_shared_wal_state(
    git_repo: GitFixture,
    tmp_path: Path,
) -> None:
    """Gate 1 is one working tree, enforced where every entry point starts."""

    linked = tmp_path / "linked"
    git_repo.git("worktree", "add", "--detach", str(linked), git_repo.base)

    with pytest.raises(UnsupportedWorktreeError) as refused:
        Coordinator(linked)

    assert refused.value.kind == "unsupported_worktree"
    assert not (git_repo.root / ".git" / "onetree").exists()


def test_primary_repository_shapes_are_not_misclassified_as_linked_worktrees(
    tmp_path: Path,
) -> None:
    """The one-worktree rule is about shared Git dirs, not unusual paths."""

    primary = _repository_with_ref_backend(tmp_path / "primary", "files")

    separate_work = tmp_path / "separate-work"
    separate_git = tmp_path / "separate.git"
    subprocess.run(
        [
            _GIT_EXECUTABLE,
            "init",
            "--quiet",
            "--initial-branch=main",
            f"--separate-git-dir={separate_git}",
            separate_work,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    separate = GitFixture(separate_work, "")
    separate.git("config", "user.name", "Separate Tests")
    separate.git("config", "user.email", "separate@example.invalid")
    separate.write("a.txt", "a\n")
    separate.git("add", "a.txt")
    separate.git("commit", "--quiet", "-m", "baseline")

    symlinked_git = _repository_with_ref_backend(tmp_path / "symlinked-git", "files")
    real_git = tmp_path / "real-symlinked.git"
    symlinked_git.root.joinpath(".git").rename(real_git)
    symlinked_git.root.joinpath(".git").symlink_to(real_git, target_is_directory=True)

    symlinked_root = tmp_path / "repository-link"
    symlinked_root.symlink_to(primary.root, target_is_directory=True)

    outer = _repository_with_ref_backend(tmp_path / "outer", "files")
    inner = _repository_with_ref_backend(tmp_path / "inner", "files")
    outer.git(
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "--quiet",
        str(inner.root),
        "sub",
    )
    outer.git("commit", "--quiet", "-m", "add submodule")

    for path in (
        primary.root,
        separate_work,
        symlinked_git.root,
        symlinked_root,
        outer.root / "sub",
    ):
        repository = Repository(path)
        assert repository.git_dir == repository.common_git_dir
