from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from typing import Any

import pytest
from conftest import GitFixture

from onetree import git as git_module
from onetree.core import Coordinator
from onetree.errors import GitError, RefCorruptError
from onetree.git import GitResult, Repository


@pytest.fixture(params=["sha1", "sha256"])
def ref_repo(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> GitFixture:
    monkeypatch.setenv("GIT_DEFAULT_HASH", request.param)
    monkeypatch.setenv("GIT_DEFAULT_REF_FORMAT", "files")
    repository: GitFixture = request.getfixturevalue("git_repo")
    assert Coordinator(repository.root).repo.object_format == request.param
    assert Coordinator(repository.root).repo.ref_format == "files"
    return repository


@pytest.mark.parametrize("branches", [1, 10, 100])
def test_plain_branch_audit_command_census(
    ref_repo: GitFixture, monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None], branches: int,
) -> None:
    git_repo = ref_repo
    for number in range(branches - 1):
        git_repo.git("update-ref", f"refs/heads/topic-{number}", git_repo.base)
    coordinator = Coordinator(git_repo.root)
    calls: Counter[str] = Counter()
    original = Repository.git

    def observed(self: Repository, *arguments: str, **kwargs: Any) -> GitResult:
        calls[arguments[0]] += 1
        return original(self, *arguments, **kwargs)

    monkeypatch.setattr(Repository, "git", observed)
    assert coordinator.repo.git("rev-parse", "HEAD").stdout.decode().strip() == git_repo.base
    assert calls == {"rev-parse": 1}
    calls.clear()
    report = coordinator.inspect_wal()
    assert report.consistent
    assert report.head is None
    assert git_repo.oid() == git_repo.base
    record_property("implementation", str(git_module.__file__))
    record_property("branches", branches)
    record_property("git_calls", json.dumps(dict(sorted(calls.items()))))
    assert calls["symbolic-ref"] == 0


@pytest.mark.parametrize("contents", ["null", "missing", "blob", "malformed", "suffix"])
def test_plain_ref_shortcut_does_not_admit_a_broken_branch(
    ref_repo: GitFixture, contents: str,
) -> None:
    git_repo = ref_repo
    coordinator = Coordinator(git_repo.root)
    reference = "refs/heads/other"
    path = coordinator.repo.git_path(reference)
    git_repo.git("update-ref", reference, git_repo.base)
    assert coordinator.repo.audit_heads()[reference] == git_repo.base
    if contents == "null":
        value = coordinator.repo.zero_oid
    elif contents == "missing":
        value = "f" * coordinator.repo.oid_length
        assert git_repo.git("cat-file", "-e", value, check=False).returncode != 0
    elif contents == "blob":
        value = git_repo.git("rev-parse", "HEAD:a.txt").stdout.strip()
    elif contents == "malformed":
        value = "not-an-object-id"
    else:
        value = git_repo.base + "junk"
    path.write_text(value + "\n", encoding="ascii")
    failure = RefCorruptError if contents in {"blob", "missing"} else GitError
    with pytest.raises(failure):
        coordinator.inspect_wal()
    assert coordinator.wal.read().head is None
    assert git_repo.oid() == git_repo.base
    path.write_text(git_repo.base + "\n", encoding="ascii")
    assert coordinator.inspect_wal().consistent


def test_unreadable_direct_ref_still_reaches_gits_refusal(ref_repo: GitFixture) -> None:
    coordinator = Coordinator(ref_repo.root)
    reference = "refs/heads/unreadable"
    ref_repo.git("update-ref", reference, ref_repo.base)
    path = coordinator.repo.git_path(reference)
    path.chmod(0)
    try:
        try:
            path.read_bytes()
        except PermissionError:
            pass
        else:
            pytest.skip("fixture permissions do not prohibit reading on this platform")
        with pytest.raises(GitError, match="cannot list audit refs"):
            coordinator.inspect_wal()
        assert coordinator.wal.read().head is None
    finally:
        path.chmod(0o600)
    assert coordinator.inspect_wal().consistent


def test_direct_ref_replaced_by_symbolic_ref_is_reexamined(ref_repo: GitFixture) -> None:
    git_repo = ref_repo
    coordinator = Coordinator(git_repo.root)
    reference = "refs/heads/other"
    git_repo.git("update-ref", reference, git_repo.base)
    assert coordinator.repo.audit_heads()[reference] == git_repo.base
    git_repo.git("symbolic-ref", reference, "refs/heads/missing")
    assert reference not in coordinator.repo.audit_heads()
    git_repo.git("symbolic-ref", reference, "refs/heads/main")
    assert coordinator.repo.audit_heads()[reference] == git_repo.base
    coordinator.repo.git_path(reference).write_text(git_repo.base + "\n", encoding="ascii")
    assert coordinator.repo.audit_heads()[reference] == git_repo.base
    assert coordinator.inspect_wal().consistent
