"""onetree's standard commit payload gate: the checks most repositories want, and no more.

    python -I -S commit_payload.py commit <commit> --repo <repository> --json

`onetree init` copies this file into the repository's git directory and admits that copy, so a
repository can land without writing a gate first. It refuses a commit that adds or changes a
file holding unresolved merge conflict markers, a private key, or more than 50 MiB. Every other
commit is CLEAN. It reads the commit with git, prints one JSON object and never writes to
stderr. Exit status: 0 CLEAN, 1 REFUSE, 2 DEGRADED (the commit could not be read).

Only the standard library is used, because the broker runs gates with ``-I -S``.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from typing import Any

MAX_BYTES = 50 * 1024 * 1024
_PRIVATE_KEY = re.compile(rb"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")
_EXIT = {"CLEAN": 0, "REFUSE": 1, "DEGRADED": 2}


def _marker(line: bytes, sign: bytes) -> bool:
    return line.startswith(sign * 7) and (len(line) == 7 or line[7:8] == b" ")


def has_conflict_markers(content: bytes) -> bool:
    """A start, a separator and an end marker line, in that order, as git writes them."""

    state = 0
    for raw in content.split(b"\n"):
        line = raw.rstrip(b"\r")
        if _marker(line, b"<"):
            state = 1
        elif state == 1 and line == b"=" * 7:
            state = 2
        elif state == 2 and _marker(line, b">"):
            return True
    return False


def _git(repository: str, *arguments: str) -> bytes:
    executable = shutil.which("git") or "git"
    completed = subprocess.run(  # noqa: S603 - fixed git argv, no shell
        [executable, "-C", repository, *arguments],
        capture_output=True,
        check=True,
    )
    return completed.stdout


def findings_for(path: str, content: bytes) -> list[dict[str, Any]]:
    """What the gate objects to in one file's new content."""

    found: list[dict[str, Any]] = []
    if len(content) > MAX_BYTES:
        found.append({"path": path, "check": "large_file", "bytes": len(content)})
        return found
    if b"\0" in content[:8192]:
        return found  # binary: the text checks do not apply
    if has_conflict_markers(content):
        found.append({"path": path, "check": "conflict_markers"})
    if _PRIVATE_KEY.search(content):
        found.append({"path": path, "check": "private_key"})
    return found


def audit(commit: str, repository: str) -> dict[str, Any]:
    report: dict[str, Any] = {
        "candidate": commit,
        "repository": repository,
        "staged_files": [],
        "findings": [],
        "degraded": [],
        "verdict": "CLEAN",
    }
    try:
        listed = _git(repository, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", commit)
        paths = sorted(item.decode("utf-8", "strict") for item in listed.split(b"\0") if item)
        report["staged_files"] = paths
        for path in paths:
            entry = _git(repository, "ls-tree", "-z", commit, "--", path).rstrip(b"\0")
            if not entry:
                continue  # deleted
            mode, kind, _ = entry.split(b"\t", 1)[0].split(b" ")
            if kind != b"blob" or mode == b"120000":
                continue  # a submodule or a symbolic link: no file content to read
            size = int(_git(repository, "cat-file", "-s", f"{commit}:{path}"))
            if size > MAX_BYTES:
                report["findings"].append({"path": path, "check": "large_file", "bytes": size})
                continue
            report["findings"].extend(
                findings_for(path, _git(repository, "cat-file", "blob", f"{commit}:{path}"))
            )
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        report["degraded"] = [{"reason": "the commit could not be read", "detail": str(error)}]
        report["findings"] = []
        report["verdict"] = "DEGRADED"
        return report
    if report["findings"]:
        report["verdict"] = "REFUSE"
    return report


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 5 or arguments[0] != "commit" or arguments[2] != "--repo" or (
        arguments[4] != "--json"
    ):
        report: dict[str, Any] = {
            "candidate": arguments[1] if len(arguments) > 1 else "",
            "repository": arguments[3] if len(arguments) > 3 else "",
            "staged_files": [],
            "findings": [],
            "degraded": [{"reason": "usage: commit <commit> --repo <repository> --json"}],
            "verdict": "DEGRADED",
        }
    else:
        report = audit(arguments[1], arguments[3])
    sys.stdout.write(json.dumps(report, sort_keys=True) + "\n")
    return _EXIT[report["verdict"]]


if __name__ == "__main__":
    raise SystemExit(main())
