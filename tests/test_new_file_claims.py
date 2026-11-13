"""A session can claim a file before it creates it, and land it.

Known bad: main 0215e36, where every claim path must exist, so adding one file means claiming the
whole folder it goes into.
"""

from __future__ import annotations

import os

import pytest
from conftest import GitFixture

from onetree.core import Coordinator
from onetree.errors import ClaimConflictError, InvalidPathError


def test_a_new_file_and_new_folders_can_be_claimed_checked_and_landed(git_repo: GitFixture) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    coordinator.register_session("beta", pid=os.getpid(), ttl=300)
    new = git_repo.root / "src" / "pkg" / "new.py"

    claimed = coordinator.claim("alpha", [str(new)])
    assert [claim.path for claim in claimed.claims] == ["src/pkg/new.py"]
    assert coordinator.check("alpha", str(new)).owned
    assert coordinator.check("beta", str(new)).blocked
    with pytest.raises(ClaimConflictError) as refused:
        coordinator.claim("beta", [str(git_repo.root / "src")])
    assert refused.value.details["holders"] == ["alpha"]

    new.parent.mkdir(parents=True)
    new.write_text("VALUE = 1\n", encoding="utf-8")
    assert coordinator.stage_claimed("alpha") == ("src/pkg/new.py",)
    landed = coordinator.land("alpha", message="add new.py")
    assert git_repo.changed_paths(landed.commit) == {"src/pkg/new.py"}


def test_a_claimed_file_never_created_can_be_released(git_repo: GitFixture) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    planned = str(git_repo.root / "planned.txt")
    coordinator.claim("alpha", [planned])
    assert coordinator.stage_claimed("alpha") == ()
    assert coordinator.release("alpha", [planned]).released == ("planned.txt",)


def test_paths_that_cannot_name_a_file_stay_refused(git_repo: GitFixture) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    reasons = []
    for value in (
        str(git_repo.root / "a.txt" / "inside-a-file"),
        str(git_repo.root / "missing" / ".." / "a.txt"),
        str(git_repo.root / ".git" / "new"),
    ):
        with pytest.raises(InvalidPathError) as caught:
            coordinator.claim("alpha", [value])
        reasons.append(caught.value.details["reason"])
    assert reasons == ["unresolvable", "nonexistent", "git_internal"]
    assert coordinator.claim("alpha", [str(git_repo.root / "brand-new.txt")]).claims
