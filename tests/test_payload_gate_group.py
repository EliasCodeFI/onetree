"""Payload-gate process-group lifetime on timeout (task 142).

The gate runs as the leader of its own process group. When the broker's wait on it
times out, the whole group is killed, so a descendant the gate forked cannot outlive a
refused landing while holding the broker's output pipe. Only descendants that stay in the
group are covered: one that calls ``setsid`` has left it, and the fixture for that case
asserts the shape of the gap rather than pretending it is closed.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import GitFixture

from onetree import git as git_module
from onetree.core import Coordinator
from onetree.errors import PayloadGateError

pytestmark = pytest.mark.skipif(
    os.name != "posix" or not Path("/proc").is_dir(),
    reason="process groups are a POSIX contract; liveness is read from /proc",
)

# Runs under ``python -I -S`` with an environment admitted by name, so every parameter is a
# baked-in source literal rather than an inherited variable. The grandchild inherits the
# broker's stdout and stderr pipe ends unless ``__CLOSE_PIPES__`` is set, records its pid
# once it is running, then sleeps for longer than any timeout the tests use. The leader
# does nothing else until that pid is published, so every later step of the gate implies
# the descendant is observable.
_FORKING_GATE_SOURCE = """\
import json
import os
import sys
import time

child = os.fork()
if child == 0:
    if __LEAVE_GROUP__:
        os.setsid()
    if __CLOSE_PIPES__:
        sink = os.open(os.devnull, os.O_WRONLY)
        os.dup2(sink, 1)
        os.dup2(sink, 2)
    with open(__PID_PATH__ + ".tmp", "w") as stream:
        stream.write(str(os.getpid()))
    os.rename(__PID_PATH__ + ".tmp", __PID_PATH__)
    time.sleep(120)
    os._exit(0)
while not os.path.exists(__PID_PATH__):
    time.sleep(0.005)
if __GATE_HANGS__:
    if __CLOSE_PIPES__:
        sink = os.open(os.devnull, os.O_WRONLY)
        os.dup2(sink, 1)
        os.dup2(sink, 2)
    time.sleep(120)
print(
    json.dumps(
        {
            "candidate": sys.argv[2],
            "repository": sys.argv[4],
            "staged_files": ["a.txt"],
            "findings": [],
            "degraded": [],
            "verdict": "CLEAN",
        }
    )
)
"""

_GRACE_SECONDS = 10.0


def _write_forking_gate(
    path: Path,
    pid_path: Path,
    *,
    gate_hangs: bool = False,
    leave_group: bool = False,
    close_pipes: bool = False,
) -> Path:
    source = (
        _FORKING_GATE_SOURCE.replace("__PID_PATH__", repr(str(pid_path)))
        .replace("__GATE_HANGS__", repr(gate_hangs))
        .replace("__LEAVE_GROUP__", repr(leave_group))
        .replace("__CLOSE_PIPES__", repr(close_pipes))
    )
    path.write_text(source, encoding="utf-8")
    return path


def _stage(git_repo: GitFixture, gate: Path) -> Coordinator:
    git_module.PAYLOAD_GATE_SOURCE_SHA256 = hashlib.sha256(gate.read_bytes()).hexdigest()
    coordinator = Coordinator(git_repo.root, commit_payload=gate)
    session = coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    coordinator.claim("alpha", [str(git_repo.root / "a.txt")])
    git_repo.write("a.txt", "gate lifetime\n")
    git_repo.stage(session.index_path, "a.txt")
    return coordinator


def _process_state(pid: int) -> str | None:
    """The scheduler state letter from /proc, or None once the pid is gone."""

    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
    except FileNotFoundError:
        return None
    return text.rsplit(")", 1)[1].split()[0]


def _running(pid: int) -> bool:
    return _process_state(pid) not in (None, "Z")


def _wait_until_not_running(pid: int, grace: float) -> bool:
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not _running(pid):
            return True
        time.sleep(0.05)
    return not _running(pid)


def _recorded_pid(pid_path: Path) -> int:
    assert pid_path.is_file(), "the gate never ran its descendant"
    return int(pid_path.read_text(encoding="ascii"))


def _kill_quietly(pid: int) -> None:
    if _running(pid):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


@pytest.mark.parametrize(
    ("gate_hangs", "close_pipes"),
    [(False, False), (True, False), (True, True)],
    ids=["gate-exits-clean", "gate-hangs", "gate-hangs-with-pipes-closed"],
)
def test_timeout_terminates_descendants_holding_the_gate_output_pipe(
    git_repo: GitFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate_hangs: bool,
    close_pipes: bool,
) -> None:
    """Three timeout shapes: a held pipe after exit, a hanging gate, and a hanging gate
    whose pipes are already closed, where the deadline fires while waiting for its exit."""

    pid_path = tmp_path / "descendant.pid"
    gate = _write_forking_gate(
        tmp_path / "forking.py", pid_path, gate_hangs=gate_hangs, close_pipes=close_pipes
    )
    monkeypatch.setattr(git_module, "PAYLOAD_GATE_TIMEOUT_SECONDS", 1.0)
    coordinator = _stage(git_repo, gate)
    branch_before = git_repo.oid()
    wal_before = coordinator.wal.read().head

    started = time.monotonic()
    descendant: int | None = None
    try:
        with pytest.raises(PayloadGateError, match="timed out"):
            coordinator.land("alpha")
        elapsed = time.monotonic() - started
        descendant = _recorded_pid(pid_path)

        assert elapsed < _GRACE_SECONDS, "the wait was bounded by the descendant, not the timeout"
        assert git_repo.oid() == branch_before
        assert coordinator.wal.read().head == wal_before
        assert _wait_until_not_running(descendant, _GRACE_SECONDS), (
            f"descendant {descendant} survived the timeout in state {_process_state(descendant)}"
        )
    finally:
        if descendant is not None:
            _kill_quietly(descendant)


def test_timeout_leaves_processes_outside_the_gate_group_alive(
    git_repo: GitFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid_path = tmp_path / "descendant.pid"
    gate = _write_forking_gate(tmp_path / "forking.py", pid_path)
    monkeypatch.setattr(git_module, "PAYLOAD_GATE_TIMEOUT_SECONDS", 1.0)
    coordinator = _stage(git_repo, gate)
    bystander = subprocess.Popen(  # noqa: S603 - fixture interpreter, no shell
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    descendant: int | None = None
    try:
        with pytest.raises(PayloadGateError, match="timed out"):
            coordinator.land("alpha")
        descendant = _recorded_pid(pid_path)

        assert _wait_until_not_running(descendant, _GRACE_SECONDS)
        assert bystander.poll() is None, "a process outside the gate group was signalled"
        assert _running(os.getpid())
    finally:
        bystander.kill()
        bystander.wait()
        if descendant is not None:
            _kill_quietly(descendant)


def test_a_descendant_that_leaves_the_group_is_outside_the_contract(
    git_repo: GitFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shape of the recorded gap: ``setsid`` in a descendant escapes the kill."""

    pid_path = tmp_path / "escaped.pid"
    gate = _write_forking_gate(tmp_path / "escaping.py", pid_path, leave_group=True)
    monkeypatch.setattr(git_module, "PAYLOAD_GATE_TIMEOUT_SECONDS", 1.0)
    coordinator = _stage(git_repo, gate)
    branch_before = git_repo.oid()
    escaped: int | None = None
    try:
        with pytest.raises(PayloadGateError, match="timed out"):
            coordinator.land("alpha")
        escaped = _recorded_pid(pid_path)

        assert git_repo.oid() == branch_before
        assert _running(escaped), "an escaped descendant is not contained by the group kill"
    finally:
        if escaped is not None:
            _kill_quietly(escaped)


def test_a_successful_gate_keeps_its_detached_descendant(
    git_repo: GitFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful-gate behaviour is unchanged: nothing is signalled on a CLEAN verdict."""

    pid_path = tmp_path / "detached.pid"
    gate = _write_forking_gate(tmp_path / "detaching.py", pid_path, close_pipes=True)
    monkeypatch.setattr(git_module, "PAYLOAD_GATE_TIMEOUT_SECONDS", 30.0)
    coordinator = _stage(git_repo, gate)
    branch_before = git_repo.oid()
    detached: int | None = None
    try:
        started = time.monotonic()
        landed = coordinator.land("alpha")
        elapsed = time.monotonic() - started
        detached = _recorded_pid(pid_path)

        assert elapsed < _GRACE_SECONDS
        assert git_repo.oid() == landed.commit != branch_before
        assert _running(detached)
    finally:
        if detached is not None:
            _kill_quietly(detached)


# Cancellation: the gate forks a pipe-holding grandchild, announces readiness only once
# the grandchild has published its pid, then waits for a release file. Both pids are
# published atomically: written to a temporary name and renamed into place, so the
# file the sender polls for never exists without its complete contents. Readiness is
# therefore causal and complete: an interrupt sent after ``ready`` exists can arrive
# neither before the descendant is observable nor before the leader's pid is readable,
# whatever the scheduler does. The ``publication_delay`` parameter is the scheduling
# control: it stalls each writer between opening its file and writing the pid. Under
# that input a fixture that announced readiness straight after the fork reported "the
# gate never ran its descendant", and one that wrote ``ready`` in place was interrupted
# with an empty file and failed on ``int('')``. The worker below is interrupted with a
# real SIGINT while the gate is running and the gate is released 50 ms later. A
# cleanup that reaps the leader before signalling its group observes ECHILD at the
# moment of the signal; the audit hook records that without changing the signal.
_RELEASED_GATE_SOURCE = """\
import os
import pathlib
import time

child = os.fork()
if child == 0:
    with open(__PID_PATH__ + ".tmp", "w") as stream:
        time.sleep(__PUBLICATION_DELAY__)
        stream.write(str(os.getpid()))
    os.replace(__PID_PATH__ + ".tmp", __PID_PATH__)
    time.sleep(120)
    os._exit(0)
while not pathlib.Path(__PID_PATH__).exists():
    time.sleep(0.005)
with open(__READY_PATH__ + ".tmp", "w") as ready_stream:
    time.sleep(__PUBLICATION_DELAY__)
    ready_stream.write(str(os.getpid()))
os.replace(__READY_PATH__ + ".tmp", __READY_PATH__)
while __WAIT_FOR_RELEASE__ and not pathlib.Path(__RELEASE_PATH__).exists():
    time.sleep(0.005)
"""


def _observe_cancellation(
    root: str, directory: str, gate_exits_first: bool, publication_delay: float
) -> None:
    """Worker body: run the gate helper, take a real SIGINT, record what killpg saw.

    With ``gate_exits_first`` the gate exits as soon as it has announced readiness and
    the interrupt arrives 200 ms later, while the descendant still holds the pipes; the
    leader is then a zombie when the group is signalled. Otherwise the gate keeps
    running until released 50 ms after the interrupt. ``publication_delay`` stalls both
    pid publications between open and write.
    """

    import json
    import sys
    import threading

    workdir = Path(directory)
    ready = workdir / "ready"
    release = workdir / "release"
    events: list[dict[str, object]] = []

    def observe(event: str, args: tuple[object, ...]) -> None:
        if event != "os.killpg":
            return
        group, signum = args
        assert isinstance(group, int)
        try:
            status = os.waitid(os.P_PID, group, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            events.append({"pgid": group, "signal": signum, "leader": "reaped"})
        else:
            leader = "running" if status is None else "exited"
            events.append({"pgid": group, "signal": signum, "leader": leader})

    sys.addaudithook(observe)
    gate = (
        _RELEASED_GATE_SOURCE.replace("__PID_PATH__", repr(str(workdir / "descendant.pid")))
        .replace("__READY_PATH__", repr(str(ready)))
        .replace("__RELEASE_PATH__", repr(str(release)))
        .replace("__WAIT_FOR_RELEASE__", repr(not gate_exits_first))
        .replace("__PUBLICATION_DELAY__", repr(publication_delay))
    )
    git_module.PAYLOAD_GATE_TIMEOUT_SECONDS = 30.0

    def interrupt() -> None:
        until = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < until:
            time.sleep(0.005)
        if ready.exists():
            if gate_exits_first:
                time.sleep(0.2)
            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(0.05)
            release.write_text("exit", encoding="ascii")

    sender = threading.Thread(target=interrupt)
    sender.start()
    result: dict[str, object] = {"runtime": git_module.__file__}
    try:
        git_module.Repository(Path(root))._run_payload_gate([sys.executable, "-c", gate])
        result["outcome"] = "returned"
    except BaseException as error:  # noqa: BLE001 - the outcome type is the observation
        result["outcome"] = type(error).__name__
    finally:
        sender.join(timeout=5)
        result["events"] = events
        result["gate_pid"] = int(ready.read_text(encoding="ascii")) if ready.exists() else None
        (workdir / "result.json").write_text(json.dumps(result), encoding="utf-8")


@pytest.mark.parametrize(
    "publication_delay", [0.0, 0.3], ids=["publication-prompt", "publication-delayed"]
)
@pytest.mark.parametrize("gate_exits_first", [False, True], ids=["gate-running", "gate-exited"])
def test_cancellation_signals_the_group_before_the_leader_is_reaped(
    git_repo: GitFixture,
    tmp_path: Path,
    gate_exits_first: bool,
    publication_delay: float,
) -> None:
    import json
    import multiprocessing

    worker = multiprocessing.get_context("fork").Process(
        target=_observe_cancellation,
        args=(str(git_repo.root), str(tmp_path), gate_exits_first, publication_delay),
    )
    worker.start()
    descendant: int | None = None
    try:
        worker.join(timeout=20)
        assert worker.exitcode == 0, worker.exitcode
        result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
        assert result["runtime"] == git_module.__file__
        assert result["outcome"] == "KeyboardInterrupt", result
        assert result["gate_pid"] is not None, "the gate never announced readiness"
        descendant = _recorded_pid(tmp_path / "descendant.pid")

        assert len(result["events"]) == 1, result
        (event,) = result["events"]
        assert event["pgid"] == result["gate_pid"]
        assert event["signal"] == signal.SIGKILL
        assert event["leader"] != "reaped", result
        assert _wait_until_not_running(descendant, _GRACE_SECONDS), (
            f"descendant {descendant} survived the cancellation in state "
            f"{_process_state(descendant)}"
        )
    finally:
        if worker.is_alive():
            worker.kill()
            worker.join(timeout=5)
        if descendant is not None:
            _kill_quietly(descendant)
