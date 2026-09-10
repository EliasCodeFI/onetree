from __future__ import annotations

import copy
import os
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
from conftest import GitFixture
from test_cli import _json_call
from test_referee import referee_command

from onetree import workspace
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
    "bare", "switch", "checkout", "reset", "stash", "log_disabled", "rewrite", "referee_changed",
])
def test_unexplained_git_operation_quarantines_later_landing(
    git_repo: GitFixture, operation: str,
) -> None:
    coordinator = protected(git_repo)
    if operation == "bare":
        git_repo.external_commit("b.txt", "bare\n", "outside broker")
    elif operation == "switch":
        git_repo.git("switch", "-c", "other")
    elif operation == "checkout":
        git_repo.git("branch", "other")
        git_repo.git("checkout", "--quiet", "other")
    elif operation == "stash":
        git_repo.write("b.txt", "peer dirty\n")
        git_repo.git("stash", "--quiet")
        assert (git_repo.root / "b.txt").read_text() == "b0\n"
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


def test_reflog_prefix_rewrite_of_equal_length_loses_continuity(git_repo: GitFixture) -> None:
    coordinator = protected(git_repo)
    epoch = coordinator.protect_workspace("operator")
    log = git_repo.root / ".git/logs/HEAD"
    content = bytearray(log.read_bytes())
    marker = content.rfind(b"baseline")
    assert marker >= 0
    content[marker:marker + 8] = b"BASELINE"
    log.write_bytes(bytes(content))
    # RED control: the rewritten log keeps the recorded size and carries no line
    # past it, so an evaluator that only parses appended transitions sees nothing.
    assert log.stat().st_size == epoch["logs"]["HEAD"]["size"]
    assert log.read_bytes()[epoch["logs"]["HEAD"]["size"]:] == b""
    before = git_repo.oid()
    with pytest.raises(LandingRefusedError) as caught:
        coordinator.land("alpha")
    assert caught.value.details["reason"] == "workspace_changed"
    assert "continuity was lost" in str(caught.value)
    assert git_repo.oid() == before


def test_malformed_reflog_transition_is_refused(git_repo: GitFixture) -> None:
    coordinator = protected(git_repo)
    log = git_repo.root / ".git/logs/HEAD"
    with log.open("ab") as stream:
        stream.write(f"{git_repo.base} {git_repo.base}\n".encode())
    # RED control: the appended line names no movement, so a transition walk that
    # tolerated short lines would read old == new and report nothing.
    assert log.read_bytes().splitlines()[-1].split(b" ", 2) == [
        git_repo.base.encode(), git_repo.base.encode(),
    ]
    with pytest.raises(LandingRefusedError) as caught:
        coordinator.land("alpha")
    assert caught.value.details["reason"] == "workspace_changed"
    assert "malformed transition" in str(caught.value)


def test_dead_lander_intent_ahead_in_the_train_is_skipped_not_matched(
    git_repo: GitFixture,
) -> None:
    coordinator = protected(git_repo)

    def crash(point: str, _context: Mapping[str, Any]) -> None:
        if point == "after_intent":
            raise RuntimeError("lander died after intent")

    with pytest.raises(RuntimeError, match="lander died"):
        coordinator.land("alpha", checkpoint=crash)
    pending = [event for event in coordinator.inspect_wal().events if event.kind == "land_intent"]
    assert len(pending) == 1
    assert git_repo.oid() == git_repo.base

    beta = coordinator.register_session("beta", pid=os.getpid())
    coordinator.claim("beta", [str(git_repo.root / "b.txt")])
    git_repo.write("b.txt", "beta\n")
    git_repo.stage(beta.index_path, "b.txt")
    landed = coordinator.land("beta")
    assert git_repo.oid() == landed.commit

    # The reflog now holds one transition (base -> beta) while the recorded
    # intents hold two (base -> alpha's candidate, base -> beta). A walk that
    # demanded the pairs in order would call beta's landing unexplained.
    gamma = coordinator.register_session("gamma", pid=os.getpid())
    coordinator.claim("gamma", [str(git_repo.root / "nested/c.txt")])
    git_repo.write("nested/c.txt", "gamma\n")
    git_repo.stage(gamma.index_path, "nested/c.txt")
    assert coordinator.land("gamma").commit == git_repo.oid()


def test_external_move_hidden_from_the_reflog_is_detected(
    git_repo: GitFixture, tmp_path: Path,
) -> None:
    coordinator = protected(git_repo)
    logs = git_repo.root / ".git/logs"
    hidden = tmp_path / "hidden-logs"
    logs.rename(hidden)
    moved = git_repo.external_commit("b.txt", "bare\n", "outside broker, unlogged")
    if logs.exists():
        shutil.rmtree(logs)
    hidden.rename(logs)
    # RED control: the restored logs record no transition at all, so an evaluator
    # that only walks appended reflog lines finds nothing to explain.
    epoch = coordinator.inspect_wal().events[0]
    assert epoch.kind == "workspace_protect"
    assert (logs / "HEAD").stat().st_size == epoch.data["logs"]["HEAD"]["size"]
    assert git_repo.oid() == moved != git_repo.base
    with pytest.raises(LandingRefusedError) as caught:
        coordinator.land("alpha")
    assert caught.value.details["reason"] == "workspace_changed"
    assert "moved without continuous reflog evidence" in str(caught.value)
    assert git_repo.oid() == moved


def test_reset_inside_the_fence_window_is_refused_at_the_fence(git_repo: GitFixture) -> None:
    coordinator = protected(git_repo)
    git_repo.write("b.txt", "peer dirty\n")
    reset_done = False

    def checkpoint(point: str, _context: Mapping[str, Any]) -> None:
        nonlocal reset_done
        if point == "before_intent" and not reset_done:
            reset_done = True
            git_repo.git("reset", "--hard", "HEAD")

    with pytest.raises(LandingRefusedError) as caught:
        coordinator.land("alpha", checkpoint=checkpoint)
    assert reset_done
    assert caught.value.details["reason"] == "workspace_changed"
    assert caught.value.details["paths"] == ["a.txt"]
    assert git_repo.oid() == git_repo.base
    # The reset itself is what the guard cannot prevent: the peer's work is gone.
    assert (git_repo.root / "b.txt").read_text() == "b0\n"
    assert not coordinator.inspect_wal().consistent


def test_landing_outside_the_protected_branch_is_refused(git_repo: GitFixture) -> None:
    git_repo.git("branch", "other")
    coordinator = Coordinator(git_repo.root)
    coordinator.protect_workspace("operator")
    session = coordinator.register_session("alpha", pid=os.getpid(), target="refs/heads/other")
    coordinator.claim("alpha", [str(git_repo.root / "a.txt")])
    git_repo.write("a.txt", "alpha on other\n")
    git_repo.stage(session.index_path, "a.txt")
    with pytest.raises(LandingRefusedError) as caught:
        coordinator.land("alpha", target="refs/heads/other")
    assert caught.value.details["reason"] == "workspace_changed"
    assert "outside the protected shared branch" in str(caught.value)
    assert git_repo.oid("refs/heads/other") == git_repo.base
    assert git_repo.oid() == git_repo.base


def test_second_workspace_protect_cannot_replace_a_violated_baseline(
    git_repo: GitFixture,
) -> None:
    coordinator = protected(git_repo)
    git_repo.external_commit("b.txt", "bare\n", "outside broker")
    state = coordinator._fold(coordinator.wal.read())
    assert coordinator._workspace_violation(state) is not None
    replacement = workspace.baseline(coordinator.repo)
    event = EventRecord(
        "forged", (), coordinator.wal.document("workspace_protect", "operator", replacement)
    )
    # RED control: the replacement baseline is valid on its own, so a reader that
    # only validated the document would install it and the violation would vanish.
    workspace.validate(replacement, coordinator.repo)
    with pytest.raises(WalCorruptError, match="cannot be silently replaced"):
        coordinator._apply(state, event)
    assert coordinator._workspace_violation(state) is not None


def test_forged_intent_evidence_is_refused_on_replay(git_repo: GitFixture) -> None:
    git_repo.git("config", "onetree.referee", referee_command("pass"))
    coordinator = protected(git_repo)
    coordinator.land("alpha")
    intent = next(event for event in coordinator.wal.read().events if event.kind == "land_intent")
    state = coordinator._fold(coordinator.wal.read())

    def forged(mutate: Callable[[dict[str, Any]], None]) -> EventRecord:
        document = copy.deepcopy(intent.document)
        document["data"]["operation"] = "f" * 32
        document["event_id"] = "forged-intent"
        mutate(document["data"])
        return EventRecord("forged", (), document)

    def receipt(**fields: Any) -> Callable[[dict[str, Any]], None]:
        def mutate(data: dict[str, Any]) -> None:
            data["referee_receipt"].update(fields)
        return mutate

    def drop_receipt(data: dict[str, Any]) -> None:
        del data["referee_receipt"]

    cases = [
        (receipt(result="red", returncode=1), "failed referee"),
        (receipt(candidate="0" * 40, tested=["0" * 40]), "candidate differs"),
        (receipt(baseline="1" * 40), "baseline differs"),
        (receipt(command=None, result="not_configured", tested=[], returncode=None),
         "another referee"),
        (drop_receipt, "no referee evidence"),
    ]
    for mutate, expected in cases:
        with pytest.raises(WalCorruptError, match=expected):
            coordinator._apply(copy.deepcopy(state), forged(mutate))
    # Control: the unforged document replays.
    coordinator._apply(copy.deepcopy(state), forged(lambda data: None))


def test_invalid_referee_setting_under_protection_is_not_a_quarantine(
    git_repo: GitFixture,
) -> None:
    coordinator = protected(git_repo)
    git_repo.git("config", "onetree.refereeTimeout", "nan")
    with pytest.raises(LandingRefusedError) as caught:
        coordinator.land("alpha")
    assert caught.value.details["reason"] == "referee_config"
    assert caught.value.details["paths"] == ["a.txt"]
    report = coordinator.inspect_wal()
    assert any(item.kind == "referee_config" for item in report.divergences)
    assert not any(item.kind == "workspace_changed" for item in report.divergences)
    with pytest.raises(LandingRefusedError) as protect:
        coordinator.protect_workspace("operator")
    assert protect.value.details["reason"] == "referee_config"

    # Fixing the setting is the whole remedy: no baseline reconciliation follows.
    git_repo.git("config", "--unset", "onetree.refereeTimeout")
    landed = coordinator.land("alpha")
    assert git_repo.oid() == landed.commit
    assert coordinator.inspect_wal().consistent


def test_setting_broken_inside_the_fence_window_is_a_configuration_change(
    git_repo: GitFixture,
) -> None:
    coordinator = protected(git_repo)

    def checkpoint(point: str, _context: Mapping[str, Any]) -> None:
        if point == "before_intent":
            git_repo.git("config", "onetree.refereeTimeout", "nan")

    with pytest.raises(LandingRefusedError) as caught:
        coordinator.land("alpha", checkpoint=checkpoint)
    assert caught.value.details["reason"] == "referee_config_changed"
    assert coordinator.inspect_wal().events[-1].data["reason"] == "referee_config_changed"
    assert git_repo.oid() == git_repo.base
