from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from onetree import git as git_module
from onetree.errors import PayloadGateError

_SOURCE_PIN = git_module.PAYLOAD_GATE_SOURCE_SHA256


@pytest.fixture(autouse=True)
def clean_commit_payload_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ONETREE_COMMIT_PAYLOAD", raising=False)
    monkeypatch.setattr(git_module, "PAYLOAD_GATE_SOURCE_SHA256", _SOURCE_PIN)


def test_complete_published_guard_commit_through_real_consumer(tmp_path: Path) -> None:
    source = os.environ.get("ONETREE_TEST_GUARD_REPOSITORY")
    revision = os.environ.get("ONETREE_TEST_GUARD_REVISION")
    output = os.environ.get("ONETREE_TEST_GUARD_MEASUREMENT")
    configured_gate = os.environ.get("ONETREE_TEST_ESTATE_GATE")
    if not all((source, revision, output, configured_gate)):
        pytest.skip("requires published guard repository/revision, gate and measurement path")
    assert source is not None
    assert revision is not None
    assert output is not None
    assert configured_gate is not None
    gate = Path(configured_gate).resolve(strict=True)
    snapshot = tmp_path / "guard-repository"
    git = shutil.which("git")
    assert git is not None
    subprocess.run(
        [git, "clone", "--no-checkout", "--no-hardlinks", source, str(snapshot)],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        [git, "-C", str(snapshot), "checkout", "--detach", revision],
        check=True, capture_output=True, text=True,
    )
    paths = subprocess.run(
        [git, "-C", str(snapshot), "diff-tree", "--no-commit-id", "--name-only", "-r", revision],
        check=True, capture_output=True, text=True,
    ).stdout.splitlines()
    assert "hooks/guard.js" in paths
    assert "hooks/guard.test.mjs" in paths
    repository = git_module.Repository(snapshot)
    observation: dict[str, object] = {
        "consumer_implementation": str(Path(git_module.__file__).resolve()),
        "consumer_sha256": hashlib.sha256(Path(git_module.__file__).read_bytes()).hexdigest(),
        "producer": str(gate),
        "producer_sha256": hashlib.sha256(gate.read_bytes()).hexdigest(),
        "source_repository": source,
        "candidate": revision,
        "expected_paths": paths,
        "budget_seconds": git_module.PAYLOAD_GATE_TIMEOUT_SECONDS,
        "executing_native_session": os.environ.get("CODEX_THREAD_ID"),
    }
    receipt = None
    started = time.monotonic()
    try:
        receipt = repository.audit_commit_payload(revision, paths, gate)
        observation.update(outcome="accepted", receipt=receipt)
    except PayloadGateError as error:
        observation.update(outcome="refused", error=str(error), details=error.details)
    finally:
        observation["elapsed_seconds"] = time.monotonic() - started
        Path(output).write_text(json.dumps(observation, indent=2) + "\n", encoding="utf-8")
    assert receipt is not None, observation
