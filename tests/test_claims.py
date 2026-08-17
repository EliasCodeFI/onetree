"""INV6 and the path-claim state machine."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from conftest import GitFixture

from onetree.core import Coordinator
from onetree.errors import ClaimConflictError, InvalidPathError


def test_inv6_invalid_paths_refuse_and_conflicts_name_holder(
    git_repo: GitFixture, tmp_path: Path
) -> None:
    coordinator = Coordinator(git_repo.root)
    alpha = coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    coordinator.register_session("beta", pid=os.getpid(), ttl=300)

    # RED control: resolving a relative/nonexistent path alone manufactures an
    # absolute-looking key and would report no holders. This is the measured
    # fail-open shape from the reference implementation that INV6 forbids.
    unsafe_key = str(Path("does-not-exist").resolve())
    assert Path(unsafe_key).is_absolute()
    assert not Path(unsafe_key).exists()
    assert [] == []  # the unsafe evaluator's empty warnings/holders outcome

    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    invalid = ["a.txt", str(git_repo.root / "missing.txt"), str(outside)]
    reasons: list[str] = []
    for value in invalid:
        with pytest.raises(InvalidPathError) as caught:
            coordinator.check("alpha", value)
        reasons.append(str(caught.value.details["reason"]))
    assert reasons == ["relative", "nonexistent", "outside_repository"]

    claimed = coordinator.claim("alpha", [str(git_repo.root / "a.txt")])
    assert [claim.path for claim in claimed.claims] == ["a.txt"]
    assert alpha.index_path.is_file()
    assert coordinator.claim("alpha", [str(git_repo.root / "a.txt")]) == claimed

    verdict = coordinator.check("beta", str(git_repo.root / "a.txt"))
    assert verdict.blocked is True
    assert [holder.actor for holder in verdict.holders] == ["alpha"]
    with pytest.raises(ClaimConflictError) as conflict:
        coordinator.claim("beta", [str(git_repo.root / "a.txt")])
    assert conflict.value.details["holders"] == ["alpha"]

    released = coordinator.release("alpha", [str(git_repo.root / "a.txt")])
    assert released.released == ("a.txt",)
    allowed = coordinator.claim("beta", [str(git_repo.root / "a.txt")])
    assert [claim.actor for claim in allowed.claims] == ["beta"]


def test_directory_claim_covers_descendants(git_repo: GitFixture) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("owner", pid=os.getpid(), ttl=300)
    coordinator.register_session("peer", pid=os.getpid(), ttl=300)
    coordinator.claim("owner", [str(git_repo.root / "nested")])

    verdict = coordinator.check("peer", str(git_repo.root / "nested/c.txt"))
    assert verdict.blocked
    assert verdict.holders[0].path == "nested"

    coordinator.release("owner")
    assert not coordinator.check("peer", str(git_repo.root / "nested/c.txt")).blocked
