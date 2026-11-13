from __future__ import annotations

import os
from pathlib import Path

import pytest
from conftest import GitFixture
from test_cli import _json_call
from test_defensive import _expose_intent

from onetree import core
from onetree.core import Coordinator
from onetree.errors import GitError, IdentityError, InvalidPathError, NarrowRefusedError
from onetree.wal import WAL_REF


def test_narrow_has_no_visible_unheld_window(
    git_repo: GitFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator = Coordinator(git_repo.root)
    peer = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    peer.register_session("beta", pid=os.getpid(), ttl=300)
    parent = str(git_repo.root / "nested")
    coordinator.claim("alpha", [parent, str(git_repo.root / "a.txt")])
    created = git_repo.write("nested/new.txt", "new file\n")
    before = coordinator.wal.read()
    visible: list[bool] = []
    update_ref = coordinator.repo.update_ref

    def observe(reference: str, new: str, expected: str | None) -> bool:
        if reference == WAL_REF:
            visible.append(peer.check("beta", str(created)).blocked)
        won = update_ref(reference, new, expected)
        if reference == WAL_REF:
            visible.append(peer.check("beta", str(created)).blocked)
        return won

    monkeypatch.setattr(coordinator.repo, "update_ref", observe)
    coordinator.narrow("alpha", parent, [str(created)])
    assert visible
    assert all(visible), visible
    assert len(visible) == 2
    after = coordinator.wal.read()
    assert [event.kind for event in after.events[len(before.events) :]] == ["claim", "release"]
    assert peer.check("beta", str(created)).holders[0].path == "nested/new.txt"
    assert not peer.check("beta", str(git_repo.root / "nested/c.txt")).blocked
    assert peer.check("beta", str(git_repo.root / "a.txt")).holders[0].path == "a.txt"


def test_narrow_preserves_existing_generations_and_does_not_renew_leases(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    session = coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    child = str(git_repo.root / "nested/c.txt")
    existing = coordinator.claim("alpha", [child], ttl=200).claims[0]
    parent = str(git_repo.root / "nested")
    surrendered = coordinator.claim("alpha", [parent], ttl=100).claims[0]
    created = git_repo.write("nested/new.txt", "new\n")
    result = coordinator.narrow("alpha", parent, [child, str(created), child])
    assert result.released == ("nested",)
    assert [claim.path for claim in result.claims] == ["nested/c.txt", "nested/new.txt"]
    assert result.claims[0] == existing
    new = result.claims[1]
    assert new.expires_at == surrendered.expires_at
    assert new.session_generation == session.generation
    assert new.generation not in {surrendered.generation, existing.generation}
    fresh = Coordinator(git_repo.root).inspect_wal()
    assert fresh.consistent
    assert len(fresh.active_claims) == 2


def test_narrow_collapses_overlapping_requested_children(git_repo: GitFixture) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    coordinator.claim("alpha", [str(git_repo.root)])
    result = coordinator.narrow(
        "alpha",
        str(git_repo.root),
        [str(git_repo.root / "nested/c.txt"), str(git_repo.root / "nested")],
    )
    assert [claim.path for claim in result.claims] == ["nested"]
    assert coordinator.check("beta", str(git_repo.root / "nested/c.txt")).blocked
    assert not coordinator.check("beta", str(git_repo.root / "a.txt")).blocked


@pytest.mark.parametrize(
    "case", ["empty", "self", "sibling", "relative", "missing", "git", "alias"]
)
def test_invalid_narrow_batch_preserves_wal(git_repo: GitFixture, case: str) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    parent = str(git_repo.root / "nested")
    coordinator.claim("alpha", [parent])
    child = str(git_repo.root / "nested/c.txt")
    alias = git_repo.root / "nested/alias"
    alias.symlink_to(git_repo.root / "a.txt")
    invalid = {
        "empty": [],
        "self": [parent],
        "sibling": [str(git_repo.root / "a.txt")],
        "relative": ["nested/c.txt"],
        "missing": [str(git_repo.root / "nested/c.txt/missing")],
        "git": [str(git_repo.root / ".git/config")],
        "alias": [str(alias)],
    }[case]
    before = coordinator.wal.read()
    with pytest.raises(InvalidPathError):
        coordinator.narrow("alpha", parent, [] if case == "empty" else [child, *invalid])
    assert coordinator.wal.read() == before
    assert coordinator.check("beta", child).holders[0].path == "nested"


@pytest.mark.parametrize(("actor", "held"), [("alpha", "."), ("beta", "nested")])
def test_narrow_requires_exact_owned_parent(git_repo: GitFixture, actor: str, held: str) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    coordinator.register_session("beta", pid=os.getpid(), ttl=300)
    original = coordinator.claim("alpha", [str(git_repo.root / held)]).claims[0]
    before = coordinator.wal.read()
    with pytest.raises(NarrowRefusedError) as caught:
        coordinator.narrow(
            actor, str(git_repo.root / "nested"), [str(git_repo.root / "nested/c.txt")]
        )
    assert caught.value.details["reason"] == "not_held"
    after = coordinator.wal.read()
    assert [event.kind for event in after.events[len(before.events) :]] == ["refused"]
    assert after.events[-1].data["action"] == "narrow"
    assert coordinator.check("beta", str(git_repo.root / "nested/c.txt")).holders == (original,)


@pytest.mark.parametrize("parent_fenced", [True, False])
def test_narrow_preserves_pending_landing_fences(git_repo: GitFixture, parent_fenced: bool) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    if parent_fenced:
        coordinator.claim("alpha", [str(git_repo.root)])
    _, fenced = _expose_intent(git_repo, coordinator, move_target=False)
    if not parent_fenced:
        coordinator.claim("alpha", [str(git_repo.root)])
    if parent_fenced:
        with pytest.raises(NarrowRefusedError) as caught:
            coordinator.narrow("alpha", str(git_repo.root), [str(git_repo.root / "a.txt")])
        assert caught.value.details["reason"] == "pending_land"
        assert (
            coordinator.check("beta", str(git_repo.root / "a.txt")).holders[0].generation == fenced
        )
    else:
        result = coordinator.narrow(
            "alpha",
            str(git_repo.root),
            [str(git_repo.root / "a.txt"), str(git_repo.root / "b.txt")],
        )
        assert result.claims[0].generation == fenced
    assert coordinator.inspect_wal().pending_intents == 1


@pytest.mark.parametrize(
    "interleave", ["unrelated", "replace", "expire", "session", "rotate", "pending"]
)
def test_narrow_revalidates_after_lost_cas(
    git_repo: GitFixture, monkeypatch: pytest.MonkeyPatch, interleave: str
) -> None:
    coordinator = Coordinator(git_repo.root)
    peer = Coordinator(git_repo.root)
    session = coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    peer.register_session("beta", pid=os.getpid(), ttl=300)
    parent = str(git_repo.root)
    original = coordinator.claim("alpha", [parent], ttl=100).claims[0]
    child = str(git_repo.root / "nested/c.txt")
    update_ref = coordinator.repo.update_ref
    calls = 0

    def contend(reference: str, new: str, expected: str | None) -> bool:
        nonlocal calls
        if reference == WAL_REF:
            calls += 1
            if calls == 1:
                if interleave == "replace":
                    peer.release("alpha", [parent])
                    peer.claim("alpha", [parent])
                elif interleave == "pending":
                    _expose_intent(git_repo, peer, move_target=False)
                else:
                    if interleave in {"expire", "session", "rotate"}:
                        deadline = (
                            original.expires_at if interleave == "expire" else session.expires_at
                        )
                        monkeypatch.setattr(core.time, "time", lambda: deadline + 1)
                    if interleave == "rotate":
                        peer.register_session("alpha", pid=os.getpid(), ttl=300)
                        peer.claim("alpha", [parent])
                    peer.wal.append("refused", "beta", {"action": "probe", "reason": "interleave"})
        return update_ref(reference, new, expected)

    monkeypatch.setattr(coordinator.repo, "update_ref", contend)
    if interleave == "unrelated":
        assert coordinator.narrow("alpha", parent, [child]).released == (".",)
        assert calls == 2
        assert coordinator.check("beta", child).holders[0].path == "nested/c.txt"
    elif interleave == "session":
        with pytest.raises(IdentityError):
            coordinator.narrow("alpha", parent, [child])
    else:
        with pytest.raises(NarrowRefusedError) as caught:
            coordinator.narrow("alpha", parent, [child])
        assert (
            caught.value.details["reason"]
            == {
                "replace": "claim_changed",
                "rotate": "claim_changed",
                "expire": "not_held",
                "pending": "pending_land",
            }[interleave]
        )
    report = coordinator.inspect_wal()
    assert report.consistent is (interleave != "pending")
    if interleave in {"replace", "rotate"}:
        assert coordinator.check("beta", child).holders[0].generation != original.generation
        assert coordinator.check("beta", child).holders[0].path == "."
    elif interleave == "pending":
        assert report.pending_intents == 1
        assert coordinator.check("beta", child).holders[0].generation == original.generation
    else:
        assert any(event.data.get("reason") == "interleave" for event in report.events)


def test_narrow_retry_exhaustion_preserves_claim(
    git_repo: GitFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    coordinator.claim("alpha", [str(git_repo.root / "nested")])
    before = coordinator.wal.read()
    monkeypatch.setattr(core, "MAX_CAS_RETRIES", 2)
    monkeypatch.setattr(coordinator.repo, "update_ref", lambda *args: False)
    with pytest.raises(GitError, match="narrow CAS retry limit exceeded"):
        coordinator.narrow(
            "alpha", str(git_repo.root / "nested"), [str(git_repo.root / "nested/c.txt")]
        )
    assert coordinator.wal.read() == before


def test_narrow_cli_json_and_human(
    git_repo: GitFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    from onetree.cli import main

    common = ["--repo", str(git_repo.root), "--session", "alpha"]
    _, registered = _json_call(capsys, ["session", *common, "--pid", str(os.getpid())])
    parent = str(git_repo.root / "nested")
    child = str(git_repo.root / "nested/c.txt")
    assert _json_call(capsys, ["claim", parent, *common])[0] == 0
    code, payload = _json_call(capsys, ["narrow", parent, child, *common])
    assert code == 0
    assert payload["narrow"]["released"] == ["nested"]
    assert payload["narrow"]["claims"][0]["path"] == "nested/c.txt"
    code, refused = _json_call(capsys, ["narrow", parent, child, *common])
    assert code == 1
    assert refused["error"]["type"] == "narrow_refused"
    assert refused["error"]["reason"] == "not_held"
    git_repo.write("nested/c.txt", "land after narrowing\n")
    git_repo.stage(Path(registered["session"]["index"]), "nested/c.txt")
    assert _json_call(capsys, ["land", *common])[0] == 0
    assert _json_call(capsys, ["claim", parent, *common])[0] == 0
    assert main(["narrow", parent, child, *common]) == 0
    assert "retained 1 claim(s)" in capsys.readouterr().out
