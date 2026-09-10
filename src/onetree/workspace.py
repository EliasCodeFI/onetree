from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Mapping, Sequence
from typing import Any

from onetree import referee
from onetree.errors import GitError
from onetree.git import Repository


def _log(repo: Repository, reference: str) -> bytes:
    path = repo.git_path("logs/" + reference)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("reflog is not a regular file")
        content = stream.read()
    if not content or not content.endswith(b"\n"):
        raise ValueError("reflog is absent or incomplete")
    return content


def _enabled(repo: Repository) -> bool:
    setting = repo.git("config", "--bool", "--get", "core.logAllRefUpdates", check=False)
    return repo.ref_format == "files" and setting.returncode == 0 and setting.stdout == b"true\n"


def baseline(repo: Repository) -> dict[str, Any]:
    if not _enabled(repo):
        raise ValueError("workspace protection requires files refs and explicit reflog enablement")
    target = repo.head_ref()
    tip = repo.require_commit(target)
    logs = {}
    for reference in ("HEAD", target):
        content = _log(repo, reference)
        logs[reference] = {"size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    return {"target": target, "tip": tip, "logs": logs,
            "referee": list(referee.configuration(repo))}


def validate(value: Any, repo: Repository) -> None:
    if not isinstance(value, dict) or set(value) != {"target", "tip", "logs", "referee"}:
        raise ValueError("invalid workspace baseline")
    if not isinstance(value["target"], str) or not isinstance(value["tip"], str):
        raise ValueError("invalid workspace object identity")
    target = repo.validate_branch_ref(value["target"])
    repo.validate_oid(value["tip"])
    configured = value["referee"]
    if (not isinstance(configured, list) or len(configured) != 2
            or (configured[0] is not None and (
                not isinstance(configured[0], str) or not configured[0].strip()))
            or not referee.valid_seconds(configured[1])):
        raise ValueError("invalid protected referee configuration")
    logs = value["logs"]
    if not isinstance(logs, dict) or set(logs) != {"HEAD", target}:
        raise ValueError("workspace baseline log set differs")
    for checkpoint in logs.values():
        if not isinstance(checkpoint, dict) or set(checkpoint) != {"size", "sha256"}:
            raise ValueError("invalid reflog checkpoint")
        if type(checkpoint["size"]) is not int or checkpoint["size"] <= 0:
            raise ValueError("invalid reflog checkpoint size")
        digest = checkpoint["sha256"]
        if not isinstance(digest, str) or len(digest) != 64 or any(
            char not in "0123456789abcdef" for char in digest
        ):
            raise ValueError("invalid reflog checkpoint digest")


def violation(
    repo: Repository, epoch: Mapping[str, Any], transitions: Sequence[tuple[str, str]],
) -> str | None:
    """Name what changed in the protected workspace, or None when nothing did.

    An unreadable referee configuration is not a workspace change: it propagates as
    the referee's own typed refusal (`referee_config`) so the operator fixes the
    setting instead of being sent to baseline reconciliation.
    """
    try:
        if not _enabled(repo):
            return "workspace reflog evidence is unavailable or disabled"
    except GitError as error:
        return "workspace observation failed: " + str(error)
    if list(referee.configuration(repo)) != epoch["referee"]:
        return "protected referee configuration changed"
    try:
        if repo.symbolic_head() != epoch["target"]:
            return "shared HEAD changed branch or became detached"
        observed = repo.require_commit(str(epoch["target"]))
        for reference, checkpoint in epoch["logs"].items():
            content = _log(repo, reference)
            offset = checkpoint["size"]
            if len(content) < offset or hashlib.sha256(content[:offset]).hexdigest() != checkpoint[
                "sha256"
            ]:
                return "workspace reflog continuity was lost"
            tip = epoch["tip"]
            cursor = 0
            for line in content[offset:].splitlines():
                fields = line.split(b" ", 2)
                if len(fields) != 3:
                    return "workspace reflog has a malformed transition"
                old, new = (item.decode("ascii", errors="strict") for item in fields[:2])
                if old != tip or old == new:
                    return "workspace reflog contains an unexplained transition"
                while cursor < len(transitions) and transitions[cursor] != (old, new):
                    cursor += 1
                if cursor == len(transitions):
                    return "workspace changed outside a recorded broker intent"
                cursor += 1
                tip = new
            if tip != observed:
                return "workspace target moved without continuous reflog evidence"
    except (OSError, UnicodeError, ValueError, GitError) as error:
        return "workspace observation failed: " + str(error)
    return None
