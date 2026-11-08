"""Per-task isolated working views with recoverable checkpoints (opt-in spike).

A task view is a detached linked Git worktree of the primary clone, placed in a
directory outside the primary working tree. Its state is preserved only as
commits under ``refs/onetree/tasks/<task>/checkpoints/<n>`` and its base under
``refs/onetree/tasks/<task>/base``; nothing is written to the WAL. A proposal
imports one checkpoint's changes into a registered session's private index and
hands off to :meth:`Coordinator.land`, which keeps every landing invariant.

The coordinator stays bound to the primary clone: this module never constructs a
:class:`Repository` over a linked worktree, and a view is driven only through
argv Git invocations whose working directory is the view.

Spike scope: Python API only, no CLI, no WAL record of tasks, no locking between
two writers of one task beyond ref compare-and-swap.
"""

from __future__ import annotations

import os
import re
import shutil
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from onetree.core import Coordinator, LandResult, _covers
from onetree.errors import OnetreeError
from onetree.git import GitResult, Repository, TreeEntry

TASK_REF_PREFIX = "refs/onetree/tasks/"
_TASK_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_IDENTITY = {
    "GIT_AUTHOR_NAME": "onetree task checkpoint",
    "GIT_AUTHOR_EMAIL": "onetree@localhost.invalid",
    "GIT_COMMITTER_NAME": "onetree task checkpoint",
    "GIT_COMMITTER_EMAIL": "onetree@localhost.invalid",
}


class TaskWorkspaceError(OnetreeError):
    """A task-view operation refused; ``details["reason"]`` names why."""

    kind = "task_workspace_refused"


@dataclass(frozen=True)
class TaskView:
    task: str
    path: Path
    base: str
    head: str


@dataclass(frozen=True)
class TaskCheckpoint:
    task: str
    number: int
    commit: str
    tree: str
    ref: str


@dataclass(frozen=True)
class Proposal:
    task: str
    checkpoint: TaskCheckpoint
    paths: tuple[str, ...]
    landed: LandResult


@dataclass(frozen=True)
class _Change:
    path: str
    old: TreeEntry | None
    new: TreeEntry | None


def _refuse(message: str, reason: str, **details: object) -> TaskWorkspaceError:
    return TaskWorkspaceError(message, reason=reason, **details)


def _validate_task(task: str) -> str:
    if not isinstance(task, str) or _TASK_NAME.fullmatch(task) is None:
        raise _refuse("task names are 1-64 of [A-Za-z0-9_-]", "invalid_task", task=task)
    return task


def _validate_include(raw: str) -> str:
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or any(part in {"", ".", ".."} for part in raw.split("/")):
        raise _refuse("included paths are normalized repository-relative paths",
                      "invalid_include", path=raw)
    return raw


class TaskWorkspaces:
    """Task views of one primary clone, materialized below ``views_root``."""

    def __init__(self, repository: Path | str, views_root: Path | str) -> None:
        # Repository() refuses a linked worktree, so tasks always hang off the primary.
        self.repo = Repository(repository)
        root = Path(views_root).expanduser().resolve()
        for protected in (self.repo.root, self.repo.common_git_dir):
            if root == protected or root.is_relative_to(protected):
                raise _refuse("task views must live outside the primary working tree",
                              "views_inside_primary", views_root=str(root))
        self.views_root = root

    # -- refs ---------------------------------------------------------------

    @staticmethod
    def _base_ref(task: str) -> str:
        return f"{TASK_REF_PREFIX}{task}/base"

    @staticmethod
    def _checkpoint_ref(task: str, number: int) -> str:
        return f"{TASK_REF_PREFIX}{task}/checkpoints/{number}"

    def view_path(self, task: str) -> Path:
        return self.views_root / _validate_task(task)

    def base(self, task: str) -> str:
        base = self.repo.resolve_ref(self._base_ref(_validate_task(task)), missing_ok=True)
        if base is None:
            raise _refuse("task does not exist", "unknown_task", task=task)
        return base

    def checkpoints(self, task: str) -> tuple[TaskCheckpoint, ...]:
        prefix = f"{TASK_REF_PREFIX}{_validate_task(task)}/checkpoints/"
        output = self.repo.git_text(
            "for-each-ref", "--format=%(refname)%00%(objectname)%00%(tree)", prefix
        )
        found: list[TaskCheckpoint] = []
        for line in output.splitlines():
            ref, commit, tree = line.split("\0")
            suffix = ref[len(prefix):]
            if not suffix.isdigit() or str(int(suffix)) != suffix:
                raise _refuse("malformed checkpoint ref", "checkpoint_corrupt", ref=ref)
            found.append(TaskCheckpoint(task, int(suffix), commit, tree, ref))
        return tuple(sorted(found, key=lambda checkpoint: checkpoint.number))

    def latest(self, task: str) -> TaskCheckpoint:
        found = self.checkpoints(task)
        if not found:
            raise _refuse("task has no checkpoint", "no_checkpoint", task=task)
        return found[-1]

    # -- view plumbing ------------------------------------------------------

    def _in_view(
        self,
        view: Path,
        *arguments: str,
        index: Path | None = None,
        identity: bool = False,
        check: bool = True,
    ) -> GitResult:
        environment = Repository._environment(index)
        if identity:
            environment.update(_IDENTITY)
        return Repository._invoke(view, arguments, environment=environment, check=check)

    def _view_text(self, view: Path, *arguments: str, index: Path | None = None) -> str:
        return self._in_view(view, *arguments, index=index).stdout.decode("utf-8").strip()

    def _existing_view(self, task: str) -> Path:
        view = self.view_path(task)
        if not view.is_dir():
            raise _refuse("task view directory is missing; recover it", "view_missing",
                          task=task, path=str(view))
        common = Path(self._view_text(view, "rev-parse", "--path-format=absolute",
                                      "--git-common-dir")).resolve()
        top = Path(self._view_text(view, "rev-parse", "--show-toplevel")).resolve()
        if common != self.repo.common_git_dir or top != view:
            raise _refuse("task view does not belong to this repository", "foreign_view",
                          task=task, path=str(view))
        return view

    def _add_worktree(self, view: Path, commit: str) -> TaskView:
        self.views_root.mkdir(parents=True, exist_ok=True)
        self.repo.git("worktree", "add", "--detach", "--quiet", str(view), commit)
        return TaskView(view.name, view, self.base(view.name), commit)

    # -- public operations --------------------------------------------------

    def create(self, task: str, base: str) -> TaskView:
        view = self.view_path(task)
        commit = self.repo.resolve_ref(base, missing_ok=True)
        if commit is None:
            raise _refuse("task base does not name a commit", "invalid_base", base=base)
        if view.exists() or view.is_symlink():
            raise _refuse("task view path already exists", "view_present", path=str(view))
        if not self.repo.update_ref(self._base_ref(task), commit, None):
            raise _refuse("task already exists", "task_exists", task=task)
        return self._add_worktree(view, commit)

    def checkpoint(self, task: str, include: Sequence[str] = ()) -> TaskCheckpoint:
        """Commit the view's tracked state plus ``include``; never ignored files.

        Tracked means present in the view's index or in the previous checkpoint, so
        an untracked file included once stays in later checkpoints until deleted.
        """

        view = self._existing_view(task)
        paths = [_validate_include(raw) for raw in include]
        previous = self.checkpoints(task)
        parent = previous[-1].commit if previous else self.base(task)
        view_index = Path(self._view_text(view, "rev-parse", "--path-format=absolute",
                                          "--git-path", "index"))
        temporary = self.repo.new_temp_index()
        try:
            shutil.copyfile(view_index, temporary)
            staged = set(self._view_text(view, "ls-files", "-z", index=temporary).split("\0"))
            if previous:
                for path, entry in self.repo.tree_entries(previous[-1].commit).items():
                    if path not in staged:
                        self._in_view(view, "update-index", "--add", "--cacheinfo",
                                      entry.mode, entry.oid, path, index=temporary)
            self._in_view(view, "add", "--update", "--", ".", index=temporary)
            if paths:
                added = self._in_view(view, "add", "--", *paths, index=temporary, check=False)
                if added.returncode != 0:
                    raise _refuse("an included path could not be added (ignored or absent)",
                                  "include_refused", paths=paths,
                                  detail=added.stderr.decode("utf-8", errors="replace").strip())
            tree = self.repo.write_tree(temporary)
        finally:
            temporary.unlink(missing_ok=True)
        number = previous[-1].number + 1 if previous else 1
        commit = self._in_view(
            view, "commit-tree", tree, "-p", parent,
            "-m", f"onetree task {task} checkpoint {number}", identity=True,
        ).stdout.decode("ascii").strip()
        reference = self._checkpoint_ref(task, number)
        if not self.repo.update_ref(reference, self.repo.validate_oid(commit), None):
            raise _refuse("a concurrent checkpoint took this number", "checkpoint_race",
                          ref=reference)
        return TaskCheckpoint(task, number, commit, tree, reference)

    def recover(self, task: str) -> TaskView:
        """Re-materialize the latest checkpoint after the view's directory was deleted."""

        view = self.view_path(task)
        latest = self.latest(task)
        if view.exists() or view.is_symlink():
            raise _refuse("task view still exists", "view_present", path=str(view))
        # The deleted directory leaves stale worktree metadata that blocks re-adding it.
        self.repo.git("worktree", "prune")
        return self._add_worktree(view, latest.commit)

    def remove(self, task: str) -> None:
        """Delete the view directory; checkpoints and the base ref are kept."""

        view = self._existing_view(task)
        self.repo.git("worktree", "remove", "--force", str(view))

    def propose(
        self,
        task: str,
        coordinator: Coordinator,
        actor: str,
        *,
        generation: str,
        message: str | None = None,
        checkpoint: int | None = None,
    ) -> Proposal:
        """Import the latest checkpoint into ``actor``'s private index, then land it.

        Every refusal raised here leaves the session index and the target untouched.
        A refusal from ``Coordinator.land`` leaves the imported changes staged in the
        session index, exactly as if the session had staged them itself.
        """

        if coordinator.repo.common_git_dir != self.repo.common_git_dir:
            raise _refuse("coordinator belongs to another repository", "foreign_repository",
                          repository=str(coordinator.repo.root))
        latest = self.latest(task)
        if checkpoint is not None and checkpoint != latest.number:
            raise _refuse("a newer checkpoint supersedes the proposed one", "stale_checkpoint",
                          proposed=checkpoint, latest=latest.number)
        base = self.base(task)
        state = coordinator._fold(coordinator.wal.read())
        now = time.time()
        session = coordinator._active_session(state, actor, now)
        if session is None:
            raise _refuse("proposing session is not active", "session_inactive", actor=actor)
        if session.generation != generation:
            raise _refuse("proposal belongs to a stale session generation", "stale_generation",
                          expected=generation, current=session.generation)
        base_entries = self.repo.tree_entries(base)
        task_entries = self.repo.tree_entries(latest.commit)
        changes = [
            _Change(path, base_entries.get(path), task_entries.get(path))
            for path in sorted(set(base_entries) | set(task_entries))
            if base_entries.get(path) != task_entries.get(path)
        ]
        if not changes:
            raise _refuse("checkpoint changes nothing", "nothing_changed", task=task)
        owned = [claim.path for claim in state.active_claims(now) if claim.actor == actor]
        missing = [c.path for c in changes if not any(_covers(own, c.path) for own in owned)]
        if missing:
            raise _refuse("proposal contains unclaimed paths", "unclaimed_paths", paths=missing)
        index = coordinator.repo.ensure_index(actor, coordinator._session_baseline(state, session))
        current = coordinator.repo.tree_entries(coordinator.repo.write_tree(index))
        conflicts = [c.path for c in changes if current.get(c.path) not in (c.old, c.new)]
        if conflicts:
            raise _refuse("session index holds a different version than the task base",
                          "conflict", paths=conflicts)
        temporary = self.repo.new_temp_index()
        try:
            shutil.copyfile(index, temporary)
            for change in changes:
                if change.new is None:
                    self.repo.git("update-index", "--force-remove", "--", change.path,
                                  index=temporary)
                else:
                    self.repo.git("update-index", "--add", "--cacheinfo", change.new.mode,
                                  change.new.oid, change.path, index=temporary)
            os.replace(temporary, index)
        finally:
            temporary.unlink(missing_ok=True)
        landed = coordinator.land(
            actor,
            message=message or f"onetree task {task} checkpoint {latest.number}",
            target=session.target,
        )
        return Proposal(task, latest, tuple(c.path for c in changes), landed)
