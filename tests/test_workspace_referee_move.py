from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from conftest import _GIT_EXECUTABLE, GitFixture
from test_referee import configure, staged

from onetree.core import Coordinator
from onetree.errors import LandingRefusedError, WalDivergedError


@pytest.mark.parametrize("external_move", [False, True])
def test_protected_target_movement_during_referee_execution(
    git_repo: GitFixture, tmp_path: Path, external_move: bool,
) -> None:
    external_index = tmp_path / "external.index"
    git_repo.git("read-tree", git_repo.base, index=external_index)
    git_repo.write("b.txt", "outside broker\n")
    git_repo.stage(external_index, "b.txt")
    external = git_repo.raw_commit(external_index, git_repo.base, "external candidate")
    git_repo.write("b.txt", "b0\n")
    assert external != git_repo.base
    assert git_repo.oid() == git_repo.base
    marker = tmp_path / "referee-executions.jsonl"
    configure(git_repo, f"""
import json
import os
import subprocess
from pathlib import Path

git = {_GIT_EXECUTABLE!r}
shared = {str(git_repo.root)!r}
def read(*args):
    return subprocess.run([git, *args], check=True, capture_output=True,
                          text=True).stdout.strip()
candidate = read('rev-parse', 'HEAD')
before = read('-C', shared, 'rev-parse', 'refs/heads/main')
assert Path('a.txt').read_text() == 'alpha\\n'
assert Path('b.txt').read_text() == 'b0\\n'
if {external_move!r}:
    subprocess.run([git, '-C', shared, 'update-ref', 'refs/heads/main',
                    {external!r}, {git_repo.base!r}], check=True)
after = read('-C', shared, 'rev-parse', 'refs/heads/main')
with Path({str(marker)!r}).open('a') as stream:
    stream.write(json.dumps(dict(pid=os.getpid(), candidate=candidate,
                                 before=before, after=after)) + '\\n')
"""
    )
    coordinator = Coordinator(git_repo.root)
    coordinator.protect_workspace("operator")
    staged(git_repo)
    if external_move:
        with pytest.raises(LandingRefusedError) as caught:
            coordinator.land("alpha")
        assert caught.value.details["reason"] == "workspace_changed"
        assert caught.value.details["paths"] == ["a.txt"]
        assert git_repo.oid() == external
        assert git_repo.git("show", "HEAD:a.txt").stdout == "a0\n"
        assert git_repo.git("show", "HEAD:b.txt").stdout == "outside broker\n"
        report = coordinator.inspect_wal()
        assert not report.consistent
        assert any(item.kind == "workspace_changed" for item in report.divergences)
        assert report.events[-1].kind == "refused"
        assert report.events[-1].data["reason"] == "workspace_changed"
        assert not any(event.kind == "land_intent" for event in report.events)
        with pytest.raises(WalDivergedError) as recovery:
            coordinator.recover()
        assert any(item["kind"] == "workspace_changed"
                   for item in recovery.value.details["divergences"])
        assert git_repo.oid() == external
    else:
        landed = coordinator.land("alpha")
        assert landed.commit == git_repo.oid()
        assert landed.referee_receipt["result"] == "pass"
        assert landed.referee_receipt["baseline"] == git_repo.base
        assert git_repo.git("show", "HEAD:a.txt").stdout == "alpha\n"
        assert git_repo.git("show", "HEAD:b.txt").stdout == "b0\n"
        assert coordinator.inspect_wal().consistent

    runs = [json.loads(line) for line in marker.read_text().splitlines()]
    assert len(runs) == 1
    execution = runs[0]
    assert execution["pid"] != os.getpid()
    assert execution["before"] == git_repo.base
    assert execution["after"] == (external if external_move else git_repo.base)
    candidate = execution["candidate"]
    assert git_repo.oid(candidate + "^") == git_repo.base
    assert git_repo.git("show", candidate + ":a.txt").stdout == "alpha\n"
    assert git_repo.git("show", candidate + ":b.txt").stdout == "b0\n"
    if external_move:
        assert candidate != git_repo.oid()
    else:
        assert candidate == git_repo.oid()
