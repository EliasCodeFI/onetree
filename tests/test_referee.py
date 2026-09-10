from __future__ import annotations

import copy
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from conftest import _GIT_EXECUTABLE, GitFixture
from test_cli import _json_call

from onetree import referee
from onetree.core import Coordinator
from onetree.errors import LandingRefusedError, WalCorruptError
from onetree.wal import EventRecord


def referee_command(code: str) -> str:
    return shlex.join([sys.executable, "-I", "-S", "-c", code])


def configure(git_repo: GitFixture, code: str) -> None:
    git_repo.git("config", "onetree.referee", referee_command(code))


def inherited_config_values(git_repo: GitFixture, key: str) -> list[str]:
    """What `git config` answers for `key` under the test process's own environment."""
    completed = subprocess.run(
        [_GIT_EXECUTABLE, "-C", str(git_repo.root), "config", "--null", "--get-all", key],
        check=False, capture_output=True, env=os.environ.copy(),
    )
    return [item.decode() for item in completed.stdout.split(b"\0") if item]


def staged(git_repo: GitFixture, actor: str = "alpha", path: str = "a.txt") -> Coordinator:
    coordinator = Coordinator(git_repo.root)
    session = coordinator.register_session(actor, pid=os.getpid())
    coordinator.claim(actor, [str(git_repo.root / path)])
    git_repo.write(path, actor + "\n")
    git_repo.stage(session.index_path, path)
    return coordinator


def test_red_referee_refuses_and_retains_exact_candidate(git_repo: GitFixture) -> None:
    configure(git_repo, "raise SystemExit(7)")
    coordinator = staged(git_repo)
    with pytest.raises(LandingRefusedError) as caught:
        coordinator.land("alpha")
    assert caught.value.details["reason"] == "referee_red"
    assert caught.value.details["paths"] == ["a.txt"]
    assert git_repo.oid() == git_repo.base
    event = coordinator.inspect_wal().events[-1]
    assert event.kind == "refused"
    receipt = event.data["referee_receipt"]
    assert receipt["baseline"] == git_repo.base
    assert receipt["result"] == "red"
    assert receipt["returncode"] == 7
    assert receipt["localization"] == "unresolved"
    assert receipt["tested"] == [receipt["candidate"]]
    assert git_repo.git("show", receipt["candidate"] + ":a.txt").stdout == "alpha\n"
    assert not any(event.kind == "land_intent" for event in coordinator.inspect_wal().events)


def test_referee_reads_index_candidate_not_shared_work(git_repo: GitFixture) -> None:
    configure(git_repo, "from pathlib import Path; assert Path('a.txt').read_text() == 'alpha\\n'; "
              "assert Path('b.txt').read_text() == 'b0\\n'")
    coordinator = staged(git_repo)
    git_repo.write("a.txt", "not staged\n")
    git_repo.write("b.txt", "peer dirty\n")
    landed = coordinator.land("alpha")
    assert landed.referee_receipt["result"] == "pass"
    assert landed.referee_receipt["candidate"] == landed.commit
    assert (git_repo.root / "a.txt").read_text() == "not staged\n"
    assert (git_repo.root / "b.txt").read_text() == "peer dirty\n"


def test_referee_timeout_never_moves_target(git_repo: GitFixture) -> None:
    configure(git_repo, "import time; time.sleep(10)")
    git_repo.git("config", "onetree.refereeTimeout", "0.05")
    coordinator = staged(git_repo)
    started = time.monotonic()
    with pytest.raises(LandingRefusedError) as caught:
        coordinator.land("alpha")
    elapsed = time.monotonic() - started
    assert caught.value.details["reason"] == "referee_timeout"
    assert git_repo.oid() == git_repo.base
    receipt = coordinator.inspect_wal().events[-1].data["referee_receipt"]
    assert receipt["result"] == "timeout"
    # The referee slept for 10 s. A landing that returns well inside that window
    # killed the process group; one that waited for the sleeper did not.
    assert elapsed < 5
    assert receipt["duration_seconds"] < 5


@pytest.mark.parametrize("channel", ["GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
                                     "GIT_CONFIG_GLOBAL"])
def test_inherited_git_config_environment_cannot_demote_a_red_referee(
    git_repo: GitFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, channel: str,
) -> None:
    configure(git_repo, "raise SystemExit(3)")
    red = git_repo.git("config", "--get", "onetree.referee").stdout.strip()
    coordinator = staged(git_repo)
    if channel == "GIT_CONFIG":
        # The deprecated redirect: `git config` reads this file instead of the repository.
        empty = tmp_path / "empty-config"
        empty.write_text("", encoding="utf-8")
        monkeypatch.setenv("GIT_CONFIG", str(empty))
    elif channel == "GIT_CONFIG_PARAMETERS":
        monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'onetree.referee=/bin/true'")
    elif channel == "GIT_CONFIG_COUNT":
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "onetree.referee")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "/bin/true")
    else:
        alternate = tmp_path / "global-config"
        alternate.write_text("[onetree]\n\treferee = /bin/true\n", encoding="utf-8")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(alternate))

    # RED control: under the environment the lander inherits, git itself no longer
    # answers with the one red command, so an evaluator reading through that
    # environment would run no referee or a different one.
    assert inherited_config_values(git_repo, "onetree.referee") != [red]

    with pytest.raises(LandingRefusedError) as caught:
        coordinator.land("alpha")
    assert caught.value.details["reason"] == "referee_red"
    assert caught.value.details["referee_receipt"]["command"] == red
    assert git_repo.oid() == git_repo.base


def test_duplicate_or_empty_referee_setting_is_a_typed_refusal(git_repo: GitFixture) -> None:
    git_repo.git("config", "onetree.referee", referee_command("raise SystemExit(9)"))
    git_repo.git("config", "--add", "onetree.referee", referee_command("pass"))
    coordinator = staged(git_repo)
    # RED control: an evaluator that took the first value would run the red
    # referee and refuse for its verdict; one that took the last would land.
    assert len(inherited_config_values(git_repo, "onetree.referee")) == 2
    with pytest.raises(LandingRefusedError) as duplicate:
        coordinator.land("alpha")
    assert duplicate.value.details["reason"] == "referee_config"
    assert git_repo.oid() == git_repo.base

    git_repo.git("config", "--unset-all", "onetree.referee")
    git_repo.git("config", "onetree.referee", "")
    with pytest.raises(LandingRefusedError) as empty:
        coordinator.land("alpha")
    assert empty.value.details["reason"] == "referee_config"
    assert git_repo.oid() == git_repo.base

    git_repo.git("config", "--unset-all", "onetree.referee")
    configure(git_repo, "pass")
    assert coordinator.land("alpha").referee_receipt["result"] == "pass"


def test_referee_child_sees_only_the_admitted_environment(
    git_repo: GitFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure(git_repo, "import os; raise SystemExit(3 if 'ONETREE_TEST_CANARY' in os.environ "
              "else 0)")
    monkeypatch.setenv("ONETREE_TEST_CANARY", "leaked")
    command = git_repo.git("config", "--get", "onetree.referee").stdout.strip()
    # RED control: the same command run with the inherited environment sees the
    # canary and exits red, so a referee child that inherited would refuse below.
    inherited = subprocess.run(shlex.split(command), check=False, env=os.environ.copy())
    assert inherited.returncode == 3
    landed = staged(git_repo).land("alpha")
    assert landed.referee_receipt["result"] == "pass"


def test_referee_retests_candidate_after_wal_and_target_race(git_repo: GitFixture) -> None:
    configure(git_repo, "from pathlib import Path; "
              "assert not (Path('a.txt').read_text() == 'alpha\\n' and "
              "Path('b.txt').read_text() == 'beta\\n')")
    coordinator = staged(git_repo)
    staged(git_repo, "beta", "b.txt")
    interleaved = False

    def checkpoint(point: str, _context: Mapping[str, Any]) -> None:
        nonlocal interleaved
        if point == "before_intent" and not interleaved:
            interleaved = True
            coordinator.land("beta")

    with pytest.raises(LandingRefusedError) as caught:
        coordinator.land("alpha", checkpoint=checkpoint)
    assert caught.value.details["reason"] == "referee_red"
    assert git_repo.git("show", "HEAD:b.txt").stdout == "beta\n"
    assert git_repo.git("show", "HEAD:a.txt").stdout == "a0\n"


def test_later_intent_cannot_overtake_pending_target(git_repo: GitFixture) -> None:
    configure(git_repo, "pass")
    coordinator = staged(git_repo)
    staged(git_repo, "beta", "b.txt")
    observed: list[str] = []

    def checkpoint(point: str, _context: Mapping[str, Any]) -> None:
        if point == "after_intent":
            with pytest.raises(LandingRefusedError) as caught:
                coordinator.land("beta")
            observed.append(caught.value.details["reason"])

    coordinator.land("alpha", checkpoint=checkpoint)
    assert observed == ["train_pending"]
    coordinator.land("beta")
    assert git_repo.git("show", "HEAD:a.txt").stdout == "alpha\n"
    assert git_repo.git("show", "HEAD:b.txt").stdout == "beta\n"


def test_unconfigured_referee_is_explicitly_structural_only(git_repo: GitFixture) -> None:
    landed = staged(git_repo).land("alpha")
    assert landed.referee_receipt["result"] == "not_configured"
    assert landed.referee_receipt["tested"] == []


@pytest.mark.parametrize(("field", "value"), [
    ("version", True), ("timeout_seconds", -1), ("stdout_tail", {}),
    ("stderr_tail", None), ("output_truncated", "yes"),
])
def test_malformed_referee_evidence_is_rejected(
    git_repo: GitFixture, field: str, value: Any,
) -> None:
    configure(git_repo, "pass")
    receipt = dict(staged(git_repo).land("alpha").referee_receipt)
    receipt[field] = value
    with pytest.raises(ValueError, match="invalid referee"):
        referee.validate_receipt(receipt)


def test_command_parse_failure_does_not_claim_execution(git_repo: GitFixture) -> None:
    git_repo.git("config", "onetree.referee", "'unterminated")
    coordinator = staged(git_repo)
    with pytest.raises(LandingRefusedError) as caught:
        coordinator.land("alpha")
    receipt = caught.value.details["referee_receipt"]
    assert receipt["result"] == "error"
    assert receipt["tested"] == []
    assert receipt["returncode"] is None


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "invalid"])
def test_invalid_timeout_is_recorded_before_landing(git_repo: GitFixture, value: str) -> None:
    configure(git_repo, "pass")
    git_repo.git("config", "onetree.refereeTimeout", value)
    coordinator = staged(git_repo)
    with pytest.raises(LandingRefusedError) as caught:
        coordinator.land("alpha")
    assert caught.value.details["reason"] == "referee_config"
    assert caught.value.details["paths"] == ["a.txt"]
    assert coordinator.inspect_wal().events[-1].data["reason"] == "referee_config"
    assert git_repo.oid() == git_repo.base


def test_missing_program_is_an_unexecuted_error(git_repo: GitFixture) -> None:
    git_repo.git("config", "onetree.referee", "/absent-onetree-test-program")
    with pytest.raises(LandingRefusedError) as caught:
        staged(git_repo).land("alpha")
    assert caught.value.details["referee_receipt"]["tested"] == []
    assert caught.value.details["reason"] == "referee_error"


def test_referee_output_is_bounded_and_kept_out_of_cli_stdout(
    git_repo: GitFixture, capsys: pytest.CaptureFixture[str],
) -> None:
    configure(git_repo, "import sys; print('x' * 5000); print('bad', file=sys.stderr); "
              "raise SystemExit(8)")
    with pytest.raises(LandingRefusedError) as caught:
        staged(git_repo).land("alpha")
    receipt = caught.value.details["referee_receipt"]
    assert receipt["output_truncated"] is True
    assert len(receipt["stdout_tail"]) == 4096
    assert receipt["stderr_tail"] == "bad\n"

    # The CLI's JSON stdout is exactly one object; the referee's own printing
    # reaches it only inside the recorded receipt tail.
    configure(git_repo, "print('JUNK-NOT-JSON'); raise SystemExit(5)")
    code, payload = _json_call(capsys, ["land", "--repo", str(git_repo.root), "--session", "alpha"])
    assert code != 0
    assert payload["ok"] is False
    assert payload["error"]["reason"] == "referee_red"
    assert payload["error"]["referee_receipt"]["stdout_tail"] == "JUNK-NOT-JSON\n"


def test_referee_configuration_change_at_fence_refuses(git_repo: GitFixture) -> None:
    configure(git_repo, "pass")
    coordinator = staged(git_repo)

    def checkpoint(point: str, _context: Mapping[str, Any]) -> None:
        if point == "before_intent":
            configure(git_repo, "raise SystemExit(1)")

    with pytest.raises(LandingRefusedError) as caught:
        coordinator.land("alpha", checkpoint=checkpoint)
    assert caught.value.details["reason"] == "referee_config_changed"
    assert git_repo.oid() == git_repo.base


def test_malformed_configuration_at_fence_retains_refusal(git_repo: GitFixture) -> None:
    configure(git_repo, "pass")
    coordinator = staged(git_repo)

    def checkpoint(point: str, _context: Mapping[str, Any]) -> None:
        if point == "before_intent":
            git_repo.git("config", "onetree.refereeTimeout", "nan")

    with pytest.raises(LandingRefusedError) as caught:
        coordinator.land("alpha", checkpoint=checkpoint)
    assert caught.value.details["paths"] == ["a.txt"]
    event = coordinator.inspect_wal().events[-1]
    assert event.kind == "refused"
    assert event.data["reason"] == "referee_config_changed"


@pytest.mark.parametrize("case", ["wrong_kind", "passing_refusal", "duration", "timeout"])
def test_referee_receipt_replay_refuses_malformed_carriers(
    git_repo: GitFixture, case: str,
) -> None:
    configure(git_repo, "pass")
    coordinator = staged(git_repo)
    receipt = dict(coordinator.land("alpha").referee_receipt)
    data: dict[str, Any] = {
        "action": "land", "reason": "referee_pass", "referee_receipt": receipt,
    }
    kind = "refused"
    if case == "wrong_kind":
        kind = "gc"
        data = {"claim_generations": [], "session_generations": [], "temp_indexes": [],
                "referee_receipt": {"malformed": True}}
    elif case in {"duration", "timeout"}:
        receipt["result"] = "error"
        receipt["returncode"] = None
        receipt[case + "_seconds"] = 10**400
        data["reason"] = "referee_error"
    event = EventRecord("invalid", (), coordinator.wal.document(kind, "alpha", data))
    with pytest.raises(WalCorruptError):
        coordinator._apply(coordinator._fold(coordinator.wal.read()), event)


@pytest.mark.parametrize(("field", "value"), [
    ("candidate", None), ("baseline", "wrong"), ("tested", []), ("command", None),
    ("result", "unknown"), ("duration_seconds", -1), ("returncode", False),
    ("returncode", 1), ("localization", "pair"),
])
def test_referee_receipt_semantics_reject_inconsistent_evidence(
    git_repo: GitFixture, field: str, value: Any,
) -> None:
    configure(git_repo, "pass")
    receipt = copy.deepcopy(dict(staged(git_repo).land("alpha").referee_receipt))
    receipt[field] = value
    with pytest.raises(ValueError, match="referee|localization"):
        referee.validate_receipt(receipt)


@pytest.mark.parametrize(("patch", "message"), [
    ({"result": "red", "returncode": 0}, "red referee has no failed exit"),
    ({"result": "red", "returncode": None}, "red referee has no failed exit"),
    ({"result": "timeout", "returncode": 1}, "unobserved exit"),
    ({"result": "not_configured", "returncode": 0, "command": None, "tested": []},
     "unobserved exit"),
])
def test_receipt_verdict_and_exit_must_agree(
    git_repo: GitFixture, patch: dict[str, Any], message: str,
) -> None:
    configure(git_repo, "pass")
    receipt = copy.deepcopy(dict(staged(git_repo).land("alpha").referee_receipt))
    receipt.update(patch)
    with pytest.raises(ValueError, match=message):
        referee.validate_receipt(receipt)
    # Control: the same verdict with the exit it implies is accepted.
    if patch["result"] == "red":
        receipt["returncode"] = 1
    else:
        receipt["returncode"] = None
    referee.validate_receipt(receipt)


def test_red_verdict_is_not_retried_to_green_after_wal_race(
    git_repo: GitFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    counter = tmp_path / "controlled-referee-count"
    configure(git_repo, f"from pathlib import Path; p=Path({str(counter)!r}); "
              "n=int(p.read_text()) if p.exists() else 0; p.write_text(str(n+1)); "
              "raise SystemExit(1 if n == 0 else 0)")
    coordinator = staged(git_repo)
    original = referee.run

    def interleave(*args: Any, **kwargs: Any) -> Mapping[str, Any]:
        try:
            return original(*args, **kwargs)
        except LandingRefusedError:
            coordinator.register_session("observer", pid=os.getpid())
            raise

    monkeypatch.setattr(referee, "run", interleave)
    with pytest.raises(LandingRefusedError) as caught:
        coordinator.land("alpha")
    assert caught.value.details["reason"] == "referee_red"
    assert counter.read_text() == "1"
    assert git_repo.oid() == git_repo.base
    assert coordinator.inspect_wal().events[-1].data["reason"] == "referee_red"
