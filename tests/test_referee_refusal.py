from __future__ import annotations

from pathlib import Path

import pytest
from conftest import GitFixture
from test_cli import _json_call
from test_referee import configure, staged

from onetree.cli import main
from onetree.errors import LandingRefusedError
from onetree.git import Repository
from onetree.wal import WAL_REF


@pytest.mark.parametrize("caller", ["api", "json", "human"])
@pytest.mark.parametrize("locked", [False, True])
@pytest.mark.parametrize("result", ["red", "timeout"])
def test_referee_refusal_survives_recording_failure(
    git_repo: GitFixture, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    caller: str, locked: bool, result: str,
) -> None:
    marker = tmp_path / "referee-ran"
    lock = Repository(git_repo.root).ref_lock_path(WAL_REF)
    code = (
        "from pathlib import Path\n"
        f"with Path({str(marker)!r}).open('a') as stream:\n"
        "    stream.write('ran\\n')\n"
    )
    if locked:
        code += f"Path({str(lock)!r}).touch(); "
    if result == "red":
        code += "raise SystemExit(7)"
    else:
        code += "import time; time.sleep(20)"
        git_repo.git("config", "onetree.refereeTimeout", "0.5")
    configure(git_repo, code)
    coordinator = staged(git_repo)
    before = coordinator.wal.read().head
    arguments = ["land", "--repo", str(git_repo.root), "--session", "alpha"]
    if caller == "api":
        with pytest.raises(LandingRefusedError) as refused:
            coordinator.land("alpha")
        status = refused.value.exit_code
        error = refused.value.payload()
    elif caller == "json":
        status, payload = _json_call(capsys, arguments)
        error = payload["error"]
    else:
        status = main(arguments)
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "candidate referee" in captured.err
        if locked:
            assert "refusal recording failed (ref_locked)" in captured.err
            assert WAL_REF in captured.err
        else:
            assert "refusal recording failed" not in captured.err
        error = None
    assert status == 1
    assert marker.read_text() == "ran\n"
    assert lock.exists() is locked
    report = coordinator.inspect_wal()
    assert report.consistent
    assert not any(event.kind in {"land_intent", "landed"} for event in report.events)
    assert git_repo.oid() == git_repo.base
    refusals = [event for event in report.events if event.kind == "refused"]
    if locked:
        assert not refusals
        assert coordinator.wal.read().head == before
    else:
        assert len(refusals) == 1
        assert coordinator.wal.read().head != before
    if error is not None:
        assert error["type"] == "land_refused"
        assert error["reason"] == f"referee_{result}"
        receipt = error["referee_receipt"]
        assert receipt["result"] == result
        assert receipt["baseline"] == git_repo.base
        assert receipt["tested"] == [receipt["candidate"]]
        assert git_repo.git("show", receipt["candidate"] + ":a.txt").stdout == "alpha\n"
        if locked:
            assert error["recording_error"]["type"] == "ref_locked"
            assert error["recording_error"]["reference"] == WAL_REF
        else:
            assert "recording_error" not in error
            assert refusals[0].data["referee_receipt"] == receipt
