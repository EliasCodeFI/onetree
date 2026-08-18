"""Round AF: ref corruption, branch-versus-WAL divergence, locks, and residue."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from conftest import GitFixture

from onetree import core
from onetree.core import Coordinator
from onetree.errors import (
    GitError,
    InvalidPathError,
    RefCorruptError,
    RefLockedError,
    WalCorruptError,
    WalDivergedError,
)
from onetree.git import Repository
from onetree.proc import process_start
from onetree.wal import WAL_REF

_DANGLING = "0123456789" * 4


def _write_ref(git_repo: GitFixture, reference: str, value: str) -> None:
    """Point a ref at raw bytes, the way an interrupted writer can leave it."""

    path = git_repo.root / ".git" / reference
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{value}\n", encoding="utf-8")


def _stage_and_land(git_repo: GitFixture, coordinator: Coordinator, actor: str) -> str:
    session = coordinator.register_session(actor, pid=os.getpid(), ttl=300)
    coordinator.claim(actor, [str(git_repo.root / "a.txt")])
    git_repo.write("a.txt", "landed payload\n")
    git_repo.stage(session.index_path, "a.txt")
    return coordinator.land(actor, message="payload").commit


def _forge_landing(
    git_repo: GitFixture,
    coordinator: Coordinator,
    *,
    candidate: str,
    expected_head: str,
    tree: str,
    landed_override: Mapping[str, Any] | None = None,
) -> None:
    """Append a well-formed intent/landed pair that may disagree with Git or itself."""

    entry = {"mode": "100644", "oid": git_repo.base}
    operation = "a" * 32
    intent = coordinator.wal.document(
        "land_intent",
        "alpha",
        {
            "operation": operation,
            "attempt": 1,
            "target": "refs/heads/main",
            "expected_head": expected_head,
            "candidate": candidate,
            "tree": tree,
            "session_generation": "session",
            "fences": [{"path": "a.txt", "claim_generation": "claim"}],
            "payload": [{"path": "a.txt", "old": entry, "new": None}],
        },
    )
    landed = coordinator.wal.document(
        "landed",
        "alpha",
        {
            "operation": operation,
            "intent": intent["event_id"],
            "target": "refs/heads/main",
            "expected_head": expected_head,
            "commit": candidate,
            "tree": tree,
            "payload": ["a.txt"],
            "recovered": False,
            **dict(landed_override or {}),
        },
    )
    snapshot = coordinator.wal.read()
    assert coordinator.wal.try_append((intent, landed), snapshot.head) is not None


@pytest.mark.parametrize("shape", ["blob", "tree", "dangling", "unreadable"])
def test_corrupt_wal_ref_is_refused_instead_of_reading_as_an_empty_healthy_wal(
    git_repo: GitFixture,
    shape: str,
) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    coordinator.claim("alpha", [str(git_repo.root / "a.txt")])
    assert coordinator.check("alpha", str(git_repo.root / "a.txt")).owned

    values = {
        "blob": coordinator.repo.hash_blob(b"not a commit\n"),
        "tree": git_repo.git("rev-parse", f"{git_repo.base}^{{tree}}").stdout.strip(),
        "dangling": _DANGLING,
        "unreadable": "not-an-object-id",
    }
    _write_ref(git_repo, WAL_REF, values[shape])

    # RED control: the probe Round A relied on cannot tell this ref from an
    # absent one. Both exit 1, so `missing_ok` read the corruption as "no WAL",
    # and every claim question was then answered over an empty log.
    corrupt = git_repo.git(
        "rev-parse", "--verify", "--quiet", "--end-of-options", f"{WAL_REF}^{{commit}}", check=False
    )
    absent = git_repo.git(
        "rev-parse",
        "--verify",
        "--quiet",
        "--end-of-options",
        "refs/onetree/absent^{commit}",
        check=False,
    )
    assert corrupt.returncode == absent.returncode == 1

    for probe in (
        lambda: coordinator.check("alpha", str(git_repo.root / "a.txt")),
        coordinator.inspect_wal,
        coordinator.recover,
        lambda: coordinator.claim("alpha", [str(git_repo.root / "b.txt")]),
        lambda: coordinator.gc("collector"),
    ):
        with pytest.raises(WalCorruptError, match="corrupt"):
            probe()


def test_resolve_ref_separates_absent_corrupt_and_present(git_repo: GitFixture) -> None:
    repository = Repository(git_repo.root)
    assert repository.resolve_ref("refs/heads/main") == git_repo.base
    assert repository.resolve_ref("refs/heads/missing", missing_ok=True) is None
    assert repository.object_type(git_repo.base) == "commit"
    assert repository.object_type(_DANGLING) is None

    blob = repository.hash_blob(b"blob\n")
    _write_ref(git_repo, "refs/onetree/corrupt", blob)
    # `missing_ok` never converts a present-but-wrong ref into an absent one.
    for missing_ok in (True, False):
        with pytest.raises(RefCorruptError, match="does not resolve to a commit") as corrupt:
            repository.resolve_ref("refs/onetree/corrupt", missing_ok=missing_ok)
        assert corrupt.value.details == {
            "reference": "refs/onetree/corrupt",
            "oid": blob,
            "object_type": "blob",
        }

    _write_ref(git_repo, "refs/onetree/unreadable", "definitely-not-an-oid")
    with pytest.raises(RefCorruptError, match="cannot be read"):
        repository.resolve_ref("refs/onetree/unreadable", missing_ok=True)


def test_branch_rollback_and_missing_target_are_reported_as_divergence(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    commit = _stage_and_land(git_repo, coordinator, "alpha")
    assert coordinator.inspect_wal().consistent

    # Direction B: the WAL records a landing the branch no longer contains.
    git_repo.git("update-ref", "refs/heads/main", git_repo.base, commit)
    report = coordinator.inspect_wal()
    assert not report.consistent
    assert [item.kind for item in report.divergences] == ["landed_not_on_target"]
    assert report.divergences[0].commit == commit
    with pytest.raises(WalDivergedError):
        coordinator.recover()

    # The target disappearing entirely is the same disagreement, named separately.
    git_repo.git("update-ref", "-d", "refs/heads/main")
    assert [item.kind for item in coordinator.inspect_wal().divergences] == ["target_missing"]


def test_a_refused_candidate_on_the_branch_is_reported_as_divergence(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    session = coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    claim = coordinator.claim("alpha", [str(git_repo.root / "a.txt")]).claims[0]
    git_repo.write("a.txt", "refused payload\n")
    git_repo.stage(session.index_path, "a.txt")
    tree = coordinator.repo.write_tree(session.index_path)
    candidate = coordinator.repo.commit_tree(
        tree,
        coordinator.repo.landing_message("onetree land: alpha", "b" * 32, "c" * 32, "alpha"),
        (git_repo.base,),
    )
    entry = coordinator.repo.tree_entries(git_repo.base)["a.txt"].payload()
    new_entry = coordinator.repo.tree_entries(tree)["a.txt"].payload()
    intent = coordinator.wal.document(
        "land_intent",
        "alpha",
        {
            "operation": "b" * 32,
            "attempt": 1,
            "target": "refs/heads/main",
            "expected_head": git_repo.base,
            "candidate": candidate,
            "tree": tree,
            "session_generation": session.generation,
            "lander": {"pid": 999_999_999, "start": 1},
            "fences": [{"path": "a.txt", "claim_generation": claim.generation}],
            "payload": [{"path": "a.txt", "old": entry, "new": new_entry}],
        },
        event_id="c" * 32,
    )
    snapshot = coordinator.wal.read()
    assert coordinator.wal.try_append((intent,), snapshot.head) is not None

    refused = coordinator.recover()
    assert refused.refused == 1

    # The branch then acquires the very candidate the WAL refused.
    git_repo.git("update-ref", "refs/heads/main", candidate, git_repo.base)
    report = coordinator.inspect_wal()
    assert [item.kind for item in report.divergences] == ["refused_candidate_on_target"]
    assert report.divergences[0].operation == "b" * 32
    with pytest.raises(WalDivergedError) as diverged:
        coordinator.recover()
    assert diverged.value.details["divergences"][0]["commit"] == candidate


def test_permitted_hand_commits_on_the_target_are_not_divergence(
    git_repo: GitFixture,
) -> None:
    """Ordinary Git is still allowed on these branches before the dogfood cutover.

    Divergence detection therefore keys on the landing trailer the broker writes,
    not on the mere presence of a commit the WAL never saw.
    """

    coordinator = Coordinator(git_repo.root)
    _stage_and_land(git_repo, coordinator, "alpha")
    git_repo.external_commit("b.txt", "hand edit\n", "ordinary commit")
    report = coordinator.inspect_wal()
    assert report.consistent
    assert report.divergences == ()
    assert coordinator.recover().consistent


def test_a_stale_ref_lock_is_named_in_a_typed_refusal_and_never_deleted(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    lock = coordinator.repo.ref_lock_path(WAL_REF)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("", encoding="utf-8")

    assert str(lock) in coordinator.inspect_wal().ref_locks
    with pytest.raises(RefLockedError) as refused:
        coordinator.recover()
    assert refused.value.details["lock"] == str(lock)
    assert refused.value.details["reference"] == WAL_REF
    # A lock names no owner, so recovery reports it rather than guessing.
    assert lock.exists()

    with pytest.raises(RefLockedError):
        coordinator.gc("collector")
    assert lock.exists()

    lock.unlink()
    assert coordinator.recover().consistent


def test_update_ref_separates_a_lost_race_from_a_genuine_git_failure(
    git_repo: GitFixture,
) -> None:
    repository = Repository(git_repo.root)
    other = git_repo.external_commit("b.txt", "moved\n", "move the branch")

    # A lost race: the expected value is stale, so the caller may re-audit.
    assert repository.update_ref("refs/heads/main", git_repo.base, git_repo.base) is False

    # A genuine failure at the current expected value is not a lost race, and
    # misreading it as one would spin the caller's CAS loop to its retry limit.
    with pytest.raises(GitError, match="cannot update ref"):
        repository.update_ref("refs/heads/main", _DANGLING, other)
    assert git_repo.oid() == other

    lock = repository.ref_lock_path("refs/heads/main")
    lock.write_text("", encoding="utf-8")
    try:
        with pytest.raises(RefLockedError, match="locked"):
            repository.update_ref("refs/heads/main", git_repo.base, other)
    finally:
        lock.unlink()
    assert repository.update_ref("refs/heads/main", git_repo.base, other) is True


def test_a_crash_leaving_only_head_lock_is_still_reported_as_lock_contention(
    git_repo: GitFixture,
) -> None:
    """Measured, not reasoned: a real SIGKILL inside ``update-ref refs/heads/main``.

    Moving the branch HEAD points at also writes the HEAD reflog, so Git takes
    ``HEAD.lock`` alongside the branch's own lock. The kill landed between the
    two and left only ``HEAD.lock`` behind. Naming just the branch lock sends an
    operator to a file that is not there, and the retry afterwards fails as an
    unattributed Git error naming a lock nothing reported.
    """

    coordinator = Coordinator(git_repo.root)
    repository = coordinator.repo
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    base_tree = git_repo.git("show", "-s", "--format=%T", git_repo.base).stdout.strip()
    candidate = repository.commit_tree(base_tree, "candidate", (git_repo.base,))

    assert repository.symbolic_head() == "refs/heads/main"
    branch_lock = repository.ref_lock_path("refs/heads/main")
    head_lock = repository.ref_lock_path("HEAD")
    assert head_lock != branch_lock
    assert repository.ref_lock_paths("refs/heads/main") == (branch_lock, head_lock)

    head_lock.write_text("", encoding="utf-8")
    with pytest.raises(RefLockedError, match="locked") as refused:
        repository.update_ref("refs/heads/main", candidate, git_repo.base)
    assert not branch_lock.exists()
    assert refused.value.details["lock"] == str(head_lock)
    assert refused.value.details["locks"] == [str(head_lock)]
    assert git_repo.oid() == git_repo.base
    assert str(head_lock) in coordinator.inspect_wal().ref_locks

    head_lock.unlink()
    assert coordinator.inspect_wal().ref_locks == ()
    assert repository.update_ref("refs/heads/main", candidate, git_repo.base) is True

    # A detached HEAD is not locked by a branch update, so it is not reported.
    git_repo.git("checkout", "--detach", "--quiet", candidate)
    assert repository.symbolic_head() is None
    assert repository.ref_lock_paths("refs/heads/main") == (branch_lock,)


@pytest.mark.parametrize("arm", ["missing_commit", "wrong_parent", "wrong_tree"])
def test_landed_verification_refuses_each_disagreement_with_git(
    git_repo: GitFixture,
    arm: str,
) -> None:
    coordinator = Coordinator(git_repo.root)
    base_tree = git_repo.git("show", "-s", "--format=%T", git_repo.base).stdout.strip()
    other = git_repo.external_commit("b.txt", "other\n", "other")
    other_tree = git_repo.git("show", "-s", "--format=%T", other).stdout.strip()
    candidate = coordinator.repo.commit_tree(base_tree, "candidate", (git_repo.base,))

    if arm == "missing_commit":
        _forge_landing(
            git_repo,
            coordinator,
            candidate=_DANGLING,
            expected_head=git_repo.base,
            tree=base_tree,
        )
        match = "does not resolve"
    elif arm == "wrong_parent":
        _forge_landing(
            git_repo, coordinator, candidate=candidate, expected_head=other, tree=base_tree
        )
        match = "parent differs"
    else:
        _forge_landing(
            git_repo,
            coordinator,
            candidate=candidate,
            expected_head=git_repo.base,
            tree=other_tree,
        )
        match = "tree differs"

    with pytest.raises(WalCorruptError, match=match):
        coordinator.inspect_wal()
    with pytest.raises(WalCorruptError, match=match):
        coordinator.recover()


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("commit", "e" * 40, "differs from its intent"),
        ("target", "refs/heads/other", "differs from its intent"),
        ("expected_head", "e" * 40, "differs from its intent"),
        ("tree", "e" * 40, "differs from its intent"),
        ("payload", ["b.txt"], "payload differs from its intent"),
        ("payload", "a.txt", "landed.payload is invalid"),
        ("recovered", "yes", "recovered is invalid"),
    ],
)
def test_a_landed_event_that_disagrees_with_its_intent_refuses_replay(
    git_repo: GitFixture,
    field: str,
    value: Any,
    match: str,
) -> None:
    """Write-ahead ordering removed the adjacency rule; agreement replaces it.

    Round A could require the landed commit to sit directly on its intent,
    because one transaction wrote both. The branch CAS now sits between them and
    an unrelated append may legitimately intervene, so the landed event has to
    be pinned to its intent field by field instead.
    """

    coordinator = Coordinator(git_repo.root)
    base_tree = git_repo.git("show", "-s", "--format=%T", git_repo.base).stdout.strip()
    candidate = coordinator.repo.commit_tree(base_tree, "candidate", (git_repo.base,))
    _forge_landing(
        git_repo,
        coordinator,
        candidate=candidate,
        expected_head=git_repo.base,
        tree=base_tree,
        landed_override={field: value},
    )
    with pytest.raises(WalCorruptError, match=match):
        coordinator.inspect_wal()


@pytest.mark.parametrize("second", ["intent", "landed"])
def test_a_reused_landing_operation_refuses_replay(git_repo: GitFixture, second: str) -> None:
    """An operation id is single-use whether its first use is open or already closed."""

    coordinator = Coordinator(git_repo.root)
    base_tree = git_repo.git("show", "-s", "--format=%T", git_repo.base).stdout.strip()
    candidate = coordinator.repo.commit_tree(base_tree, "candidate", (git_repo.base,))
    if second == "landed":
        _forge_landing(
            git_repo,
            coordinator,
            candidate=candidate,
            expected_head=git_repo.base,
            tree=base_tree,
        )
    entry = {"mode": "100644", "oid": git_repo.base}
    coordinator.wal.append(
        "land_intent",
        "alpha",
        {
            "operation": "a" * 32,
            "attempt": 1,
            "target": "refs/heads/main",
            "expected_head": git_repo.base,
            "candidate": candidate,
            "tree": base_tree,
            "session_generation": "session",
            "fences": [{"path": "a.txt", "claim_generation": "claim"}],
            "payload": [{"path": "a.txt", "old": entry, "new": None}],
        },
    )
    if second == "intent":
        coordinator.wal.append(
            "land_intent",
            "alpha",
            {
                "operation": "a" * 32,
                "attempt": 2,
                "target": "refs/heads/main",
                "expected_head": git_repo.base,
                "candidate": candidate,
                "tree": base_tree,
                "session_generation": "session",
                "fences": [{"path": "a.txt", "claim_generation": "claim"}],
                "payload": [{"path": "a.txt", "old": entry, "new": None}],
            },
        )
    with pytest.raises(WalCorruptError, match="duplicate landing operation"):
        coordinator.inspect_wal()


def test_a_target_moved_between_the_intent_and_the_branch_cas_closes_its_intent(
    git_repo: GitFixture,
) -> None:
    """A lost branch CAS must not strand the intent that was written ahead of it."""

    coordinator = Coordinator(git_repo.root)
    session = coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    coordinator.claim("alpha", [str(git_repo.root / "a.txt")])
    git_repo.write("a.txt", "a1\n")
    git_repo.stage(session.index_path, "a.txt")
    moved = False

    def move_target(point: str, _context: Mapping[str, Any]) -> None:
        nonlocal moved
        if point == "after_intent" and not moved:
            moved = True
            git_repo.external_commit("b.txt", "external\n", "external move")

    result = coordinator.land("alpha", checkpoint=move_target)
    assert result.attempts == 2

    report = coordinator.inspect_wal()
    kinds = [event.kind for event in report.events]
    assert kinds.count("land_intent") == 2
    assert kinds.count("landed") == 1
    assert [
        event.data.get("reason") for event in report.events if event.kind == "refused"
    ] == ["target_moved"]
    assert report.consistent
    assert report.pending_intents == 0
    # The abandoned attempt's candidate never reached the branch.
    assert report.divergences == ()
    assert git_repo.git("show", f"{git_repo.oid()}:a.txt").stdout == "a1\n"
    assert git_repo.git("show", f"{git_repo.oid()}:b.txt").stdout == "external\n"


def test_a_landing_whose_landed_mark_is_lost_names_the_recovery_that_completes_it(
    git_repo: GitFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = Coordinator(git_repo.root)
    session = coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    coordinator.claim("alpha", [str(git_repo.root / "a.txt")])
    git_repo.write("a.txt", "marked\n")
    git_repo.stage(session.index_path, "a.txt")

    original = Coordinator._append_document

    def refuse(
        self: Coordinator, document: Mapping[str, Any], *, what: str
    ) -> Any:
        if what == "landed mark":
            raise GitError("injected append failure")
        return original(self, document, what=what)

    monkeypatch.setattr(Coordinator, "_append_document", refuse)
    with pytest.raises(GitError, match="wal --recover"):
        coordinator.land("alpha")
    monkeypatch.undo()

    # The branch moved and the intent is pending: exactly the state recovery
    # was built to complete, which is why the error names it.
    assert git_repo.oid() != git_repo.base
    assert coordinator.inspect_wal().pending_intents == 1
    recovery = coordinator.recover()
    assert (recovery.repaired, recovery.refused) == (1, 0)
    assert coordinator.inspect_wal().consistent


def test_temp_index_residue_is_reported_and_swept_only_when_its_owner_is_gone(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    directory = coordinator.repo.temp_index_dir()
    directory.mkdir(parents=True, exist_ok=True)

    live = directory / f"index-{os.getpid()}-{process_start(os.getpid())}-{'0' * 12}"
    dead = directory / f"index-999999999-1-{'1' * 12}"
    unowned = directory / "index-not-a-tag"
    stray = directory / "stray.txt"
    for residue in (live, dead, unowned, stray):
        residue.write_text("residue\n", encoding="utf-8")

    reported = {Path(item.path).name for item in coordinator.inspect_wal().temp_indexes}
    assert reported == {live.name, dead.name, unowned.name}

    result = coordinator.gc("collector")
    assert result.removed_temp_indexes == 1
    assert not dead.exists()
    # A live owner's index, an index with no owner tag, and a file that is not a
    # temporary index at all are all left alone.
    assert live.exists()
    assert unowned.exists()
    assert stray.exists()

    swept = [
        event.data.get("temp_indexes")
        for event in coordinator.inspect_wal().events
        if event.kind == "gc"
    ]
    assert swept == [[dead.name]]


def test_a_crashed_overlay_leaves_residue_that_gc_reclaims(git_repo: GitFixture) -> None:
    """The residue is real, not synthetic: build one through the overlay path."""

    coordinator = Coordinator(git_repo.root)
    temporary = coordinator.repo.new_temp_index()
    coordinator.repo.git("read-tree", git_repo.base, index=temporary)
    assert temporary.exists()

    # Nothing swept it before Round AF; the owner is this live process, so gc
    # must still refuse to touch it.
    assert coordinator.gc("collector").removed_temp_indexes == 0
    assert temporary.exists()
    temporary.unlink()


@pytest.mark.parametrize("relative", [".git", ".git/config", ".git/refs"])
def test_git_internal_paths_are_a_typed_refusal_not_a_silent_allow(
    git_repo: GitFixture,
    relative: str,
) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    target = git_repo.root / relative

    # RED control: canonicalization alone accepts these — they are absolute,
    # they exist, and they resolve inside the repository — so `check` answered
    # blocked:false over Git's own metadata.
    assert target.exists()
    assert target.resolve().relative_to(git_repo.root).as_posix() == relative

    for probe in (
        lambda: coordinator.check("alpha", str(target)),
        lambda: coordinator.claim("alpha", [str(target)]),
    ):
        with pytest.raises(InvalidPathError) as refused:
            probe()
        assert refused.value.details["reason"] == "git_internal"


def test_git_internal_refusal_covers_a_gitlink_and_a_detached_git_dir(
    git_repo: GitFixture,
    tmp_path: Path,
) -> None:
    """The two Git-metadata tests are not redundant; each catches a shape the other misses.

    A nested ``.git`` gitlink lives inside the work tree but outside this
    repository's git dir, and a detached git dir lives outside the work tree
    entirely -- where the containment test is the only thing that keeps the
    refusal precise instead of collapsing into "outside the repository".
    """

    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    gitlink = git_repo.root / "sub" / ".git"
    gitlink.parent.mkdir()
    gitlink.write_text("gitdir: ../../elsewhere\n", encoding="utf-8")
    with pytest.raises(InvalidPathError) as nested:
        coordinator.check("alpha", str(gitlink))
    assert nested.value.details["reason"] == "git_internal"

    detached = tmp_path / "detached"
    work_tree = tmp_path / "work"
    git_repo.git(
        "init",
        "--quiet",
        f"--separate-git-dir={detached}",
        "--initial-branch=main",
        str(work_tree),
    )
    other = Coordinator(work_tree)
    assert other.repo.git_dir == detached.resolve()
    with pytest.raises(InvalidPathError) as outside:
        other.check("alpha", str(detached / "config"))
    assert outside.value.details["reason"] == "git_internal"


def test_registration_warns_when_liveness_degrades_to_bare_pid_existence(
    git_repo: GitFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(core, "_pid_start", lambda pid: None)
    coordinator = Coordinator(git_repo.root)
    session = coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    assert session.pid_start is None
    assert session.warnings == (core.DEGRADED_LIVENESS,)
    assert session.payload()["warnings"] == [core.DEGRADED_LIVENESS]

    registered = [
        event for event in coordinator.inspect_wal().events if event.kind == "session_register"
    ]
    assert registered[0].data["warnings"] == [core.DEGRADED_LIVENESS]
    # The warning survives replay, so a peer reading the WAL sees it too.
    folded = coordinator._fold(coordinator.wal.read())
    assert folded.sessions["alpha"].warnings == (core.DEGRADED_LIVENESS,)


def test_land_refuses_when_the_target_ref_disappears_mid_landing(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    session = coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    coordinator.claim("alpha", [str(git_repo.root / "a.txt")])
    git_repo.write("a.txt", "vanishing target\n")
    git_repo.stage(session.index_path, "a.txt")

    def delete_target(point: str, _context: Mapping[str, Any]) -> None:
        if point == "after_audit":
            git_repo.git("update-ref", "-d", "refs/heads/main")

    with pytest.raises(GitError, match="unborn"):
        coordinator.land("alpha", checkpoint=delete_target)


def test_registration_refuses_an_unborn_target(git_repo: GitFixture) -> None:
    coordinator = Coordinator(git_repo.root)
    with pytest.raises(GitError, match="unborn"):
        coordinator.register_session(
            "alpha", pid=os.getpid(), ttl=300, target="refs/heads/nowhere"
        )
