"""INV4: the refs-backed WAL is a validated linear commit chain."""

from __future__ import annotations

import os

import pytest
from conftest import GitFixture

from onetree.core import Coordinator
from onetree.errors import WalCorruptError
from onetree.wal import WAL_REF, Wal


def test_inv4_malformed_non_linear_wal_refuses_and_valid_append_is_linear(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    wal = Wal(coordinator.repo)
    first = wal.read()
    assert first.head is not None
    original_document = git_repo.git("show", f"{first.head}:event.json").stdout

    # RED control: raw plumbing can point the ref at a merge-shaped event.
    # The WAL reader must reject it instead of folding an arbitrary first-parent view.
    tree = git_repo.git("show", "-s", "--format=%T", first.head).stdout.strip()
    malformed = git_repo.git(
        "commit-tree",
        tree,
        "-p",
        first.head,
        "-p",
        git_repo.base,
        input_text="unsafe non-linear WAL\n",
    ).stdout.strip()
    git_repo.git("update-ref", WAL_REF, malformed, first.head)
    with pytest.raises(WalCorruptError, match="exactly one parent"):
        wal.read()

    git_repo.git("update-ref", WAL_REF, first.head, malformed)
    coordinator.claim("alpha", [str(git_repo.root / "a.txt")])
    valid = wal.read()
    assert len(valid.events) == 2
    assert valid.events[1].parents == (valid.events[0].oid,)
    assert git_repo.git("show", f"{first.head}:event.json").stdout == original_document
    reflog = git_repo.git("reflog", "show", "--format=%H", WAL_REF).stdout.splitlines()
    assert valid.head in reflog
    assert first.head in reflog


def test_wal_inspection_reports_folded_state(git_repo: GitFixture) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    coordinator.claim("alpha", [str(git_repo.root / "a.txt")])
    report = coordinator.inspect_wal()

    assert report.consistent
    assert [event.kind for event in report.events] == ["session_register", "claim"]
    assert report.active_sessions == ("alpha",)
    assert report.active_claims == ("a.txt",)
