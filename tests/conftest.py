"""Git-backed fixtures for Round A invariant tests."""

from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from coverage import Coverage

from onetree import git as git_module
from onetree import referee


@pytest.fixture(autouse=True)
def collect_referee_supervisor() -> Iterator[None]:
    """Measure the isolated supervisor without widening production child environments.

    Coverage's subprocess collector needs its configuration passed explicitly.
    Its package-name filter also misses a package file executed as a script,
    so include the same source directory. Only the installed supervisor receives
    this test instrumentation; referee commands keep their production environment.
    """
    config = os.environ.get("COVERAGE_PROCESS_CONFIG")
    if config is None:
        yield
        return
    decoded = Coverage(config_file=f":data:{config}").config
    decoded.source_dirs = [*decoded.source_dirs, str(Path(referee.__file__).parent)]
    config = decoded.serialize()
    supervisor = str(Path(referee.__file__).with_name("_referee_supervisor.py"))
    original = subprocess.Popen

    def measured(*args: Any, **kwargs: Any) -> subprocess.Popen[Any]:
        arguments = args[0] if args else kwargs.get("args", ())
        if isinstance(arguments, (list, tuple)) and tuple(arguments[:3]) == (
            sys.executable, "-I", supervisor,
        ):
            kwargs["env"] = {**kwargs["env"], "COVERAGE_PROCESS_CONFIG": config}
        return original(*args, **kwargs)

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(subprocess, "Popen", measured)
        yield

_GIT_EXECUTABLE = shutil.which("git") or "git"

_GATE_CHILD_SEARCH_PATH = os.pathsep.join(entry for entry in os.defpath.split(os.pathsep) if entry)
"""The `PATH` a payload-gate child is given: the platform default, empty components dropped.

This mirrors `Repository._payload_gate_environment` rather than reading `os.defpath`
directly. An empty component would make `shutil.which` search the working directory and
hand back a bare relative name, which means something different in the child, whose
working directory is the repository under audit.
"""

GATE_GIT_LITERAL = repr(shutil.which("git", path=_GATE_CHILD_SEARCH_PATH) or _GIT_EXECUTABLE)
"""The Git executable as a Python literal, for embedding in a payload-gate stub's source.

A gate child's PATH is chosen rather than inherited -- the platform default with its empty
components dropped, and in one test a default that test supplies itself -- so a stub that
names `git` bare resolves it there, not through the PATH these tests run under, and not
necessarily to the same Git the fixtures themselves invoke. Stubs embed this literal instead.

It is resolved through the child's own search path first, so on any host carrying a Git in
the platform default the stub names one the child can actually execute. That is what makes a
Git installed elsewhere fail the audit it is meant to fail rather than every landing test at
once -- a promise the ambient resolution alone did not keep, because a PATH-dependent shim
resolves in the parent and then cannot find its own target in the child. The fallback to
ambient resolution is reached only where the platform default carries no Git at all, and
there the shipped gate could not run Git either.
"""

_CLEAN_PAYLOAD_GATE_SOURCE = """\
import json
import subprocess
import sys

candidate = sys.argv[2]
repository = sys.argv[4]
completed = subprocess.run(
    [_GIT_, "-C", repository, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", candidate],
    check=True,
    capture_output=True,
)
paths = sorted(
    path.decode("utf-8", errors="strict")
    for path in completed.stdout.split(b"\\0")
    if path
)
print(
    json.dumps(
        {
            "candidate": candidate,
            "repository": repository,
            "staged_files": paths,
            "findings": [],
            "degraded": [],
            "verdict": "CLEAN",
        }
    )
)
""".replace("_GIT_", GATE_GIT_LITERAL)


SHIPPED_PAYLOAD_GATE_SOURCE_SHA256 = git_module.PAYLOAD_GATE_SOURCE_SHA256
"""The digest the package ships, captured before any fixture can rebind it.

`clean_commit_payload_gate` is autouse, so inside a test body the module global always
names that test's throwaway gate. Every assertion about the SHIPPED pin has to read this
snapshot instead, or it asserts the fixture's own value and passes for any pin at all.
"""


@pytest.fixture(autouse=True)
def clean_commit_payload_gate(tmp_path: Path) -> Iterator[None]:
    """Give every landing test an exact-output gate unless it overrides one.

    The patcher is this fixture's own, not the `monkeypatch` fixture. That fixture is one
    instance per test, shared with the test body, and `undo()` on it reverts everything the
    instance has recorded -- including a setup this fixture performed before the body ran.
    Two tests call `monkeypatch.undo()` mid-body, and sharing the instance silently starves
    them of a gate from that line on.
    """

    gate = tmp_path / "clean_commit_payload.py"
    gate.write_text(_CLEAN_PAYLOAD_GATE_SOURCE, encoding="utf-8")
    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(
            git_module,
            "PAYLOAD_GATE_SOURCE_SHA256",
            hashlib.sha256(gate.read_bytes()).hexdigest(),
        )
        patcher.setenv("ONETREE_COMMIT_PAYLOAD", str(gate))
        yield


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
if [ -z "${ONETREE_TEST_KILL_REF+x}${ONETREE_TEST_KILL_STATE+x}" ] &&
   [ -z "${ONETREE_TEST_KILL_NTH+x}${ONETREE_TEST_KILL_MODE+x}" ]; then
  exec __CAPTURED_REAL_GIT__ "$@"
fi
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
    source = _KILL_GIT_SOURCE.replace("__CAPTURED_REAL_GIT__", shlex.quote(_GIT_EXECUTABLE))
    executable.write_text(source, encoding="utf-8")
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
