from __future__ import annotations

import copy
import os
import subprocess
from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from conftest import _GIT_EXECUTABLE, GitFixture
from test_cli import _json_call

from onetree import core, workspace
from onetree.cli import main
from onetree.core import Coordinator
from onetree.errors import (
    LandingRefusedError,
    UnrecordedLandingError,
    WalCorruptError,
    WalDivergedError,
)
from onetree.wal import EventRecord, Wal


def epoch_id(coordinator: Coordinator) -> str:
    return next(event.event_id for event in reversed(coordinator.wal.read().events)
                if event.kind in {"workspace_protect", "workspace_reconcile"})


def evidence(git_repo: GitFixture) -> tuple[Any, ...]:
    return (
        git_repo.git("for-each-ref", "--format=%(refname) %(objectname)", "refs/heads").stdout,
        (git_repo.root / ".git/index").read_bytes(),
        (git_repo.root / "a.txt").read_bytes(),
        (git_repo.root / "b.txt").read_bytes(),
        (git_repo.root / ".git/logs/HEAD").read_bytes(),
        (git_repo.root / ".git/logs/refs/heads/main").read_bytes(),
    )


@pytest.mark.parametrize("change", ["expiry", "routine_expiry", "external", "healthy"])
def test_explicit_reconciliation_preserves_history_and_allows_fresh_work(
    git_repo: GitFixture, change: str,
) -> None:
    if change == "routine_expiry":
        aged = os.environ.copy()
        aged.pop("GIT_INDEX_FILE", None)
        aged["GIT_COMMITTER_DATE"] = "2026-01-01T00:00:00Z"
        git_repo.write("b.txt", "aged\n")
        for arguments in (["add", "--", "b.txt"], ["commit", "--quiet", "-m", "aged"]):
            subprocess.run([_GIT_EXECUTABLE, "-C", str(git_repo.root), *arguments],
                           check=True, capture_output=True, env=aged)
    coordinator = Coordinator(git_repo.root)
    original = dict(coordinator.protect_workspace("operator"))
    prior_id = epoch_id(coordinator)
    if change == "expiry":
        git_repo.git("reflog", "expire", "--expire=now", "--all")
    elif change == "routine_expiry":
        tip = git_repo.oid()
        git_repo.git("reflog", "expire", "--all")
        assert git_repo.oid() == tip
        assert (git_repo.root / ".git/logs/HEAD").read_bytes()
    elif change == "external":
        git_repo.external_commit("b.txt", "external\n", "outside broker")
    if change != "healthy":
        with pytest.raises(WalDivergedError):
            coordinator.recover()
    before = coordinator.wal.read()
    held = evidence(git_repo)
    result = coordinator.reconcile_workspace(
        "operator", expected_epoch=prior_id, reason="Inspected current shared state",
    )
    after = coordinator.wal.read()
    assert after.events[:-1] == before.events
    event = after.events[-1]
    assert event.kind == "workspace_reconcile"
    assert event.document["v"] == 4
    assert event.data["previous_epoch"] == prior_id
    assert event.data["reason"] == "Inspected current shared state"
    assert (event.data["violation"] is None) == (change == "healthy")
    assert event.data["baseline"]["tip"] == git_repo.oid()
    assert result == {"epoch": event.event_id, **event.data}
    assert after.events[0].data == original
    assert evidence(git_repo) == held
    assert Coordinator(git_repo.root).inspect_wal().consistent
    assert coordinator.recover().consistent

    session = coordinator.register_session("fresh", pid=os.getpid())
    coordinator.claim("fresh", [str(git_repo.root / "a.txt")])
    git_repo.write("a.txt", "fresh\n")
    git_repo.stage(session.index_path, "a.txt")
    landed = coordinator.land("fresh")
    assert landed.commit == git_repo.oid()
    assert coordinator.inspect_wal().consistent
    assert git_repo.git("show", "HEAD:b.txt").stdout == held[3].decode()


@pytest.mark.parametrize("reason", ["", "  \n\t", None, 12])
def test_reconciliation_requires_reason_without_writing(git_repo: GitFixture, reason: Any) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.protect_workspace("operator")
    before = coordinator.wal.head()
    with pytest.raises(LandingRefusedError) as caught:
        coordinator.reconcile_workspace("operator", expected_epoch=epoch_id(coordinator),
                                        reason=reason)
    assert caught.value.details["reason"] == "reconciliation_reason"
    assert coordinator.wal.head() == before


def test_reconciliation_binds_each_epoch_and_requires_protection(git_repo: GitFixture) -> None:
    coordinator = Coordinator(git_repo.root)
    with pytest.raises(LandingRefusedError) as caught:
        coordinator.reconcile_workspace("operator", expected_epoch="absent", reason="reviewed")
    assert caught.value.details["reason"] == "workspace_unprotected"
    assert coordinator.wal.head() is None
    coordinator.protect_workspace("operator")
    old = epoch_id(coordinator)
    first = coordinator.reconcile_workspace("operator", expected_epoch=old, reason="first")
    head = coordinator.wal.head()
    with pytest.raises(LandingRefusedError) as caught:
        coordinator.reconcile_workspace("operator", expected_epoch=old, reason="stale")
    assert caught.value.details["reason"] == "workspace_epoch_changed"
    assert coordinator.wal.head() == head
    second = coordinator.reconcile_workspace("operator", expected_epoch=first["epoch"],
                                             reason="second")
    assert second["previous_epoch"] == first["epoch"]
    assert second["epoch"] != first["epoch"]


def test_reconciliation_refuses_active_claim_and_pending_intent(
    git_repo: GitFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.protect_workspace("operator")
    prior = epoch_id(coordinator)
    session = coordinator.register_session("alpha", pid=os.getpid())
    coordinator.claim("alpha", [str(git_repo.root / "a.txt")])
    before = coordinator.wal.head()
    with pytest.raises(LandingRefusedError) as caught:
        coordinator.reconcile_workspace("operator", expected_epoch=prior, reason="busy")
    assert caught.value.details["reason"] == "workspace_busy"
    assert coordinator.wal.head() == before
    git_repo.write("a.txt", "alpha\n")
    git_repo.stage(session.index_path, "a.txt")
    reached = []

    def checkpoint(point: str, context: Mapping[str, Any]) -> None:
        if point == "after_intent":
            assert coordinator.inspect_wal().pending_intents == 1
            head = coordinator.wal.head()
            with monkeypatch.context() as clock:
                clock.setattr(core.time, "time", lambda: session.expires_at + 1)
                assert coordinator.inspect_wal().active_claims == ()
                with pytest.raises(LandingRefusedError) as refusal:
                    coordinator.reconcile_workspace("operator", expected_epoch=prior, reason="busy")
            assert refusal.value.details["reason"] == "workspace_busy"
            assert coordinator.wal.head() == head
            reached.append(context["intent"])

    coordinator.land("alpha", checkpoint=checkpoint)
    assert len(reached) == 1


@pytest.mark.parametrize("content", [b"partial", b"not a reflog\n", b"f" * 40 + b" 0\n"])
def test_reconciliation_refuses_malformed_reflog(git_repo: GitFixture, content: bytes) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.protect_workspace("operator")
    prior = epoch_id(coordinator)
    (git_repo.root / ".git/logs/HEAD").write_bytes(content)
    head = coordinator.wal.head()
    with pytest.raises(LandingRefusedError) as caught:
        coordinator.reconcile_workspace("operator", expected_epoch=prior, reason="reviewed")
    assert caught.value.details["reason"] == "workspace_unavailable"
    assert coordinator.wal.head() == head
    assert (git_repo.root / ".git/logs/HEAD").read_bytes() == content


@pytest.mark.parametrize("field", ["previous_epoch", "reason", "violation", "baseline", "extra"])
def test_replay_refuses_bad_reconciliation_record(git_repo: GitFixture, field: str) -> None:
    coordinator = Coordinator(git_repo.root)
    baseline = dict(coordinator.protect_workspace("operator"))
    data: dict[str, Any] = {"previous_epoch": epoch_id(coordinator), "reason": "reviewed",
                            "violation": None, "baseline": baseline}
    bad = copy.deepcopy(data)
    bad[field] = {"previous_epoch": "stale", "reason": " ", "violation": 1,
                  "baseline": {}, "extra": True}[field]
    event = EventRecord("bad", (), Wal.document("workspace_reconcile", "operator", bad))
    with pytest.raises(WalCorruptError):
        coordinator._apply(coordinator._fold(coordinator.wal.read()), event)
    good = EventRecord("good", (), Wal.document("workspace_reconcile", "operator", data))
    state = coordinator._fold(coordinator.wal.read())
    coordinator._apply(state, good)
    assert state.workspace_epoch == baseline


def test_reconciliation_cli_json_preserves_error_and_result(
    git_repo: GitFixture, capsys: pytest.CaptureFixture[str],
) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.protect_workspace("operator")
    arguments = ["reconcile", "--repo", str(git_repo.root), "--session", "operator",
                 "--expected-epoch", epoch_id(coordinator), "--reason", "Inspected expiry"]
    git_repo.git("reflog", "expire", "--expire=now", "--all")
    code, payload = _json_call(capsys, arguments)
    assert code == 0
    assert payload["ok"]
    assert payload["workspace"]["epoch"] == epoch_id(coordinator)
    code, payload = _json_call(capsys, arguments)
    assert code != 0
    assert not payload["ok"]
    assert payload["error"]["reason"] == "workspace_epoch_changed"
    assert main(["reconcile", "--repo", str(git_repo.root), "--session", "operator",
                 "--expected-epoch", epoch_id(coordinator), "--reason", "Reviewed again"]) == 0
    assert "reconciled" in capsys.readouterr().out


def test_reconciliation_records_stale_claim_collection_without_deleting_files(
    git_repo: GitFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.protect_workspace("operator")
    prior = epoch_id(coordinator)
    session = coordinator.register_session("alpha", pid=os.getpid())
    claimed = coordinator.claim("alpha", [str(git_repo.root / "a.txt")]).claims[0]
    original_index = session.index_path.read_bytes()
    monkeypatch.setattr(core.time, "time", lambda: session.expires_at + 1)
    assert coordinator.inspect_wal().active_claims == ()
    before = coordinator.wal.read()
    coordinator.reconcile_workspace("operator", expected_epoch=prior, reason="Lease expired")
    after = coordinator.wal.read()
    assert after.events[:-2] == before.events
    assert [event.kind for event in after.events[-2:]] == ["gc", "workspace_reconcile"]
    assert after.events[-2].data == {"claim_generations": [claimed.generation],
                                   "session_generations": [], "temp_indexes": []}
    assert session.index_path.read_bytes() == original_index


@pytest.mark.parametrize("race", ["claim", "epoch"])
def test_reconciliation_cas_rechecks_a_competing_writer(
    git_repo: GitFixture, monkeypatch: pytest.MonkeyPatch, race: str,
) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.protect_workspace("operator")
    prior = epoch_id(coordinator)
    peer = Coordinator(git_repo.root)
    peer.register_session("peer", pid=os.getpid())
    original = coordinator.wal.try_append
    observed = []

    def interleave(documents: Sequence[Mapping[str, Any]], expected: str | None,
                   ) -> tuple[EventRecord, ...] | None:
        if not observed:
            if race == "claim":
                peer.claim("peer", [str(git_repo.root / "b.txt")])
            else:
                peer.reconcile_workspace("peer", expected_epoch=prior, reason="Peer review")
            observed.append(peer.wal.head())
        return original(documents, expected)

    monkeypatch.setattr(coordinator.wal, "try_append", interleave)
    with pytest.raises(LandingRefusedError) as caught:
        coordinator.reconcile_workspace("operator", expected_epoch=prior, reason="Owner review")
    assert caught.value.details["reason"] == (
        "workspace_busy" if race == "claim" else "workspace_epoch_changed"
    )
    assert coordinator.wal.head() == observed[0]
    assert all(event.actor != "operator" or event.kind != "workspace_reconcile"
               for event in coordinator.wal.read().events)


@pytest.mark.parametrize("shape", ["missing_landing", "unrecorded_landing"])
def test_reconciliation_does_not_clear_other_branch_divergence(
    git_repo: GitFixture, shape: str,
) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.protect_workspace("operator")
    prior = epoch_id(coordinator)
    session = coordinator.register_session("alpha", pid=os.getpid())
    coordinator.claim("alpha", [str(git_repo.root / "a.txt")])
    git_repo.write("a.txt", "alpha\n")
    git_repo.stage(session.index_path, "a.txt")
    coordinator.land("alpha")
    coordinator.release("alpha")
    if shape == "missing_landing":
        git_repo.git("update-ref", "refs/heads/main", git_repo.base, git_repo.oid())
        expected_error = WalDivergedError
    else:
        git_repo.external_commit("b.txt", "unrecorded\n",
                                 "External\n\nOnetree-Operation: " + "a" * 32)
        expected_error = UnrecordedLandingError
    before = coordinator.wal.head()
    tip = git_repo.oid()
    with pytest.raises(expected_error):
        coordinator.reconcile_workspace("operator", expected_epoch=prior, reason="Cannot fix this")
    assert coordinator.wal.head() == before
    assert git_repo.oid() == tip


def test_new_epoch_does_not_allow_later_unexplained_changes(git_repo: GitFixture) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.protect_workspace("operator")
    git_repo.git("reflog", "expire", "--expire=now", "--all")
    coordinator.reconcile_workspace("operator", expected_epoch=epoch_id(coordinator),
                                    reason="Accepted expired reflogs")
    session = coordinator.register_session("alpha", pid=os.getpid())
    coordinator.claim("alpha", [str(git_repo.root / "a.txt")])
    git_repo.write("a.txt", "alpha\n")
    git_repo.stage(session.index_path, "a.txt")
    git_repo.external_commit("b.txt", "external\n", "Outside the new epoch")
    tip = git_repo.oid()
    with pytest.raises(LandingRefusedError) as caught:
        coordinator.land("alpha")
    assert caught.value.details["reason"] == "workspace_changed"
    assert git_repo.oid() == tip


@pytest.mark.parametrize("shape", ["missing", "symlink", "disabled", "detached"])
def test_reconciliation_refuses_unavailable_observation(
    git_repo: GitFixture, shape: str,
) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.protect_workspace("operator")
    prior = epoch_id(coordinator)
    log = git_repo.root / ".git/logs/HEAD"
    if shape in {"missing", "symlink"}:
        saved = git_repo.root / "saved-log"
        saved.write_bytes(log.read_bytes())
        log.unlink()
        if shape == "symlink":
            log.symlink_to(saved)
    elif shape == "disabled":
        git_repo.git("config", "core.logAllRefUpdates", "false")
    else:
        git_repo.git("checkout", "--detach", "--quiet", "HEAD")
    before = coordinator.wal.head()
    tip = git_repo.oid()
    with pytest.raises(LandingRefusedError) as caught:
        coordinator.reconcile_workspace("operator", expected_epoch=prior, reason="Not readable")
    assert caught.value.details["reason"] == "workspace_unavailable"
    assert coordinator.wal.head() == before
    assert git_repo.oid() == tip


def test_reconciliation_refuses_movement_during_observation(
    git_repo: GitFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.protect_workspace("operator")
    prior = epoch_id(coordinator)
    original = workspace.baseline
    moved = []

    def observe(*args: Any, **kwargs: Any) -> dict[str, Any]:
        baseline = original(*args, **kwargs)
        if not moved:
            git_repo.external_commit("b.txt", "interleaved\n", "During observation")
            moved.append(git_repo.oid())
        return baseline

    monkeypatch.setattr(workspace, "baseline", observe)
    before = coordinator.wal.head()
    with pytest.raises(LandingRefusedError) as caught:
        coordinator.reconcile_workspace("operator", expected_epoch=prior, reason="Inspected before")
    assert caught.value.details["reason"] == "workspace_unavailable"
    assert coordinator.wal.head() == before
    assert git_repo.oid() == moved[0]


def test_replay_refuses_reconciliation_while_claims_remain(git_repo: GitFixture) -> None:
    coordinator = Coordinator(git_repo.root)
    baseline = dict(coordinator.protect_workspace("operator"))
    prior = epoch_id(coordinator)
    coordinator.register_session("alpha", pid=os.getpid())
    coordinator.claim("alpha", [str(git_repo.root / "a.txt")])
    data = {"previous_epoch": prior, "reason": "Forged idle state",
            "violation": None, "baseline": baseline}
    event = EventRecord("bad", (), Wal.document("workspace_reconcile", "operator", data))
    with pytest.raises(WalCorruptError, match="invalid workspace reconciliation"):
        coordinator._apply(coordinator._fold(coordinator.wal.read()), event)
