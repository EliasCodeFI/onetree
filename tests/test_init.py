"""`onetree init` sets a repository up in one command, with a standard payload gate.

Known bad: main 0215e36, which has no `init` and ships no gate: a repository refuses every landing
until its operator writes and admits a gate of their own.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from conftest import GitFixture

from onetree.cli import main
from onetree.core import Coordinator
from onetree.errors import LandingRefusedError


def run(argv: Sequence[str]) -> int:
    try:
        return main(list(argv))
    except SystemExit as exit_:  # the parser refuses a command or option it does not know
        return int(exit_.code or 0)


def as_json(capsys: pytest.CaptureFixture[str], argv: Sequence[str]) -> tuple[int, dict[str, Any]]:
    code = run([*argv, "--json"])
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else {}


def config(git_repo: GitFixture, key: str) -> str:
    return git_repo.git("config", "--get", key, check=False).stdout.strip()


@pytest.fixture
def bare_repo(git_repo: GitFixture, monkeypatch: pytest.MonkeyPatch) -> GitFixture:
    """The fixture repository without its test gate: what a new user starts from."""

    monkeypatch.delenv("ONETREE_COMMIT_PAYLOAD", raising=False)
    git_repo.git("config", "--unset", "onetree.payloadGate")
    git_repo.git("config", "--unset", "onetree.payloadGateSha256")
    return git_repo


def test_init_admits_the_standard_gate_turns_the_cache_on_and_names_the_tests(
    bare_repo: GitFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = ["--repo", str(bare_repo.root)]
    assert run(["init", *repo]) == 0
    human = capsys.readouterr().out
    assert "payload gate: the standard gate" in human
    assert "tests: none" in human
    gate = Path(config(bare_repo, "onetree.payloadGate"))
    assert gate == bare_repo.root / ".git" / "onetree" / "standard-gate" / "commit_payload.py"
    assert (
        config(bare_repo, "onetree.payloadGateSha256")
        == hashlib.sha256(gate.read_bytes()).hexdigest()
    )
    assert config(bare_repo, "onetree.walCache") == "true"

    test_command = f"{sys.executable} -c pass"
    code, payload = as_json(capsys, ["init", *repo, "--test", test_command])
    assert code == 0, payload
    assert payload["init"]["gate"] == "standard"
    assert payload["init"]["tests"] == test_command
    assert config(bare_repo, "onetree.referee") == test_command

    gate.chmod(0o644)
    gate.write_text("# changed by hand\n", encoding="utf-8")
    code, payload = as_json(capsys, ["init", *repo])
    assert payload["init"]["gate"] == "updated"
    assert b"standard commit payload gate" in gate.read_bytes()


def test_the_standard_gate_lands_clean_work_and_refuses_conflicts_and_keys(
    bare_repo: GitFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(["init", "--repo", str(bare_repo.root), "--test", f"{sys.executable} -c pass"]) == 0
    capsys.readouterr()
    coordinator = Coordinator(bare_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    coordinator.claim("alpha", [str(bare_repo.root / "a.txt")])

    bare_repo.write("a.txt", "<<<<<<< ours\na1\n=======\na2\n>>>>>>> theirs\n")
    coordinator.stage_claimed("alpha")
    with pytest.raises(LandingRefusedError) as refused:
        coordinator.land("alpha", message="conflict")
    assert refused.value.details["reason"] == "payload_gate_refused"
    assert refused.value.details["findings"] == [{"check": "conflict_markers", "path": "a.txt"}]

    header = "-----BEGIN " + "OPENSSH PRIVATE" + " KEY-----"
    bare_repo.write("a.txt", f"{header}\nAAAA\n")
    coordinator.stage_claimed("alpha")
    with pytest.raises(LandingRefusedError) as refused:
        coordinator.land("alpha", message="key")
    assert refused.value.details["findings"] == [{"check": "private_key", "path": "a.txt"}]

    bare_repo.write("a.txt", "a1\n")
    coordinator.stage_claimed("alpha")
    landed = coordinator.land("alpha", message="clean")
    assert landed.referee_receipt is not None
    assert landed.referee_receipt["result"] == "pass"


def test_init_keeps_or_replaces_a_gate_of_the_operators_own(
    git_repo: GitFixture, capsys: pytest.CaptureFixture[str], clean_commit_payload_gate: Path
) -> None:
    repo = ["--repo", str(git_repo.root)]
    code, payload = as_json(capsys, ["init", *repo])
    assert code == 0, payload
    assert payload["init"]["gate"] == "kept"
    assert payload["init"]["gate_path"] == str(clean_commit_payload_gate.resolve())
    code, payload = as_json(capsys, ["init", *repo, "--gate", str(clean_commit_payload_gate)])
    assert payload["init"]["gate"] == "admitted"


def test_init_refuses_a_test_command_it_cannot_run(
    bare_repo: GitFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    for command in ("", "pytest 'unclosed"):
        code, payload = as_json(capsys, ["init", "--repo", str(bare_repo.root), "--test", command])
        assert code == 1, payload
        assert payload["error"]["reason"] == "referee_config"
    assert config(bare_repo, "onetree.payloadGate") == ""


def test_init_can_protect_the_branch_and_renews_the_protection(
    bare_repo: GitFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = ["--repo", str(bare_repo.root), "--protect"]
    code, payload = as_json(capsys, ["init", *repo])
    assert code == 0, payload
    assert payload["init"]["protected"] is True
    code, payload = as_json(capsys, ["init", *repo, "--test", f"{sys.executable} -c pass"])
    assert code == 0, payload
    assert "branch protection renewed for the new settings" in payload["init"]["notes"]


def test_the_standard_gate_script_answers_the_broker_protocol(
    git_repo: GitFixture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert importlib.util.find_spec("onetree.standard_gate") is not None
    gate = importlib.import_module("onetree.standard_gate")
    head = git_repo.external_commit("a.txt", "a1\n", "a change for the gate to read")

    assert gate.main(["commit", head, "--repo", str(git_repo.root), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["verdict"] == "CLEAN"
    assert report["staged_files"] == ["a.txt"]

    assert gate.main(["commit", "f" * 40, "--repo", str(git_repo.root), "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["verdict"] == "DEGRADED"
    assert gate.main(["commit", head]) == 2
    assert json.loads(capsys.readouterr().out)["degraded"][0]["reason"].startswith("usage")

    monkeypatch.setattr(gate, "MAX_BYTES", 2)
    assert gate.main(["commit", head, "--repo", str(git_repo.root), "--json"]) == 1
    findings = json.loads(capsys.readouterr().out)["findings"]
    assert {finding["check"] for finding in findings} == {"large_file"}
    assert gate.findings_for("x", b"\0" * 2) == []
    assert gate.findings_for("x", b"1234") == [{"path": "x", "check": "large_file", "bytes": 4}]
    assert gate.has_conflict_markers(b"<<<<<<<\r\nx\r\n=======\r\ny\r\n>>>>>>>\r\n")
    assert not gate.has_conflict_markers(b"Title\n=======\n")
