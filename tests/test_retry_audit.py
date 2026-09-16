from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from conftest import GitFixture
from test_legacy_adopted_wal import _adopt_legacy, _legacy_document, _persist
from test_referee import staged
from test_referee import test_referee_retests_candidate_after_wal_and_target_race as run_race

from onetree.core import Coordinator, _RetryAudit
from onetree.errors import OnetreeError, UnrecordedLandingError, WalCorruptError
from onetree.git import Repository


@pytest.fixture
def walks(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, tuple[str, ...], set[str]]]:
    walks: list[tuple[str, tuple[str, ...], set[str]]] = []
    original = Repository._walked_commit_oids

    def observe(repo: Repository, target: str, excludes: Sequence[str]) -> list[str]:
        result = original(repo, target, excludes)
        walks.append((target, tuple(excludes), set(result)))
        return result

    monkeypatch.setattr(Repository, "_walked_commit_oids", observe)
    return walks


def test_retry_walk_excludes_verified_prefix(
    git_repo: GitFixture, walks: list[tuple[str, tuple[str, ...], set[str]]],
) -> None:
    run_race(git_repo)
    assert walks[0][1] == ()
    assert git_repo.base in walks[0][2]
    # The fixture's inner beta landing succeeds; alpha retries and its referee
    # rejects the combined payload. These are the real audit walk's returned OIDs.
    assert walks[-1][0] == git_repo.oid()
    assert walks[-1][1] == (git_repo.base,)
    assert walks[-1][2] == {git_repo.oid()}


def test_public_and_nested_landings_do_not_share_proofs(
    git_repo: GitFixture, walks: list[tuple[str, tuple[str, ...], set[str]]],
) -> None:
    coordinator = staged(git_repo)
    staged(git_repo, "beta", "b.txt")
    nested = False

    def race(point: str, _context: Mapping[str, Any]) -> None:
        nonlocal nested
        if point == "before_intent" and not nested:
            nested = True
            before = len(walks)
            coordinator.land("beta")
            assert walks[before][1] == ()
            assert git_repo.base in walks[before][2]

    coordinator.land("alpha", checkpoint=race)
    assert nested
    walks.clear()
    # Even on this same Coordinator, the next public mutation must start fresh.
    coordinator.register_session("gamma", pid=os.getpid())
    assert walks[0][1] == ()
    assert git_repo.base in walks[0][2]
    walks.clear()
    with pytest.raises(OnetreeError):
        coordinator.land("alpha")  # no new payload, but still audits first
    assert walks[0][1] == ()
    assert git_repo.base in walks[0][2]


def marked_child(coordinator: Coordinator, parent: str) -> str:
    tree = coordinator.repo.git_text("show", "-s", "--format=%T", parent)
    return coordinator.repo.commit_tree(
        tree, coordinator.repo.landing_message("unrecorded", "d" * 32, "e" * 32, "ghost"),
        (parent,),
    )


@pytest.mark.parametrize("placement", ["suffix", "new_branch", "non_descendant"])
def test_retry_refuses_unrecorded_history(
    git_repo: GitFixture, placement: str,
    walks: list[tuple[str, tuple[str, ...], set[str]]],
) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid())
    tree = coordinator.repo.git_text("show", "-s", "--format=%T", git_repo.base)
    previous = coordinator.repo.commit_tree(tree, "ordinary", (git_repo.base,))
    assert coordinator.repo.update_ref("refs/heads/main", previous, git_repo.base)
    proof = _RetryAudit()
    coordinator._read_wal(retry_audit=proof)
    ghost = marked_child(coordinator, previous if placement == "suffix" else git_repo.base)
    tip = ghost
    # The missing landing is below ordinary commits, not just at the new tip.
    for number in range(8):
        tip = coordinator.repo.commit_tree(tree, f"ordinary {number}", (tip,))
    target = "refs/heads/new" if placement == "new_branch" else "refs/heads/main"
    assert coordinator.repo.update_ref(target, tip, None if placement == "new_branch" else previous)
    walks.clear()
    with pytest.raises(UnrecordedLandingError) as caught:
        coordinator._read_wal(retry_audit=proof)
    assert caught.value.details["commits"] == [ghost]
    failed_walk = next(item for item in walks if item[0] == tip)
    assert ghost in failed_walk[2]
    assert failed_walk[1] == ((previous,) if placement == "suffix" else ())
    assert proof.heads["refs/heads/main"] == previous


def test_retry_wal_rewind_invalidates_recorded_prefix(
    git_repo: GitFixture, walks: list[tuple[str, tuple[str, ...], set[str]]],
) -> None:
    coordinator = staged(git_repo)
    before = coordinator.wal.head()
    landed = coordinator.land("alpha")
    proof = _RetryAudit()
    coordinator._read_wal(retry_audit=proof)
    after = coordinator.wal.head()
    assert before is not None
    assert after is not None
    assert coordinator.repo.update_ref("refs/onetree/wal", before, after)
    walks.clear()
    with pytest.raises(UnrecordedLandingError) as caught:
        coordinator._read_wal(retry_audit=proof)
    assert caught.value.details["commits"] == [landed.commit]
    assert walks[0][1] == ()
    assert landed.commit in walks[0][2]


def test_unstable_scan_never_establishes_a_prefix(
    git_repo: GitFixture, monkeypatch: pytest.MonkeyPatch,
    walks: list[tuple[str, tuple[str, ...], set[str]]],
) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid())
    ghost = marked_child(coordinator, git_repo.base)
    original = Repository._walked_commit_oids
    fired = False

    def interleave(repo: Repository, target: str, excludes: Sequence[str]) -> list[str]:
        nonlocal fired
        result = original(repo, target, excludes)
        if not fired:
            fired = True
            assert repo.update_ref("refs/heads/main", ghost, git_repo.base)
        return result

    monkeypatch.setattr(Repository, "_walked_commit_oids", interleave)
    proof = _RetryAudit()
    walks.clear()
    with pytest.raises(UnrecordedLandingError) as caught:
        coordinator._read_wal(retry_audit=proof)
    assert caught.value.details["commits"] == [ghost]
    assert len(walks) == 2
    assert all(item[1] == () for item in walks)
    assert proof.heads == {}


@pytest.mark.parametrize("change", ["removed", "forged"])
def test_legacy_proof_requires_current_verified_records(
    git_repo: GitFixture, change: str,
    walks: list[tuple[str, tuple[str, ...], set[str]]],
) -> None:
    commit, scalar = _adopt_legacy(git_repo, "repeated")
    coordinator = Coordinator(git_repo.root)
    proof = _RetryAudit()
    coordinator._read_wal(retry_audit=proof)
    coordinator._read_wal(retry_audit=proof)
    assert walks[-1][1] == (commit,)
    assert walks[-1][2] == set()
    git_repo.git("update-ref", "-d", "refs/onetree/wal")
    if change == "removed":
        with pytest.raises(UnrecordedLandingError) as caught:
            coordinator._read_wal(retry_audit=proof)
        assert caught.value.details["commits"] == [commit]
        assert walks[-1][1] == ()
    else:
        document = _legacy_document(git_repo, commit, scalar)
        data = dict(document["data"])  # type: ignore[arg-type]
        data["tree"] = git_repo.git("rev-parse", f"{git_repo.base}^{{tree}}").stdout.strip()
        document["data"] = data
        _persist(git_repo, document)
        with pytest.raises(WalCorruptError, match="tree differs from WAL"):
            coordinator._read_wal(retry_audit=proof)


@pytest.mark.parametrize("boundary", ["shallow", "grafts"])
def test_cached_prefix_does_not_bypass_history_boundaries(
    git_repo: GitFixture, boundary: str,
) -> None:
    coordinator = Coordinator(git_repo.root)
    tree = coordinator.repo.git_text("show", "-s", "--format=%T", git_repo.base)
    tip = coordinator.repo.commit_tree(tree, "ordinary", (git_repo.base,))
    assert coordinator.repo.update_ref("refs/heads/main", tip, git_repo.base)
    proof = _RetryAudit()
    coordinator._read_wal(retry_audit=proof)
    if boundary == "shallow":
        coordinator.repo.git_path("shallow").write_text(tip + "\n")
    else:
        info = coordinator.repo.git_dir / "info"
        info.mkdir(exist_ok=True)
        (info / "grafts").write_text(tip + "\n")
    with pytest.raises(OnetreeError) as caught:
        coordinator._read_wal(retry_audit=proof)
    assert caught.value.kind == ("shallow_history" if boundary == "shallow" else "legacy_grafts")
