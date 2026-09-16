from __future__ import annotations

import json
import os
from typing import Any

import pytest
from conftest import GitFixture

from onetree.cli import main
from onetree.core import Coordinator


def _claims(git_repo: GitFixture) -> tuple[str, str, str]:
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid())
    coordinator.register_session("beta", pid=os.getpid())
    owned = str(git_repo.root / "a.txt")
    blocked = str(git_repo.root / "b.txt")
    clear = str(git_repo.root / "clear.txt")
    git_repo.write("clear.txt", "unclaimed\n")
    coordinator.claim("alpha", [owned])
    coordinator.claim("beta", [blocked])
    return owned, blocked, clear


def _call(
    git_repo: GitFixture,
    capsys: pytest.CaptureFixture[str],
    paths: list[str],
) -> tuple[int, dict[str, Any]]:
    code = main(["check", *paths, "--repo", str(git_repo.root), "--session", "alpha", "--json"])
    output = capsys.readouterr()
    assert output.err == ""
    assert output.out.count("\n") == 1
    payload = json.loads(output.out)
    assert isinstance(payload, dict)
    return code, payload


@pytest.mark.parametrize("reverse", [False, True])
def test_batch_checks_every_path_in_input_order(
    git_repo: GitFixture, capsys: pytest.CaptureFixture[str], reverse: bool
) -> None:
    owned, blocked, clear = _claims(git_repo)
    paths = [blocked, clear, owned]
    if reverse:
        paths.reverse()
    code, payload = _call(git_repo, capsys, paths)
    assert code == 1
    assert payload["ok"] is True
    assert payload["blocked"] is True
    rows = payload["checks"]
    assert [row["input"] for row in rows] == paths
    by_input = {row["input"]: row for row in rows}
    assert all(row["ok"] for row in rows)
    assert by_input[blocked]["check"]["blocked"] is True
    assert by_input[blocked]["check"]["holders"][0]["actor"] == "beta"
    assert by_input[owned]["check"]["blocked"] is False
    assert by_input[owned]["check"]["owned"][0]["actor"] == "alpha"
    assert by_input[clear]["check"] == {
        "path": "clear.txt", "blocked": False, "holders": [], "owned": []
    }


@pytest.mark.parametrize("missing_index", [0, 1, 2])
def test_missing_path_is_a_typed_error_without_a_phantom_clear_row(
    git_repo: GitFixture, capsys: pytest.CaptureFixture[str], missing_index: int
) -> None:
    _, blocked, clear = _claims(git_repo)
    missing = str(git_repo.root / "absent.txt")
    paths = [blocked, clear]
    paths.insert(missing_index, missing)
    code, payload = _call(git_repo, capsys, paths)
    assert code == 2
    assert payload["ok"] is False
    assert payload["blocked"] is True
    rows = payload["checks"]
    assert [row["input"] for row in rows] == paths
    invalid = rows[missing_index]
    assert invalid["ok"] is False
    assert invalid["error"]["type"] == "invalid_path"
    assert "check" not in invalid
    valid = {row["input"]: row["check"] for row in rows if row["ok"]}
    assert valid[blocked]["blocked"] is True
    assert valid[clear]["blocked"] is False


def test_clear_batch_and_duplicates_keep_each_requested_row(
    git_repo: GitFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    owned, _, clear = _claims(git_repo)
    code, payload = _call(git_repo, capsys, [owned, clear, clear])
    assert code == 0
    assert payload["ok"] is True
    assert payload["blocked"] is False
    assert [row["input"] for row in payload["checks"]] == [owned, clear, clear]


def test_single_path_json_keeps_its_existing_shape_and_own_claims(
    git_repo: GitFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    owned, _, _ = _claims(git_repo)
    code, payload = _call(git_repo, capsys, [owned])
    assert code == 0
    assert set(payload) == {"ok", "check"}
    assert set(payload["check"]) == {"path", "blocked", "holders", "owned"}
    assert payload["check"]["owned"][0]["actor"] == "alpha"


def test_single_path_human_output_exposes_own_claim(
    git_repo: GitFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    owned, _, _ = _claims(git_repo)
    assert main(["check", owned, "--repo", str(git_repo.root), "--session", "alpha"]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert "OWNED" in output.out
    assert "alpha" in output.out
    assert "a.txt" in output.out


def test_human_batch_reports_each_verdict_and_escapes_path_linebreaks(
    git_repo: GitFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    owned, blocked, _ = _claims(git_repo)
    strange = str(git_repo.root / "line\nCLEAR forged.txt")
    git_repo.write("line\nCLEAR forged.txt", "unclaimed\n")
    missing = str(git_repo.root / "missing\nCLEAR fake.txt")
    assert main([
        "check", blocked, owned, strange, missing,
        "--repo", str(git_repo.root), "--session", "alpha",
    ]) == 2
    output = capsys.readouterr()
    assert output.err == ""
    rows = output.out.splitlines()
    assert len(rows) == 4
    assert rows[0].startswith("BLOCKED ")
    assert "beta" in rows[0]
    assert rows[1].startswith("OWNED ")
    assert "alpha" in rows[1]
    assert rows[2].startswith("CLEAR ")
    assert "\\n" in rows[2]
    assert rows[3].startswith("ERROR ")
    assert "invalid_path" in rows[3]


def test_batch_check_is_read_only(
    git_repo: GitFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    owned, blocked, clear = _claims(git_repo)
    before = git_repo.git("rev-parse", "refs/onetree/wal").stdout
    _call(git_repo, capsys, [owned, blocked, clear])
    assert git_repo.git("rev-parse", "refs/onetree/wal").stdout == before


def test_directory_check_exposes_both_peer_and_own_claims(
    git_repo: GitFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    _claims(git_repo)
    root = str(git_repo.root)
    code, payload = _call(git_repo, capsys, [root])
    assert code == 1
    assert payload["check"]["holders"][0]["actor"] == "beta"
    assert payload["check"]["owned"][0]["actor"] == "alpha"
    assert main(["check", root, "--repo", root, "--session", "alpha"]) == 1
    line = capsys.readouterr().out
    assert line.startswith("BLOCKED ")
    assert "beta" in line
    assert "owned by" in line
    assert "alpha" in line
