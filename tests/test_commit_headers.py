from __future__ import annotations

import os

import pytest
from conftest import GitFixture
from test_wal_verify_cost import ref_repo as ref_repo

from onetree.core import Coordinator
from onetree.errors import GitError


@pytest.mark.parametrize("spelling", ["lowercase", "uppercase", "mixed"])
@pytest.mark.parametrize("merge", [False, True])
@pytest.mark.parametrize("entry", ["adopt", "recorded"])
def test_git_valid_parent_headers_keep_canonical_wal_identity(
    ref_repo: GitFixture, spelling: str, merge: bool, entry: str
) -> None:
    git_repo = ref_repo
    coordinator = Coordinator(git_repo.root)
    tree = git_repo.git("rev-parse", f"{git_repo.base}^{{tree}}").stdout.strip()
    parents = [git_repo.base]
    if merge:
        parents.append(git_repo.git(
            "commit-tree", tree, "-p", git_repo.base, input_text="other parent\n"
        ).stdout.strip())
    encoded = [parent.upper() if spelling == "uppercase" else parent for parent in parents]
    if spelling == "mixed":
        encoded = [
            "".join(character.upper() if position % 2 else character
                    for position, character in enumerate(parent))
            for parent in parents
        ]
    if spelling != "lowercase":
        assert encoded != parents
    operation = "b" * 32
    raw = (
        f"tree {tree}\n" + "".join(f"parent {parent}\n" for parent in encoded)
        + "author Headers <headers@example.invalid> 1700000000 +0000\n"
        "committer Headers <headers@example.invalid> 1700000000 +0000\n\n"
        f"Git-valid parent headers\n\nOnetree-Operation: {operation}\n"
    )
    commit = git_repo.git(
        "hash-object", "-w", "-t", "commit", "--stdin", input_text=raw
    ).stdout.strip()
    assert git_repo.git("cat-file", "commit", commit).stdout == raw
    assert git_repo.git("show", "-s", "--format=%P", commit).stdout.split() == parents
    git_repo.git("update-ref", "refs/heads/main", commit, git_repo.base)
    git_repo.git("fsck", "--strict", "--no-reflogs")

    if entry == "adopt":
        assert not coordinator.inspect_wal().consistent
        result = coordinator.adopt_landings()
        assert result.adopted
        assert result.report.consistent
        assert all(record.data["expected_head"] == parents[0] for record in result.adopted)
        assert all(record.data["commit"] == commit for record in result.adopted)
    else:
        document = coordinator.wal.document("adopted", "operator", {
            "operation": operation, "target": "refs/heads/main",
            "expected_head": parents[0], "commit": commit, "tree": tree,
        })
        assert coordinator.wal.try_append((document,), None) is not None
        assert coordinator.inspect_wal().consistent

    coordinator.register_session("beta", pid=os.getpid(), ttl=300)
    assert coordinator.inspect_wal().consistent
    assert coordinator.repo.commit_parents(commit) == tuple(parents)
    assert coordinator.repo.commit_object_parents(
        raw.encode("ascii"), commit=commit
    ) == tuple(parents)
    assert git_repo.oid() == commit
    wal_before = coordinator.wal.head()
    assert coordinator.adopt_landings().adopted == ()
    assert coordinator.wal.head() == wal_before


@pytest.mark.parametrize(
    "damage", ["empty", "short", "long", "nonhex", "leading", "trailing", "nonascii"]
)
def test_raw_parent_normalization_retains_validation(ref_repo: GitFixture, damage: str) -> None:
    repository = Coordinator(ref_repo.root).repo
    width = repository.oid_length
    bad = {
        "empty": b"",
        "short": b"A" * (width - 1),
        "long": b"A" * (width + 1),
        "nonhex": b"G" * width,
        "leading": b" " + b"A" * width,
        "trailing": b"A" * width + b" ",
        "nonascii": b"\xff" + b"A" * (width - 1),
    }[damage]
    tree = ref_repo.git("rev-parse", f"{ref_repo.base}^{{tree}}").stdout.strip().encode("ascii")
    raw = b"tree " + tree + b"\nparent " + bad + b"\n\nmessage\n"
    message = (
        "commit parent is not ASCII" if damage == "nonascii"
        else "malformed commit parent object ID"
    )
    with pytest.raises(GitError, match=message):
        repository.commit_object_parents(raw, commit=ref_repo.base)


def test_public_oid_validation_stays_canonical(ref_repo: GitFixture) -> None:
    repository = Coordinator(ref_repo.root).repo
    assert repository.validate_oid(ref_repo.base) == ref_repo.base
    uppercase = ref_repo.base.upper()
    assert uppercase != ref_repo.base
    with pytest.raises(GitError, match="malformed"):
        repository.validate_oid(uppercase)
