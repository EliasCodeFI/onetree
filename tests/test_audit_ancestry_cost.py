from __future__ import annotations

import json
import weakref
from collections.abc import Callable, Collection, Sequence

import pytest
from conftest import GitFixture
from test_recovery_semantics import _marked_commit

from onetree import core as core_module
from onetree import git as git_module
from onetree.core import Coordinator
from onetree.git import MarkedCommit, Repository


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
@pytest.mark.parametrize("merge", [False, True])
@pytest.mark.parametrize("duplicate_head", [False, True])
def test_validated_landed_ancestry_reuses_the_complete_branch_walk(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
    object_format: str,
    merge: bool,
    duplicate_head: bool,
) -> None:
    monkeypatch.setenv("GIT_DEFAULT_HASH", object_format)
    monkeypatch.setenv("GIT_DEFAULT_REF_FORMAT", "files")
    fixture: GitFixture = request.getfixturevalue("git_repo")
    coordinator = Coordinator(fixture.root)
    assert coordinator.repo.object_format == object_format
    first = _marked_commit(coordinator, "refs/heads/main", "a" * 32, (fixture.base,))
    second = _marked_commit(
        coordinator, "refs/heads/main", "b" * 32, (fixture.base if merge else first,),
    )
    tip = _marked_commit(
        coordinator, "refs/heads/main", "c" * 32,
        (first, second) if merge else (second,),
    )
    adoption = coordinator.adopt_landings("refs/heads/main")
    assert {event.data["commit"] for event in adoption.adopted} == {first, second, tip}
    assert adoption.report.consistent
    if duplicate_head:
        fixture.git("update-ref", "refs/heads/a-alias", tip)
    before = coordinator.wal.read().head
    queries: list[tuple[str, str]] = []
    walks: list[tuple[str, tuple[str, ...]]] = []
    original = Repository.is_ancestor
    original_walk = Repository._walked_commit_oids

    def observed(self: Repository, ancestor: str, descendant: str) -> bool:
        queries.append((ancestor, descendant))
        return original(self, ancestor, descendant)

    def observed_walk(self: Repository, target: str, excludes: Sequence[str]) -> list[str]:
        walks.append((target, tuple(excludes)))
        return original_walk(self, target, excludes)

    monkeypatch.setattr(Repository, "is_ancestor", observed)
    monkeypatch.setattr(Repository, "_walked_commit_oids", observed_walk)
    assert coordinator.repo.is_ancestor(first, tip)
    assert queries == [(first, tip)]
    assert {first, second, tip} <= set(coordinator.repo._walked_commit_oids(tip, ()))
    assert walks == [(tip, ())]
    queries.clear()
    walks.clear()
    report = coordinator.inspect_wal()
    assert report.consistent
    assert report.divergences == ()
    assert coordinator.wal.read().head == before
    record_property("core_implementation", str(core_module.__file__))
    record_property("git_implementation", str(git_module.__file__))
    record_property("ancestry_queries", json.dumps(queries))
    record_property("branch_walks", json.dumps(walks))
    assert walks == [(tip, ())]
    assert queries == []


def test_distinct_heads_do_not_retain_completed_witness_batches(
    git_repo: GitFixture,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
) -> None:
    coordinator = Coordinator(git_repo.root)
    first = _marked_commit(coordinator, "refs/heads/main", "a" * 32, (git_repo.base,))
    second = _marked_commit(coordinator, "refs/heads/main", "b" * 32, (first,))
    third = _marked_commit(coordinator, "refs/heads/main", "c" * 32, (second,))
    assert coordinator.adopt_landings().report.consistent
    tree = coordinator.repo.git_text("show", "-s", "--format=%T", third)
    for number in range(3):
        tip = coordinator.repo.commit_tree(tree, f"plain branch {number}", (third,))
        git_repo.git("update-ref", f"refs/heads/topic-{number}", tip)
    retained: list[weakref.ReferenceType[MarkedCommit]] = []
    counts: list[int] = []
    calibration = MarkedCommit(first, frozenset())
    retained.append(weakref.ref(calibration))
    assert sum(item() is not None for item in retained) == 1
    del calibration
    assert sum(item() is not None for item in retained) == 0
    original = Repository._marked_commits_from_oids

    def observed(
        self: Repository,
        commits: Sequence[str],
        *,
        reference: str,
        recorded: Collection[tuple[str, str]] | None = None,
        pending: Collection[str] = (),
    ) -> list[MarkedCommit]:
        counts.append(sum(item() is not None for item in retained))
        result = original(self, commits, reference=reference, recorded=recorded, pending=pending)
        retained.extend(weakref.ref(item) for item in result)
        return result

    monkeypatch.setattr(Repository, "_marked_commits_from_oids", observed)
    assert coordinator.inspect_wal().consistent
    record_property("implementation", str(git_module.__file__))
    record_property("previous_witnesses_at_next_walk", json.dumps(counts))
    assert len(counts) == 4
    assert max(counts) <= 1
