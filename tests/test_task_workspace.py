"""Task views: isolated linked worktrees, recoverable checkpoints, broker proposals.

Each guarantee here was observed failing against a known-bad variant of
``onetree.task_workspace`` before the guard existed.
"""

from __future__ import annotations

import os
import shlex
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import _GIT_EXECUTABLE, GitFixture

from onetree.core import Coordinator
from onetree.errors import LandingRefusedError, UnsupportedWorktreeError
from onetree.task_workspace import TaskWorkspaceError, TaskWorkspaces


def view_git(view: Path, *arguments: str) -> None:
    environment = os.environ.copy()
    environment.pop("GIT_INDEX_FILE", None)
    subprocess.run([_GIT_EXECUTABLE, "-C", str(view), *arguments], check=True,
                   capture_output=True, env=environment)


def workspaces(git_repo: GitFixture, tmp_path: Path) -> TaskWorkspaces:
    return TaskWorkspaces(git_repo.root, tmp_path / "views")


def session_index_tree(git_repo: GitFixture, coordinator: Coordinator, actor: str) -> str:
    return git_repo.git("write-tree", index=coordinator.repo.index_path(actor)).stdout.strip()


def base_tree(git_repo: GitFixture) -> str:
    return git_repo.oid(f"{git_repo.base}^{{tree}}")


ERASURES = {
    "checkout_path": ("checkout", "--quiet", "--", "b.txt"),
    "restore": ("restore", "b.txt"),
    "read_tree": ("read-tree", "-u", "--reset", "HEAD"),
    "checkout_index": ("checkout-index", "-f", "-a"),
}


@pytest.mark.parametrize("operation", sorted(ERASURES))
def test_path_form_erasure_in_one_view_leaves_peer_bytes_intact(
    git_repo: GitFixture, tmp_path: Path, operation: str,
) -> None:
    """The four reflog-invisible erasures of test_workspace_guard stay inside their view."""
    tasks = workspaces(git_repo, tmp_path)
    view_a = tasks.create("A", git_repo.base).path
    view_b = tasks.create("B", git_repo.base).path
    assert view_a != view_b
    assert not view_a.is_relative_to(git_repo.root)
    (view_a / "b.txt").write_text("task A dirty\n")
    (view_b / "b.txt").write_text("task B dirty\n")
    git_repo.write("b.txt", "primary dirty\n")

    view_git(view_a, *ERASURES[operation])

    assert (view_a / "b.txt").read_text() == "b0\n"
    assert (view_b / "b.txt").read_text() == "task B dirty\n"
    assert (git_repo.root / "b.txt").read_text() == "primary dirty\n"
    # Control: the same command run where B's bytes live does erase them.
    view_git(view_b, *ERASURES[operation])
    assert (view_b / "b.txt").read_text() == "b0\n"


def test_checkpoint_survives_view_deletion_and_gc_prune_now(
    git_repo: GitFixture, tmp_path: Path,
) -> None:
    tasks = workspaces(git_repo, tmp_path)
    view = tasks.create("survive", git_repo.base).path
    (view / "a.txt").write_text("edited\n")
    (view / "b.txt").unlink()
    (view / "nested/c.txt").chmod(0o755)
    (view / ".gitignore").write_text("*.log\n")
    (view / "new.txt").write_text("included\n")
    (view / "scratch.txt").write_text("not included\n")
    (view / "debug.log").write_text("ignored\n")
    first = tasks.checkpoint("survive", include=[".gitignore", "new.txt"])
    (view / "a.txt").write_text("edited twice\n")
    second = tasks.checkpoint("survive")
    assert (first.number, second.number) == (1, 2)

    shutil.rmtree(view)
    git_repo.git("gc", "--quiet", "--prune=now")
    assert git_repo.git("cat-file", "-e", second.commit, check=False).returncode == 0

    recovered = tasks.recover("survive")
    assert recovered.path == view
    assert recovered.head == second.commit
    assert (view / "a.txt").read_text() == "edited twice\n"
    assert not (view / "b.txt").exists()
    assert (view / "nested/c.txt").stat().st_mode & stat.S_IXUSR
    assert (view / "new.txt").read_text() == "included\n"
    assert not (view / "scratch.txt").exists()
    assert not (view / "debug.log").exists()
    assert (git_repo.root / "b.txt").read_text() == "b0\n"


def test_checkpoint_refuses_an_ignored_include(git_repo: GitFixture, tmp_path: Path) -> None:
    tasks = workspaces(git_repo, tmp_path)
    view = tasks.create("ignored", git_repo.base).path
    (view / ".gitignore").write_text("*.log\n")
    (view / "debug.log").write_text("ignored\n")
    with pytest.raises(TaskWorkspaceError) as caught:
        tasks.checkpoint("ignored", include=[".gitignore", "debug.log"])
    assert caught.value.details["reason"] == "include_refused"
    assert tasks.checkpoints("ignored") == ()


def test_views_and_coordinator_stay_bound_to_the_primary(
    git_repo: GitFixture, tmp_path: Path,
) -> None:
    with pytest.raises(TaskWorkspaceError) as caught:
        TaskWorkspaces(git_repo.root, git_repo.root / "views")
    assert caught.value.details["reason"] == "views_inside_primary"
    view = workspaces(git_repo, tmp_path).create("bound", git_repo.base).path
    with pytest.raises(UnsupportedWorktreeError):
        Coordinator(view)
    with pytest.raises(UnsupportedWorktreeError):
        TaskWorkspaces(view, tmp_path / "elsewhere")


def test_unclaimed_path_refuses_with_target_and_session_index_unchanged(
    git_repo: GitFixture, tmp_path: Path,
) -> None:
    coordinator = Coordinator(git_repo.root)
    session = coordinator.register_session("alpha", pid=os.getpid())
    coordinator.claim("alpha", [str(git_repo.root / "a.txt")])
    tasks = workspaces(git_repo, tmp_path)
    view = tasks.create("unclaimed", git_repo.base).path
    (view / "a.txt").write_text("task\n")
    (view / "b.txt").write_text("task\n")
    tasks.checkpoint("unclaimed")
    events = len(coordinator.inspect_wal().events)

    with pytest.raises(TaskWorkspaceError) as caught:
        tasks.propose("unclaimed", coordinator, "alpha", generation=session.generation)
    assert caught.value.details == {"reason": "unclaimed_paths", "paths": ["b.txt"]}
    assert git_repo.oid() == git_repo.base
    assert session_index_tree(git_repo, coordinator, "alpha") == base_tree(git_repo)
    assert len(coordinator.inspect_wal().events) == events


def test_proposal_lands_adds_modifications_deletions_and_modes(
    git_repo: GitFixture, tmp_path: Path,
) -> None:
    coordinator = Coordinator(git_repo.root)
    session = coordinator.register_session("alpha", pid=os.getpid())
    coordinator.claim("alpha", [str(git_repo.root)])
    tasks = workspaces(git_repo, tmp_path)
    view = tasks.create("full", git_repo.base).path
    (view / "a.txt").write_text("modified\n")
    (view / "b.txt").unlink()
    (view / "nested/c.txt").chmod(0o755)
    (view / "added.txt").write_text("added\n")
    tasks.checkpoint("full", include=["added.txt"])

    proposal = tasks.propose("full", coordinator, "alpha", generation=session.generation)
    assert proposal.paths == ("a.txt", "added.txt", "b.txt", "nested/c.txt")
    assert git_repo.oid() == proposal.landed.commit
    assert git_repo.changed_paths(proposal.landed.commit) == set(proposal.paths)
    listing = git_repo.git("ls-tree", "-r", proposal.landed.commit).stdout
    assert "100755 blob" in next(line for line in listing.splitlines() if "nested/c.txt" in line)
    assert "b.txt" not in listing
    assert coordinator.inspect_wal().consistent


COMBINED_REFEREE = shlex.join([sys.executable, "-I", "-S", "-c", (
    "import pathlib, sys\n"
    "pair = (pathlib.Path('a.txt').read_text(), pathlib.Path('b.txt').read_text())\n"
    "sys.exit(0 if pair in {('a0\\n', 'peer\\n'), ('task\\n', 'peer\\n')} else 5)\n"
)])


@pytest.mark.parametrize("registered", ["before_peer", "after_peer"])
def test_target_moved_after_checkpoint_lands_a_fresh_referee_validated_candidate(
    git_repo: GitFixture, tmp_path: Path, registered: str,
) -> None:
    """The referee is green only for a candidate that keeps the peer's landed change."""
    git_repo.git("config", "onetree.referee", COMBINED_REFEREE)
    coordinator = Coordinator(git_repo.root)
    tasks = workspaces(git_repo, tmp_path)
    view = tasks.create("moved", git_repo.base).path
    (view / "a.txt").write_text("task\n")
    tasks.checkpoint("moved")
    alpha = None
    if registered == "before_peer":
        alpha = coordinator.register_session("alpha", pid=os.getpid())
    beta = coordinator.register_session("beta", pid=os.getpid())
    coordinator.claim("beta", [str(git_repo.root / "b.txt")])
    git_repo.write("b.txt", "peer\n")
    git_repo.stage(beta.index_path, "b.txt")
    peer = coordinator.land("beta").commit
    coordinator.release("beta", [str(git_repo.root / "b.txt")])
    if alpha is None:
        alpha = coordinator.register_session("alpha", pid=os.getpid())
    coordinator.claim("alpha", [str(git_repo.root / "a.txt"), str(git_repo.root / "b.txt")])

    proposal = tasks.propose("moved", coordinator, "alpha", generation=alpha.generation)
    landed = proposal.landed
    assert git_repo.oid(f"{landed.commit}^") == peer
    assert git_repo.git("show", f"{landed.commit}:a.txt").stdout == "task\n"
    assert git_repo.git("show", f"{landed.commit}:b.txt").stdout == "peer\n"
    assert landed.referee_receipt is not None
    assert landed.referee_receipt["result"] == "pass"
    assert landed.referee_receipt["candidate"] == landed.commit
    assert coordinator.inspect_wal().consistent


def test_conflicting_session_version_refuses_before_import(
    git_repo: GitFixture, tmp_path: Path,
) -> None:
    coordinator = Coordinator(git_repo.root)
    tasks = workspaces(git_repo, tmp_path)
    view = tasks.create("conflict", git_repo.base).path
    (view / "b.txt").write_text("task\n")
    tasks.checkpoint("conflict")
    beta = coordinator.register_session("beta", pid=os.getpid())
    coordinator.claim("beta", [str(git_repo.root / "b.txt")])
    git_repo.write("b.txt", "peer\n")
    git_repo.stage(beta.index_path, "b.txt")
    coordinator.land("beta")
    coordinator.release("beta", [str(git_repo.root / "b.txt")])
    alpha = coordinator.register_session("alpha", pid=os.getpid())
    coordinator.claim("alpha", [str(git_repo.root / "b.txt")])
    tip = git_repo.oid()
    before = session_index_tree(git_repo, coordinator, "alpha")

    with pytest.raises(TaskWorkspaceError) as caught:
        tasks.propose("conflict", coordinator, "alpha", generation=alpha.generation)
    assert caught.value.details == {"reason": "conflict", "paths": ["b.txt"]}
    assert git_repo.oid() == tip
    assert session_index_tree(git_repo, coordinator, "alpha") == before


def foreign_repository(git_repo: GitFixture, tmp_path: Path) -> Path:
    """A clone: same history and base, but none of the task's checkpoint objects."""
    root = tmp_path / "foreign"
    subprocess.run([_GIT_EXECUTABLE, "clone", "--quiet", "--no-local", str(git_repo.root),
                    str(root)], check=True, capture_output=True)
    return root


def test_proposal_from_a_foreign_repository_refuses(git_repo: GitFixture, tmp_path: Path) -> None:
    tasks = workspaces(git_repo, tmp_path)
    view = tasks.create("foreign", git_repo.base).path
    (view / "a.txt").write_text("task\n")
    tasks.checkpoint("foreign")
    other = foreign_repository(git_repo, tmp_path)
    coordinator = Coordinator(other)
    session = coordinator.register_session("alpha", pid=os.getpid())
    coordinator.claim("alpha", [str(other)])
    tip = coordinator.repo.require_commit("refs/heads/main")

    with pytest.raises(TaskWorkspaceError) as caught:
        tasks.propose("foreign", coordinator, "alpha", generation=session.generation)
    assert caught.value.details["reason"] == "foreign_repository"
    assert coordinator.repo.require_commit("refs/heads/main") == tip
    assert git_repo.oid() == git_repo.base


def test_proposal_from_a_stale_generation_refuses(git_repo: GitFixture, tmp_path: Path) -> None:
    coordinator = Coordinator(git_repo.root)
    stale = coordinator.register_session("alpha", pid=os.getpid(), ttl=0.5)
    time.sleep(0.6)
    current = coordinator.register_session("alpha", pid=os.getpid())
    assert current.generation != stale.generation
    coordinator.claim("alpha", [str(git_repo.root / "a.txt")])
    tasks = workspaces(git_repo, tmp_path)
    view = tasks.create("stale", git_repo.base).path
    (view / "a.txt").write_text("task\n")
    tasks.checkpoint("stale")

    with pytest.raises(TaskWorkspaceError) as caught:
        tasks.propose("stale", coordinator, "alpha", generation=stale.generation)
    assert caught.value.details["reason"] == "stale_generation"
    assert git_repo.oid() == git_repo.base
    assert session_index_tree(git_repo, coordinator, "alpha") == base_tree(git_repo)


def test_proposal_of_a_superseded_checkpoint_refuses(git_repo: GitFixture, tmp_path: Path) -> None:
    coordinator = Coordinator(git_repo.root)
    session = coordinator.register_session("alpha", pid=os.getpid())
    coordinator.claim("alpha", [str(git_repo.root / "a.txt")])
    tasks = workspaces(git_repo, tmp_path)
    view = tasks.create("superseded", git_repo.base).path
    (view / "a.txt").write_text("one\n")
    tasks.checkpoint("superseded")
    (view / "a.txt").write_text("two\n")
    tasks.checkpoint("superseded")
    with pytest.raises(TaskWorkspaceError) as caught:
        tasks.propose("superseded", coordinator, "alpha", generation=session.generation,
                      checkpoint=1)
    assert caught.value.details["reason"] == "stale_checkpoint"
    assert git_repo.oid() == git_repo.base


def test_a_broker_refusal_after_import_keeps_the_target(
    git_repo: GitFixture, tmp_path: Path,
) -> None:
    git_repo.git("config", "onetree.referee", COMBINED_REFEREE)
    coordinator = Coordinator(git_repo.root)
    session = coordinator.register_session("alpha", pid=os.getpid())
    coordinator.claim("alpha", [str(git_repo.root / "a.txt")])
    tasks = workspaces(git_repo, tmp_path)
    view = tasks.create("red", git_repo.base).path
    (view / "a.txt").write_text("task\n")
    tasks.checkpoint("red")
    with pytest.raises(LandingRefusedError) as caught:
        tasks.propose("red", coordinator, "alpha", generation=session.generation)
    assert caught.value.details["reason"] == "referee_red"
    assert git_repo.oid() == git_repo.base
