from __future__ import annotations

import os
from functools import partial

import pytest
from conftest import GitFixture

from onetree.core import Coordinator
from onetree.errors import GitError, LandingRefusedError


@pytest.mark.parametrize("operation", ["register", "land"])
def test_dangling_private_index_refuses_without_replacing_evidence(
    git_repo: GitFixture, operation: str,
) -> None:
    coordinator = Coordinator(git_repo.root)
    session = coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    coordinator.claim("alpha", [str(git_repo.root / "a.txt")])
    git_repo.write("a.txt", "intended change\n")
    git_repo.stage(session.index_path, "a.txt")
    wal_before = coordinator.inspect_wal().head
    shared_index = git_repo.root / ".git" / "index"
    shared_before = shared_index.read_bytes()
    missing = git_repo.root / "missing-index-target"
    session.index_path.unlink()
    session.index_path.symlink_to(missing)
    invoke = (
        partial(coordinator.register_session, "alpha", pid=os.getpid(), ttl=300)
        if operation == "register"
        else partial(coordinator.land, "alpha")
    )

    with pytest.raises(GitError, match="session index is not a regular file"):
        invoke()

    assert session.index_path.is_symlink()
    assert session.index_path.readlink() == missing
    assert not missing.exists()
    assert shared_index.read_bytes() == shared_before
    assert git_repo.oid() == git_repo.base
    assert coordinator.inspect_wal().head == wal_before


@pytest.mark.parametrize("damage", ["bad_signature", "truncated"])
def test_corrupt_private_index_is_typed_and_preserves_both_refs(
    git_repo: GitFixture, damage: str,
) -> None:
    coordinator = Coordinator(git_repo.root)
    session = coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    coordinator.claim("alpha", [str(git_repo.root / "a.txt")])
    git_repo.write("a.txt", "intended change\n")
    git_repo.stage(session.index_path, "a.txt")
    original = session.index_path.read_bytes()
    damaged = b"BAD!" + original[4:] if damage == "bad_signature" else original[:8]
    session.index_path.write_bytes(damaged)
    wal_before = coordinator.inspect_wal().head

    with pytest.raises(GitError) as refused:
        coordinator.land("alpha")

    assert refused.value.kind == "git_error"
    assert session.index_path.read_bytes() == damaged
    assert git_repo.oid() == git_repo.base
    assert coordinator.inspect_wal().head == wal_before


def test_foreign_base_index_cannot_introduce_unclaimed_content(git_repo: GitFixture) -> None:
    coordinator = Coordinator(git_repo.root)
    session = coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    coordinator.claim("alpha", [str(git_repo.root / "a.txt")])
    other_index = git_repo.root / ".git" / "foreign-index"
    git_repo.git("read-tree", git_repo.base, index=other_index)
    blob = git_repo.git("hash-object", "-w", "--stdin", input_text="foreign b\n").stdout.strip()
    git_repo.git("update-index", "--cacheinfo", "100644", blob, "b.txt", index=other_index)
    foreign_base = git_repo.raw_commit(other_index, git_repo.base, "foreign index base")
    git_repo.git("read-tree", foreign_base, index=session.index_path)
    git_repo.write("a.txt", "intended change\n")
    git_repo.stage(session.index_path, "a.txt")

    with pytest.raises(LandingRefusedError) as refused:
        coordinator.land("alpha")

    assert refused.value.details["reason"] == "unclaimed_paths"
    assert refused.value.details["paths"] == ["b.txt"]
    assert git_repo.oid() == git_repo.base
    events = coordinator.inspect_wal().events
    assert events[-1].kind == "refused"
    assert not any(event.kind in {"land_intent", "landed"} for event in events)

    git_repo.git("read-tree", session.base, index=session.index_path)
    git_repo.stage(session.index_path, "a.txt")
    landed = coordinator.land("alpha", message="valid staging control")
    assert landed.payload == ("a.txt",)
    assert git_repo.git("show", f"{landed.commit}:a.txt").stdout == "intended change\n"
    assert git_repo.git("show", f"{landed.commit}:b.txt").stdout == "b0\n"
