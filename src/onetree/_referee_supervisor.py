"""Own referee resources after the submitting process dies.

EOF on the lander's lifetime pipe or loss of its parent relationship cancels
preparation or execution, kills the active group, and releases the scratch directory.
The parent check prevents another fork's inherited writer from delaying cleanup.
No success is emitted until that cleanup finishes. This is cooperative POSIX
process-group ownership, not containment of children that deliberately detach.
"""

from __future__ import annotations

import contextlib
import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

OUTPUT_TAIL_BYTES = 4096


class _CancelledError(Exception):
    pass


@dataclass(frozen=True)
class _Lifetime:
    descriptor: int
    parent: int


def _tail(stream: BinaryIO) -> tuple[str, bool]:
    size = stream.seek(0, os.SEEK_END)
    stream.seek(max(0, size - OUTPUT_TAIL_BYTES))
    return stream.read().decode("utf-8", errors="replace"), size > OUTPUT_TAIL_BYTES


def _check_lifetime(lifetime: _Lifetime, delay: float = 0.0) -> None:
    if os.getppid() != lifetime.parent or select.select(
        [lifetime.descriptor], [], [], delay
    )[0]:
        raise _CancelledError


def _default_interrupt() -> None:
    # An ignored SIGINT survives exec. A lander started with it ignored (a background
    # job of a non-interactive shell) must not pass that on to the referee command.
    signal.signal(signal.SIGINT, signal.SIG_DFL)


def _command(
    arguments: Sequence[str], cwd: Path, environment: Mapping[str, str],
    lifetime: _Lifetime, timeout: float | None, stdout: BinaryIO, stderr: BinaryIO,
) -> int:
    _check_lifetime(lifetime)
    process = subprocess.Popen(  # noqa: S603 - broker-owned argv, no shell
        arguments, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
        stdout=stdout, stderr=stderr, start_new_session=True,
        preexec_fn=_default_interrupt,  # the supervisor has one thread
    )
    deadline = None if timeout is None else time.monotonic() + timeout
    try:
        while True:
            _check_lifetime(lifetime)
            # Keep the leader waitable until after killpg: its pid cannot be
            # recycled into an unrelated process group in the cleanup window.
            if os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT):
                break
            remaining = 0.05 if deadline is None else deadline - time.monotonic()
            if timeout is not None and remaining <= 0:
                raise subprocess.TimeoutExpired(arguments, timeout)
            _check_lifetime(lifetime, min(0.05, remaining))
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        code = process.wait()
    return code


def _run(request: dict[str, Any], lifetime: _Lifetime) -> dict[str, Any]:
    result: dict[str, Any] = {
        "result": "error", "returncode": None, "tested": [],
        "stdout_tail": "", "stderr_tail": "", "output_truncated": False,
    }
    environment = request["environment"]
    timeout = request["timeout"]
    try:
        _check_lifetime(lifetime)
        with tempfile.TemporaryDirectory(prefix="onetree-referee-", dir=request["tempdir"]) as raw:
            scratch = Path(raw)
            snapshot = scratch / "candidate"
            preparation = (
                (scratch, ["clone", "--quiet", "--shared", "--no-checkout", "--template=",
                           "--", request["root"], str(snapshot)]),
                (snapshot, ["-c", "core.hooksPath=" + os.devnull, "checkout", "--quiet",
                            "--detach", request["candidate"]]),
            )
            for cwd, arguments in preparation:
                with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                    code = _command(
                        [*request["git"], "-C", str(cwd), *arguments], cwd, environment,
                        lifetime, None, stdout, stderr,
                    )
                    if code != 0:
                        detail, _ = _tail(stderr)
                        raise ValueError(f"referee preparation exited {code}: {detail}")
            with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                try:
                    code = _command(request["arguments"], snapshot, environment,
                                    lifetime, timeout, stdout, stderr)
                    result.update(result="pass" if code == 0 else "red", returncode=code)
                except subprocess.TimeoutExpired:
                    result["result"] = "timeout"
                result["tested"] = [request["candidate"]]
                for key, stream in (("stdout_tail", stdout), ("stderr_tail", stderr)):
                    result[key], truncated = _tail(stream)
                    result["output_truncated"] |= truncated
    except (OSError, ValueError) as error:
        result.update(result="error", stderr_tail=str(error))
    return result


def main() -> int:
    lifetime = _Lifetime(int(sys.argv[1]), int(sys.argv[2]))
    try:
        data = bytearray()
        while not data.endswith(b"\n"):
            _check_lifetime(lifetime)
            if not select.select([sys.stdin.fileno()], [], [], 0.05)[0]:
                continue
            block = os.read(sys.stdin.fileno(), 65536)
            if not block:
                raise ValueError("incomplete referee request")
            data.extend(block)
        request = json.loads(data)
        result = _run(request, lifetime)
        _check_lifetime(lifetime)
        sys.stdout.write(json.dumps(result) + "\n")
        return 0
    except (_CancelledError, BrokenPipeError):
        return 1
    finally:
        os.close(lifetime.descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
