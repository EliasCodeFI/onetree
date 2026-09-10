from __future__ import annotations

import contextlib
import math
import os
import shlex
import signal
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from onetree.errors import GitError, LandingRefusedError
from onetree.git import Repository

DEFAULT_TIMEOUT = 300.0
OUTPUT_TAIL_BYTES = 4096


def valid_seconds(value: Any, *, allow_zero: bool = False) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value) and (value >= 0 if allow_zero else value > 0)
    except OverflowError:
        return False


REPOSITORY_SCOPES = frozenset({"local", "worktree"})


def configuration(repo: Repository) -> tuple[str | None, float]:
    """Read the referee the repository configures, and nothing an operator brings along.

    The protected epoch records this pair and every peer must observe the same one, so
    only the repository's own scopes count: what git reports as `local` (`.git/config`
    with its includes followed) and `worktree` (`.git/config.worktree`), both of which
    live under `.git/` and are shared by every peer of the one tree. The operator's
    global and system files and the command-line scope are not the repository's.
    Measured 2026-09-10: an effective-scope read ran a global red referee the repository
    never configured and put a global timeout into the epoch (Round BF DUO); a `--local`
    read skipped `[include]` and `config.worktree` and demoted a repository-configured
    red referee to `not_configured` (Round BF2 DUO).
    """
    values: list[str | None] = []
    for key in ("onetree.referee", "onetree.refereeTimeout"):
        result = repo.git("config", "--null", "--show-scope", "--get-all", key, check=False)
        if result.returncode == 1:
            values.append(None)
            continue
        if result.returncode != 0 or not result.stdout.endswith(b"\0"):
            raise LandingRefusedError("cannot read referee configuration", reason="referee_config",
                                      paths=[])
        try:
            fields = result.stdout[:-1].decode("utf-8", errors="strict").split("\0")
        except UnicodeError as error:
            raise LandingRefusedError("referee configuration is not UTF-8",
                                      reason="referee_config", paths=[]) from error
        if len(fields) % 2:
            raise LandingRefusedError("cannot read referee configuration", reason="referee_config",
                                      paths=[])
        items = [
            value for scope, value in zip(fields[::2], fields[1::2], strict=True)
            if scope in REPOSITORY_SCOPES
        ]
        if not items:
            values.append(None)
            continue
        if len(items) != 1 or not items[0].strip():
            raise LandingRefusedError("referee configuration must have one nonempty value",
                                      reason="referee_config", paths=[])
        values.append(items[0])
    try:
        timeout = float(values[1]) if values[1] is not None else DEFAULT_TIMEOUT
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("nonpositive or nonfinite timeout")
    except ValueError as error:
        raise LandingRefusedError("invalid referee timeout", reason="referee_config",
                                  paths=[]) from error
    return values[0], timeout


def configuration_matches(repo: Repository, expected: tuple[str | None, float]) -> bool:
    try:
        return configuration(repo) == expected
    except LandingRefusedError:
        return False


def validate_receipt(receipt: Any) -> None:
    fields = {"version", "candidate", "baseline", "command", "timeout_seconds", "result",
              "returncode", "duration_seconds", "tested", "localization", "stdout_tail",
              "stderr_tail", "output_truncated"}
    if (not isinstance(receipt, dict) or set(receipt) != fields
            or type(receipt.get("version")) is not int or receipt["version"] != 1):
        raise ValueError("invalid referee receipt")
    if receipt.get("result") not in {"pass", "red", "timeout", "error", "not_configured"}:
        raise ValueError("invalid referee result")
    for name in ("candidate", "baseline"):
        value = receipt.get(name)
        if not isinstance(value, str) or len(value) not in {40, 64} or any(
            char not in "0123456789abcdef" for char in value
        ):
            raise ValueError("invalid referee object identity")
    tested = receipt.get("tested")
    configured = receipt["result"] != "not_configured"
    permitted_tests = ([receipt["candidate"]], []) if receipt["result"] == "error" else (
        [receipt["candidate"]] if configured else [],
    )
    if tested not in permitted_tests:
        raise ValueError("referee tested set differs from candidate")
    command = receipt.get("command")
    if configured and (not isinstance(command, str) or not command.strip()):
        raise ValueError("invalid referee command")
    if not configured and command is not None:
        raise ValueError("unconfigured referee has a command")
    duration = receipt.get("duration_seconds")
    if not valid_seconds(duration, allow_zero=True):
        raise ValueError("invalid referee duration")
    timeout = receipt["timeout_seconds"]
    if not valid_seconds(timeout):
        raise ValueError("invalid referee timeout")
    if not all(isinstance(receipt[key], str) for key in ("stdout_tail", "stderr_tail")):
        raise ValueError("invalid referee output")
    if type(receipt["output_truncated"]) is not bool:
        raise ValueError("invalid referee output truncation flag")
    code = receipt.get("returncode")
    if code is not None and type(code) is not int:
        raise ValueError("invalid referee return code")
    if receipt["result"] == "pass" and code != 0:
        raise ValueError("passing referee has no successful exit")
    if receipt["result"] == "red" and (code is None or code == 0):
        raise ValueError("red referee has no failed exit")
    if receipt["result"] in {"not_configured", "timeout"} and code is not None:
        raise ValueError("referee receipt has an unobserved exit")
    if receipt.get("localization") != "unresolved":
        raise ValueError("unsupported localization claim")


def run(
    repo: Repository, candidate: str, baseline: str, paths: Sequence[str],
    configured: tuple[str | None, float],
) -> Mapping[str, Any]:
    command, timeout = configured
    receipt: dict[str, Any] = {
        "version": 1, "candidate": candidate, "baseline": baseline,
        "command": command, "timeout_seconds": timeout, "result": "not_configured",
        "returncode": None, "duration_seconds": 0.0, "tested": [],
        "localization": "unresolved", "stdout_tail": "", "stderr_tail": "",
        "output_truncated": False,
    }
    if command is None:
        return receipt
    started = time.monotonic()
    receipt["result"] = "error"
    try:
        arguments = shlex.split(command)
        if not arguments:
            raise ValueError("referee command has no arguments")
        if os.name != "posix":
            raise ValueError("configured referee requires POSIX process-group cleanup")
        with tempfile.TemporaryDirectory(prefix="onetree-referee-") as directory:
            scratch = Path(directory)
            snapshot = scratch / "candidate"
            environment = {
                "PATH": os.defpath, "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull, "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_GRAFT_FILE": os.devnull, "GIT_TERMINAL_PROMPT": "0",
                "ONETREE_CANDIDATE": candidate, "ONETREE_BASELINE": baseline,
            }
            repo._invoke(scratch, ("clone", "--quiet", "--shared", "--no-checkout",
                                   "--template=", "--", str(repo.root), str(snapshot)),
                         environment=environment, check=True)
            repo._invoke(snapshot, ("-c", "core.hooksPath=" + os.devnull,
                                    "checkout", "--quiet", "--detach", candidate),
                         environment=environment, check=True)
            with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                process = subprocess.Popen(  # noqa: S603 - owner-configured argv, no shell
                    arguments, cwd=snapshot, env=environment, stdin=subprocess.DEVNULL,
                    stdout=stdout, stderr=stderr, start_new_session=True,
                )
                receipt["tested"] = [candidate]
                try:
                    code = process.wait(timeout=timeout)
                    receipt.update(result="pass" if code == 0 else "red", returncode=code)
                except subprocess.TimeoutExpired:
                    receipt["result"] = "timeout"
                finally:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                for name, stream in (("stdout_tail", stdout), ("stderr_tail", stderr)):
                    size = stream.seek(0, os.SEEK_END)
                    stream.seek(max(0, size - OUTPUT_TAIL_BYTES))
                    receipt[name] = stream.read().decode("utf-8", errors="replace")
                    receipt["output_truncated"] |= size > OUTPUT_TAIL_BYTES
    except (OSError, ValueError, GitError) as error:
        receipt.update(result="error", stderr_tail=str(error))
    receipt["duration_seconds"] = time.monotonic() - started
    if receipt["result"] != "pass":
        raise LandingRefusedError(
            "candidate referee did not pass", reason="referee_" + receipt["result"],
            paths=list(paths), referee_receipt=receipt,
        )
    return receipt
