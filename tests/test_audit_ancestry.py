from __future__ import annotations

import os
import zlib
from collections.abc import Callable

import pytest
from conftest import GitFixture
from test_recovery_semantics import _marked_commit

from onetree import core as core_module
from onetree import git as git_module
from onetree.core import Coordinator
from onetree.errors import GitError
from onetree.git import Repository


@pytest.fixture(params=["sha1", "sha256"])
def ancestry_repo(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
) -> GitFixture:
    monkeypatch.setenv("GIT_DEFAULT_HASH", request.param)
    monkeypatch.setenv("GIT_DEFAULT_REF_FORMAT", "files")
    fixture: GitFixture = request.getfixturevalue("git_repo")
    assert Repository(fixture.root).object_format == request.param
    record_property("core_implementation", str(core_module.__file__))
    record_property("git_implementation", str(git_module.__file__))
    return fixture


def _refused_candidate(fixture: GitFixture) -> tuple[Coordinator, str]:
    coordinator = Coordinator(fixture.root)
    session = coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    claim = coordinator.claim("alpha", [str(fixture.root / "a.txt")]).claims[0]
    fixture.write("a.txt", "refused payload\n")
    fixture.stage(session.index_path, "a.txt")
    tree = coordinator.repo.write_tree(session.index_path)
    candidate = coordinator.repo.commit_tree(
        tree,
        coordinator.repo.landing_message("candidate", "b" * 32, "c" * 32, "alpha"),
        (fixture.base,),
    )
    old = coordinator.repo.tree_entries(fixture.base)["a.txt"].payload()
    new = coordinator.repo.tree_entries(tree)["a.txt"].payload()
    intent = coordinator.wal.document(
        "land_intent", "alpha",
        {
            "operation": "b" * 32,
            "attempt": 1,
            "target": "refs/heads/main",
            "expected_head": fixture.base,
            "candidate": candidate,
            "tree": tree,
            "session_generation": session.generation,
            "lander": {"pid": 999_999_999, "start": 1},
            "fences": [{"path": "a.txt", "claim_generation": claim.generation}],
            "payload": [{"path": "a.txt", "old": old, "new": new}],
        },
        event_id="c" * 32,
    )
    assert coordinator.wal.try_append((intent,), coordinator.wal.read().head) is not None
    assert coordinator.recover().refused == 1
    assert coordinator.inspect_wal().consistent
    return coordinator, candidate


@pytest.mark.parametrize("shape", ["missing", "blob", "malformed_commit"])
def test_refused_candidate_object_errors_are_not_clean_nonmembership(
    ancestry_repo: GitFixture, shape: str,
) -> None:
    coordinator, candidate = _refused_candidate(ancestry_repo)
    path = ancestry_repo.root / ".git" / "objects" / candidate[:2] / candidate[2:]
    assert path.is_file()
    path.unlink()
    if shape == "blob":
        path.write_bytes(zlib.compress(b"blob 7\0payload"))
    elif shape == "malformed_commit":
        path.write_bytes(zlib.compress(b"commit 7\0broken\n"))
    with pytest.raises(GitError, match="cannot test ancestry") as error:
        coordinator.inspect_wal()
    assert error.value.details["operation"] == "merge-base"


def test_refused_query_error_precedes_unmarked_history_error(ancestry_repo: GitFixture) -> None:
    coordinator, candidate = _refused_candidate(ancestry_repo)
    path = ancestry_repo.root / ".git" / "objects" / candidate[:2] / candidate[2:]
    original = path.read_bytes()
    path.unlink()
    tree = ancestry_repo.git("rev-parse", f"{ancestry_repo.base}^{{tree}}").stdout.strip()
    malformed = ancestry_repo.git(
        "hash-object", "-t", "commit", "--literally", "-w", "--stdin",
        input_text=(
            f"tree {tree}\nparent {ancestry_repo.base}\n"
            "author A <a@example.invalid> 1 +0000\n"
            "committer A <a@example.invalid> 1 +0000\n"
        ),
    ).stdout.strip()
    ancestry_repo.git("update-ref", "refs/heads/main", malformed, ancestry_repo.base)
    with pytest.raises(GitError, match="cannot test ancestry") as error:
        coordinator.inspect_wal()
    assert error.value.details["operation"] == "merge-base"
    path.write_bytes(original)
    with pytest.raises(GitError, match="commit object has no header terminator"):
        coordinator.inspect_wal()


def test_valid_refused_candidate_is_reexamined_after_target_moves(
    ancestry_repo: GitFixture,
) -> None:
    coordinator, candidate = _refused_candidate(ancestry_repo)
    wal_head = coordinator.wal.read().head
    before = coordinator.inspect_wal()
    assert before.consistent
    assert before.divergences == ()
    ancestry_repo.git("update-ref", "refs/heads/main", candidate, ancestry_repo.base)
    observed = coordinator.inspect_wal()
    assert not observed.consistent
    identities = [
        (item.kind, item.target, item.commit, item.operation) for item in observed.divergences
    ]
    assert identities == [
        ("refused_candidate_on_target", "refs/heads/main", candidate, "b" * 32),
    ]
    ancestry_repo.git("update-ref", "refs/heads/main", ancestry_repo.base, candidate)
    restored = coordinator.inspect_wal()
    assert restored.consistent
    assert restored.divergences == ()
    assert coordinator.wal.read().head == wal_head


@pytest.mark.parametrize("boundary", ["shallow", "grafts", "replace"])
def test_positive_landed_membership_and_ancestry_share_the_repository_graph(
    ancestry_repo: GitFixture, boundary: str,
) -> None:
    coordinator = Coordinator(ancestry_repo.root)
    first = _marked_commit(coordinator, "refs/heads/main", "d" * 32, (ancestry_repo.base,))
    tip = _marked_commit(coordinator, "refs/heads/main", "e" * 32, (first,))
    adopted = coordinator.adopt_landings("refs/heads/main")
    assert {event.data["commit"] for event in adopted.adopted} == {first, tip}
    assert adopted.report.consistent
    if boundary == "shallow":
        coordinator.repo.git_path("shallow").write_text(first + "\n", encoding="ascii")
    elif boundary == "grafts":
        path = coordinator.repo.git_dir / "info" / "grafts"
        path.parent.mkdir(exist_ok=True)
        path.write_text(f"{tip} {ancestry_repo.base}\n", encoding="ascii")
    else:
        ancestry_repo.git("replace", "--graft", tip, ancestry_repo.base)
    assert coordinator.repo.is_ancestor(first, tip)
    visible = {marked.commit for marked in coordinator.repo.marked_commits(tip, ())}
    assert {first, tip} <= visible
    report = coordinator.inspect_wal()
    expected = {"shallow": {"shallow_history"}, "grafts": {"legacy_grafts"}, "replace": set()}
    assert {item.kind for item in report.divergences} == expected[boundary]


def test_excluded_landed_commit_still_uses_the_ancestry_fallback(
    ancestry_repo: GitFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = Coordinator(ancestry_repo.root)
    first = _marked_commit(coordinator, "refs/heads/main", "d" * 32, (ancestry_repo.base,))
    tip = _marked_commit(coordinator, "refs/heads/main", "e" * 32, (first,))
    assert coordinator.adopt_landings().report.consistent
    assert coordinator.repo._walked_commit_oids(tip, (first,)) == [tip]
    monkeypatch.setattr(Coordinator, "_scan_floor", staticmethod(lambda state, target: (first,)))
    calls: list[tuple[str, str]] = []
    original = Repository.is_ancestor

    def observed(self: Repository, ancestor: str, descendant: str) -> bool:
        calls.append((ancestor, descendant))
        return original(self, ancestor, descendant)

    monkeypatch.setattr(Repository, "is_ancestor", observed)
    assert coordinator.repo.is_ancestor(first, tip)
    assert calls == [(first, tip)]
    calls.clear()
    assert coordinator.inspect_wal().consistent
    assert (first, tip) in calls


def test_failed_raw_audit_does_not_supply_positive_landed_membership(
    ancestry_repo: GitFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = Coordinator(ancestry_repo.root)
    first = _marked_commit(coordinator, "refs/heads/main", "d" * 32, (ancestry_repo.base,))
    assert coordinator.adopt_landings().report.consistent
    tree = coordinator.repo.git_text("show", "-s", "--format=%T", first)
    malformed = ancestry_repo.git(
        "hash-object", "-t", "commit", "--literally", "-w", "--stdin",
        input_text=(
            f"tree {tree}\nparent {first}\n"
            "author A <a@example.invalid> 1 +0000\n"
            "committer A <a@example.invalid> 1 +0000\n"
        ),
    ).stdout.strip()
    ancestry_repo.git("update-ref", "refs/heads/main", malformed, first)
    calls: list[tuple[str, str]] = []
    original = Repository.is_ancestor

    def observed(self: Repository, ancestor: str, descendant: str) -> bool:
        calls.append((ancestor, descendant))
        return original(self, ancestor, descendant)

    monkeypatch.setattr(Repository, "is_ancestor", observed)
    assert coordinator.repo.is_ancestor(first, malformed)
    assert calls == [(first, malformed)]
    calls.clear()
    with pytest.raises(GitError, match="commit object has no header terminator"):
        coordinator.inspect_wal()
    assert calls == [(first, malformed)]
