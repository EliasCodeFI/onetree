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


def test_status_human_and_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["status"]) == 0
    out = capsys.readouterr()
    assert __version__ in out.out
    assert out.err == ""

    code, payload = _json_call(capsys, ["status"])
    assert code == 0
    assert payload == {
        "name": "onetree",
        "ok": True,
        "phase": "gate-1-round-a",
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
    assert "DIVERGED on 1 record(s)" in capsys.readouterr().out

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
