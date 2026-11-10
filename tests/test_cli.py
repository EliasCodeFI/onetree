"""CLI contract: every Round A command has single-object JSON output."""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from conftest import GitFixture

from onetree import __version__, core
from onetree.cli import main


def _json_call(
    capsys: pytest.CaptureFixture[str], argv: Sequence[str]
) -> tuple[int, dict[str, Any]]:
    code = main([*argv, "--json"])
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.count("\n") == 1
    payload = json.loads(captured.out)
    assert isinstance(payload, dict)
    return code, payload


@pytest.mark.parametrize("boundary", ["grafts", "shallow"])
@pytest.mark.parametrize("with_wal", [False, True])
def test_repository_boundary_reports_audit_target_findings(
    git_repo: GitFixture, capsys: pytest.CaptureFixture[str], boundary: str, with_wal: bool
) -> None:
    coordinator = core.Coordinator(git_repo.root)
    if with_wal:
        coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    tip = git_repo.external_commit("b.txt", "ordinary tip\n", "ordinary tip")
    git_repo.git("branch", "other", tip)
    command = ["wal", "--repo", str(git_repo.root)]
    code, clean = _json_call(capsys, command)
    assert code == 0
    assert clean["wal"]["consistent"] is True
    wal_before = coordinator.wal.head()
    refs_before = git_repo.git("show-ref").stdout

    if boundary == "grafts":
        marker = coordinator.repo.git_dir / "info" / "grafts"
        marker.parent.mkdir(exist_ok=True)
        marker.write_text(f"{tip} {git_repo.base}\n", encoding="ascii")
        assert coordinator.repo.nonempty_graft_file() == marker
        kind = "legacy_grafts"
    else:
        coordinator.repo.git_path("shallow").write_text(tip + "\n", encoding="ascii")
        kind = "shallow_history"

    code, document = _json_call(capsys, command)
    repeat_code, repeated = _json_call(capsys, command)
    assert code == repeat_code == 1
    assert document == repeated
    assert document["wal"]["consistent"] is False
    rows = document["wal"]["divergences"]
    assert len(rows) == 3
    assert {row["target"] for row in rows} == {"HEAD", "refs/heads/main", "refs/heads/other"}
    assert {row["kind"] for row in rows} == {kind}
    assert all(set(row) == {"kind", "target", "detail", "commit", "operation"} for row in rows)
    assert all(isinstance(row["target"], str) for row in rows)
    assert main(command) == 1
    human = capsys.readouterr()
    assert human.err == ""
    assert coordinator.wal.head() == wal_before
    assert git_repo.git("show-ref").stdout == refs_before
    assert "DIVERGED on 3 audit-target finding(s)" in human.out


def test_status_human_and_json(capsys: pytest.CaptureFixture[str]) -> None:
    """Known bad: base 0adeed777bc67580ad3b560cad5c966564118b4d reports the old phase."""
    assert main(["status"]) == 0
    out = capsys.readouterr()
    assert __version__ in out.out
    assert out.err == ""

    code, payload = _json_call(capsys, ["status"])
    assert code == 0
    assert payload == {
        "name": "onetree",
        "ok": True,
        "phase": "gate-3-dogfood",
        "version": __version__,
    }


def test_round_a_cli_json_lifecycle(
    git_repo: GitFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    common = ["--repo", str(git_repo.root), "--session", "alpha"]
    code, session = _json_call(
        capsys,
        ["session", *common, "--pid", str(os.getpid()), "--ttl", "300"],
    )
    assert code == 0
    index = session["session"]["index"]

    code, invalid = _json_call(capsys, ["check", "a.txt", *common])
    assert code == 2
    assert invalid["error"]["type"] == "invalid_path"

    absolute = str(git_repo.root / "a.txt")
    for command in (["claim", absolute, *common], ["check", absolute, *common]):
        code, payload = _json_call(capsys, command)
        assert code == 0
        assert payload["ok"] is True

    beta = ["--repo", str(git_repo.root), "--session", "beta"]
    code, _ = _json_call(
        capsys,
        ["session", *beta, "--pid", str(os.getpid()), "--ttl", "300"],
    )
    assert code == 0
    code, blocked = _json_call(capsys, ["check", absolute, *beta])
    assert code == 1
    assert blocked["check"]["holders"][0]["actor"] == "alpha"

    git_repo.write("a.txt", "cli payload\n")
    git_repo.stage(Path(index), "a.txt")
    code, landed = _json_call(capsys, ["land", *common, "-m", "CLI land"])
    assert code == 0
    assert landed["land"]["commit"] == git_repo.oid()

    code, wal = _json_call(capsys, ["wal", "--repo", str(git_repo.root)])
    assert code == 0
    assert wal["wal"]["consistent"] is True

    code, recovery = _json_call(
        capsys,
        ["wal", "--repo", str(git_repo.root), "--recover"],
    )
    assert code == 0
    assert recovery["recovery"]["pending"] == 0

    code, released = _json_call(capsys, ["release", *common])
    assert code == 0
    assert released["release"]["released"] == ["a.txt"]

    code, gc = _json_call(capsys, ["gc", *common])
    assert code == 0
    assert gc["gc"]["event"] == "gc"


def test_cli_reports_divergence_liveness_warnings_and_swept_residue(
    git_repo: GitFixture,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    common = ["--repo", str(git_repo.root), "--session", "alpha"]
    monkeypatch.setattr(core, "_pid_start", lambda pid: None)
    code, session = _json_call(capsys, ["session", *common, "--pid", str(os.getpid())])
    assert code == 0
    assert session["session"]["warnings"] == [core.DEGRADED_LIVENESS]

    assert main(["session", *common, "--pid", str(os.getpid())]) == 0
    human = capsys.readouterr()
    assert f"[warning: {core.DEGRADED_LIVENESS}]" in human.out
    monkeypatch.undo()

    absolute = str(git_repo.root / "a.txt")
    assert _json_call(capsys, ["claim", absolute, *common])[0] == 0
    git_repo.write("a.txt", "diverging payload\n")
    git_repo.stage(Path(session["session"]["index"]), "a.txt")
    code, landed = _json_call(capsys, ["land", *common])
    assert code == 0
    assert landed["land"]["intent"]

    # Roll the branch back behind the recorded landing.
    git_repo.git("update-ref", "refs/heads/main", git_repo.base, landed["land"]["commit"])
    code, wal = _json_call(capsys, ["wal", "--repo", str(git_repo.root)])
    assert code == 1
    assert wal["wal"]["consistent"] is False
    assert wal["wal"]["divergences"][0]["kind"] == "landed_not_on_target"
    assert wal["wal"]["ref_locks"] == []

    assert main(["wal", "--repo", str(git_repo.root)]) == 1
    assert "DIVERGED on 1 audit-target finding(s)" in capsys.readouterr().out

    code, failed = _json_call(capsys, ["wal", "--repo", str(git_repo.root), "--recover"])
    assert code == 2
    assert failed["error"]["type"] == "wal_diverged"

    git_repo.git("update-ref", "refs/heads/main", landed["land"]["commit"], git_repo.base)
    code, gc = _json_call(capsys, ["gc", *common])
    assert code == 0
    assert gc["gc"]["removed_temp_indexes"] == 0
    code, recovery = _json_call(capsys, ["wal", "--repo", str(git_repo.root), "--recover"])
    assert code == 0
    assert (recovery["recovery"]["repaired"], recovery["recovery"]["refused"]) == (0, 0)
    assert recovery["recovery"]["locks"] == []

    # Residue is reported to the human reader too: it is what makes the next
    # land refuse, and a JSON-only report is one an operator never sees.
    repository = core.Coordinator(git_repo.root).repo
    lock = repository.ref_lock_path(core.WAL_REF)
    lock.write_text("", encoding="utf-8")
    residue = repository.temp_index_dir() / "index-1-1-0123456789ab"
    residue.parent.mkdir(parents=True, exist_ok=True)
    residue.write_text("", encoding="utf-8")
    assert main(["wal", "--repo", str(git_repo.root)]) == 0
    line = capsys.readouterr().out
    assert "1 ref lock(s) held" in line
    assert "1 temp index(es) present" in line
    lock.unlink()
    residue.unlink()


def test_unknown_command_is_refused() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["no-such-command"])
    assert exc_info.value.code == 2
