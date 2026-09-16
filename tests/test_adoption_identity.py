from __future__ import annotations

import os

import pytest
from conftest import GitFixture
from test_recovery_semantics import _marked_commit, _pending_intent, _register, _stage

from onetree.core import Coordinator
from onetree.errors import OnetreeError


@pytest.mark.parametrize("cross_target", [False, True])
def test_adoption_preserves_distinct_unrecorded_commits_with_one_operation(
    git_repo: GitFixture, cross_target: bool,
) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    operation = "a" * 32
    genuine = _marked_commit(coordinator, "refs/heads/main", operation, (git_repo.base,))
    target = "refs/heads/release" if cross_target else "refs/heads/main"
    copied = _marked_commit(
        coordinator, target, operation, (git_repo.base if cross_target else genuine,),
        extra="\n\nseparate commit",
    )
    assert copied != genuine
    assert {item.commit for item in coordinator.inspect_wal().divergences} == {genuine, copied}

    adoption = coordinator.adopt_landings(target)
    expected = {copied} if cross_target else {genuine, copied}
    assert {event.data["commit"] for event in adoption.adopted} == expected
    assert {event.data["operation"] for event in adoption.adopted} == {operation}
    if cross_target:
        assert not adoption.report.consistent
        assert [(item.target, item.commit) for item in adoption.report.divergences] == [
            ("refs/heads/main", genuine),
        ]
        head = coordinator.wal.read().head
        with pytest.raises(OnetreeError) as refused:
            coordinator.register_session("beta", pid=os.getpid(), ttl=300)
        assert refused.value.kind == "unrecorded_landing"
        assert coordinator.wal.read().head == head
        remaining = coordinator.adopt_landings("refs/heads/main")
        assert [event.data["commit"] for event in remaining.adopted] == [genuine]
    assert coordinator.inspect_wal().consistent
    assert coordinator.recover().consistent
    coordinator.register_session("beta", pid=os.getpid(), ttl=300)
    assert coordinator.adopt_landings().adopted == ()


@pytest.mark.parametrize("payload", ["real_oid", "invalid_oid"])
def test_adoption_never_turns_trailer_control_bytes_into_a_second_record(
    git_repo: GitFixture, payload: str,
) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    forged = git_repo.base if payload == "real_oid" else "not-an-oid"
    operation = "b" * 32 + "\x1e" + forged + "\x1fFORGED"
    actual = _marked_commit(coordinator, "refs/heads/main", operation, (git_repo.base,))
    raw = coordinator.repo.git("cat-file", "commit", actual).stdout
    assert operation.encode() in raw
    assert coordinator.repo.landing_operations(git_repo.base) == frozenset()
    before = coordinator.wal.read()
    with pytest.raises(OnetreeError) as refused:
        coordinator.register_session("beta", pid=os.getpid(), ttl=300)
    assert refused.value.kind == "unrecorded_landing"
    assert coordinator.wal.read().head == before.head

    adoption = coordinator.adopt_landings()
    assert [event.data["commit"] for event in adoption.adopted] == [actual]
    after = coordinator.wal.read()
    appended = after.events[len(before.events):]
    assert len(appended) == 1
    assert appended[0].kind == "adopted"
    assert appended[0].data["commit"] == actual
    assert adoption.report.consistent
    assert coordinator.inspect_wal().consistent
    assert coordinator.recover().consistent
    assert coordinator.adopt_landings().adopted == ()
    coordinator.gc("collector")
    coordinator.register_session("beta", pid=os.getpid(), ttl=300)
    assert coordinator.inspect_wal().consistent


def test_cherry_picked_refused_candidate_is_a_distinct_adoptable_commit(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    session = _register(coordinator, git_repo, "alpha", "a.txt")
    _stage(git_repo, session, "a.txt", "candidate bytes\n")
    candidate = _pending_intent(git_repo, coordinator, session)
    recovery = coordinator.recover()
    assert recovery.refused == 1
    git_repo.git("reset", "--hard", git_repo.base)
    git_repo.git("cherry-pick", "-x", candidate)
    copied = git_repo.oid()
    assert copied != candidate
    assert git_repo.git("show", f"{copied}:a.txt").stdout == "candidate bytes\n"
    assert [item.commit for item in coordinator.inspect_wal().divergences] == [copied]
    before = coordinator.wal.read().head
    with pytest.raises(OnetreeError) as refused:
        coordinator.register_session("beta", pid=os.getpid(), ttl=300)
    assert refused.value.kind == "unrecorded_landing"
    assert coordinator.wal.read().head == before
    adoption = coordinator.adopt_landings()
    assert [event.data["commit"] for event in adoption.adopted] == [copied]
    assert adoption.report.consistent
    assert coordinator.recover().consistent
    coordinator.register_session("beta", pid=os.getpid(), ttl=300)
