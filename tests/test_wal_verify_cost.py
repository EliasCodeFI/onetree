"""Landed-evidence verification reads Git objects in one batch per call.

Every mutation and inspection re-verifies the recorded landed and adopted commits
against Git. These cases pin the cost of that verification to one Git
process per call for ordinary records, and hold the admission semantics fixed for
the shapes Git may hand back instead of a commit: an object that is absent,
corrupt, truncated, a blob, or a tag, and a record whose parent, tree or landing
operation disagrees with the commit it names. Nothing is cached across calls: an
object that vanishes between two public calls under a stable WAL head is refused
by the second call and admitted again once it is back.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from conftest import _GIT_EXECUTABLE, GitFixture

from onetree import git as git_module
from onetree.core import Coordinator
from onetree.errors import GitError, WalCorruptError


@pytest.fixture(params=["sha1", "sha256"])
def ref_repo(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> GitFixture:
    monkeypatch.setenv("GIT_DEFAULT_HASH", request.param)
    monkeypatch.setenv("GIT_DEFAULT_REF_FORMAT", "files")
    repository: GitFixture = request.getfixturevalue("git_repo")
    assert Coordinator(repository.root).repo.object_format == request.param
    return repository


def _adopt(
    coordinator: Coordinator,
    git_repo: GitFixture,
    index: Path,
    number: int,
    *,
    record: Callable[[str, str, str, str], dict[str, Any]] | None = None,
) -> tuple[str, str, str, str]:
    """Land one real marked commit on main and record it as adopted; return its facts.

    ``record`` overrides fields of the adopted document so that a damaged record
    is the only one naming its landing: the fold refuses a second record for the
    same commit and operation before Git is ever asked.
    """

    git_repo.write("a.txt", f"adopted payload {number}\n")
    git_repo.stage(index, "a.txt")
    tree = coordinator.repo.write_tree(index)
    parent = git_repo.oid()
    operation = f"{number:032x}"
    commit = coordinator.repo.commit_tree(
        tree,
        coordinator.repo.landing_message("adopted", operation, "f" * 32, "alpha"),
        (parent,),
    )
    assert coordinator.repo.update_ref("refs/heads/main", commit, parent)
    document = coordinator.wal.document(
        "adopted",
        "operator",
        {
            "operation": operation,
            "target": "refs/heads/main",
            "expected_head": parent,
            "commit": commit,
            "tree": tree,
            **(record(commit, tree, parent, operation) if record is not None else {}),
        },
    )
    assert coordinator.wal.try_append((document,), coordinator.wal.head()) is not None
    return commit, tree, parent, operation


def _loose_object(git_repo: GitFixture, oid: str) -> Path:
    path = git_repo.root / ".git" / "objects" / oid[:2] / oid[2:]
    assert path.exists(), "fixture objects are loose"
    return path


def _verb(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    """Name a child process by its Git verb: the argument after ``-C <dir>``."""

    argv = list(args[0] if args else kwargs.get("args", ()))
    if "-C" in argv and len(argv) > argv.index("-C") + 2:
        return str(argv[argv.index("-C") + 2])
    return str(argv[0]) if argv else "?"


@contextmanager
def _counting(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Counter[str], Counter[str]]]:
    """Count every child process, and separately those started inside _verify_landed.

    The count sits on ``subprocess.Popen``, the one call every child goes
    through (``subprocess.run`` is built on it), so a batch read that bypasses
    ``Repository.git`` is counted like any other process. Both wrappers forward
    unchanged; nothing decides differently because it is counted.
    """

    total: Counter[str] = Counter()
    inside: Counter[str] = Counter()
    depth = 0
    original_popen = subprocess.Popen
    original_verify = Coordinator._verify_landed

    def counted_popen(*args: Any, **kwargs: Any) -> Any:
        total[_verb(args, kwargs)] += 1
        if depth:
            inside[_verb(args, kwargs)] += 1
        return original_popen(*args, **kwargs)

    def counted_verify(self: Coordinator, state: Any) -> None:
        nonlocal depth
        depth += 1
        try:
            original_verify(self, state)
        finally:
            depth -= 1

    monkeypatch.setattr(subprocess, "Popen", counted_popen)
    monkeypatch.setattr(Coordinator, "_verify_landed", counted_verify)
    yield total, inside


@pytest.mark.parametrize("landed", [1, 5, 20])
@pytest.mark.parametrize("action", ["register", "inspect"])
def test_verification_issues_one_git_process_per_call(
    ref_repo: GitFixture,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
    landed: int,
    action: str,
) -> None:
    git_repo = ref_repo
    coordinator = Coordinator(git_repo.root)
    session = coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    coordinator.claim("alpha", [str(git_repo.root / "a.txt")])
    for number in range(landed):
        _adopt(coordinator, git_repo, session.index_path, number)
    assert coordinator.inspect_wal().consistent
    actions: dict[str, Callable[[], object]] = {
        "register": lambda: coordinator.register_session("beta", pid=os.getpid(), ttl=300),
        "inspect": lambda: coordinator.inspect_wal(),
    }
    head = git_repo.oid()
    with _counting(monkeypatch) as (total, inside):
        # The counter proves itself on one known process before it is believed.
        assert coordinator.repo.git("rev-parse", "HEAD").stdout.decode().strip() == head
        assert total == {"rev-parse": 1}
        assert not inside
        total.clear()
        actions[action]()
    record_property("landed", landed)
    record_property("git_calls_total", json.dumps(dict(sorted(total.items()))))
    record_property("git_calls_in_verify_landed", json.dumps(dict(sorted(inside.items()))))
    assert inside["show"] == 0
    assert sum(inside.values()) == 1, dict(inside)
    assert total["show"] == 0


@pytest.mark.parametrize(
    "shape",
    [
        "missing",
        "corrupt",
        "truncated",
        "blob",
        "tag",
        "wrong_parent",
        "wrong_tree",
        "wrong_operation",
    ],
)
@pytest.mark.parametrize("caller", ["inspect", "register", "claim"])
def test_a_record_git_cannot_confirm_is_refused_before_any_write(
    ref_repo: GitFixture, shape: str, caller: str
) -> None:
    git_repo = ref_repo
    coordinator = Coordinator(git_repo.root)
    session = coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    coordinator.claim("alpha", [str(git_repo.root / "a.txt")])
    expected: type[Exception] = WalCorruptError
    match: str | None = "differs from WAL"
    damage: Callable[[str, str, str, str], dict[str, Any]] | None = None
    if shape == "blob":
        damage = lambda commit, tree, parent, operation: {  # noqa: E731 - table row
            "commit": git_repo.git("rev-parse", f"{commit}:a.txt").stdout.strip()
        }
        match = "does not resolve"
    elif shape == "tag":
        def damage(commit: str, tree: str, parent: str, operation: str) -> dict[str, Any]:
            git_repo.git("tag", "-a", "-m", "landing tag", "landing-tag", commit)
            return {"commit": git_repo.git("rev-parse", "refs/tags/landing-tag").stdout.strip()}
    elif shape == "wrong_parent":
        damage = lambda commit, tree, parent, operation: {  # noqa: E731 - table row
            "expected_head": coordinator.repo.zero_oid
        }
        match = "parent differs"
    elif shape == "wrong_tree":
        damage = lambda commit, tree, parent, operation: {  # noqa: E731 - table row
            "tree": coordinator.repo.git_text("show", "-s", "--format=%T", parent)
        }
        match = "tree differs"
    elif shape == "wrong_operation":
        damage = lambda commit, tree, parent, operation: {  # noqa: E731 - table row
            "operation": "d" * 32
        }
        match = "landing trailer"
    first, _, _, _ = _adopt(coordinator, git_repo, session.index_path, 0, record=damage)
    # The damaged record is not the branch tip, so resolving the target still
    # succeeds and only the landed-evidence verification can refuse.
    _adopt(coordinator, git_repo, session.index_path, 1)
    if shape == "missing":
        _loose_object(git_repo, first).unlink()
        match = "does not resolve"
    elif shape == "corrupt":
        path = _loose_object(git_repo, first)
        path.chmod(0o644)
        path.write_bytes(b"\x00not a zlib stream" * 4)
        expected = GitError
        match = None
    elif shape == "truncated":
        path = _loose_object(git_repo, first)
        raw = path.read_bytes()
        path.chmod(0o644)
        path.write_bytes(raw[: len(raw) // 2])
        expected = GitError
        match = None
    head_before = coordinator.wal.head()
    target_before = git_repo.oid()
    actions: dict[str, Callable[[], object]] = {
        "inspect": lambda: coordinator.inspect_wal(),
        "register": lambda: coordinator.register_session("beta", pid=os.getpid(), ttl=300),
        "claim": lambda: coordinator.claim("alpha", [str(git_repo.root / "b.txt")]),
    }
    with pytest.raises(expected, match=match) as refused:
        actions[caller]()
    if expected is GitError:
        assert not isinstance(refused.value, WalCorruptError)
    assert coordinator.wal.head() == head_before
    assert git_repo.oid() == target_before


def test_an_object_that_vanishes_under_a_stable_wal_head_is_refused_until_it_returns(
    ref_repo: GitFixture,
) -> None:
    git_repo = ref_repo
    coordinator = Coordinator(git_repo.root)
    session = coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    coordinator.claim("alpha", [str(git_repo.root / "a.txt")])
    first, _, _, _ = _adopt(coordinator, git_repo, session.index_path, 0)
    _adopt(coordinator, git_repo, session.index_path, 1)
    coordinator.register_session("beta", pid=os.getpid(), ttl=300)
    assert coordinator.inspect_wal().consistent
    head = coordinator.wal.head()
    path = _loose_object(git_repo, first)
    raw = path.read_bytes()
    path.unlink()
    with pytest.raises(WalCorruptError, match="does not resolve"):
        coordinator.register_session("gamma", pid=os.getpid(), ttl=300)
    with pytest.raises(WalCorruptError, match="does not resolve"):
        coordinator.inspect_wal()
    assert coordinator.wal.head() == head
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(raw)
    coordinator.register_session("gamma", pid=os.getpid(), ttl=300)
    assert coordinator.wal.head() != head
    assert coordinator.inspect_wal().consistent


_BOUNDED_READER = """
import json, resource, sys
resource.setrlimit(resource.RLIMIT_AS, (int(sys.argv[3]),) * 2)
sys.path.insert(0, sys.argv[1])
from onetree import core
report = core.Coordinator(sys.argv[2]).inspect_wal()
print(json.dumps({"source": core.__file__, "consistent": report.consistent,
                  "divergences": len(report.divergences)}))
"""


@pytest.mark.parametrize("count", [8, 256])
def test_a_large_ordinary_history_is_read_within_a_bounded_address_space(
    git_repo: GitFixture, tmp_path: Path, count: int
) -> None:
    """The history walk reduces each raw body as it arrives and keeps none.

    A reader that collected every body of the shared batch read before parsing
    raised MemoryError on an ordinary history of 256 one-mebibyte messages under
    a 160 MiB address space, where the earlier reader read it as consistent
    (measured 2026-09-16). The ceiling is the probe
    condition, not a product limit; the eight-commit case is the control that
    the reader itself works under it.
    """

    message = b"ordinary prose " + b"x" * (1024 * 1024 - len(b"ordinary prose "))
    stream = tmp_path / "history.fast-import"
    with stream.open("wb") as output:
        for number in range(count):
            parent = git_repo.base if number == 0 else f":{number}"
            output.write(
                f"commit refs/heads/main\nmark :{number + 1}\n"
                "committer Review <review@example.invalid> 1700000000 +0000\n"
                f"data {len(message)}\n".encode()
            )
            output.write(message)
            output.write(f"\nfrom {parent}\n\n".encode())
    with stream.open("rb") as source:
        subprocess.run(  # noqa: S603 - argv-only, the fixture's own Git
            [_GIT_EXECUTABLE, "-C", str(git_repo.root), "fast-import", "--quiet"],
            stdin=source, check=True, capture_output=True,
        )
    assert int(git_repo.git("rev-list", "--count", "HEAD").stdout) == count + 1
    head = git_repo.oid()
    source_dir = str(Path(git_module.__file__).parents[1])
    environment = {
        key: value for key, value in os.environ.items() if key != "PYTHONPATH"
    }
    read = subprocess.run(  # noqa: S603 - argv-only, this interpreter
        [sys.executable, "-B", "-c", _BOUNDED_READER, source_dir, str(git_repo.root),
         str(160 * 1024 * 1024)],
        env=environment, text=True, capture_output=True, timeout=120, check=False,
    )
    assert read.returncode == 0, read.stderr[-2000:]
    answer = json.loads(read.stdout)
    assert answer["source"].startswith(source_dir)
    assert answer["consistent"] is True
    assert answer["divergences"] == 0
    assert git_repo.oid() == head


def test_duplicate_filtering_does_not_grow_with_the_request(git_repo: GitFixture) -> None:
    """Deduplication is keyed, not scanned: equality work stays below the input size.

    Testing each name against a growing list was measured at 8128, 32640 and
    130816 string comparisons for 128, 256 and 512 names.
    The observer below forwards every comparison unchanged and
    proves itself on one true and one false comparison first.
    """

    comparisons = 0

    class ObservedOid(str):
        __slots__ = ()
        __hash__ = str.__hash__

        def __eq__(self, other: object) -> bool:
            nonlocal comparisons
            comparisons += 1
            return str.__eq__(self, other)

    same = ObservedOid("a" * 40)
    assert same == "a" * 40
    assert comparisons == 1
    assert (same == "b" * 40) is False  # `!=` would bypass the observed __eq__
    assert comparisons == 2
    repo = Coordinator(git_repo.root).repo
    for count in (128, 256, 512):
        names = [ObservedOid(f"{number:040x}") for number in range(1, count + 1)]
        comparisons = 0
        assert repo.landed_evidence(dict.fromkeys(names, ()), reference="probe") == {}
        assert comparisons <= count, (count, comparisons)


@pytest.mark.parametrize("header", ["lowercase", "uppercase"])
@pytest.mark.parametrize("caller", ["inspect", "register"])
def test_a_tree_header_git_admits_keeps_its_admission(
    ref_repo: GitFixture, header: str, caller: str
) -> None:
    """The raw tree header is read the way ``show --format=%T`` reported it.

    Git writes a commit whose tree ID is uppercase hexadecimal through ordinary
    ``hash-object -w -t commit`` and it passes ``fsck --strict``; ``show``
    reports the tree lowercase, and the earlier reader compared that. A reader
    that validated the raw header lowercase-only refused such a commit with a
    GitError (measured 2026-09-16); this one normalizes the header.
    A tree line that is not hexadecimal at all is not a reachable
    input: ``update-ref`` refuses to point a branch at such a commit even when
    ``--literally`` has written it.
    """

    git_repo = ref_repo
    tree = git_repo.git("rev-parse", f"{git_repo.base}^{{tree}}").stdout.strip()
    operation = "a" * 32
    header_tree = tree.upper() if header == "uppercase" else tree
    raw = (
        f"tree {header_tree}\nparent {git_repo.base}\n"
        "author Review <review@example.invalid> 1700000000 +0000\n"
        "committer Review <review@example.invalid> 1700000000 +0000\n\n"
        f"accepted Git object\n\nOnetree-Operation: {operation}\n"
    )
    commit = git_repo.git(
        "hash-object", "-w", "-t", "commit", "--stdin", input_text=raw
    ).stdout.strip()
    assert git_repo.git("cat-file", "commit", commit).stdout == raw
    git_repo.git("update-ref", "refs/heads/main", commit, git_repo.base)
    assert git_repo.git("show", "-s", "--format=%T", commit).stdout.strip() == tree
    git_repo.git("fsck", "--strict", "--no-reflogs")
    coordinator = Coordinator(git_repo.root)
    document = coordinator.wal.document("adopted", "operator", {
        "operation": operation, "target": "refs/heads/main",
        "expected_head": git_repo.base, "commit": commit, "tree": tree,
    })
    assert coordinator.wal.try_append((document,), None) is not None
    actions: dict[str, Callable[[], object]] = {
        "inspect": lambda: coordinator.inspect_wal(),
        "register": lambda: coordinator.register_session("beta", pid=os.getpid(), ttl=300),
    }
    actions[caller]()
    assert coordinator.inspect_wal().consistent
    assert git_repo.oid() == commit
