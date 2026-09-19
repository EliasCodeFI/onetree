from __future__ import annotations

from collections.abc import Callable

import pytest
from conftest import GitFixture

from onetree import git as git_module
from onetree.core import Coordinator
from onetree.errors import GitError
from onetree.git import Repository


@pytest.fixture(params=["sha1", "sha256"])
def audit_repo(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> GitFixture:
    monkeypatch.setenv("GIT_DEFAULT_HASH", request.param)
    monkeypatch.setenv("GIT_DEFAULT_REF_FORMAT", "files")
    repository: GitFixture = request.getfixturevalue("git_repo")
    assert Repository(repository.root).object_format == request.param
    return repository


def _commit(fixture: GitFixture, parent: str, message: str) -> str:
    tree = fixture.git("rev-parse", f"{fixture.base}^{{tree}}").stdout.strip()
    return fixture.git("commit-tree", tree, "-p", parent, input_text=message).stdout.strip()


def test_plain_history_retains_no_audit_witnesses(
    audit_repo: GitFixture, record_property: Callable[[str, object], None],
) -> None:
    head = audit_repo.base
    commits = [head]
    for number in range(8):
        head = _commit(audit_repo, head, f"ordinary commit {number}\n")
        commits.append(head)
    repository = Repository(audit_repo.root)
    public = list(repository.marked_commits(head))
    assert [item.commit for item in public] == list(reversed(commits))
    assert all(not item.operations for item in public)
    witnesses = list(repository.audit_marked_commits(head, (), recorded=(), pending=()))
    record_property("implementation", str(git_module.__file__))
    record_property("public_entries", len(public))
    record_property("audit_entries", len(witnesses))
    assert witnesses == []


def test_sparse_audit_preserves_marked_commits_and_divergences(audit_repo: GitFixture) -> None:
    head = audit_repo.base
    marked: dict[str, frozenset[str]] = {}
    for number, operation in enumerate((None, "a" * 32, None, "b" * 32, None)):
        message = f"commit {number}\n"
        if operation is not None:
            message += f"\nOnetree-Operation: {operation}\n"
        head = _commit(audit_repo, head, message)
        if operation is not None:
            marked[head] = frozenset({operation})
    audit_repo.git("update-ref", "refs/heads/main", head, audit_repo.base)
    coordinator = Coordinator(audit_repo.root)
    report = coordinator.inspect_wal()
    assert {(item.kind, item.commit, item.operation) for item in report.divergences} == {
        ("unrecorded_landing", commit, next(iter(operations)))
        for commit, operations in marked.items()
    }
    witnesses = list(coordinator.repo.audit_marked_commits(head, (), recorded=(), pending=()))
    assert {item.commit: item.operations for item in witnesses} == marked
    assert len(list(coordinator.repo.marked_commits(head))) == 6


def test_sparse_audit_validates_unmarked_objects_before_returning(audit_repo: GitFixture) -> None:
    tree = audit_repo.git("rev-parse", f"{audit_repo.base}^{{tree}}").stdout.strip()
    raw = (
        f"tree {tree}\nparent {audit_repo.base}\n"
        "author A <a@example.invalid> 1 +0000\n"
        "committer A <a@example.invalid> 1 +0000\n"
    )
    malformed = audit_repo.git(
        "hash-object", "-t", "commit", "--literally", "-w", "--stdin", input_text=raw,
    ).stdout.strip()
    head = _commit(audit_repo, malformed, f"marked head\n\nOnetree-Operation: {'c' * 32}\n")
    repository = Repository(audit_repo.root)
    with pytest.raises(GitError, match="commit object has no header terminator"):
        repository.audit_marked_commits(head, (), recorded=(), pending=())
