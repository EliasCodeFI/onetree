"""Git-backed fixtures for Round A invariant tests."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

_GIT_EXECUTABLE = shutil.which("git") or "git"

# A PATH-shim git that SIGKILLs the broker, or itself, from inside a matched
# invocation.
#
# Python checkpoints inside the broker cannot reach this window: for the whole
# of a matched invocation the caller is blocked in subprocess.run, so a kill
# issued from here lands while the ref update is in flight. A single-ref update
# commits with one rename, so "in flight" has exactly two observable outcomes --
# the rename happened, or it did not -- and both are produced here by mode.
#
# Which process dies is the second axis, and it decides who is left to observe
# the outcome. `before`/`after` kill the broker, reproducing a crash that leaves
# recovery to a later run. `self_after` kills only the git process *after the
# real Git command has returned*: the ref moved, and the broker survives to read
# a non-zero exit status over an update that already applied. This is not a
# mid-rename death; the broker cannot distinguish the two observations.
_KILL_GIT_SOURCE = """#!/bin/bash
set -u
matched_operation=0
matched_reference=0
for argument in "$@"; do
  case "$argument" in
    update-ref) matched_operation=1 ;;
    "$ONETREE_TEST_KILL_REF") matched_reference=1 ;;
  esac
done
if [ "$matched_operation" = 1 ] && [ "$matched_reference" = 1 ]; then
  seen=0
  if [ -f "$ONETREE_TEST_KILL_STATE" ]; then seen=$(<"$ONETREE_TEST_KILL_STATE"); fi
  seen=$((seen + 1))
  printf '%s' "$seen" > "$ONETREE_TEST_KILL_STATE"
  if [ "$seen" = "$ONETREE_TEST_KILL_NTH" ]; then
    if [ "$ONETREE_TEST_KILL_MODE" = self_after ]; then
      "$ONETREE_TEST_REAL_GIT" "$@"
      kill -KILL "$$"
      exit 98
    fi
    if [ "$ONETREE_TEST_KILL_MODE" = self_after_hand_commits ]; then
      "$ONETREE_TEST_REAL_GIT" "$@"
      repository=""
      previous=""
      for argument in "$@"; do
        if [ "$previous" = -C ]; then repository="$argument"; break; fi
        previous="$argument"
      done
      if [ -z "$repository" ]; then exit 97; fi
      hand_commits="${ONETREE_TEST_HAND_COMMITS:-0}"
      count=0
      while [ "$count" -lt "$hand_commits" ]; do
        "$ONETREE_TEST_REAL_GIT" -C "$repository" commit \\
          --quiet --allow-empty -m "ordinary hand interleave $count"
        count=$((count + 1))
      done
      kill -KILL "$$"
      exit 98
    fi
    if [ "$ONETREE_TEST_KILL_MODE" = after ]; then
      "$ONETREE_TEST_REAL_GIT" "$@"
    fi
    kill -KILL "$PPID"
    exit 0
  fi
fi
exec "$ONETREE_TEST_REAL_GIT" "$@"
"""


@dataclass(frozen=True)
class KillGit:
    """A git shim that SIGKILLs the broker from inside one ref update."""

    executable: Path
    state: Path

    def environment(
        self,
        reference: str,
        nth: int,
        mode: str,
        *,
        hand_commits: int = 0,
    ) -> dict[str, str]:
        return {
            "ONETREE_TEST_REAL_GIT": _GIT_EXECUTABLE,
            "ONETREE_TEST_KILL_STATE": str(self.state),
            "ONETREE_TEST_KILL_REF": reference,
            "ONETREE_TEST_KILL_NTH": str(nth),
            "ONETREE_TEST_KILL_MODE": mode,
            "ONETREE_TEST_HAND_COMMITS": str(hand_commits),
        }


def install_kill_git(executable: str, environment: dict[str, str]) -> None:
    """Point this process's Git layer at the shim, through PATH resolution."""

    import onetree.git

    os.environ.update(environment)
    os.environ["PATH"] = f"{Path(executable).parent}{os.pathsep}{os.environ['PATH']}"
    resolved = shutil.which("git")
    assert resolved == executable, resolved
    # The module resolved the real git at import time, before PATH was changed.
    onetree.git._GIT_EXECUTABLE = resolved


@pytest.fixture
def kill_git(tmp_path: Path) -> KillGit:
    directory = tmp_path / "killgit"
    directory.mkdir()
    executable = directory / "git"
    executable.write_text(_KILL_GIT_SOURCE, encoding="utf-8")
    executable.chmod(0o755)
    return KillGit(executable=executable, state=tmp_path / "killgit.count")


@dataclass(frozen=True)
class GitFixture:
    """One isolated non-bare repository with a small tracked baseline."""

    root: Path
    base: str

    def git(
        self,
        *args: str,
        index: Path | None = None,
        input_text: str | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.pop("GIT_INDEX_FILE", None)
        if index is not None:
            env["GIT_INDEX_FILE"] = str(index)
        return subprocess.run(
            [_GIT_EXECUTABLE, "-C", str(self.root), *args],
            check=check,
            capture_output=True,
            text=True,
            input=input_text,
            env=env,
        )

    def oid(self, revision: str = "refs/heads/main") -> str:
        return self.git("rev-parse", "--verify", revision).stdout.strip()

    def write(self, relative: str, content: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def stage(self, index: Path, *relative: str) -> None:
        self.git("add", "--", *relative, index=index)

    def raw_commit(self, index: Path, parent: str, message: str = "unsafe") -> str:
        tree = self.git("write-tree", index=index).stdout.strip()
        return self.git(
            "commit-tree",
            tree,
            "-p",
            parent,
            input_text=f"{message}\n",
        ).stdout.strip()

    def changed_paths(self, commit: str) -> set[str]:
        output = self.git(
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            "-z",
            commit,
        ).stdout
        return {path for path in output.split("\0") if path}

    def sync_shared_index(self) -> None:
        self.git("read-tree", "HEAD")

    def external_commit(self, relative: str, content: str, message: str) -> str:
        self.sync_shared_index()
        self.write(relative, content)
        self.git("add", "--", relative)
        self.git("commit", "--quiet", "-m", message)
        return self.oid()


@pytest.fixture
def git_repo(tmp_path: Path) -> GitFixture:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(
        [_GIT_EXECUTABLE, "init", "--quiet", "--initial-branch=main", str(root)],
        check=True,
        capture_output=True,
        text=True,
    )
    fixture = GitFixture(root=root, base="")
    fixture.git("config", "user.name", "Round A Tests")
    fixture.git("config", "user.email", "round-a@example.invalid")
    fixture.write("a.txt", "a0\n")
    fixture.write("b.txt", "b0\n")
    fixture.write("nested/c.txt", "c0\n")
    fixture.git("add", "--", "a.txt", "b.txt", "nested/c.txt")
    fixture.git("commit", "--quiet", "-m", "baseline")
    return GitFixture(root=root, base=fixture.oid())
