from __future__ import annotations

import os
from collections.abc import Callable

import pytest
from conftest import GitFixture
from test_cli import _json_call

from onetree.cli import main
from onetree.core import Coordinator
from onetree.errors import WalCorruptError


@pytest.mark.parametrize("shape", ["valid", "wrong_parent", "wrong_tree"])
@pytest.mark.parametrize("caller", ["register", "claim", "release", "json", "human"])
def test_writes_validate_adopted_git_semantics(
    git_repo: GitFixture,
    capsys: pytest.CaptureFixture[str],
    shape: str,
    caller: str,
) -> None:
    coordinator = Coordinator(git_repo.root)
    session = coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    coordinator.claim("alpha", [str(git_repo.root / "a.txt")])
    original_tree = coordinator.repo.git_text("show", "-s", "--format=%T", git_repo.base)
    git_repo.write("a.txt", "adopted payload\n")
    git_repo.stage(session.index_path, "a.txt")
    tree = coordinator.repo.write_tree(session.index_path)
    assert tree != original_tree
    operation = "e" * 32
    commit = coordinator.repo.commit_tree(
        tree,
        coordinator.repo.landing_message("adopted", operation, "f" * 32, "alpha"),
        (git_repo.base,),
    )
    assert coordinator.repo.update_ref("refs/heads/main", commit, git_repo.base)
    document = coordinator.wal.document(
        "adopted",
        "operator",
        {
            "operation": operation,
            "target": "refs/heads/main",
            "expected_head": (
                coordinator.repo.zero_oid if shape == "wrong_parent" else git_repo.base
            ),
            "commit": commit,
            "tree": original_tree if shape == "wrong_tree" else tree,
        },
    )
    before_forge = coordinator.wal.head()
    assert coordinator.wal.try_append((document,), before_forge) is not None
    before_write = coordinator.wal.head()
    if shape == "valid":
        assert coordinator.inspect_wal().consistent
    else:
        with pytest.raises(WalCorruptError, match="differs from WAL"):
            coordinator.inspect_wal()

    actions: dict[str, Callable[[], object]] = {
        "register": lambda: coordinator.register_session("beta", pid=os.getpid(), ttl=300),
        "claim": lambda: coordinator.claim("alpha", [str(git_repo.root / "b.txt")]),
        "release": lambda: coordinator.release("alpha", [str(git_repo.root / "a.txt")]),
    }
    if caller in actions:
        if shape == "valid":
            actions[caller]()
        else:
            with pytest.raises(WalCorruptError, match="differs from WAL"):
                actions[caller]()
    else:
        argv = ["session", "--repo", str(git_repo.root), "--session", "beta",
                "--pid", str(os.getpid()), "--ttl", "300"]
        if caller == "json":
            code, payload = _json_call(capsys, argv)
            assert code == (0 if shape == "valid" else 2)
            if shape != "valid":
                assert payload["error"]["type"] == "wal_corrupt"
                assert "differs from WAL" in payload["error"]["message"]
        else:
            code = main(argv)
            captured = capsys.readouterr()
            assert code == (0 if shape == "valid" else 2)
            if shape != "valid":
                assert "differs from WAL" in captured.err
                assert captured.out == ""
    assert git_repo.oid() == commit
    if shape != "valid":
        assert coordinator.wal.head() == before_write
    else:
        assert coordinator.wal.head() != before_write
        assert coordinator.inspect_wal().consistent
