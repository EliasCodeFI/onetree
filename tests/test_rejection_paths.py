from __future__ import annotations

import os
import time
from collections.abc import Mapping
from typing import Any

import pytest
from conftest import GitFixture

from onetree import core
from onetree.core import Coordinator
from onetree.errors import LandingRefusedError


@pytest.mark.parametrize("case", ["claim_expiry", "session_expiry", "diverged"])
def test_refusal_and_wal_name_exact_payload_paths(
    git_repo: GitFixture, monkeypatch: pytest.MonkeyPatch, case: str,
) -> None:
    coordinator = Coordinator(git_repo.root)
    session = coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    coordinator.claim("alpha", [str(git_repo.root / "nested")], ttl=10)
    git_repo.write("nested/c.txt", "candidate\n")
    git_repo.stage(session.index_path, "nested/c.txt")
    before = time.time()
    if case == "diverged":
        tree = git_repo.git("show", "-s", "--format=%T", git_repo.base).stdout.strip()
        orphan = git_repo.git("commit-tree", tree, input_text="unrelated\n").stdout.strip()
        git_repo.git("update-ref", "refs/heads/main", orphan, git_repo.base)

    def checkpoint(point: str, _context: Mapping[str, Any]) -> None:
        if point == "before_intent" and case != "diverged":
            advance = 20 if case == "claim_expiry" else 400
            monkeypatch.setattr(core.time, "time", lambda: before + advance)

    with pytest.raises(LandingRefusedError) as caught:
        coordinator.land("alpha", checkpoint=checkpoint)
    assert caught.value.details["paths"] == ["nested/c.txt"]
    assert coordinator.wal.read().events[-1].data["paths"] == ["nested/c.txt"]
