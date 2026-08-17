"""Git-backed fixtures for Round A invariant tests."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

_GIT_EXECUTABLE = shutil.which("git") or "git"


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
