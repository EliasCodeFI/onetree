from __future__ import annotations

import copy
import os
from collections.abc import Mapping
from typing import Any

import pytest
from conftest import GitFixture
from test_cli import _json_call

from onetree.core import Coordinator
from onetree.errors import LandingRefusedError, WalCorruptError, WalDivergedError
from onetree.wal import EventRecord


def protected(git_repo: GitFixture) -> Coordinator:
    coordinator = Coordinator(git_repo.root)
    coordinator.protect_workspace("operator")
    session = coordinator.register_session("alpha", pid=os.getpid())
    coordinator.claim("alpha", [str(git_repo.root / "a.txt")])
    git_repo.write("a.txt", "staged alpha\n")
    git_repo.stage(session.index_path, "a.txt")
    return coordinator


@pytest.mark.parametrize("operation", [
    "bare", "switch", "reset", "log_disabled", "rewrite", "referee_changed",
])
def test_unexplained_git_operation_quarantines_later_landing(
    git_repo: GitFixture, operation: str,
) -> None:
    coordinator = protected(git_repo)
    if operation == "bare":
        git_repo.external_commit("b.txt", "bare\n", "outside broker")
    elif operation == "switch":
        git_repo.git("switch", "-c", "other")
    elif operation == "reset":
        git_repo.write("b.txt", "peer dirty\n")
        git_repo.git("reset", "--hard", "HEAD")
        assert (git_repo.root / "b.txt").read_text() == "b0\n"
    elif operation == "log_disabled":
        git_repo.git("config", "core.logAllRefUpdates", "false")
    elif operation == "referee_changed":
        git_repo.git("config", "onetree.referee", "false")
    else:
        log = git_repo.root / ".git/logs/HEAD"
        log.write_bytes(b"")
    before = git_repo.oid()
    with pytest.raises(LandingRefusedError) as caught:
        coordinator.land("alpha", target="refs/heads/main")
    assert caught.value.details["reason"] == "workspace_changed"
    assert caught.value.details["paths"] == ["a.txt"]
    assert git_repo.oid() == before
    report = coordinator.inspect_wal()
    assert not report.consistent
    assert any(item.kind == "workspace_changed" for item in report.divergences)
    assert report.events[-1].data["reason"] == "workspace_changed"


def test_broker_transitions_remain_allowed(git_repo: GitFixture) -> None:
    coordinator = protected(git_repo)
    first = coordinator.land("alpha")
    assert first.commit == git_repo.oid()
    assert coordinator.inspect_wal().consistent
    second = coordinator.register_session("beta", pid=os.getpid())
    coordinator.claim("beta", [str(git_repo.root / "b.txt")])
    git_repo.write("b.txt", "beta\n")
    git_repo.stage(second.index_path, "b.txt")
    coordinator.land("beta")
    assert coordinator.inspect_wal().consistent


def test_new_registration_cannot_erase_workspace_violation(git_repo: GitFixture) -> None:
    coordinator = protected(git_repo)
    git_repo.git("reset", "--hard", "HEAD")
    coordinator.register_session("beta", pid=os.getpid())
    assert not coordinator.inspect_wal().consistent
    with pytest.raises(LandingRefusedError):
        coordinator.protect_workspace("operator")


def test_recovery_does_not_report_all_clear_for_workspace_violation(git_repo: GitFixture) -> None:
    coordinator = protected(git_repo)
    git_repo.git("reset", "--hard", "HEAD")
    with pytest.raises(WalDivergedError) as caught:
        coordinator.recover()
    assert any(item["kind"] == "workspace_changed" for item in caught.value.details["divergences"])


@pytest.mark.parametrize("target", [None, [], 1])
def test_workspace_malformed_target_is_typed_corruption(git_repo: GitFixture, target: Any) -> None:
    coordinator = Coordinator(git_repo.root)
    epoch = dict(coordinator.protect_workspace("operator"))
    epoch["target"] = target
    event = EventRecord("invalid", (), coordinator.wal.document("workspace_protect", "bad", epoch))
    with pytest.raises(WalCorruptError):
        coordinator._apply(coordinator._fold(type(coordinator.wal.read())(None, ())), event)


def test_refused_intent_is_not_authority_for_external_move(git_repo: GitFixture) -> None:
    coordinator = protected(git_repo)
    session = coordinator.register_session("beta", pid=os.getpid())
    coordinator.claim("beta", [str(git_repo.root / "b.txt")])
    git_repo.write("b.txt", "beta\n")
    git_repo.stage(session.index_path, "b.txt")
    captured: dict[str, Any] = {}

    def checkpoint(point: str, context: Mapping[str, Any]) -> None:
        if point == "after_intent":
            captured.update(context)
            raise RuntimeError("stop after intent")

    with pytest.raises(RuntimeError, match="stop after intent"):
        coordinator.land("alpha", checkpoint=checkpoint)
    coordinator._close_intent("alpha", captured["operation"], captured["intent"], "test_refusal")
    git_repo.git("update-ref", "refs/heads/main", captured["candidate"], git_repo.base)
    with pytest.raises(LandingRefusedError) as caught:
        coordinator.land("beta")
    assert caught.value.details["reason"] == "workspace_changed"


@pytest.mark.parametrize("case", ["timeout", "command", "logs", "size", "digest"])
def test_malformed_workspace_baseline_is_rejected(git_repo: GitFixture, case: str) -> None:
    coordinator = Coordinator(git_repo.root)
    epoch = copy.deepcopy(dict(coordinator.protect_workspace("operator")))
    if case == "timeout":
        epoch["referee"][1] = float("nan")
    elif case == "command":
        epoch["referee"][0] = ""
    elif case == "logs":
        epoch["logs"] = {}
    elif case == "size":
        epoch["logs"]["HEAD"]["size"] = True
    else:
        epoch["logs"]["HEAD"]["sha256"] = "wrong"
    document = {"event": "workspace_protect", "data": epoch, "actor": "bad", "event_id": "new"}
    event = EventRecord("invalid", (), document)
    with pytest.raises(WalCorruptError):
        coordinator._apply(coordinator._fold(type(coordinator.wal.read())(None, ())), event)


def test_protect_and_quarantined_recovery_cli_are_single_json_objects(
    git_repo: GitFixture, capsys: pytest.CaptureFixture[str],
) -> None:
    code, payload = _json_call(capsys, [
        "protect", "--repo", str(git_repo.root), "--session", "operator",
    ])
    assert code == 0
    assert payload["workspace"]["target"] == "refs/heads/main"
    git_repo.git("reset", "--hard", "HEAD")
    code, payload = _json_call(capsys, ["wal", "--repo", str(git_repo.root), "--recover"])
    assert code == 2
    assert payload["ok"] is False
    assert payload["error"]["type"] == "wal_diverged"
    assert payload["error"]["divergences"][0]["kind"] == "workspace_changed"


def test_protect_requires_observable_history_and_no_current_claims(git_repo: GitFixture) -> None:
    coordinator = Coordinator(git_repo.root)
    git_repo.git("config", "core.logAllRefUpdates", "false")
    with pytest.raises(LandingRefusedError) as unavailable:
        coordinator.protect_workspace("operator")
    assert unavailable.value.details["reason"] == "workspace_unavailable"
    git_repo.git("config", "core.logAllRefUpdates", "true")
    coordinator.register_session("alpha", pid=os.getpid())
    coordinator.claim("alpha", [str(git_repo.root / "a.txt")])
    with pytest.raises(LandingRefusedError) as busy:
        coordinator.protect_workspace("operator")
    assert busy.value.details["reason"] == "workspace_busy"


def test_protect_is_idempotent_without_advancing_baseline(git_repo: GitFixture) -> None:
    coordinator = Coordinator(git_repo.root)
    epoch = coordinator.protect_workspace("operator")
    wal_before = coordinator.wal.head()
    assert coordinator.protect_workspace("operator") == epoch
    assert coordinator.wal.head() == wal_before
