from __future__ import annotations

import os
import time

import pytest
from conftest import GitFixture

from onetree.core import Coordinator
from onetree.errors import LandingRefusedError


@pytest.mark.parametrize("second_path", ["a.txt", "b.txt"])
def test_same_live_session_lands_again_without_replaying_old_payload(
    git_repo: GitFixture, second_path: str,
) -> None:
    coordinator = Coordinator(git_repo.root)
    session = coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    coordinator.claim("alpha", [str(git_repo.root)])
    git_repo.write("a.txt", "first\n")
    git_repo.stage(session.index_path, "a.txt")
    first = coordinator.land("alpha")
    git_repo.write(second_path, "second\n")
    git_repo.stage(session.index_path, second_path)
    staged_before = git_repo.git("ls-files", "--stage", "-z", index=session.index_path).stdout

    second = Coordinator(git_repo.root).land("alpha")

    assert second.payload == (second_path,)
    assert git_repo.git("show", f"HEAD:{second_path}").stdout == "second\n"
    assert git_repo.git("rev-parse", "HEAD^").stdout.strip() == first.commit
    staged_after = git_repo.git("ls-files", "--stage", "-z", index=session.index_path).stdout
    assert staged_after == staged_before
    assert coordinator.inspect_wal().consistent


def test_subsequent_submission_does_not_revert_disjoint_peer_landing(git_repo: GitFixture) -> None:
    coordinator = Coordinator(git_repo.root)
    alpha = coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    beta = coordinator.register_session("beta", pid=os.getpid(), ttl=300)
    coordinator.claim("alpha", [str(git_repo.root / "a.txt")])
    coordinator.claim("beta", [str(git_repo.root / "b.txt")])
    git_repo.write("b.txt", "peer\n")
    git_repo.stage(beta.index_path, "b.txt")
    coordinator.land("beta")
    git_repo.write("a.txt", "first\n")
    git_repo.stage(alpha.index_path, "a.txt")
    coordinator.land("alpha")
    git_repo.write("a.txt", "second\n")
    git_repo.stage(alpha.index_path, "a.txt")

    result = coordinator.land("alpha")

    assert result.payload == ("a.txt",)
    assert git_repo.git("show", "HEAD:b.txt").stdout == "peer\n"
    assert git_repo.git("show", "HEAD:a.txt").stdout == "second\n"
    assert coordinator.inspect_wal().consistent


def test_unchanged_index_after_landing_has_nothing_to_submit(git_repo: GitFixture) -> None:
    coordinator = Coordinator(git_repo.root)
    session = coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    coordinator.claim("alpha", [str(git_repo.root / "a.txt")])
    git_repo.write("a.txt", "first\n")
    git_repo.stage(session.index_path, "a.txt")
    first = coordinator.land("alpha")

    with pytest.raises(LandingRefusedError) as caught:
        coordinator.land("alpha")

    assert caught.value.details["reason"] == "nothing_staged"
    assert git_repo.oid() == first.commit


def test_staging_during_first_landing_survives_for_next_submission(git_repo: GitFixture) -> None:
    coordinator = Coordinator(git_repo.root)
    session = coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    coordinator.claim("alpha", [str(git_repo.root)])
    git_repo.write("a.txt", "first\n")
    git_repo.stage(session.index_path, "a.txt")

    def stage_next(point: str, context: dict[str, object]) -> None:
        if point == "after_tree":
            git_repo.write("b.txt", "staged during landing\n")
            git_repo.stage(session.index_path, "b.txt")

    first = coordinator.land("alpha", checkpoint=stage_next)
    assert git_repo.git("show", f"{first.commit}:b.txt").stdout != "staged during landing\n"
    second = coordinator.land("alpha")
    assert second.payload == ("b.txt",)
    assert git_repo.git("show", "HEAD:b.txt").stdout == "staged during landing\n"
    assert coordinator.inspect_wal().consistent


def test_peer_change_to_previously_landed_path_still_conflicts(git_repo: GitFixture) -> None:
    coordinator = Coordinator(git_repo.root)
    alpha = coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    path = str(git_repo.root / "a.txt")
    coordinator.claim("alpha", [path])
    git_repo.write("a.txt", "first\n")
    git_repo.stage(alpha.index_path, "a.txt")
    coordinator.land("alpha")
    coordinator.release("alpha", [path])
    beta = coordinator.register_session("beta", pid=os.getpid(), ttl=300)
    coordinator.claim("beta", [path])
    git_repo.write("a.txt", "peer\n")
    git_repo.stage(beta.index_path, "a.txt")
    peer = coordinator.land("beta")
    coordinator.release("beta", [path])
    coordinator.claim("alpha", [path])
    git_repo.write("a.txt", "next own change\n")
    git_repo.stage(alpha.index_path, "a.txt")

    with pytest.raises(LandingRefusedError) as caught:
        coordinator.land("alpha")

    assert caught.value.details["reason"] == "payload_conflict"
    assert caught.value.details["paths"] == ["a.txt"]
    assert git_repo.oid() == peer.commit


@pytest.mark.parametrize("recreate", ["register", "land"])
def test_missing_index_recovers_personal_progress_without_reverting_it(
    git_repo: GitFixture, recreate: str,
) -> None:
    coordinator = Coordinator(git_repo.root)
    session = coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    coordinator.claim("alpha", [str(git_repo.root / "a.txt")])
    git_repo.write("a.txt", "first\n")
    git_repo.stage(session.index_path, "a.txt")
    first = coordinator.land("alpha")
    session.index_path.unlink()
    if recreate == "register":
        coordinator.register_session("alpha", pid=os.getpid(), ttl=300)

    with pytest.raises(LandingRefusedError) as caught:
        coordinator.land("alpha")

    assert caught.value.details["reason"] == "nothing_staged"
    assert git_repo.oid() == first.commit
    assert git_repo.git("show", ":a.txt", index=session.index_path).stdout == "first\n"


def test_delete_then_readd_is_a_new_payload(git_repo: GitFixture) -> None:
    coordinator = Coordinator(git_repo.root)
    session = coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    coordinator.claim("alpha", [str(git_repo.root)])
    (git_repo.root / "a.txt").unlink()
    git_repo.stage(session.index_path, "a.txt")
    coordinator.land("alpha")
    git_repo.write("a.txt", "restored\n")
    git_repo.stage(session.index_path, "a.txt")

    second = coordinator.land("alpha")

    assert second.payload == ("a.txt",)
    assert git_repo.git("show", "HEAD:a.txt").stdout == "restored\n"
    assert coordinator.inspect_wal().consistent


def test_new_incarnation_does_not_inherit_previous_personal_baseline(git_repo: GitFixture) -> None:
    coordinator = Coordinator(git_repo.root)
    alpha = coordinator.register_session("alpha", pid=os.getpid(), ttl=2)
    path = str(git_repo.root / "a.txt")
    coordinator.claim("alpha", [path])
    git_repo.write("a.txt", "old incarnation\n")
    git_repo.stage(alpha.index_path, "a.txt")
    coordinator.land("alpha")
    coordinator.release("alpha", [path])
    beta = coordinator.register_session("beta", pid=os.getpid(), ttl=300)
    coordinator.claim("beta", [path])
    git_repo.write("a.txt", "peer\n")
    git_repo.stage(beta.index_path, "a.txt")
    peer = coordinator.land("beta")
    coordinator.release("beta", [path])
    time.sleep(max(0, alpha.expires_at - time.time()) + 0.05)
    renewed = coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    assert renewed.generation != alpha.generation
    assert renewed.base == peer.commit
    coordinator.claim("alpha", [path])
    git_repo.write("a.txt", "new incarnation\n")
    git_repo.stage(renewed.index_path, "a.txt")

    result = coordinator.land("alpha")

    assert git_repo.oid() == result.commit
    assert git_repo.git("show", "HEAD:a.txt").stdout == "new incarnation\n"


def test_red_referee_does_not_advance_personal_baseline(git_repo: GitFixture) -> None:
    coordinator = Coordinator(git_repo.root)
    session = coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    coordinator.claim("alpha", [str(git_repo.root / "a.txt")])
    git_repo.write("a.txt", "first\n")
    git_repo.stage(session.index_path, "a.txt")
    first = coordinator.land("alpha")
    git_repo.write("a.txt", "second\n")
    git_repo.stage(session.index_path, "a.txt")
    git_repo.git("config", "onetree.referee", "/bin/false")

    with pytest.raises(LandingRefusedError) as caught:
        coordinator.land("alpha")

    assert caught.value.details["reason"] == "referee_red"
    assert git_repo.oid() == first.commit
    git_repo.git("config", "onetree.referee", "/bin/true")
    second = coordinator.land("alpha")
    assert second.payload == ("a.txt",)
    assert git_repo.git("show", "HEAD:a.txt").stdout == "second\n"
    assert coordinator.inspect_wal().consistent
