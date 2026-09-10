from __future__ import annotations

import copy
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
from conftest import _GIT_EXECUTABLE, GitFixture
from test_cli import _json_call
from test_referee import inherited_config_values, operator_config, referee_command

from onetree import referee, workspace
from onetree.cli import main
from onetree.core import Coordinator
from onetree.errors import (
    GitError,
    LandingRefusedError,
    PayloadGateError,
    WalCorruptError,
    WalDivergedError,
)
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

    git_repo.git("branch", "other")

    def retarget(data: dict[str, Any]) -> None:
        data["target"] = "refs/heads/other"

    cases = [
        (receipt(result="red", returncode=1), "failed referee"),
        (receipt(candidate="0" * 40, tested=["0" * 40]), "candidate differs"),
        (receipt(baseline="1" * 40), "baseline differs"),
        (receipt(command=None, result="not_configured", tested=[], returncode=None),
         "another referee"),
        # A valid passing receipt from a referee the epoch does not name, differing
        # in the command alone or in the timeout alone.
        (receipt(command="/bin/true"), "another referee"),
        (receipt(timeout_seconds=5.0), "another referee"),
        (drop_receipt, "no referee evidence"),
        # The broker refuses every landing outside the protected branch, so an
        # intent elsewhere under the epoch did not come from it.
        (retarget, "outside the shared branch"),
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
    # Recovery cannot audit the workspace either; it says so by type and records
    # no `branch_diverged` row that would outlive the corrected setting.
    events_before = len(coordinator.wal.read().events)
    with pytest.raises(LandingRefusedError) as recover:
        coordinator.recover()
    assert recover.value.details["reason"] == "referee_config"
    assert len(coordinator.wal.read().events) == events_before
    assert not any(
        event.kind == "refused" and event.data.get("action") == "recover"
        for event in coordinator.wal.read().events
    )

    # Fixing the setting is the whole remedy: no baseline reconciliation follows.
    git_repo.git("config", "--unset", "onetree.refereeTimeout")
    assert coordinator.recover().consistent
    landed = coordinator.land("alpha")
    assert git_repo.oid() == landed.commit
    assert coordinator.inspect_wal().consistent


def break_referee_setting(git_repo: GitFixture, shape: str) -> tuple[str, str]:
    """Make the referee setting unreadable in one of the shapes `referee.configuration`
    refuses; returns the refusal text expected and the key to unset afterwards."""
    if shape == "timeout":
        git_repo.git("config", "onetree.refereeTimeout", "3O0")
        return "invalid referee timeout", "onetree.refereeTimeout"
    if shape == "duplicate":
        git_repo.git("config", "onetree.referee", "/bin/true")
        git_repo.git("config", "--add", "onetree.referee", "/bin/true")
        return "one nonempty value", "onetree.referee"
    if shape == "empty":
        git_repo.git("config", "onetree.referee", "")
        return "one nonempty value", "onetree.referee"
    git_repo.git("config", "onetree.referee", os.fsdecode(b"/bin/\xff"))
    return "not UTF-8", "onetree.referee"


@pytest.mark.parametrize("shape", ["timeout", "duplicate", "empty", "not_utf8"])
def test_recover_keeps_a_real_divergence_beside_an_unreadable_setting(
    git_repo: GitFixture, shape: str,
) -> None:
    coordinator = protected(git_repo)
    landed = coordinator.land("alpha")
    git_repo.git("update-ref", "refs/heads/main", git_repo.base, landed.commit)
    detail, key = break_referee_setting(git_repo, shape)
    events_before = len(coordinator.wal.read().events)
    # Control: the setting alone is the typed refusal (the test above); here the
    # branch has also moved away from what the broker landed.
    with pytest.raises(WalDivergedError) as caught:
        coordinator.recover()
    reported = {item["kind"]: item["detail"] for item in caught.value.details["divergences"]}
    assert set(reported) == {"landed_not_on_target", "referee_config"}
    assert detail in reported["referee_config"]
    # The row recovery records carries the same two items, the setting's detail included
    # (Round BF3 DUO: the error was asserted, the row was not).
    events = coordinator.wal.read().events
    assert len(events) == events_before + 1
    row = events[-1]
    assert (row.kind, row.data["action"], row.data["reason"]) == (
        "refused", "recover", "branch_diverged",
    )
    assert {item["kind"]: item["detail"] for item in row.data["divergences"]} == reported
    # Correcting the setting does not make the divergence go away: the audit now reads
    # the workspace and finds the same external move, a new divergence set recorded once.
    git_repo.git("config", "--unset-all", key)
    with pytest.raises(WalDivergedError) as still:
        coordinator.recover()
    kinds = {item["kind"] for item in still.value.details["divergences"]}
    assert kinds == {"landed_not_on_target", "workspace_changed"}
    events = coordinator.wal.read().events
    assert len(events) == events_before + 2
    assert {item["kind"] for item in events[-1].data["divergences"]} == kinds
    with pytest.raises(WalDivergedError):
        coordinator.recover()
    assert len(coordinator.wal.read().events) == events_before + 2


def test_recover_cli_human_mode_names_every_divergence(
    git_repo: GitFixture, capsys: pytest.CaptureFixture[str],
) -> None:
    """The JSON payload carried both items; the human register said only that the branch
    and the WAL disagree (Round BF3 DUO)."""
    coordinator = protected(git_repo)
    landed = coordinator.land("alpha")
    git_repo.git("update-ref", "refs/heads/main", git_repo.base, landed.commit)
    break_referee_setting(git_repo, "timeout")
    assert main(["wal", "--repo", str(git_repo.root), "--recover"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    summary, *items = captured.err.splitlines()
    assert summary.startswith("onetree: the branch and the WAL disagree")
    assert any(line.startswith("  landed_not_on_target at refs/heads/main: ") for line in items)
    assert any(
        line.startswith("  referee_config at HEAD: ") and "invalid referee timeout" in line
        for line in items
    )


def test_include_path_resolved_per_operator_is_loud_and_lands_nowhere_under_the_gate(
    git_repo: GitFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`.git/config` may include a path git expands per operator (`~`): the setting is the
    repository's, its content is whatever each peer's HOME holds. Measured 2026-09-10
    (Round BF3 DUO, task #164): a peer whose HOME reads another referee is refused by the
    epoch comparison, and the peer whose HOME matches is refused by the payload gate,
    whose child has no HOME by design, so its git cannot expand the include (git's
    diagnostic reaches the refusal only as far as the gate forwards its stderr). Nobody
    lands while the include is per-operator, and nothing is silent. This fixture asserts
    that shape; a mechanism that admits HOME for path expansion, or refuses such
    includes by name, flips it."""
    homes: dict[str, Path] = {}
    for peer, command in (("alpha", "/bin/true"), ("beta", "/bin/false")):
        homes[peer] = tmp_path / peer
        homes[peer].mkdir()
        git_repo.git("config", "--file", str(homes[peer] / "referee.gitconfig"),
                     "onetree.referee", command)
    git_repo.git("config", "include.path", "~/referee.gitconfig")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(homes["alpha"]))
    coordinator = protected(git_repo)
    # RED control: one repository, one `local`-scope setting, two contents.
    assert referee.configuration(coordinator.repo) == ("/bin/true", referee.DEFAULT_TIMEOUT)
    monkeypatch.setenv("HOME", str(homes["beta"]))
    assert referee.configuration(coordinator.repo) == ("/bin/false", referee.DEFAULT_TIMEOUT)
    with pytest.raises(LandingRefusedError) as other_home:
        coordinator.land("alpha")
    assert other_home.value.details["reason"] == "workspace_changed"
    assert "referee configuration changed" in str(other_home.value)
    monkeypatch.setenv("HOME", str(homes["alpha"]))
    with pytest.raises(PayloadGateError) as same_home:
        coordinator.land("alpha")
    assert "wrote to stderr" in str(same_home.value)
    assert same_home.value.details["stderr"]
    assert git_repo.oid() == git_repo.base
    # Control: a path git expands nowhere reads the same for every peer and lands.
    git_repo.git("config", "include.path", str(homes["alpha"] / "referee.gitconfig"))
    landed = coordinator.land("alpha")
    assert landed.referee_receipt["command"] == "/bin/true"
    assert git_repo.oid() == landed.commit


def test_recover_cli_names_the_setting_not_a_divergence(
    git_repo: GitFixture, capsys: pytest.CaptureFixture[str],
) -> None:
    protected(git_repo)
    git_repo.git("config", "onetree.refereeTimeout", "3O0")
    code, payload = _json_call(capsys, ["wal", "--repo", str(git_repo.root), "--recover"])
    assert code == 1
    assert payload["ok"] is False
    assert payload["error"]["reason"] == "referee_config"
    assert "recovery" not in payload
    assert main(["wal", "--repo", str(git_repo.root), "--recover"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "referee" in captured.err
    assert "disagree" not in captured.err


@pytest.mark.parametrize("channel", ["GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT"])
def test_inherited_git_config_environment_cannot_disable_the_reflog_switch(
    git_repo: GitFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, channel: str,
) -> None:
    """The guard reads `core.logAllRefUpdates` at effective scope, because that is what
    git consults when it writes the reflog; the environment channels that override or
    redirect that read are dropped from every broker git invocation instead."""
    coordinator = protected(git_repo)
    if channel == "GIT_CONFIG":
        operator_config(tmp_path, monkeypatch, channel, "")
    elif channel == "GIT_CONFIG_PARAMETERS":
        monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.logAllRefUpdates=false'")
    else:
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.logAllRefUpdates")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    # RED control: git under the inherited environment no longer reports the switch
    # as enabled, so a broker reading through it would see protection disabled.
    assert inherited_config_values(git_repo, "core.logAllRefUpdates") != ["true"]

    landed = coordinator.land("alpha")
    assert git_repo.oid() == landed.commit
    assert coordinator.inspect_wal().consistent


def test_operator_global_config_does_not_enter_the_protected_epoch(
    git_repo: GitFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two operators with different global git configs protect and land the same epoch."""
    git_repo.git("config", "onetree.referee", referee_command("pass"))
    operator_config(tmp_path, monkeypatch, "HOME", "[onetree]\n\trefereeTimeout = 60\n")
    # RED control: the first operator's effective-scope git sees the 60 s timeout.
    assert inherited_config_values(git_repo, "onetree.refereeTimeout") == ["60"]
    coordinator = protected(git_repo)
    epoch = coordinator.protect_workspace("operator")
    assert epoch["referee"][1] == referee.DEFAULT_TIMEOUT

    # The second operator: no global timeout at all.
    monkeypatch.setenv("HOME", str(tmp_path / "other-home"))
    (tmp_path / "other-home").mkdir()
    assert inherited_config_values(git_repo, "onetree.refereeTimeout") == []
    landed = Coordinator(git_repo.root).land("alpha")
    assert landed.referee_receipt["timeout_seconds"] == referee.DEFAULT_TIMEOUT
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


@pytest.mark.parametrize("operation", ["checkout_path", "restore", "read_tree", "checkout_index"])
def test_path_form_checkouts_erase_peer_work_without_a_reflog_transition(
    git_repo: GitFixture, operation: str,
) -> None:
    """The measured boundary of the reflog guard: this fixture asserts the gap's shape.

    None of these operations moves a ref, so the guard has no evidence and the landing
    proceeds over the erased peer work. A later mechanism that detects them flips this
    test; it must not be read as the desired outcome.
    """
    coordinator = protected(git_repo)
    git_repo.write("b.txt", "peer dirty\n")
    if operation == "checkout_path":
        git_repo.git("checkout", "--quiet", "--", "b.txt")
    elif operation == "restore":
        git_repo.git("restore", "b.txt")
    elif operation == "read_tree":
        git_repo.git("read-tree", "-u", "--reset", "HEAD")
    else:
        git_repo.git("checkout-index", "-f", "-a")
    assert (git_repo.root / "b.txt").read_text() == "b0\n"
    # The control is the `reset` case of the quarantine test above: the same
    # erasure through a ref transition refuses as `workspace_changed`.
    landed = coordinator.land("alpha")
    assert git_repo.oid() == landed.commit
    assert coordinator.inspect_wal().consistent


@pytest.mark.parametrize("shape", ["routine", "emptied"])
def test_reflog_expiry_quarantines_an_unmoved_workspace(git_repo: GitFixture, shape: str) -> None:
    """The two shapes task #151 records, each asserting the branch of the guard it reaches.

    `routine`: an entry older than `gc.reflogExpire` (90 days) sits inside the recorded
    prefix and default expiry removes it, so the log is shorter than the recorded prefix
    (the guard's length clause; the digest clause is the equal-length rewrite test's).
    `emptied`: `--expire=now` leaves no entries at all. A #151 reconciliation flips the
    shape it addresses; neither is the desired outcome for an unmoved workspace.
    """
    if shape == "routine":
        aged = os.environ.copy()
        aged.pop("GIT_INDEX_FILE", None)
        aged["GIT_COMMITTER_DATE"] = "2026-01-01T00:00:00Z"
        git_repo.write("b.txt", "aged\n")
        for arguments in (["add", "--", "b.txt"], ["commit", "--quiet", "-m", "aged"]):
            subprocess.run([_GIT_EXECUTABLE, "-C", str(git_repo.root), *arguments],
                           check=True, capture_output=True, env=aged)
    tip = git_repo.oid()
    coordinator = protected(git_repo)
    log_before = (git_repo.root / ".git/logs/HEAD").read_bytes()
    if shape == "routine":
        git_repo.git("reflog", "expire", "--all")
        expected = "continuity was lost"
    else:
        git_repo.git("reflog", "expire", "--expire=now", "--all")
        expected = "absent or incomplete"
    log_after = (git_repo.root / ".git/logs/HEAD").read_bytes()
    # Control: expiry rewrote the log (routine: shorter but not empty; emptied: empty).
    assert log_after != log_before
    assert (log_after == b"") == (shape == "emptied")
    with pytest.raises(LandingRefusedError) as caught:
        coordinator.land("alpha")
    assert caught.value.details["reason"] == "workspace_changed"
    assert expected in str(caught.value)
    assert git_repo.oid() == tip


def test_reflog_switch_read_failure_is_an_observation_failure(
    git_repo: GitFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = protected(git_repo)
    state = coordinator._fold(coordinator.wal.read())

    def failing(repo: Any) -> bool:
        raise GitError("git config could not be executed")

    monkeypatch.setattr(workspace, "_enabled", failing)
    violation = coordinator._workspace_violation(state)
    assert violation is not None
    assert violation.startswith("workspace observation failed")
    monkeypatch.undo()
    assert coordinator._workspace_violation(state) is None
