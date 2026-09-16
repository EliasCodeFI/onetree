from __future__ import annotations

import contextlib
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from conftest import _GIT_EXECUTABLE, GitFixture
from test_referee import _alive
from test_referee_crash_windows import _fork_lander, _kill, _prepare, _wait_for

from onetree import git
from onetree.errors import LandingRefusedError


@pytest.mark.parametrize("phase", [
    "referee", "clone", "checkout", "inherited_writer", "inherited_input",
])
def test_lander_death_cleans_active_group_and_scratch(
    git_repo: GitFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    coordinator, _, log, scratch = _prepare(git_repo, tmp_path)
    inherited = tmp_path / "inherited.pid"
    keep_input = phase == "inherited_input"
    expect_inherited = phase in {"inherited_writer", "inherited_input"}
    if expect_inherited:
        phase = "referee"
        original_popen = subprocess.Popen

        def fork_after_supervisor(*args: Any, **kwargs: Any) -> Any:
            child = original_popen(*args, **kwargs)
            if any(str(item).endswith("_referee_supervisor.py") for item in args[0]):
                inheritor = os.fork()
                if inheritor == 0:
                    if not keep_input and child.stdin is not None:
                        child.stdin.close()
                    time.sleep(60)
                    os._exit(0)
                inherited.write_text(str(inheritor))
            return child

        monkeypatch.setattr(subprocess, "Popen", fork_after_supervisor)
    marker = tmp_path / "lifetime.json"
    script = tmp_path / "blocked-command"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, subprocess, sys, time\n"
        f"phase = {phase!r}\n"
        "if phase == 'referee' or phase in sys.argv[1:]:\n"
        "    child = subprocess.Popen(['/bin/sleep', '60'])\n"
        "    data = dict(pid=os.getpid(), child=child.pid, cwd=os.getcwd())\n"
        f"    marker = pathlib.Path({str(marker)!r})\n"
        "    staging = marker.with_suffix('.tmp')\n"
        "    staging.write_text(json.dumps(data)); staging.replace(marker)\n"
        "    child.wait()\n"
        "else:\n"
        f"    os.execv({_GIT_EXECUTABLE!r}, [{_GIT_EXECUTABLE!r}, *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    if phase == "referee":
        git_repo.git("config", "onetree.referee", shlex.join([str(script)]))
    else:
        monkeypatch.setattr(git, "_GIT_EXECUTABLE", str(script))

    unrelated = subprocess.Popen(["/bin/sleep", "60"])
    process = _fork_lander(git_repo, "alpha", None, log, scratch)
    try:
        _wait_for(marker.exists, f"{phase} to start")
        running = json.loads(marker.read_text())
        assert _alive(running["pid"])
        assert _alive(running["child"])
        assert list(scratch.glob("onetree-referee-*"))
        if expect_inherited:
            assert inherited.exists()
            assert _alive(int(inherited.read_text()))
        if expect_inherited:
            process.kill()
            _wait_for(lambda: process.exitcode is not None, "lander death")
            assert process.exitcode == -signal.SIGKILL
        else:
            _kill(process)

        _wait_for(
            lambda: not _alive(running["pid"]) and not _alive(running["child"]),
            f"{phase} group cleanup after lander death", timeout=5,
        )
        _wait_for(lambda: not list(scratch.glob("onetree-referee-*")),
                  "scratch cleanup after lander death", timeout=5)
        assert unrelated.poll() is None
        if inherited.exists():
            assert _alive(int(inherited.read_text()))
        assert git_repo.oid() == git_repo.base
        report = coordinator.inspect_wal()
        assert report.consistent
        assert report.pending_intents == 0
        assert not any(event.kind == "land_intent" for event in report.events)
    finally:
        if process.is_alive():
            process.kill()
        process.join(timeout=5)
        if marker.exists():
            running = json.loads(marker.read_text())
            for pid in (running["pid"], running["child"]):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
        unrelated.kill()
        unrelated.wait(timeout=5)
        if inherited.exists():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(inherited.read_text()), signal.SIGKILL)


def test_referee_timeout_starts_after_preparation(
    git_repo: GitFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, _, _, _ = _prepare(git_repo, tmp_path)
    git_repo.git("config", "onetree.referee", "/bin/sleep 1")
    git_repo.git("config", "onetree.refereeTimeout", "0.001")
    script = tmp_path / "slow-clone"
    script.write_text(
        f"#!{sys.executable}\n"
        "import os, sys, time\n"
        "if 'clone' in sys.argv[1:]: time.sleep(0.05)\n"
        f"os.execv({_GIT_EXECUTABLE!r}, [{_GIT_EXECUTABLE!r}, *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    monkeypatch.setattr(git, "_GIT_EXECUTABLE", str(script))
    with pytest.raises(LandingRefusedError) as caught:
        coordinator.land("alpha")
    assert caught.value.details["reason"] == "referee_timeout"
    receipt = caught.value.details["referee_receipt"]
    assert receipt["tested"] == [receipt["candidate"]]
    assert git_repo.oid() == git_repo.base


def test_supervisor_startup_death_cannot_return_green(
    git_repo: GitFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, referee, _, scratch = _prepare(git_repo, tmp_path)
    referee.let_pass()
    original_popen = subprocess.Popen

    def kill_supervisor(*args: Any, **kwargs: Any) -> Any:
        child = original_popen(*args, **kwargs)
        if any(str(item).endswith("_referee_supervisor.py") for item in args[0]):
            child.kill()
        return child

    monkeypatch.setattr(subprocess, "Popen", kill_supervisor)
    with pytest.raises(LandingRefusedError) as caught:
        coordinator.land("alpha")
    assert caught.value.details["reason"] == "referee_error"
    assert caught.value.details["referee_receipt"]["tested"] == []
    assert referee.executions == 0
    assert git_repo.oid() == git_repo.base
    assert list(scratch.glob("onetree-referee-*")) == []
