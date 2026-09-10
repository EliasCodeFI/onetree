from __future__ import annotations

import copy
import json
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
from onetree.git import Repository
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


_GLOBAL_REFEREE = "[onetree]\n\treferee = /bin/true\n"


def operator_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, channel: str, content: str,
) -> None:
    """Route `channel` at a config file carrying `content`, the way an operator's shell would."""
    if channel == "GIT_CONFIG":
        # The deprecated redirect: `git config` reads this file instead of the repository.
        target = tmp_path / "redirected-config"
    elif channel == "HOME":
        home = tmp_path / "home"
        home.mkdir()
        target = home / ".gitconfig"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    elif channel == "XDG_CONFIG_HOME":
        target = tmp_path / "xdg" / "git" / "config"
        target.parent.mkdir(parents=True)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    else:
        target = tmp_path / (channel.lower() + "-config")
        if channel == "GIT_CONFIG_SYSTEM":
            monkeypatch.delenv("GIT_CONFIG_NOSYSTEM", raising=False)
    target.write_text(content, encoding="utf-8")
    if channel in {"GIT_CONFIG", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"}:
        monkeypatch.setenv(channel, str(target))


@pytest.mark.parametrize("channel", ["GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
                                     "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "HOME",
                                     "XDG_CONFIG_HOME"])
def test_inherited_git_config_environment_cannot_demote_a_red_referee(
    git_repo: GitFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, channel: str,
) -> None:
    configure(git_repo, "raise SystemExit(3)")
    red = git_repo.git("config", "--get", "onetree.referee").stdout.strip()
    coordinator = staged(git_repo)
    if channel == "GIT_CONFIG":
        operator_config(tmp_path, monkeypatch, channel, "")
    elif channel == "GIT_CONFIG_PARAMETERS":
        monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'onetree.referee=/bin/true'")
    elif channel == "GIT_CONFIG_COUNT":
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "onetree.referee")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "/bin/true")
    else:
        operator_config(tmp_path, monkeypatch, channel, _GLOBAL_REFEREE)

    # RED control: under the environment the lander inherits, git itself no longer
    # answers with the one red command, so an evaluator reading through that
    # environment would run no referee or a different one.
    assert inherited_config_values(git_repo, "onetree.referee") != [red]

    with pytest.raises(LandingRefusedError) as caught:
        coordinator.land("alpha")
    assert caught.value.details["reason"] == "referee_red"
    assert caught.value.details["referee_receipt"]["command"] == red
    assert git_repo.oid() == git_repo.base


def test_referee_is_repository_configuration_not_the_operators(
    git_repo: GitFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A referee in the operator's own config runs nowhere; the repository decides."""
    operator_config(tmp_path, monkeypatch, "HOME",
                    "[onetree]\n\treferee = /bin/false\n\trefereeTimeout = 0.001\n")
    # RED control: the effective-scope read the operator's git performs sees that
    # referee, so an evaluator using it would run the operator's red command.
    assert inherited_config_values(git_repo, "onetree.referee") == ["/bin/false"]
    assert inherited_config_values(git_repo, "onetree.refereeTimeout") == ["0.001"]

    landed = staged(git_repo).land("alpha")
    assert landed.referee_receipt["result"] == "not_configured"
    assert landed.referee_receipt["command"] is None
    assert landed.referee_receipt["timeout_seconds"] == referee.DEFAULT_TIMEOUT
    assert git_repo.changed_paths(landed.commit) == {"a.txt"}


@pytest.mark.parametrize("source", ["include", "includeIf", "worktree"])
def test_repository_includes_and_worktree_config_are_the_repositorys(
    git_repo: GitFixture, tmp_path: Path, source: str,
) -> None:
    """`.git/config` includes and `.git/config.worktree` are repository configuration."""
    red = referee_command("raise SystemExit(6)")
    if source == "worktree":
        git_repo.git("config", "extensions.worktreeConfig", "true")
        git_repo.git("config", "--worktree", "onetree.referee", red)
    else:
        included = tmp_path / "included.gitconfig"
        git_repo.git("config", "--file", str(included), "onetree.referee", red)
        section = "include" if source == "include" else f"includeIf.gitdir:{git_repo.root}/"
        git_repo.git("config", f"{section}.path", str(included))
    # RED control: git's effective read sees the red command, and a `--local` read
    # (no includes, no worktree file) does not.
    assert inherited_config_values(git_repo, "onetree.referee") == [red]
    local_only = git_repo.git("config", "--local", "--get-all", "onetree.referee", check=False)
    assert local_only.stdout.strip() != red

    coordinator = staged(git_repo)
    with pytest.raises(LandingRefusedError) as caught:
        coordinator.land("alpha")
    assert caught.value.details["reason"] == "referee_red"
    assert caught.value.details["referee_receipt"]["command"] == red
    assert git_repo.oid() == git_repo.base


@pytest.mark.parametrize("scope", ["system", "command"])
def test_system_and_command_scopes_are_not_the_repositorys(
    git_repo: GitFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: str,
) -> None:
    """The broker drops the environment that would redirect its own `git config`, so no
    test can hand the scope filter a `system` or `command` pair through the front door
    and the filter's exclusion of both had no fixture that could fail (Round BF3 DUO).
    Here the redirecting names are admitted into the broker's environment on purpose:
    the pair reaches the filter, and the filter alone must drop it -- a system file is
    exactly the input no environment pop can remove."""
    red = "/bin/false"
    if scope == "system":
        system_file = tmp_path / "system.gitconfig"
        git_repo.git("config", "--file", str(system_file), "onetree.referee", red)
        admitted = {"GIT_CONFIG_SYSTEM": str(system_file)}
        monkeypatch.delenv("GIT_CONFIG_NOSYSTEM", raising=False)
    else:
        admitted = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "onetree.referee",
                    "GIT_CONFIG_VALUE_0": red}
    for name, value in admitted.items():
        monkeypatch.setenv(name, value)
    original = Repository._environment

    def admitting(index: Path | None = None) -> dict[str, str]:
        environment = original(index)
        environment.update(admitted)
        return environment

    monkeypatch.setattr(Repository, "_environment", staticmethod(admitting))
    coordinator = staged(git_repo)
    # RED control: the pair now reaches the read, labelled with its scope; a filter
    # admitting that scope would run the red command.
    reported = coordinator.repo.git(
        "config", "--null", "--show-scope", "--get-all", "onetree.referee",
    ).stdout
    assert reported == f"{scope}\0{red}\0".encode()
    assert referee.configuration(coordinator.repo) == (None, referee.DEFAULT_TIMEOUT)
    landed = coordinator.land("alpha")
    assert landed.referee_receipt["result"] == "not_configured"
    assert git_repo.changed_paths(landed.commit) == {"a.txt"}


@pytest.mark.parametrize("shape", ["not_utf8", "exit_128", "unterminated", "odd_fields"])
def test_unreadable_referee_configuration_is_a_typed_refusal(
    git_repo: GitFixture, monkeypatch: pytest.MonkeyPatch, shape: str,
) -> None:
    """A value git cannot hand over as UTF-8 text is a real repository input; the other
    three are streams `git config --null --show-scope` does not produce, and the parser
    refuses them rather than guessing at pairs (uncovered arms, Round BF3 DUO). The fake
    serves the malformed stream for `onetree.referee` only and "unset" for the timeout,
    so an arm that stops refusing hands the value through instead of tripping the timeout
    parse (Round BF4 DUO: one shared stream made every arm's red an "invalid referee
    timeout" refusal, and the fixture red only on the message)."""
    coordinator = staged(git_repo)
    if shape == "not_utf8":
        git_repo.git("config", "onetree.referee", os.fsdecode(b"/bin/\xff"))
        # RED control: git reports one local pair whose value is not UTF-8.
        raw = coordinator.repo.git(
            "config", "--null", "--show-scope", "--get-all", "onetree.referee",
        ).stdout
        assert raw == b"local\0/bin/\xff\0"
        expected = "not UTF-8"
    else:
        returncode, stdout = {
            "exit_128": (128, b"local\0/bin/true\0"),
            "unterminated": (0, b"local\0/bin/true"),
            "odd_fields": (0, b"local\0"),
        }[shape]
        streams = {
            "onetree.referee": subprocess.CompletedProcess(
                ["git", "config"], returncode, stdout=stdout, stderr=b"",
            ),
            "onetree.refereeTimeout": subprocess.CompletedProcess(
                ["git", "config"], 1, stdout=b"", stderr=b"",
            ),
        }

        def fake(*arguments: str, **options: Any) -> subprocess.CompletedProcess[bytes]:
            return streams[arguments[-1]]

        monkeypatch.setattr(coordinator.repo, "git", fake)
        expected = "cannot read referee configuration"
    with pytest.raises(LandingRefusedError, match=expected) as caught:
        referee.configuration(coordinator.repo)
    assert caught.value.details["reason"] == "referee_config"
    if shape == "not_utf8":
        with pytest.raises(LandingRefusedError) as refused:
            coordinator.land("alpha")
        assert refused.value.details["reason"] == "referee_config"
        assert git_repo.oid() == git_repo.base


def test_referee_in_both_local_and_worktree_scope_is_a_duplicate(git_repo: GitFixture) -> None:
    configure(git_repo, "pass")
    git_repo.git("config", "extensions.worktreeConfig", "true")
    git_repo.git("config", "--worktree", "onetree.referee", referee_command("raise SystemExit(2)"))
    # RED control: git's effective value is the worktree one, the red command; an
    # evaluator taking the local value alone would run the passing referee and land.
    effective = git_repo.git("config", "--get", "onetree.referee").stdout.strip()
    assert effective == referee_command("raise SystemExit(2)")
    coordinator = staged(git_repo)
    with pytest.raises(LandingRefusedError) as caught:
        coordinator.land("alpha")
    assert caught.value.details["reason"] == "referee_config"
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


def admitted_referee_environment(candidate: str, baseline: str) -> dict[str, str]:
    """The referee child's whole environment: every name set to a fixed value, never forwarded."""
    return {
        "PATH": os.defpath, "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_GRAFT_FILE": os.devnull, "GIT_TERMINAL_PROMPT": "0",
        "ONETREE_CANDIDATE": candidate, "ONETREE_BASELINE": baseline,
    }


def test_referee_child_sees_only_the_admitted_environment(
    git_repo: GitFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    dump = tmp_path / "referee-environment.json"
    configure(git_repo, "import json, os, pathlib; "
              f"pathlib.Path({str(dump)!r}).write_text(json.dumps(dict(os.environ)))")
    # Caller values under every admitted name and two names that are not admitted.
    caller = {
        "PATH": str(tmp_path / "caller-bin") + os.pathsep + os.environ["PATH"],
        "LC_ALL": "en_US.UTF-8", "GIT_CONFIG_NOSYSTEM": "0",
        "GIT_CONFIG_GLOBAL": str(tmp_path / "caller.gitconfig"), "GIT_NO_REPLACE_OBJECTS": "0",
        "GIT_GRAFT_FILE": str(tmp_path / "caller-grafts"), "GIT_TERMINAL_PROMPT": "1",
        "ONETREE_CANDIDATE": "caller", "ONETREE_BASELINE": "caller",
        "ONETREE_TEST_CANARY": "leaked", "PYTHONPATH": str(tmp_path / "caller-site"),
    }
    for name, value in caller.items():
        monkeypatch.setenv(name, value)
    command = git_repo.git("config", "--get", "onetree.referee").stdout.strip()
    # RED control: the same command run with the inherited environment records every
    # caller value, so a child that inherited, or that was handed any one caller
    # value under an admitted name, differs from the mapping asserted below.
    subprocess.run(shlex.split(command), check=True, env=os.environ.copy())
    inherited = json.loads(dump.read_text())
    assert all(inherited[name] == value for name, value in caller.items())
    dump.unlink()

    landed = staged(git_repo).land("alpha")
    assert landed.referee_receipt["result"] == "pass"
    admitted = json.loads(dump.read_text())
    expected = admitted_referee_environment(landed.commit, git_repo.base)
    assert admitted == expected
    assert not any(admitted[name] == caller[name] for name in expected)


def _alive(pid: int) -> bool:
    """Whether `pid` is a running process. A zombie is dead: nothing of it can still write."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            return handle.read().rsplit(b")", 1)[1].split()[0] not in {b"Z", b"X"}
    except FileNotFoundError:
        return False
    except OSError:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True


def test_referee_timeout_kills_the_whole_process_group(
    git_repo: GitFixture, tmp_path: Path,
) -> None:
    marker = tmp_path / "grandchild.pid"
    configure(git_repo, "import pathlib, subprocess; child = subprocess.Popen(['sleep', '30']); "
              f"pathlib.Path({str(marker)!r}).write_text(str(child.pid)); child.wait()")
    git_repo.git("config", "onetree.refereeTimeout", "0.5")
    coordinator = staged(git_repo)
    # RED control: `_alive` tells a live sleeper from a killed one.
    sleeper = subprocess.Popen(["sleep", "30"])  # noqa: S607 - fixture control
    try:
        assert _alive(sleeper.pid)
    finally:
        sleeper.kill()
        sleeper.wait()
    assert not _alive(sleeper.pid)

    started = time.monotonic()
    with pytest.raises(LandingRefusedError) as caught:
        coordinator.land("alpha")
    elapsed = time.monotonic() - started
    assert caught.value.details["reason"] == "referee_timeout"
    assert git_repo.oid() == git_repo.base
    # A kill that missed the group (no session of its own, ESRCH swallowed) is
    # followed by a wait for the sleeper: 30 s, not the 0.5 s timeout.
    assert elapsed < 5
    assert caught.value.details["referee_receipt"]["duration_seconds"] < 5
    grandchild = int(marker.read_text())
    deadline = time.monotonic() + 5
    while _alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.05)
    # The referee's own child outlives a kill aimed at the referee alone; only a
    # process-group kill reaches it.
    assert not _alive(grandchild)


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
    git_repo: GitFixture, capfd: pytest.CaptureFixture[str],
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
    # reaches it only inside the recorded receipt tail. `capfd` reads file
    # descriptor 1, which is where a child's inherited stdout would land;
    # `capsys` sees only this interpreter's sys.stdout.
    configure(git_repo, "print('JUNK-NOT-JSON'); raise SystemExit(5)")
    code, payload = _json_call(capfd, ["land", "--repo", str(git_repo.root), "--session", "alpha"])
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
