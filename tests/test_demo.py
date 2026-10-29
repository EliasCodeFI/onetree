from __future__ import annotations

import hashlib
import io
import json
import multiprocessing
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from conftest import _CLEAN_PAYLOAD_GATE_SOURCE

from onetree import demo
from onetree.core import Coordinator
from onetree.errors import LandingRefusedError

_IDENTITY_NAMES = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_THREAD_ID")
_SESSION = "44444444-4444-4444-8444-444444444444"
_OTHER_SESSION = "55555555-5555-4555-8555-555555555555"


def invoke(output: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    environment = {name: value for name, value in os.environ.items() if name not in _IDENTITY_NAMES}
    return subprocess.run(
        [sys.executable, "-m", "onetree.demo", "--output", str(output), "--json", *extra],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env={
            **environment,
            "CODEX_THREAD_ID": _SESSION,
            "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
        },
    )


@pytest.fixture
def inputs(tmp_path: Path) -> tuple[Path, Path]:
    gate = Path(
        os.environ.get(
            "ONETREE_TEST_SHIPPED_GATE", str(Path.home() / ".claude/tools/commit_payload.py")
        )
    )
    if not gate.is_file():
        if "ONETREE_TEST_SHIPPED_GATE" in os.environ:
            pytest.fail(f"configured gate is missing: {gate}")
        pytest.skip("demo integration requires the admitted payload gate")
    message = tmp_path / "message.txt"
    message.write_text(
        "Demo sample\n\nCo-Authored-By: Codex <codex@example.invalid>\n"
        f"Codex-Session: {_SESSION}\n",
        encoding="utf-8",
    )
    return gate, message


def run_demo(output: Path, inputs: tuple[Path, Path], *extra: str) -> dict[str, Any]:
    gate, message = inputs
    result = invoke(output, "--commit-payload", str(gate), "--message-file", str(message), *extra)
    report: dict[str, Any] = json.loads(result.stdout)
    assert result.returncode == (0 if report["ok"] else 1), result.stderr
    return report


def test_existing_output_is_untouched(tmp_path: Path, inputs: tuple[Path, Path]) -> None:
    output = tmp_path / "existing"
    output.mkdir()
    marker = output / "keep"
    marker.write_bytes(b"unchanged")
    report = run_demo(output, inputs)
    assert not report["ok"]
    assert report["error"]["type"] == "FileExistsError"
    assert list(output.iterdir()) == [marker]
    assert marker.read_bytes() == b"unchanged"


def test_symlink_output_is_untouched(tmp_path: Path, inputs: tuple[Path, Path]) -> None:
    output = tmp_path / "link"
    target = tmp_path / "absent"
    output.symlink_to(target, target_is_directory=True)
    report = run_demo(output, inputs)
    assert not report["ok"]
    assert output.is_symlink()
    assert not target.exists()


def test_real_workers_land_with_conflict_evidence(
    tmp_path: Path,
    inputs: tuple[Path, Path],
) -> None:
    output = tmp_path / "sample"
    report = run_demo(output, inputs, "--workers", "2")
    assert report["ok"], report
    assert report["synthetic"] is True
    workers = report["workers"]
    assert len({worker["pid"] for worker in workers}) == 2
    assert all(worker["exitcode"] == 0 for worker in workers)
    assert workers[1]["conflict"]["type"] == "claim_conflict"
    assert len({worker["land"]["commit"] for worker in workers}) == 2
    for worker in workers:
        advisories = worker["land"]["gate_receipt"]["report"]["advisories"]
        assert any(
            f"Codex-Session equals CODEX_THREAD_ID ({_SESSION})" in advisory
            for advisory in advisories
        ), advisories
    assert report["wal"]["consistent"] is True
    assert report["wal"]["pending_intents"] == 0
    assert report["wal"]["active_claims"] == []
    assert json.loads((output / "report.json").read_text()) == report
    assert len(list((output / "workers").glob("*.json"))) == 2


def _admitted(gate: Path) -> dict[str, str]:
    return {"path": str(gate), "sha256": hashlib.sha256(gate.read_bytes()).hexdigest()}


def test_any_supplied_clean_gate_is_admitted_and_succeeds(
    tmp_path: Path,
    inputs: tuple[Path, Path],
) -> None:
    # docs/DEMO.md: the demo admits the gate it is given and does not check which one.
    gate = tmp_path / "stub_gate.py"
    gate.write_text(_CLEAN_PAYLOAD_GATE_SOURCE, encoding="utf-8")
    report = run_demo(tmp_path / "sample", (gate, inputs[1]), "--workers", "2")
    assert report["ok"], report
    assert report["gate"] == _admitted(gate)
    assert all("commit" in worker["land"] for worker in report["workers"])


def test_a_gate_without_a_clean_verdict_never_succeeds(
    tmp_path: Path,
    inputs: tuple[Path, Path],
) -> None:
    gate = tmp_path / "silent_gate.py"
    gate.write_text("raise SystemExit(0)\n", encoding="utf-8")
    report = run_demo(tmp_path / "sample", (gate, inputs[1]), "--workers", "2")
    assert not report["ok"]
    assert report["gate"] == _admitted(gate)
    errors = [worker["error"] for worker in report["workers"] if "error" in worker]
    assert errors
    assert {error["message"] for error in errors} == {
        "commit payload gate did not emit one UTF-8 JSON value"
    }
    assert all("land" not in worker for worker in report["workers"])


def test_other_session_identity_is_refused_by_real_gate(
    tmp_path: Path,
    inputs: tuple[Path, Path],
) -> None:
    gate, message = inputs
    message.write_text(
        message.read_text(encoding="utf-8").replace(_SESSION, _OTHER_SESSION),
        encoding="utf-8",
    )
    report = run_demo(tmp_path / "sample", (gate, message), "--workers", "2")
    assert not report["ok"]
    errors = [worker["error"] for worker in report["workers"] if "error" in worker]
    assert errors
    for error in errors:
        assert error["reason"] == "payload_gate_refused"
        assert [row["kind"] for row in error["findings"]] == ["identity-other-session"]
    assert all("land" not in worker for worker in report["workers"])
    events = report["wal"]["events"]
    assert not [event for event in events if event["event"] in {"land_intent", "landed"}]
    assert any(
        event["event"] == "refused" and event["data"].get("reason") == "payload_gate_refused"
        for event in events
    )


def test_missing_gate_does_not_create_output(tmp_path: Path, inputs: tuple[Path, Path]) -> None:
    output = tmp_path / "sample"
    report = run_demo(output, (tmp_path / "missing", inputs[1]))
    assert not report["ok"]
    assert not output.exists()


@pytest.mark.parametrize(
    "args", [("--workers", "1"), ("--timeout", "nan"), ("--timeout", "0"), ("--workers", "17")]
)
def test_invalid_limits_are_json_failures(
    tmp_path: Path,
    inputs: tuple[Path, Path],
    args: tuple[str, str],
) -> None:
    output = tmp_path / "sample"
    report = run_demo(output, inputs, *args)
    assert not report["ok"]
    assert not output.exists()


def test_argument_error_is_json(tmp_path: Path) -> None:
    result = invoke(tmp_path / "sample")
    assert result.returncode == 1
    assert json.loads(result.stdout)["error"]["type"] == "ValueError"


def test_deadline_retains_failed_evidence(tmp_path: Path, inputs: tuple[Path, Path]) -> None:
    output = tmp_path / "sample"
    report = run_demo(output, inputs, "--timeout", "0.000001")
    assert not report["ok"]
    assert report["error"]["type"] == "TimeoutError"
    assert all(worker["exitcode"] is not None for worker in report["workers"])
    assert json.loads((output / "report.json").read_text()) == report


def test_sample_violation_is_refused_by_real_referee(
    tmp_path: Path,
    inputs: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "sample"
    output.mkdir()
    gate, message = inputs
    demo._setup(output, gate, 2, 30)
    for name in _IDENTITY_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CODEX_THREAD_ID", _SESSION)
    coordinator = Coordinator(output / "repo", commit_payload=gate)
    session = coordinator.register_session("bad-sample", pid=os.getpid())
    path = output / "repo/worker-0.txt"
    coordinator.claim("bad-sample", [str(path)])
    path.write_text("invalid sample\n", encoding="utf-8")
    coordinator.repo.git("add", "--", path.name, index=session.index_path)
    before = coordinator.repo.git_text("rev-parse", "HEAD")
    with pytest.raises(LandingRefusedError) as caught:
        coordinator.land("bad-sample", message=message.read_text())
    assert caught.value.details["reason"] == "referee_red"
    assert coordinator.repo.git_text("rev-parse", "HEAD") == before
    events = coordinator.inspect_wal().events
    refusal = [event for event in events if event.kind == "refused"][-1]
    assert refusal.data["reason"] == "referee_red"


def test_closed_worker_channel_cannot_mean_success() -> None:
    parent, child = multiprocessing.get_context("spawn").Pipe()
    child.close()
    try:
        with pytest.raises(demo.DemoError, match="without landed evidence"):
            demo._collect([parent], [{}], "landed", time.monotonic() + 1, io.StringIO())
    finally:
        parent.close()


def test_mismatched_worker_phase_is_rejected() -> None:
    parent, child = multiprocessing.get_context("spawn").Pipe()
    try:
        child.send({"phase": "ready"})
        with pytest.raises(demo.DemoError, match="unexpected response"):
            demo._collect([parent], [{}], "landed", time.monotonic() + 1, io.StringIO())
    finally:
        parent.close()
        child.close()


def test_missing_worker_result_cannot_mean_success(
    tmp_path: Path,
    inputs: tuple[Path, Path],
) -> None:
    output = tmp_path / "sample"
    report = run_demo(output, inputs, "--workers", "2")
    assert report["ok"], report
    records = report["workers"]
    records[1].pop("phase")
    with pytest.raises(demo.DemoError, match="did not finish"):
        demo._verify(Coordinator(output / "repo"), records)


def test_commit_substitution_is_not_completion_evidence(
    tmp_path: Path,
    inputs: tuple[Path, Path],
) -> None:
    output = tmp_path / "sample"
    report = run_demo(output, inputs, "--workers", "2")
    assert report["ok"], report
    records = report["workers"]
    records[0]["land"]["intent"] = "not-the-recorded-intent"
    with pytest.raises(demo.DemoError, match="paired WAL evidence"):
        demo._verify(Coordinator(output / "repo"), records)
