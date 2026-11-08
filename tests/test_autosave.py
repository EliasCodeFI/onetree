"""Autosave: a session's claimed-path bytes stay recoverable after path-form erasures.

The workspace guard cannot see `checkout -- <path>`, `restore`, `read-tree -u` or
`checkout-index` (tests/test_workspace_guard.py keeps that boundary). These tests
cover the other half: what a session saved under its claims comes back after such
an erasure, and what it did not save is reported as gone rather than invented.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import GitFixture
from test_cli import _json_call

from onetree import autosave
from onetree.autosave import AutosaveRefusedError, autosave_ref
from onetree.cli import main
from onetree.core import Coordinator
from onetree.errors import ClaimConflictError, GitError, IdentityError

ERASURES = ["checkout_path", "restore", "read_tree", "checkout_index"]


def _erase(git_repo: GitFixture, operation: str, relative: str) -> None:
    if operation == "checkout_path":
        git_repo.git("checkout", "--quiet", "--", relative)
    elif operation == "restore":
        git_repo.git("restore", relative)
    elif operation == "read_tree":
        git_repo.git("read-tree", "-u", "--reset", "HEAD")
    else:
        git_repo.git("checkout-index", "-f", "-a")


def _sessions(git_repo: GitFixture) -> Coordinator:
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid())
    coordinator.register_session("beta", pid=os.getpid())
    return coordinator


def _chain_blobs(git_repo: GitFixture, actor: str) -> set[str]:
    """Every blob reachable from the actor's autosave ref, as text."""
    listed = git_repo.git("rev-list", "--objects", autosave_ref(actor)).stdout.splitlines()
    blobs: set[str] = set()
    for oid in (line.split()[0] for line in listed):
        if git_repo.git("cat-file", "-t", oid).stdout.strip() == "blob":
            blobs.add(git_repo.git("cat-file", "blob", oid).stdout)
    return blobs


@pytest.mark.parametrize("operation", ERASURES)
def test_saved_peer_bytes_survive_each_path_form_erasure(
    git_repo: GitFixture, operation: str
) -> None:
    coordinator = _sessions(git_repo)
    coordinator.protect_workspace("operator")
    coordinator.claim("beta", [str(git_repo.root / "b.txt")])
    git_repo.write("b.txt", "peer dirty\n")
    saved = autosave.save(coordinator, "beta")
    assert saved.created
    assert saved.snapshot is not None

    _erase(git_repo, operation, "b.txt")
    assert (git_repo.root / "b.txt").read_text() == "b0\n"

    restored = autosave.restore(coordinator, "beta", str(git_repo.root / "b.txt"))
    assert restored.written
    assert restored.snapshot == saved.snapshot.commit
    assert (git_repo.root / "b.txt").read_text() == "peer dirty\n"
    # The autosave ref is outside the guarded reflogs: the workspace epoch still holds.
    assert coordinator._workspace_violation(coordinator._fold(coordinator.wal.read())) is None


def test_bytes_written_after_the_last_save_are_reported_not_recoverable(
    git_repo: GitFixture,
) -> None:
    coordinator = _sessions(git_repo)
    coordinator.claim("beta", [str(git_repo.root / "b.txt")])
    git_repo.write("b.txt", "saved v1\n")
    autosave.save(coordinator, "beta")
    git_repo.write("b.txt", "unsaved v2\n")

    # While the unsaved bytes still exist, restore refuses to overwrite them.
    with pytest.raises(AutosaveRefusedError) as caught:
        autosave.restore(coordinator, "beta", str(git_repo.root / "b.txt"))
    assert caught.value.details["reason"] == "unsaved_changes"
    assert (git_repo.root / "b.txt").read_text() == "unsaved v2\n"

    _erase(git_repo, "checkout_path", "b.txt")
    restored = autosave.restore(coordinator, "beta", str(git_repo.root / "b.txt"))
    # The boundary: the newest saved bytes come back, the later write does not.
    assert (git_repo.root / "b.txt").read_text() == "saved v1\n"
    assert restored.saved_at > 0
    assert "unsaved v2\n" not in _chain_blobs(git_repo, "beta")
    assert "saved v1\n" in _chain_blobs(git_repo, "beta")


def test_saving_an_unclaimed_path_is_refused(git_repo: GitFixture) -> None:
    coordinator = _sessions(git_repo)
    coordinator.claim("beta", [str(git_repo.root / "b.txt")])
    git_repo.write("a.txt", "not beta's\n")
    with pytest.raises(AutosaveRefusedError) as caught:
        autosave.save(coordinator, "beta", [str(git_repo.root / "a.txt")])
    assert caught.value.details["reason"] == "unclaimed"
    assert caught.value.details["paths"] == ["a.txt"]
    assert (
        git_repo.git(
            "rev-parse", "--verify", "--quiet", autosave_ref("beta"), check=False
        ).returncode
        != 0
    )
    # A session with no claims has nothing to save: a no-op, not a snapshot.
    empty = autosave.save(coordinator, "alpha")
    assert not empty.created
    assert empty.snapshot is None


def test_restoring_onto_a_path_another_session_holds_refuses(git_repo: GitFixture) -> None:
    coordinator = _sessions(git_repo)
    coordinator.claim("beta", [str(git_repo.root / "b.txt")])
    git_repo.write("b.txt", "beta bytes\n")
    autosave.save(coordinator, "beta")
    coordinator.release("beta", [str(git_repo.root / "b.txt")])
    coordinator.claim("alpha", [str(git_repo.root / "b.txt")])
    git_repo.write("b.txt", "alpha bytes\n")

    with pytest.raises(ClaimConflictError) as caught:
        autosave.restore(coordinator, "beta", str(git_repo.root / "b.txt"))
    assert caught.value.details["holders"] == ["alpha"]
    assert (git_repo.root / "b.txt").read_text() == "alpha bytes\n"
    # Printing is read-only and needs no claim.
    printed = autosave.restore(coordinator, "beta", str(git_repo.root / "b.txt"), write=False)
    assert printed.content == b"beta bytes\n"
    assert not printed.written
    # A path no claim covers is refused too, not written.
    coordinator.release("alpha")
    with pytest.raises(AutosaveRefusedError) as unclaimed:
        autosave.restore(coordinator, "beta", str(git_repo.root / "b.txt"), force=True)
    assert unclaimed.value.details["reason"] == "unclaimed"


def test_snapshots_survive_gc_prune_now(git_repo: GitFixture) -> None:
    coordinator = _sessions(git_repo)
    coordinator.claim("beta", [str(git_repo.root / "nested")])
    git_repo.write("nested/fresh.txt", "untracked, only in the snapshot\n")
    git_repo.write("nested/c.txt", "c dirty\n")
    autosave.save(coordinator, "beta")
    (git_repo.root / "nested/fresh.txt").unlink()
    git_repo.git("checkout", "--quiet", "--", "nested/c.txt")
    git_repo.git("gc", "--quiet", "--prune=now")
    assert git_repo.git("count-objects", "-v").stdout.count("count: 0") == 1

    autosave.restore(coordinator, "beta", str(git_repo.root / "nested/fresh.txt"))
    autosave.restore(coordinator, "beta", str(git_repo.root / "nested/c.txt"))
    assert (git_repo.root / "nested/fresh.txt").read_text() == "untracked, only in the snapshot\n"
    assert (git_repo.root / "nested/c.txt").read_text() == "c dirty\n"


def test_executable_and_symlink_modes_round_trip(git_repo: GitFixture) -> None:
    coordinator = _sessions(git_repo)
    coordinator.claim("beta", [str(git_repo.root / "nested")])
    script = git_repo.write("nested/run.sh", "#!/bin/sh\necho hi\n")
    script.chmod(0o755)
    (git_repo.root / "nested/link").symlink_to("c.txt")
    saved = autosave.save(coordinator, "beta")
    assert saved.snapshot is not None
    modes = {
        line.split("\t")[1]: line.split()[0]
        for line in git_repo.git("ls-tree", "-r", saved.snapshot.tree).stdout.splitlines()
    }
    assert modes == {"nested/c.txt": "100644", "nested/link": "120000", "nested/run.sh": "100755"}

    script.unlink()
    (git_repo.root / "nested/link").unlink()
    for name in ("run.sh", "link"):
        autosave.restore(coordinator, "beta", str(git_repo.root / "nested" / name))
    assert script.read_text() == "#!/bin/sh\necho hi\n"
    assert script.stat().st_mode & stat.S_IXUSR
    assert (git_repo.root / "nested/link").is_symlink()
    assert os.readlink(git_repo.root / "nested/link") == "c.txt"


def test_directory_claim_saves_non_ignored_descendants_and_deletions(
    git_repo: GitFixture,
) -> None:
    coordinator = _sessions(git_repo)
    git_repo.write(".gitignore", "*.log\n")
    coordinator.claim("beta", [str(git_repo.root / "nested")])
    git_repo.write("nested/new.txt", "new\n")
    git_repo.write("nested/noise.log", "ignored\n")
    (git_repo.root / "nested/c.txt").unlink()
    saved = autosave.save(coordinator, "beta")
    assert saved.snapshot is not None
    assert saved.snapshot.scope == ("nested",)
    assert saved.snapshot.deleted == ("nested/c.txt",)
    listed = git_repo.git("ls-tree", "-r", "--name-only", saved.snapshot.tree).stdout.split()
    assert listed == ["nested/new.txt"]

    # An erasure that brings the deleted file back is undone by restoring the deletion.
    git_repo.git("checkout", "--quiet", "--", "nested/c.txt")
    restored = autosave.restore(coordinator, "beta", str(git_repo.root / "nested/c.txt"))
    assert not restored.present
    assert restored.written
    assert not (git_repo.root / "nested/c.txt").exists()


def test_save_is_a_no_op_until_claimed_bytes_change(git_repo: GitFixture) -> None:
    coordinator = _sessions(git_repo)
    coordinator.claim("beta", [str(git_repo.root / "b.txt")])
    git_repo.write("b.txt", "one\n")
    first = autosave.save(coordinator, "beta")
    again = autosave.save(coordinator, "beta")
    assert first.created
    assert not again.created
    assert first.snapshot is not None
    assert again.snapshot is not None
    assert again.snapshot.commit == first.snapshot.commit
    git_repo.write("b.txt", "two\n")
    second = autosave.save(coordinator, "beta")
    assert second.created
    assert second.snapshot is not None
    assert second.snapshot.parent == first.snapshot.commit
    history = autosave.snapshots(coordinator, "beta")
    assert [item.commit for item in history] == [second.snapshot.commit, first.snapshot.commit]
    # An older snapshot is selectable explicitly.
    git_repo.write("b.txt", "two\n")
    older = autosave.restore(
        coordinator, "beta", str(git_repo.root / "b.txt"), snapshot=first.snapshot.commit
    )
    assert older.written
    assert (git_repo.root / "b.txt").read_text() == "one\n"


def test_cli_save_list_restore(git_repo: GitFixture, capsys: pytest.CaptureFixture[str]) -> None:
    coordinator = _sessions(git_repo)
    coordinator.claim("beta", [str(git_repo.root / "b.txt")])
    git_repo.write("b.txt", "cli bytes\n")
    repo = ["--repo", str(git_repo.root), "--session", "beta"]
    code, saved = _json_call(capsys, ["autosave", "save", *repo])
    assert code == 0
    assert saved["ok"]
    assert saved["autosave"]["created"]
    commit = saved["autosave"]["snapshot"]["commit"]
    code, listed = _json_call(capsys, ["autosave", "list", *repo])
    assert code == 0
    assert [row["commit"] for row in listed["autosave"]["snapshots"]] == [commit]

    git_repo.git("restore", "b.txt")
    target = str(git_repo.root / "b.txt")
    code, printed = _json_call(capsys, ["autosave", "restore", *repo, "--stdout", target])
    assert code == 0
    assert printed["autosave"]["content_utf8"] == "cli bytes\n"
    assert not printed["autosave"]["written"]
    assert main(["autosave", "restore", *repo, "--stdout", target]) == 0
    assert capsys.readouterr().out == "cli bytes\n"
    code, restored = _json_call(capsys, ["autosave", "restore", *repo, target])
    assert code == 0
    assert restored["autosave"]["written"]
    assert (git_repo.root / "b.txt").read_text() == "cli bytes\n"

    code, refused = _json_call(capsys, ["autosave", "save", *repo, str(git_repo.root / "a.txt")])
    assert code == 1
    assert refused["error"]["type"] == "autosave_refused"


def test_save_runs_as_a_subprocess_hook_would(git_repo: GitFixture) -> None:
    """The documented PostToolUse hook shells out; the command must be self-contained."""
    coordinator = _sessions(git_repo)
    coordinator.claim("beta", [str(git_repo.root / "b.txt")])
    git_repo.write("b.txt", "hooked\n")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "onetree.cli",
            "autosave",
            "save",
            "--repo",
            str(git_repo.root),
            "--session",
            "beta",
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(Path(autosave.__file__).parents[1])},
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["autosave"]["created"]


def test_status_names_reverted_and_unsaved_paths(
    git_repo: GitFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    coordinator = _sessions(git_repo)
    coordinator.claim("beta", [str(git_repo.root / "b.txt"), str(git_repo.root / "nested")])
    git_repo.write("b.txt", "saved\n")
    git_repo.write("nested/c.txt", "saved c\n")
    autosave.save(coordinator, "beta")
    assert {row.path: row.state for row in autosave.status(coordinator, "beta")} == {
        "b.txt": "saved",
        "nested/c.txt": "saved",
    }

    git_repo.git("checkout", "--quiet", "--", "b.txt")
    git_repo.write("nested/c.txt", "written after the save\n")
    git_repo.write("nested/new.txt", "never saved\n")
    states = {row.path: row.state for row in autosave.status(coordinator, "beta")}
    # The erased file reads as the base again while its snapshot does not: the
    # victim is told, and restore can bring it back.
    assert states == {
        "b.txt": "reverted_to_base",
        "nested/c.txt": "unsaved",
        "nested/new.txt": "unsaved",
    }
    code, payload = _json_call(
        capsys, ["autosave", "status", "--repo", str(git_repo.root), "--session", "beta"]
    )
    assert code == 1
    assert {row["path"]: row["state"] for row in payload["autosave"]["paths"]} == states


def test_a_path_beginning_with_a_double_quote_is_saved_verbatim(git_repo: GitFixture) -> None:
    """`hash-object --stdin-paths` C-unquotes such a line; the path must not be reread."""
    coordinator = _sessions(git_repo)
    git_repo.write('"q.txt', "quoted\n")
    coordinator.claim("beta", [str(git_repo.root / '"q.txt')])
    saved = autosave.save(coordinator, "beta")
    assert saved.snapshot is not None
    assert git_repo.git("show", f'{saved.snapshot.tree}:"q.txt').stdout == "quoted\n"


def test_refusals_and_edge_shapes(git_repo: GitFixture) -> None:
    coordinator = _sessions(git_repo)
    target = str(git_repo.root / "b.txt")
    coordinator.claim("beta", [target, str(git_repo.root / "nested")])
    with pytest.raises(AutosaveRefusedError) as never:
        autosave.restore(coordinator, "beta", target)
    assert never.value.details["reason"] == "not_saved"
    assert autosave.snapshots(coordinator, "beta") == ()

    (git_repo.root / "b.txt").write_bytes(b"\xff\xfe binary\n")
    script = git_repo.write("nested/run.sh", "#!/bin/sh\n")
    script.chmod(0o755)
    first = autosave.save(coordinator, "beta", [target])
    assert first.snapshot is not None
    assert first.snapshot.scope == ("b.txt",)
    printed = autosave.restore(coordinator, "beta", target, write=False).payload()
    assert printed["content_hex"] == (b"\xff\xfe binary\n").hex()
    # A snapshot whose scope does not reach the path does not hold it.
    with pytest.raises(AutosaveRefusedError) as outside:
        autosave.restore(
            coordinator, "beta", str(script), snapshot=first.snapshot.commit, write=False
        )
    assert outside.value.details["reason"] == "not_saved"

    autosave.save(coordinator, "beta")
    with pytest.raises(AutosaveRefusedError) as directory:
        autosave.restore(coordinator, "beta", str(git_repo.root / "nested"))
    assert directory.value.details["reason"] == "not_a_file"
    # A mode-only change is restored even though the bytes are equal.
    script.chmod(0o644)
    assert autosave.restore(coordinator, "beta", str(script)).written
    assert script.stat().st_mode & stat.S_IXUSR
    assert not autosave.restore(coordinator, "beta", str(script)).written
    # --force overwrites bytes no snapshot holds.
    git_repo.write("b.txt", "unsaved\n")
    forced = autosave.restore(coordinator, "beta", target, force=True)
    assert forced.written
    assert (git_repo.root / "b.txt").read_bytes() == b"\xff\xfe binary\n"

    # An unmodified claimed path that was never saved is not at risk.
    coordinator.claim("beta", [str(git_repo.root / "a.txt")])
    assert "a.txt" not in {row.path for row in autosave.status(coordinator, "beta")}
    assert autosave.status(coordinator, "alpha") == ()


def test_root_claim_saves_the_whole_tree(git_repo: GitFixture) -> None:
    coordinator = _sessions(git_repo)
    coordinator.claim("beta", [str(git_repo.root)])
    saved = autosave.save(coordinator, "beta", [str(git_repo.root / "nested")])
    assert saved.snapshot is not None
    assert saved.snapshot.scope == ("nested",)
    whole = autosave.save(coordinator, "beta")
    assert whole.snapshot is not None
    assert whole.snapshot.scope == (".",)
    listed = git_repo.git("ls-tree", "-r", "--name-only", whole.snapshot.tree).stdout.split()
    assert listed == ["a.txt", "b.txt", "nested/c.txt"]
    assert len(autosave.snapshots(coordinator, "beta", limit=1)) == 1


def test_inactive_sessions_and_foreign_refs_are_refused(git_repo: GitFixture) -> None:
    coordinator = _sessions(git_repo)
    coordinator.claim("beta", [str(git_repo.root / "b.txt")])
    git_repo.write("b.txt", "beta\n")
    autosave.save(coordinator, "beta")
    git_repo.git("restore", "b.txt")
    coordinator.register_session("gamma", pid=os.getpid(), ttl=0.05)
    time.sleep(0.1)
    with pytest.raises(IdentityError):
        autosave.save(coordinator, "gamma")
    with pytest.raises(IdentityError):
        autosave.restore(coordinator, "gamma", str(git_repo.root / "b.txt"), source="beta")

    # A ref under the namespace that is not a snapshot chain is an error, not a snapshot.
    for message in ("baseline", "onetree autosave\n\nnot json", 'onetree autosave\n\n{"v": 1}'):
        tree = git_repo.git("rev-parse", "HEAD^{tree}").stdout.strip()
        commit = git_repo.git("commit-tree", tree, input_text=message + "\n").stdout.strip()
        git_repo.git("update-ref", autosave_ref("delta"), commit)
        with pytest.raises(GitError):
            autosave.snapshots(coordinator, "delta")


def test_cli_human_lines(git_repo: GitFixture, capsys: pytest.CaptureFixture[str]) -> None:
    coordinator = _sessions(git_repo)
    coordinator.claim("beta", [str(git_repo.root / "b.txt")])
    repo = ["--repo", str(git_repo.root), "--session", "beta"]
    assert main(["autosave", "list", *repo]) == 0
    assert capsys.readouterr().out == "no snapshots\n"
    git_repo.write("b.txt", "human\n")
    assert main(["autosave", "save", *repo]) == 0
    assert capsys.readouterr().out.startswith("saved 1 file(s) as ")
    assert main(["autosave", "save", *repo]) == 0
    assert capsys.readouterr().out == "nothing changed; no snapshot written\n"
    assert main(["autosave", "status", *repo]) == 0
    assert capsys.readouterr().out == "all 1 claimed path(s) saved\n"
    git_repo.git("restore", "b.txt")
    assert main(["autosave", "status", *repo]) == 1
    assert capsys.readouterr().out == "REVERTED_TO_BASE b.txt\n"
    assert main(["autosave", "restore", *repo, str(git_repo.root / "b.txt")]) == 0
    assert capsys.readouterr().out.startswith("restored b.txt (present in ")
    assert main(["autosave", "list", *repo]) == 0
    assert capsys.readouterr().out.endswith(" b.txt\n")
    assert main(["autosave", "save", *repo, str(git_repo.root / "a.txt")]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "autosave refused" in captured.err
