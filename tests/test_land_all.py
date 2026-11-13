"""`onetree land -a` stages exactly the files the session holds, after moving the session to the
branch's tip; `onetree claims` shows who holds what.

Known bad: main 0215e36, which has neither: an agent stages by hand into a private index it has
to locate, and after a peer lands a file, landing that file again is refused as a payload
conflict because the session's base never moves.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from typing import Any

import pytest
from conftest import GitFixture

from onetree.cli import main
from onetree.core import Coordinator


def run(argv: Sequence[str]) -> int:
    try:
        return main(list(argv))
    except SystemExit as exit_:  # the parser refuses a command or option it does not know
        return int(exit_.code or 0)


def as_json(capsys: pytest.CaptureFixture[str], argv: Sequence[str]) -> tuple[int, dict[str, Any]]:
    code = run([*argv, "--json"])
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else {}


def test_land_all_takes_only_the_files_the_session_holds(
    git_repo: GitFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    coordinator.claim("alpha", [str(git_repo.root / "a.txt"), str(git_repo.root / "nested")])
    git_repo.write("a.txt", "a1\n")
    git_repo.write("b.txt", "a peer's unfinished edit\n")
    git_repo.write("nested/new.txt", "new\n")
    (git_repo.root / "nested" / "c.txt").unlink()

    code, payload = as_json(
        capsys,
        ["land", "-a", "-m", "alpha's work", "--repo", str(git_repo.root), "--session", "alpha"],
    )
    assert code == 0, payload
    assert sorted(payload["land"]["payload"]) == ["a.txt", "nested/c.txt", "nested/new.txt"]
    assert git_repo.changed_paths(payload["land"]["commit"]) == {
        "a.txt",
        "nested/c.txt",
        "nested/new.txt",
    }
    assert (git_repo.root / "b.txt").read_text(encoding="utf-8") == "a peer's unfinished edit\n"


def test_land_all_follows_the_branch_after_a_peer_lands(
    git_repo: GitFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    coordinator.register_session("beta", pid=os.getpid(), ttl=300)
    b_txt = str(git_repo.root / "b.txt")
    coordinator.claim("beta", [b_txt])
    git_repo.write("b.txt", "b1\n")
    beta = ["--repo", str(git_repo.root), "--session", "beta"]
    assert run(["land", "-a", "-m", "beta's change", *beta]) == 0
    assert run(["release", b_txt, *beta]) == 0
    capsys.readouterr()

    # alpha registered before beta landed; its base still has the old b.txt.
    coordinator.claim("alpha", [b_txt])
    git_repo.write("b.txt", "b1\nb2\n")
    code, payload = as_json(
        capsys,
        ["land", "-a", "-m", "alpha's change", "--repo", str(git_repo.root), "--session", "alpha"],
    )
    assert code == 0, payload
    assert git_repo.git("show", f"{payload['land']['commit']}:b.txt").stdout == "b1\nb2\n"
    assert git_repo.git("show", f"{payload['land']['commit']}~1:b.txt").stdout == "b1\n"
    assert [claim.actor for claim in coordinator.claims()] == ["alpha"]


def test_renewal_keeps_staged_changes_on_held_paths_only(git_repo: GitFixture) -> None:
    coordinator = Coordinator(git_repo.root)
    alpha = coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    coordinator.register_session("beta", pid=os.getpid(), ttl=300)
    assert coordinator.renew("alpha").generation == alpha.generation  # nothing moved
    coordinator.claim("alpha", [str(git_repo.root / "a.txt")])
    git_repo.write("a.txt", "a1\n")
    git_repo.write("b.txt", "not held\n")
    git_repo.stage(alpha.index_path, "a.txt", "b.txt")
    coordinator.claim("beta", [str(git_repo.root / "nested" / "c.txt")])
    git_repo.write("nested/c.txt", "c1\n")
    coordinator.stage_claimed("beta")
    coordinator.land("beta", message="beta")

    renewed = coordinator.renew("alpha")
    assert renewed.generation != alpha.generation
    assert renewed.base == git_repo.oid()
    staged = git_repo.git("diff", "--cached", "--name-only", "main", index=renewed.index_path)
    assert staged.stdout.split() == ["a.txt"]
    assert [claim.path for claim in coordinator.claims() if claim.actor == "alpha"] == ["a.txt"]


def test_land_all_with_nothing_held_or_changed_is_refused(
    git_repo: GitFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    git_repo.write(".gitignore", "*.log\n")
    git_repo.write("run.log", "ignored\n")
    coordinator.claim("alpha", [str(git_repo.root / "run.log")])
    code, payload = as_json(
        capsys, ["land", "-a", "-m", "nothing", "--repo", str(git_repo.root), "--session", "alpha"]
    )
    assert code == 1, payload
    assert payload["error"]["reason"] == "nothing_staged"


def test_claims_lists_holders_and_marks_the_callers_own(
    git_repo: GitFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = ["--repo", str(git_repo.root)]
    assert run(["claims", *repo]) == 0
    assert capsys.readouterr().out == "nobody holds a claim\n"
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    coordinator.register_session("beta", pid=os.getpid(), ttl=300)
    coordinator.claim("alpha", [str(git_repo.root / "a.txt")])
    coordinator.claim("beta", [str(git_repo.root / "b.txt")])

    assert run(["claims", *repo, "--session", "alpha"]) == 0
    assert capsys.readouterr().out == "a.txt  alpha  (you)\nb.txt  beta\n"
    code, payload = as_json(capsys, ["claims", *repo, "--session", "beta"])
    assert code == 0
    assert [(row["path"], row["actor"], row["mine"]) for row in payload["claims"]] == [
        ("a.txt", "alpha", False),
        ("b.txt", "beta", True),
    ]
