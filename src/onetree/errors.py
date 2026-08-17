"""Typed failures shared by the core and the command-line adapter."""

from __future__ import annotations

from typing import Any


class OnetreeError(RuntimeError):
    """A stable machine-readable refusal or operational error."""

    kind = "onetree_error"
    exit_code = 2

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.details = details

    def payload(self) -> dict[str, Any]:
        return {"type": self.kind, "message": str(self), **self.details}


class GitError(OnetreeError):
    """Git plumbing failed or returned malformed output."""

    kind = "git_error"


class IdentityError(OnetreeError):
    """A session identity is absent, ambiguous, or malformed."""

    kind = "invalid_identity"


class InvalidPathError(OnetreeError):
    """A claim/check path cannot be represented safely inside the repository."""

    kind = "invalid_path"


class WalCorruptError(OnetreeError):
    """The WAL ref is not a valid linear chain of immutable event commits."""

    kind = "wal_corrupt"


class ClaimConflictError(OnetreeError):
    """Another live session owns an overlapping path claim."""

    kind = "claim_conflict"
    exit_code = 1


class LandingRefusedError(OnetreeError):
    """The broker refused a candidate before moving the target ref."""

    kind = "land_refused"
    exit_code = 1


class ReleaseRefusedError(OnetreeError):
    """A release requested claims that the actor does not hold."""

    kind = "release_refused"
    exit_code = 1
