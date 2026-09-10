"""A recorded claim remains releasable after its filesystem name disappears."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from conftest import GitFixture
from test_defensive import _expose_intent

from onetree.core import Coordinator
from onetree.errors import InvalidPathError, ReleaseRefusedError


@pytest.mark.parametrize("change", ["delete", "rename", "parent_rename"])
def test_release_missing_claim_preserves_other_claims_and_allows_recreation(
    git_repo: GitFixture, change: str,
) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    coordinator.register_session("beta", pid=os.getpid(), ttl=300)
    relative = "nested/c.txt" if change == "parent_rename" else "a.txt"
    original = git_repo.root / relative
    held = coordinator.claim("alpha", [str(original), str(git_repo.root / "b.txt")])
    generation = next(claim.generation for claim in held.claims if claim.path == relative)
    if change == "delete":
        original.unlink()
    elif change == "rename":
        original.rename(git_repo.root / "renamed.txt")
    else:
        original.parent.rename(git_repo.root / "renamed-parent")

    result = coordinator.release("alpha", [str(original)])

    assert result.released == (relative,)
    report = coordinator.inspect_wal()
    release = [event for event in report.events if event.kind == "release"]
    assert len(release) == 1
    assert release[0].data["generations"] == [generation]
    assert release[0].data["paths"] == [relative]
    assert coordinator.check("beta", str(git_repo.root / "b.txt")).blocked
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_text("recreated\n", encoding="utf-8")
    assert coordinator.claim("beta", [str(original)]).claims[0].actor == "beta"


@pytest.mark.parametrize("case", [
    "relative", "outside", "git_metadata", "nested_git_metadata", "outside_alias",
    "metadata_alias", "dangling_alias", "loop_alias", "unresolved_parent", "not_directory",
])
def test_missing_release_keeps_path_boundaries_and_is_atomic(
    git_repo: GitFixture, tmp_path: Path, case: str,
) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    coordinator.register_session("beta", pid=os.getpid(), ttl=300)
    original = git_repo.root / "a.txt"
    coordinator.claim("alpha", [str(original)])
    original.unlink()
    alias = git_repo.root / "alias"
    if case == "relative":
        invalid = Path("a.txt")
    elif case == "outside":
        invalid = tmp_path / "outside" / "missing.txt"
    elif case == "git_metadata":
        invalid = git_repo.root / ".git" / "missing.txt"
    elif case == "nested_git_metadata":
        invalid = git_repo.root / "nested" / ".git" / "missing.txt"
    elif case == "outside_alias":
        alias.symlink_to(tmp_path, target_is_directory=True)
        invalid = alias / "missing.txt"
    elif case == "metadata_alias":
        alias.symlink_to(coordinator.repo.git_dir, target_is_directory=True)
        invalid = alias / "missing.txt"
    elif case == "dangling_alias":
        alias.symlink_to(git_repo.root / "gone", target_is_directory=True)
        invalid = alias / "a.txt"
    elif case == "loop_alias":
        alias.symlink_to(alias, target_is_directory=True)
        invalid = alias / "a.txt"
    elif case == "unresolved_parent":
        invalid = git_repo.root / "gone" / ".." / "a.txt"
    else:
        invalid = git_repo.root / "b.txt" / "missing.txt"
    before = coordinator.wal.read().head

    with pytest.raises(InvalidPathError):
        coordinator.release("alpha", [str(original), str(invalid)])

    assert coordinator.wal.read().head == before
    original.write_text("recreated\n", encoding="utf-8")
    assert coordinator.check("beta", str(original)).blocked


def test_missing_release_cannot_release_another_sessions_claim(git_repo: GitFixture) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    coordinator.register_session("beta", pid=os.getpid(), ttl=300)
    original = git_repo.root / "a.txt"
    coordinator.claim("alpha", [str(original)])
    original.unlink()

    with pytest.raises(ReleaseRefusedError) as refused:
        coordinator.release("beta", [str(original)])

    assert refused.value.details["reason"] == "not_held"
    original.write_text("recreated\n", encoding="utf-8")
    assert coordinator.check("beta", str(original)).blocked


def test_missing_release_preserves_pending_landing_fences(git_repo: GitFixture) -> None:
    coordinator = Coordinator(git_repo.root)
    _expose_intent(git_repo, coordinator, move_target=False)
    original = git_repo.root / "a.txt"
    original.unlink()

    with pytest.raises(ReleaseRefusedError) as refused:
        coordinator.release("alpha", [str(original)])

    assert refused.value.details["reason"] == "pending_land"
    report = coordinator.inspect_wal()
    assert report.pending_intents == 1
    assert not any(event.kind == "release" for event in report.events)


def test_claim_and_check_still_refuse_a_missing_path(git_repo: GitFixture) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    missing = str(git_repo.root / "missing.txt")
    before = coordinator.wal.read().head
    for operation in (
        lambda: coordinator.claim("alpha", [missing]),
        lambda: coordinator.check("alpha", missing),
    ):
        with pytest.raises(InvalidPathError) as refused:
            operation()
        assert refused.value.details["reason"] == "nonexistent"
    assert coordinator.wal.read().head == before


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="requires POSIX access checks")
def test_missing_release_does_not_hide_permission_errors(git_repo: GitFixture) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    parent = git_repo.root / "private"
    parent.mkdir()
    original = parent / "claimed.txt"
    original.write_text("claimed\n", encoding="utf-8")
    coordinator.claim("alpha", [str(original)])
    original.unlink()
    before = coordinator.wal.read().head
    parent.chmod(0)
    try:
        with pytest.raises(InvalidPathError) as refused:
            coordinator.release("alpha", [str(original)])
        assert refused.value.details["reason"] == "unresolvable"
        assert coordinator.wal.read().head == before
    finally:
        parent.chmod(0o700)
