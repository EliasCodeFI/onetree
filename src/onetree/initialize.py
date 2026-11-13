"""`onetree init`: make a repository ready for several agents in one command."""

from __future__ import annotations

import os
import shlex
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from onetree import referee
from onetree.core import Coordinator
from onetree.errors import LandingRefusedError
from onetree.git import Repository
from onetree.wal import CACHE_CONFIG_KEY

STANDARD_GATE = Path(__file__).with_name("standard_gate.py")


def standard_gate_path(repo: Repository) -> Path:
    """Where `onetree init` keeps the copy of the standard gate it admits."""

    return repo.git_dir / "onetree" / "standard-gate" / "commit_payload.py"


@dataclass
class InitResult:
    root: str
    gate: str  # "standard", "updated", "admitted" (the --gate file) or "kept" (another gate)
    gate_path: str
    tests: str | None
    protected: bool
    notes: list[str] = field(default_factory=list)

    def payload(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "gate": self.gate,
            "gate_path": self.gate_path,
            "tests": self.tests,
            "wal_cache": True,
            "protected": self.protected,
            "notes": list(self.notes),
        }


def _install_standard_gate(repo: Repository) -> tuple[Path, bool]:
    """Write the packaged gate's bytes to the repository's copy; True when they changed."""

    target = standard_gate_path(repo)
    content = STANDARD_GATE.read_bytes()
    if target.is_file() and not target.is_symlink() and target.read_bytes() == content:
        return target, False
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".commit_payload.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(content)
        temporary.chmod(0o444)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target, True


def initialize(
    coordinator: Coordinator,
    actor: str,
    *,
    tests: str | None = None,
    gate: str | None = None,
    protect: bool = False,
) -> InitResult:
    """Admit a payload gate, name the test command, turn the log cache on, optionally protect.

    Without ``gate``, the standard gate is admitted unless the repository already admits a
    gate of its own, which is kept. Running it again updates the standard gate's copy to the
    installed onetree's version and changes only what is named.
    """

    repo = coordinator.repo
    notes: list[str] = []
    if tests is not None:
        try:
            words = shlex.split(tests)
        except ValueError as error:
            raise LandingRefusedError(
                f"the test command cannot be parsed: {error}", reason="referee_config", paths=[]
            ) from error
        if not words:
            raise LandingRefusedError(
                "the test command is empty", reason="referee_config", paths=[]
            )
    admitted = repo.payload_gate_admission()
    standard = str(standard_gate_path(repo))
    if gate is not None:
        admission = repo.admit_payload_gate(gate)
        gate_state, gate_path = "admitted", admission.path
    elif admitted is None or admitted.path == standard:
        path, changed = _install_standard_gate(repo)
        repo.admit_payload_gate(path)
        gate_state = "standard" if admitted is None else ("updated" if changed else "standard")
        gate_path = str(path)
    else:
        gate_state, gate_path = "kept", admitted.path
    if tests is not None:
        repo.git("config", "--local", "--replace-all", "onetree.referee", tests)
    repo.git("config", "--local", "--replace-all", CACHE_CONFIG_KEY, "true")
    configured, _ = referee.configuration(repo)
    if configured is None:
        notes.append("no test command: landings run without tests; add one with --test")
    protected = False
    if protect:
        try:
            coordinator.protect_workspace(actor)
        except LandingRefusedError as error:
            if error.details.get("reason") != "workspace_changed":
                raise
            epoch = coordinator._fold(coordinator.wal.read()).workspace_epoch_id
            if epoch is None:  # pragma: no cover - a refusal names a protected workspace
                raise
            coordinator.reconcile_workspace(actor, expected_epoch=epoch, reason="onetree init")
            notes.append("branch protection renewed for the new settings")
        protected = True
    return InitResult(str(repo.root), gate_state, gate_path, configured, protected, notes)
