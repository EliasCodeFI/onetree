from __future__ import annotations

import json
import math
import os
import shlex
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from onetree import git
from onetree.errors import LandingRefusedError
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
    with its includes followed) and `worktree` (`.git/config.worktree`), both reached
    through `.git/config`, which every peer of the one tree shares. An include whose path
    git expands per operator (`~`) is still the repository's setting, but its content is
    each peer's; under protection the epoch comparison makes that difference a refusal,
    not a silent choice of referee, and the payload gate refuses it for every peer. The
    operator's global and system files and the command-line scope are not the
    repository's: the broker's environment drops the names that would inject the command
    scope or redirect the read, and pops `GIT_CONFIG_NOSYSTEM` rather than setting it
    (`Repository._environment`), so its git does read the system file and this filter is
    what drops that value.
    Measured 2026-09-10: an effective-scope read ran a global red referee the repository
    never configured and put a global timeout into the epoch; a `--local` read skipped
    `[include]` and `config.worktree` and demoted a repository-configured red referee to
    `not_configured`; and the `system` exclusion needs a fixture that hands this filter a
    `system` pair, or no test of it can fail.
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
        if os.name != "posix" or not hasattr(os, "WNOWAIT"):
            raise ValueError("configured referee requires POSIX waitable process-group cleanup")
        if git._GIT_EXECUTABLE is None:
            raise ValueError("git executable was not found on PATH")
        environment = {
            "PATH": os.defpath, "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_GRAFT_FILE": os.devnull, "GIT_TERMINAL_PROMPT": "0",
            "ONETREE_CANDIDATE": candidate, "ONETREE_BASELINE": baseline,
        }
        request = {
            "root": str(repo.root), "candidate": candidate, "arguments": arguments,
            "git": [git._GIT_EXECUTABLE, *git._GIT_COMMON_OPTIONS],
            "timeout": timeout, "environment": environment, "tempdir": tempfile.gettempdir(),
        }
        reader, writer = os.pipe()
        process = None
        try:
            process = subprocess.Popen(  # noqa: S603 - installed supervisor, isolated interpreter
                [sys.executable, "-I", str(Path(__file__).with_name("_referee_supervisor.py")),
                 str(reader), str(os.getpid())],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                pass_fds=(reader,), start_new_session=True, env=environment,
            )
            os.close(reader)
            reader = -1
            stdout, stderr = process.communicate((json.dumps(request) + "\n").encode())
            if process.returncode != 0:
                raise ValueError("referee supervisor failed: " + stderr[-OUTPUT_TAIL_BYTES:].decode(
                    "utf-8", errors="replace"))
            result = json.loads(stdout)
            expected = {"result", "returncode", "tested", "stdout_tail", "stderr_tail",
                        "output_truncated"}
            if not isinstance(result, dict) or set(result) != expected:
                raise ValueError("invalid referee supervisor result")
            complete_receipt = {**receipt, **result}
            validate_receipt(complete_receipt)
            receipt = complete_receipt
        finally:
            if reader != -1:
                os.close(reader)
            os.close(writer)
            if process is not None:
                process.wait()
    except (OSError, ValueError) as error:
        receipt.update(result="error", stderr_tail=str(error))
    receipt["duration_seconds"] = time.monotonic() - started
    if receipt["result"] != "pass":
        raise LandingRefusedError(
            "candidate referee did not pass", reason="referee_" + receipt["result"],
            paths=list(paths), referee_receipt=receipt,
        )
    return receipt
