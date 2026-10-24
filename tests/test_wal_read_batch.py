"""Task 194: a certified canonical WAL read starts the same Git processes however long the log is.

``Wal.read`` ran ``ls-tree`` and ``cat-file`` once per event, 2E + 2 processes for E events
(task 66 R5, re-measured by the task 194 census). The reader now puts a batched read of the
canonical chain in front of its unchanged sequential path. The constant count is claimed only
for a read that path certifies: a declined read starts the sequential reader's processes plus
the batch reads it made before declining. The brief's contracts hold for every read: the
per-record commit, exact tree, document, object identity, ordering and typed failures are the
sequential reader's; object and ref changes are seen by the next call; no trust is cached
across calls. Which path a read took is observed, never inferred from a passing result: at
``Wal._read_canonical``, and by counting the Git processes the read actually started.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from conftest import _GIT_EXECUTABLE, GitFixture

from onetree import git as git_module
from onetree.errors import GitError
from onetree.git import Repository
from onetree.wal import WAL_REF, Wal, WalSnapshot

_CERTIFIED_CENSUS = {"rev-parse": 1, "rev-list": 1, "cat-file --batch": 3}

# Logs the Git verb (the argument after ``-C <dir>``) and the argument after it, then runs Git.
_CENSUS_SHIM = """#!/bin/bash
previous=""
verb=""
detail=""
for argument in "$@"; do
  if [ "$previous" = "-C" ]; then previous="--dir--"; continue; fi
  if [ "$previous" = "--dir--" ]; then verb="$argument"; previous="--verb--"; continue; fi
  if [ "$previous" = "--verb--" ]; then detail="$argument"; break; fi
  previous="$argument"
done
printf '%s %s\\n' "$verb" "$detail" >> __LOG__
exec __GIT__ "$@"
"""


def _git(root: Path, *arguments: str, stdin: bytes = b"") -> str:
    completed = subprocess.run(
        [_GIT_EXECUTABLE, "-C", str(root), *arguments],
        input=stdin, capture_output=True, check=True,
    )
    return completed.stdout.decode("ascii").strip()


def _document(number: int, **override: Any) -> dict[str, Any]:
    document = Wal.document("claim", "alpha", {"paths": [f"f{number}.txt"]},
                            event_id=f"{number:032x}")
    document["time"] = f"2026-09-17T00:00:{number:02d}.000000+00:00"
    document.update(override)
    return document


def _encoded(document: dict[str, Any]) -> bytes:
    return json.dumps(document, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":")).encode("ascii") + b"\n"


def _blob(root: Path, content: bytes) -> str:
    return _git(root, "hash-object", "-w", "--stdin", stdin=content)


def _tree(root: Path, rows: str) -> str:
    return _git(root, "mktree", stdin=rows.encode("ascii"))


def _raw_tree(root: Path, body: bytes) -> str:
    return _git(root, "hash-object", "-w", "-t", "tree", "--literally", "--stdin", stdin=body)


def _event_tree(root: Path, content: bytes) -> str:
    return _tree(root, f"100644 blob {_blob(root, content)}\tevent.json\n")


def _chain(root: Path, trees: list[str], *, start: str | None = None) -> list[str]:
    commits: list[str] = []
    parent = start
    for number, tree in enumerate(trees, start=1):
        arguments = ["commit-tree", tree, *(["-p", parent] if parent else [])]
        parent = _git(root, *arguments, stdin=f"onetree wal: event {number}\n".encode())
        commits.append(parent)
    _git(root, "update-ref", WAL_REF, commits[-1])
    return commits


def _canonical_chain(root: Path, count: int) -> list[str]:
    return _chain(root, [_event_tree(root, _encoded(_document(n))) for n in range(1, count + 1)])


def _loose(root: Path, oid: str) -> Path:
    path = root / ".git" / "objects" / oid[:2] / oid[2:]
    assert path.is_file(), "fixture objects are loose"
    return path


def _outcome(read: Callable[[], WalSnapshot]) -> dict[str, Any]:
    try:
        snapshot = read()
    except Exception as error:  # every failure is compared, typed or not
        cause = error.__cause__
        return {
            "raised": type(error).__name__,
            "message": str(error),
            "details": getattr(error, "details", None),
            "cause": None if cause is None else [type(cause).__name__, str(cause)],
        }
    return {"snapshot": snapshot}


def _sequential(wal: Wal, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    with monkeypatch.context() as patch:
        patch.setattr(Wal, "_read_canonical", lambda self, head, output: None)
        return _outcome(wal.read)


@contextmanager
def _paths(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """It adds a frame: not for the recursion-limit case."""

    taken: list[str] = []
    original = Wal._read_canonical

    def observed(self: Wal, head: str, output: bytes) -> WalSnapshot | None:
        snapshot = original(self, head, output)
        taken.append("sequential" if snapshot is None else "canonical")
        return snapshot

    with monkeypatch.context() as patch:
        patch.setattr(Wal, "_read_canonical", observed)
        yield taken


@contextmanager
def _census(
    directory: Path, monkeypatch: pytest.MonkeyPatch, repo: Repository
) -> Iterator[Callable[[], Counter[str]]]:
    """The counter proves itself on one known process before anything it reports is believed."""

    directory.mkdir()
    log = directory / "git.log"
    log.touch()
    shim = directory / "git"
    shim.write_text(
        _CENSUS_SHIM.replace("__LOG__", shlex.quote(str(log)))
        .replace("__GIT__", shlex.quote(_GIT_EXECUTABLE)),
        encoding="utf-8",
    )
    shim.chmod(0o755)
    seen = 0

    def counts() -> Counter[str]:
        nonlocal seen
        rows = log.read_text(encoding="utf-8").splitlines()[seen:]
        seen += len(rows)
        census: Counter[str] = Counter()
        for row in rows:
            verb, _, detail = row.partition(" ")
            census[f"cat-file {detail}" if verb == "cat-file" else verb] += 1
        return census

    with monkeypatch.context() as patch:
        patch.setattr(git_module, "_GIT_EXECUTABLE", str(shim))
        assert repo.git("rev-parse", "--is-inside-work-tree").returncode == 0
        assert counts() == {"rev-parse": 1}
        yield counts


@pytest.fixture(
    params=[("sha1", "files"), ("sha256", "files"), ("sha1", "reftable"), ("sha256", "reftable")],
    ids=lambda value: "-".join(value),
)
def format_repo(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> GitFixture:
    monkeypatch.setenv("GIT_DEFAULT_HASH", request.param[0])
    monkeypatch.setenv("GIT_DEFAULT_REF_FORMAT", request.param[1])
    repository: GitFixture = request.getfixturevalue("git_repo")
    repo = Repository(repository.root)
    assert (repo.object_format, repo.ref_format) == request.param
    return repository


def test_a_certified_canonical_read_starts_the_same_git_processes_at_every_length(
    format_repo: GitFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
) -> None:
    repo = Repository(format_repo.root)
    wal = Wal(repo)
    observed: dict[int, dict[str, int]] = {}
    with _census(tmp_path / "census", monkeypatch, repo) as counts:
        for events in (1, 2, 4, 8):
            while len(wal.read().events) < events:
                wal.append("claim", "alpha", {"paths": ["a.txt"]})
            counts()
            snapshot = wal.read()
            observed[events] = dict(counts())
            assert len(snapshot.events) == events
    record_property("git_processes_by_events", json.dumps(observed, sort_keys=True))
    assert all(census == _CERTIFIED_CENSUS for census in observed.values()), observed


def test_a_git_put_first_on_path_after_import_is_not_the_git_that_runs(
    git_repo: GitFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The known-bad census: a PATH shim installed after import observes nothing."""

    repo = Repository(git_repo.root)
    wal = Wal(repo)
    wal.append("claim", "alpha", {"paths": ["a.txt"]})
    late = tmp_path / "late"
    late.mkdir()
    log = late / "git.log"
    log.touch()
    shim = late / "git"
    shim.write_text(
        _CENSUS_SHIM.replace("__LOG__", shlex.quote(str(log)))
        .replace("__GIT__", shlex.quote(_GIT_EXECUTABLE)),
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{late}{os.pathsep}{os.environ['PATH']}")
    assert shutil.which("git") == str(shim)
    assert len(wal.read().events) == 1
    assert log.read_text(encoding="utf-8") == ""
    monkeypatch.setattr(git_module, "_GIT_EXECUTABLE", str(shim))
    assert len(wal.read().events) == 1
    assert len(log.read_text(encoding="utf-8").splitlines()) == sum(_CERTIFIED_CENSUS.values())


def test_a_writer_produced_wal_is_certified_as_the_sequential_readers_snapshot(
    format_repo: GitFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = Repository(format_repo.root)
    wal = Wal(repo)
    for number in range(3):
        wal.append("claim", "alpha", {"paths": [f"f{number}.txt"]})
    with _paths(monkeypatch) as taken:
        certified = _outcome(wal.read)
    assert taken == ["canonical"]
    assert certified == _sequential(wal, monkeypatch)
    assert len(certified["snapshot"].events) == 3


def _swap(root: Path, commits: list[str], kind: str) -> None:
    record = commits[1]
    tree = _git(root, "rev-parse", f"{record}^{{tree}}")
    blob = _git(root, "rev-parse", f"{record}:event.json")
    substitute_blob = _blob(root, _encoded(_document(9)))
    substitute_tree = _tree(root, f"100644 blob {substitute_blob}\tevent.json\n")
    targets = {
        "blob": (blob, substitute_blob),
        "tree": (tree, substitute_tree),
        "commit": (record, _git(root, "commit-tree", substitute_tree, "-p", commits[0],
                                stdin=b"substitute\n")),
    }
    target, source = targets[kind]
    path = _loose(root, target)
    path.chmod(0o644)
    path.write_bytes(_loose(root, source).read_bytes())


def _shape(root: Path, shape: str) -> None:
    first = _event_tree(root, _encoded(_document(1)))
    third = _event_tree(root, _encoded(_document(3)))
    blob = _blob(root, _encoded(_document(2)))
    raw = bytes.fromhex(blob)
    trees = {
        "extra-entry": lambda: _tree(
            root, f"100644 blob {blob}\tevent.json\n100644 blob {blob}\tother.json\n"),
        "empty-tree": lambda: _tree(root, ""),
        "wrong-name": lambda: _tree(root, f"100644 blob {blob}\tevents.json\n"),
        "mode-100755": lambda: _tree(root, f"100755 blob {blob}\tevent.json\n"),
        "subtree": lambda: _tree(root, f"040000 tree {_tree(root, '')}\tevent.json\n"),
        "symlink": lambda: _tree(root, f"120000 blob {blob}\tevent.json\n"),
        "gitlink": lambda: _tree(root, f"160000 commit {'1' * len(blob)}\tevent.json\n"),
        "mode-100664": lambda: _raw_tree(root, b"100664 event.json\0" + raw),
        "mode-100600": lambda: _raw_tree(root, b"100600 event.json\0" + raw),
        "zero-padded-mode": lambda: _raw_tree(root, b"0100644 event.json\0" + raw),
        "entry-names-tree": lambda: _raw_tree(
            root, b"100644 event.json\0" + bytes.fromhex(first)),
        "entry-names-commit": lambda: _raw_tree(
            root, b"100644 event.json\0" + bytes.fromhex(_chain(root, [first])[0])),
        "blob-not-utf8": lambda: _event_tree(root, b'{"v": "\xff"}\n'),
        "blob-not-json": lambda: _event_tree(root, b"not json\n"),
        "schema-version-zero": lambda: _event_tree(root, _encoded(_document(2, v=0))),
        "deeply-nested-json": lambda: _event_tree(
            root, b"[" * 100_000 + b"]" * 100_000 + b"\n"),
        "integer-over-digit-limit": lambda: _event_tree(
            root, _encoded(_document(2))[:-2] + b',"n":' + b"9" * 5000 + b"}\n"),
        "duplicate-json-key": lambda: _event_tree(
            root, _encoded(_document(2))[:-2] + b',"actor":"beta"}\n'),
        "legacy-adopted-v1": lambda: _event_tree(root, _encoded(
            _document(2, event="adopted", v=1, data={"operation": "a,b"}))),
    }
    if shape in trees:
        commits = _chain(root, [first, trees[shape](), third])
    else:
        commits = _chain(root, [first, _event_tree(root, _encoded(_document(2))), third])
    if shape == "root-tree-missing":
        _loose(root, _git(root, "rev-parse", f"{commits[1]}^{{tree}}")).unlink()
    elif shape == "event-blob-missing":
        _loose(root, _git(root, "rev-parse", f"{commits[1]}:event.json")).unlink()
    elif shape in {"commit-bytes-renamed", "tree-bytes-renamed", "blob-bytes-renamed"}:
        _swap(root, commits, shape.split("-", 1)[0])
    elif shape == "merge-at-tip":
        merge = _git(root, "commit-tree", third, "-p", commits[1], "-p", commits[0],
                     stdin=b"merge\n")
        _git(root, "update-ref", WAL_REF, merge)
    elif shape == "shallow-boundary":
        (root / ".git" / "shallow").write_text(commits[1] + "\n", encoding="ascii")
    elif shape in {"commit-without-message", "uppercase-tree-header"}:
        tree = _git(root, "rev-parse", f"{commits[1]}^{{tree}}")
        header = tree.upper() if shape == "uppercase-tree-header" else tree
        identity = "Round A Tests <round-a@example.invalid> 1700000000 +0000"
        body = f"tree {header}\nparent {commits[0]}\nauthor {identity}\ncommitter {identity}\n"
        if shape == "uppercase-tree-header":
            body += "\nuppercase tree header\n"
        second = _git(root, "hash-object", "-w", "-t", "commit", "--literally", "--stdin",
                      stdin=body.encode("ascii"))
        _chain(root, [third], start=second)


# (shape, whether the canonical path certifies it). Every shape reads as the sequential reader
# reads it; the three `_read_event` refusals are named in the assertions below.
_SHAPES = [
    ("extra-entry", False), ("empty-tree", False), ("wrong-name", False),
    ("mode-100755", False), ("subtree", False), ("symlink", False), ("gitlink", False),
    ("mode-100664", False), ("mode-100600", False), ("zero-padded-mode", False),
    ("entry-names-tree", False), ("entry-names-commit", False),
    ("root-tree-missing", False), ("event-blob-missing", False),
    ("blob-not-utf8", False), ("blob-not-json", False), ("schema-version-zero", False),
    ("deeply-nested-json", False), ("integer-over-digit-limit", False),
    ("commit-bytes-renamed", False), ("tree-bytes-renamed", False),
    ("blob-bytes-renamed", False), ("merge-at-tip", False),
    ("commit-without-message", False),
    ("duplicate-json-key", True), ("legacy-adopted-v1", True),
    ("shallow-boundary", True), ("uppercase-tree-header", True),
]

_REFUSALS = {
    "extra-entry": ("WalCorruptError", "WAL event tree must contain exactly event.json", None),
    "empty-tree": ("WalCorruptError", "WAL event tree must contain exactly event.json", None),
    "wrong-name": ("WalCorruptError", "WAL event tree has an invalid entry", None),
    "mode-100755": ("WalCorruptError", "WAL event tree has an invalid entry", None),
    "subtree": ("WalCorruptError", "WAL event tree has an invalid entry", None),
    "symlink": ("WalCorruptError", "WAL event tree has an invalid entry", None),
    "gitlink": ("WalCorruptError", "WAL event tree has an invalid entry", None),
    "entry-names-tree": ("WalCorruptError", "WAL event.json is not valid UTF-8 JSON", "GitError"),
    "entry-names-commit": (
        "WalCorruptError", "WAL event.json is not valid UTF-8 JSON", "GitError"),
    "event-blob-missing": (
        "WalCorruptError", "WAL event.json is not valid UTF-8 JSON", "GitError"),
    "blob-not-utf8": (
        "WalCorruptError", "WAL event.json is not valid UTF-8 JSON", "UnicodeDecodeError"),
    "blob-not-json": (
        "WalCorruptError", "WAL event.json is not valid UTF-8 JSON", "JSONDecodeError"),
    "schema-version-zero": ("WalCorruptError", "WAL schema version is invalid", None),
    "root-tree-missing": ("GitError", "git ls-tree failed with exit 128", None),
    "commit-bytes-renamed": ("GitError", "hash mismatch", None),
    "tree-bytes-renamed": ("GitError", "hash mismatch", None),
    "deeply-nested-json": (
        "WalCorruptError", "could not be decoded within the active interpreter limits",
        "RecursionError"),
    "integer-over-digit-limit": (
        "WalCorruptError", "could not be decoded within the active interpreter limits",
        "ValueError"),
    "merge-at-tip": (
        "WalCorruptError", "each WAL commit must have exactly one parent except the root", None),
}


@pytest.mark.parametrize(("shape", "certified"), _SHAPES)
def test_every_shape_reads_as_the_sequential_reader_reads_it(
    git_repo: GitFixture, monkeypatch: pytest.MonkeyPatch, shape: str, certified: bool
) -> None:
    root = git_repo.root
    _shape(root, shape)
    wal = Wal(Repository(root))
    with _paths(monkeypatch) as taken:
        outcome = _outcome(wal.read)
    assert taken == ["canonical" if certified else "sequential"]
    assert outcome == _sequential(wal, monkeypatch)
    if shape in _REFUSALS:
        kind, message, cause = _REFUSALS[shape]
        assert outcome["raised"] == kind
        assert message in outcome["message"]
        assert (outcome["cause"] or [None])[0] == cause
        if kind == "WalCorruptError" and shape != "merge-at-tip":
            second = _git(root, "rev-list", "--reverse", WAL_REF).split()[1]
            limit = shape in {"deeply-nested-json", "integer-over-digit-limit"}
            assert outcome["details"] == {
                "oid": second, **({"reason": "json_decode_limit"} if limit else {}),
            }
    else:
        assert "snapshot" in outcome


@pytest.mark.parametrize(
    ("second", "fourth", "message"),
    [
        ("schema", "blob-missing", "WAL schema version is invalid"),
        ("blob-missing", "schema", "WAL event.json is not valid UTF-8 JSON"),
        ("schema", "tree-missing", "WAL schema version is invalid"),
        ("extra-entry", "schema", "WAL event tree must contain exactly event.json"),
    ],
)
def test_the_first_failure_in_chain_order_is_the_one_raised(
    git_repo: GitFixture, monkeypatch: pytest.MonkeyPatch, second: str, fourth: str, message: str
) -> None:
    root = git_repo.root

    def tree(damage: str, number: int) -> str:
        if damage == "schema":
            return _event_tree(root, _encoded(_document(number, v=0)))
        if damage == "extra-entry":
            blob = _blob(root, _encoded(_document(number)))
            return _tree(root, f"100644 blob {blob}\tevent.json\n100644 blob {blob}\tx.json\n")
        return _event_tree(root, _encoded(_document(number)))

    commits = _chain(root, [
        _event_tree(root, _encoded(_document(1))), tree(second, 2),
        _event_tree(root, _encoded(_document(3))), tree(fourth, 4),
    ])
    for record, damage in ((commits[1], second), (commits[3], fourth)):
        if damage == "blob-missing":
            _loose(root, _git(root, "rev-parse", f"{record}:event.json")).unlink()
        elif damage == "tree-missing":
            _loose(root, _git(root, "rev-parse", f"{record}^{{tree}}")).unlink()
    wal = Wal(Repository(root))
    with _paths(monkeypatch) as taken:
        outcome = _outcome(wal.read)
    assert taken == ["sequential"]
    assert outcome == _sequential(wal, monkeypatch)
    assert outcome["message"] == message
    assert outcome["details"] == {"oid": commits[1]}


def test_each_call_reads_the_refs_and_objects_as_they_are_now(
    git_repo: GitFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = git_repo.root
    wal = Wal(Repository(root))
    wal.append("claim", "alpha", {"paths": ["a.txt"]})
    wal.append("claim", "alpha", {"paths": ["b.txt"]})
    readings: list[tuple[int | str, list[str]]] = []

    def reading() -> None:
        with _paths(monkeypatch) as taken:
            outcome = _outcome(wal.read)
        assert outcome == _sequential(wal, monkeypatch)
        count = len(outcome["snapshot"].events) if "snapshot" in outcome else outcome["message"]
        readings.append((count, taken))

    reading()
    wal.append("claim", "alpha", {"paths": ["c.txt"]})
    reading()
    head = _git(root, "rev-parse", WAL_REF)
    valid = _git(root, "commit-tree", _event_tree(root, _encoded(_document(4))), "-p", head,
                 stdin=b"valid\n")
    _git(root, "update-ref", WAL_REF, valid)
    reading()
    blob = _blob(root, _encoded(_document(5)))
    extra = _git(root, "commit-tree",
                 _tree(root, f"100644 blob {blob}\tevent.json\n100644 blob {blob}\tx.json\n"),
                 "-p", valid, stdin=b"extra entry\n")
    _git(root, "update-ref", WAL_REF, extra)
    reading()
    _git(root, "update-ref", WAL_REF, valid)
    reading()
    path = _loose(root, _git(root, "rev-parse", f"{head}:event.json"))
    retained = path.read_bytes()
    path.unlink()
    reading()
    path.write_bytes(retained)
    reading()
    assert readings == [
        (2, ["canonical"]),
        (3, ["canonical"]),
        (4, ["canonical"]),
        ("WAL event tree must contain exactly event.json", ["sequential"]),
        (4, ["canonical"]),
        ("WAL event.json is not valid UTF-8 JSON", ["sequential"]),
        (4, ["canonical"]),
    ]


def test_batch_and_single_git_reads_run_with_the_same_options_and_environment(
    git_repo: GitFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = Repository(git_repo.root)
    wal = Wal(repo)
    for number in range(3):
        wal.append("claim", "alpha", {"paths": [f"f{number}.txt"]})
    expected = _sequential(wal, monkeypatch)
    calls: list[tuple[list[str], dict[str, str]]] = []
    original = subprocess.run

    def recorded(arguments: list[str], **options: Any) -> Any:
        calls.append((list(arguments), dict(options["env"])))
        return original(arguments, **options)

    with monkeypatch.context() as patch:
        patch.setenv("GIT_DIR", str(git_repo.root / "elsewhere"))
        patch.setenv("GIT_CONFIG_PARAMETERS", "'core.commitgraph'='true'")
        patch.setattr(subprocess, "run", recorded)
        with _paths(monkeypatch) as taken:
            outcome = _outcome(wal.read)
    assert taken == ["canonical"]
    assert outcome == expected
    prefix = [str(git_module._GIT_EXECUTABLE), *git_module._GIT_COMMON_OPTIONS,
              "-C", str(repo.root)]
    verbs = [arguments[len(prefix)] for arguments, _ in calls]
    assert verbs == ["rev-parse", "rev-list", "cat-file", "cat-file", "cat-file"]
    assert all(arguments[: len(prefix)] == prefix for arguments, _ in calls)
    environments = [environment for _, environment in calls]
    assert all(environment == environments[0] for environment in environments)
    assert environments[0]["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert environments[0]["GIT_GRAFT_FILE"] == os.devnull
    assert "GIT_DIR" not in environments[0]
    assert "GIT_CONFIG_PARAMETERS" not in environments[0]


def test_replace_refs_reach_neither_reader(
    git_repo: GitFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Exercise a replacement-capable caller; each reader must suppress replacements itself.
    monkeypatch.delenv("GIT_NO_REPLACE_OBJECTS", raising=False)
    root = git_repo.root
    commits = _canonical_chain(root, 3)
    wal = Wal(Repository(root))
    before = _outcome(wal.read)
    blob = _git(root, "rev-parse", f"{commits[1]}:event.json")
    tree = _git(root, "rev-parse", f"{commits[1]}^{{tree}}")
    substitute_blob = _blob(root, _encoded(_document(8)))
    substitute_tree = _event_tree(root, _encoded(_document(9)))
    substitute_commit = _git(root, "commit-tree", _event_tree(root, _encoded(_document(7))),
                             "-p", commits[0], stdin=b"substitute\n")
    _git(root, "replace", blob, substitute_blob)
    _git(root, "replace", tree, substitute_tree)
    _git(root, "replace", commits[1], substitute_commit)
    # Control: without the reader's environment every replacement is live.
    assert _git(root, "cat-file", "blob", blob) == _encoded(_document(8)).decode().strip()
    assert _git(root, "rev-parse", f"{commits[1]}^{{tree}}") != tree
    with _paths(monkeypatch) as taken:
        after = _outcome(wal.read)
    assert taken == ["canonical"]
    assert after == before
    assert after == _sequential(wal, monkeypatch)


def test_a_batch_that_cannot_spool_is_declined(
    git_repo: GitFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = git_repo.root
    _canonical_chain(root, 3)
    wal = Wal(Repository(root))
    expected = _sequential(wal, monkeypatch)
    with monkeypatch.context() as patch:
        patch.setattr(tempfile, "tempdir", str(tmp_path / "absent"))
        with _paths(monkeypatch) as taken:
            outcome = _outcome(wal.read)
    assert taken == ["sequential"]
    assert outcome == expected
    assert len(outcome["snapshot"].events) == 3


def test_a_batch_git_failure_is_declined(
    git_repo: GitFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = git_repo.root
    _canonical_chain(root, 2)
    wal = Wal(Repository(root))
    expected = _sequential(wal, monkeypatch)

    def failing(self: Repository, oids: Any, *, reference: str) -> Iterator[Any]:
        raise GitError("git cat-file failed with exit 128: injected", operation="cat-file")

    with monkeypatch.context() as patch:
        patch.setattr(Repository, "cat_file_batch", failing)
        with _paths(monkeypatch) as taken:
            outcome = _outcome(wal.read)
    assert taken == ["sequential"]
    assert outcome == expected


def test_history_rows_outside_the_canonical_chain_are_declined(git_repo: GitFixture) -> None:
    root = git_repo.root
    commits = _canonical_chain(root, 2)
    wal = Wal(Repository(root))
    head = commits[1]
    blob = _git(root, "rev-parse", f"{head}:event.json")
    declined = [
        (head, b"\xff\n"),  # not ASCII
        (head, b"\n"),  # an empty row
        (head, b"xyz\n"),  # not an object ID
        (head, f"{commits[0]} {head}\n{head} {commits[0]}\n".encode()),  # a root with a parent
        (head, f"{commits[0]}\n".encode()),  # does not end at the head
        (blob, f"{blob}\n".encode()),  # a row naming a blob
    ]
    for tip, output in declined:
        assert wal._read_canonical(tip, output) is None, output
    history = f"{commits[0]}\n{head} {commits[0]}\n".encode()
    assert wal._read_canonical(head, history) == wal.read()


def test_the_recursion_limit_refuses_the_same_nesting_on_both_paths(
    git_repo: GitFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At the deepest nesting the sequential reader decodes, the batched read certifies it.

    Every read below is made by ``attempt`` called from this function's own frame, so the
    boundary is searched at the call depth it is then compared at. Paths are told apart by the
    Git processes a read starts, not by wrapping ``_read_canonical``, which would add a frame.
    """

    root = git_repo.root
    repo = Repository(root)
    wal = Wal(repo)
    opening, _, closing = _encoded(_document(1, data={"nest": "@"})).partition(b'"@"')

    def attempt(depth: int, *, sequential: bool) -> dict[str, Any]:
        tree = _event_tree(root, opening + b"[" * depth + b"]" * depth + closing)
        _git(root, "update-ref", WAL_REF, _git(root, "commit-tree", tree, stdin=b"nested\n"))
        with monkeypatch.context() as patch:
            if sequential:
                patch.setattr(Wal, "_read_canonical", lambda self, head, output: None)
            outcome = _outcome(wal.read)
        if "snapshot" not in outcome:
            assert outcome["raised"] == "WalCorruptError", outcome
            assert outcome["cause"][0] == "RecursionError", outcome
        return outcome

    low, high = 1, 64
    while "snapshot" in attempt(high, sequential=True):
        low, high = high, high * 2
        assert high <= 1 << 22
    while high - low > 1:
        middle = (low + high) // 2
        if "snapshot" in attempt(middle, sequential=True):
            low = middle
        else:
            high = middle
    with _census(tmp_path / "census", monkeypatch, repo) as counts:
        decoded = attempt(low, sequential=False)
        decoded_census = counts()
        refused = attempt(high, sequential=False)
        refused_census = counts()
    assert "snapshot" in decoded
    assert decoded_census == _CERTIFIED_CENSUS
    assert refused["raised"] == "WalCorruptError"
    assert refused["cause"][0] == "RecursionError"
    assert refused["details"] == {"oid": wal.head(), "reason": "json_decode_limit"}
    assert refused_census == {**_CERTIFIED_CENSUS, "ls-tree": 1, "cat-file blob": 1}
