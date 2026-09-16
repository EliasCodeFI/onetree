"""The synthetic task corpus: what each writer intends to do, fixed by the seed.

A corpus is F text files under ``corpus/``. Each writer gets M tasks; a task rewrites one or
more files by read-modify-write, appending one token line ``tok:<writer>:<task>:<hex>`` to
the file's existing lines. Read-modify-write is the point: a byte a writer produced can be
lost only if something between the write and the landing replaced the file wholesale, which
is exactly the collision the floor system permits and the claims system forbids.

Conflict pressure is a grid parameter: with probability ``conflict`` a task's path is drawn
from a hot pool every writer shares, otherwise from the writer's private pool. Private pools
are disjoint, so ``conflict=0`` gives writers no common path and ``conflict=1`` gives them
nothing else. The grid (writers, tasks, files, conflict, paths per task) is task 25's to fix;
the values a run uses are recorded verbatim and labelled placeholders until then.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from onetree.bench.seeds import SeedKey

HEADER = "onetree-bench corpus file\n"
TOKEN_PATTERN = re.compile(r"^tok:w(\d+):t(\d+):([0-9a-f]{12})$")


@dataclass(frozen=True)
class Grid:
    """The run grid as used; the frozen protocol defers its final values to an amendment."""

    writers: int
    tasks: int
    files: int
    conflict: float
    paths_per_task: int = 1
    jitter_ms: int = 20
    amended: bool = False

    def __post_init__(self) -> None:
        if self.writers < 1 or self.tasks < 1 or self.files < 1 or self.paths_per_task < 1:
            raise ValueError("writers, tasks, files and paths_per_task must be positive")
        if not 0.0 <= self.conflict <= 1.0:
            raise ValueError("conflict must be within [0, 1]")
        if self.jitter_ms < 0:
            raise ValueError("jitter_ms must be non-negative")
        if self.paths_per_task > self.files:
            raise ValueError("paths_per_task cannot exceed files")

    def payload(self) -> dict[str, object]:
        return {
            "writers": self.writers,
            "tasks": self.tasks,
            "files": self.files,
            "conflict": self.conflict,
            "paths_per_task": self.paths_per_task,
            "jitter_ms": self.jitter_ms,
            "amended": self.amended,
            "status": "amended by task 25"
            if self.amended
            else "PLACEHOLDER: not amended (task 25)",
        }


@dataclass(frozen=True)
class Task:
    writer: int
    index: int
    paths: tuple[str, ...]
    tokens: tuple[str, ...]
    think_ms: int

    def token(self, path: str, attempt: int = 1) -> str:
        """The token one attempt writes at ``path``; every attempt's token is distinct.

        A retry that rewrote the first attempt's token would make an overwritten first
        write indistinguishable from a landed one, so attempts after the first derive
        their own token from the seeded one.
        """

        if attempt < 1:
            raise ValueError("attempts are numbered from 1")
        seeded = self.tokens[self.paths.index(path)]
        if attempt == 1:
            return seeded
        return hashlib.sha256(f"{seeded}/{attempt}".encode()).hexdigest()[:12]

    def token_line(self, path: str, attempt: int = 1) -> str:
        return f"tok:w{self.writer}:t{self.index}:{self.token(path, attempt)}"


@dataclass(frozen=True)
class Corpus:
    files: tuple[str, ...]
    hot: tuple[str, ...]
    private: dict[int, tuple[str, ...]] = field(default_factory=dict)
    tasks: dict[int, tuple[Task, ...]] = field(default_factory=dict)

    def all_tokens(self) -> dict[str, tuple[int, int, str]]:
        """token -> (writer, task index, path) for every token any writer intends to write."""

        result: dict[str, tuple[int, int, str]] = {}
        for writer, tasks in self.tasks.items():
            for task in tasks:
                for path, token in zip(task.paths, task.tokens, strict=True):
                    result[token] = (writer, task.index, path)
        return result

    def payload(self) -> dict[str, object]:
        """The corpus as committed raw data; ``build`` under the same seed reproduces it."""

        return {
            "files": list(self.files),
            "hot": list(self.hot),
            "private": {str(writer): list(paths) for writer, paths in self.private.items()},
            "tasks": {
                str(writer): [
                    {
                        "index": task.index,
                        "paths": list(task.paths),
                        "tokens": list(task.tokens),
                        "think_ms": task.think_ms,
                    }
                    for task in tasks
                ]
                for writer, tasks in self.tasks.items()
            },
        }


def file_name(index: int) -> str:
    return f"corpus/f{index:03d}.txt"


def build(grid: Grid, key: SeedKey) -> Corpus:
    """Generate the corpus for ``grid`` under ``key``; identical inputs give identical output.

    Pools: the hot pool takes ``ceil(files * conflict)`` files (at least one when conflict
    is positive); the rest is split round-robin into per-writer private pools. A writer whose
    private pool is empty (more writers than private files) draws from the hot pool only,
    which the receipts expose through ``private`` sizes.
    """

    files = tuple(file_name(index) for index in range(grid.files))
    hot_count = 0 if grid.conflict == 0.0 else max(1, -(-grid.files * grid.conflict // 1))
    hot_count = int(min(hot_count, grid.files))
    hot = files[:hot_count]
    rest = files[hot_count:]
    private: dict[int, tuple[str, ...]] = {
        writer: tuple(rest[writer :: grid.writers]) for writer in range(grid.writers)
    }
    tasks: dict[int, tuple[Task, ...]] = {}
    for writer in range(grid.writers):
        writer_key = key.spawn("writer", str(writer))
        generated: list[Task] = []
        for index in range(grid.tasks):
            rng = writer_key.spawn("task", str(index)).rng()
            chosen: list[str] = []
            pool_private = private[writer]
            while len(chosen) < grid.paths_per_task:
                use_hot = bool(hot) and (not pool_private or rng.random() < grid.conflict)
                pool = hot if use_hot else pool_private
                candidates = [path for path in pool if path not in chosen]
                if not candidates:
                    candidates = [path for path in files if path not in chosen]
                chosen.append(rng.choice(candidates))
            tokens = tuple(f"{rng.getrandbits(48):012x}" for _ in chosen)
            think = rng.randint(0, grid.jitter_ms) if grid.jitter_ms else 0
            generated.append(Task(writer, index, tuple(chosen), tokens, think))
        tasks[writer] = tuple(generated)
    return Corpus(files=files, hot=hot, private=private, tasks=tasks)


def tokens_in(text: str) -> list[str]:
    """Every token line's hex in ``text``, in file order."""

    found: list[str] = []
    for line in text.splitlines():
        match = TOKEN_PATTERN.match(line)
        if match:
            found.append(match.group(3))
    return found


def well_formed(text: str) -> bool:
    """The corpus-file invariant the hermetic referee checks."""

    lines = text.split("\n")
    if not lines or lines[0] + "\n" != HEADER:
        return False
    body = lines[1:]
    if body and body[-1] == "":
        body = body[:-1]
    return all(TOKEN_PATTERN.match(line) for line in body)


def rewrite(existing: str, task: Task, path: str, attempt: int = 1) -> str:
    """Read-modify-write: the file's current lines plus this attempt's token line."""

    base = existing if existing.startswith(HEADER) else HEADER
    if not base.endswith("\n"):
        base += "\n"
    return base + task.token_line(path, attempt) + "\n"


def paths_of(tasks: Sequence[Task]) -> set[str]:
    return {path for task in tasks for path in task.paths}


__all__ = [
    "HEADER",
    "TOKEN_PATTERN",
    "Corpus",
    "Grid",
    "Task",
    "build",
    "file_name",
    "paths_of",
    "rewrite",
    "tokens_in",
    "well_formed",
]
