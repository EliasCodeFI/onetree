from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import ci_matrix
import pytest


def _uv(tmp_path: Path, output: str) -> Path:
    executable = tmp_path / "uv-fixture"
    executable.write_text(
        f"#!{sys.executable}\nimport sys\n"
        f"sys.stdout.write({output!r})\n", encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def _resolve(minor: str) -> ci_matrix.Interpreter:
    return ci_matrix.Interpreter(minor, sys.executable, f"{minor}.99")


@pytest.mark.parametrize("output", ["uv 999.0.0\n", "not a uv version\n"])
def test_plan_refuses_wrong_uv_before_creating_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], output: str,
) -> None:
    executable = _uv(tmp_path, output)
    run = tmp_path / "run"
    provenance = tmp_path / "provenance.json"
    rc = ci_matrix.main([
        "plan", "--run-root", str(run), "--provenance", str(provenance),
        "--source", str(tmp_path), "--home", str(tmp_path), "--uv", str(executable),
    ], resolve=_resolve)
    captured = capsys.readouterr()
    assert rc == 2
    assert "uv version" in captured.err
    assert captured.out == ""
    assert not run.exists()
    assert not provenance.exists()


@pytest.mark.parametrize("suffix", ["", " (fixture build)"])
def test_matching_uv_plan_and_setup_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], suffix: str,
) -> None:
    workflow = ci_matrix.parse_workflow(ci_matrix.WORKFLOW.read_text())
    executable = _uv(tmp_path, f"uv {workflow.uv_version}{suffix}\n")
    source = tmp_path / "source"
    source.mkdir()
    run = tmp_path / "run"
    rc = ci_matrix.main([
        "plan", "--run-root", str(run), "--provenance", str(tmp_path / "provenance.json"),
        "--source", str(source), "--home", str(tmp_path), "--uv", str(executable),
    ], resolve=_resolve)
    assert rc == 0
    plan = json.loads(capsys.readouterr().out)
    for gate in plan[:2]:
        result = subprocess.run(gate["cmd"], capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stdout + result.stderr
    assert (run / workflow.minors[0] / "stamps" / "2.pass").exists()


def test_changed_uv_after_plan_cannot_pass_setup(tmp_path: Path) -> None:
    workflow = ci_matrix.parse_workflow(ci_matrix.WORKFLOW.read_text())
    executable = _uv(tmp_path, "uv 999.0.0\n")
    source = tmp_path / "source"
    source.mkdir()
    run = tmp_path / "run"
    run.mkdir()
    (run / "run-id").write_text("fixture\n")
    plan, _ = ci_matrix.build_plan(
        workflow, source=source, run_root=run, run_id="fixture", home=str(tmp_path),
        uv=str(executable), extras=[], resolve=_resolve,
    )
    checkout = subprocess.run(plan[0]["cmd"], capture_output=True, text=True, check=False)
    assert checkout.returncode == 0, checkout.stdout + checkout.stderr
    setup = subprocess.run(plan[1]["cmd"], capture_output=True, text=True, check=False)
    assert setup.returncode != 0
    leg = run / workflow.minors[0]
    assert "uv version" in (leg / "logs" / "2.log").read_text()
    assert not (leg / "stamps" / "2.pass").exists()
    next_step = subprocess.run(plan[2]["cmd"], capture_output=True, text=True, check=False)
    assert next_step.returncode == ci_matrix.NOT_RUN
