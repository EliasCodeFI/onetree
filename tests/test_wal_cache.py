"""With `onetree.walCache` on, readers keep a copy of the verified log and read only new events.

Known bad: main 0215e36, which ignores the setting and reads and checks the whole chain on every
command (838 ms per `onetree check` at 10,000 events, measured on 2026-11-13).
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from pathlib import Path

import pytest
from conftest import GitFixture

from onetree.cli import main
from onetree.core import Coordinator
from onetree.wal import WAL_REF


def run(argv: Sequence[str]) -> int:
    try:
        return main(list(argv))
    except SystemExit as exit_:
        return int(exit_.code or 0)


def cache_file(git_repo: GitFixture) -> Path:
    return git_repo.root / ".git" / "onetree" / "cache" / "wal.jsonl"


def truth(git_repo: GitFixture) -> list[tuple[str, str]]:
    """Every event as git holds it, read with the cache bypassed."""

    snapshot = Coordinator(git_repo.root).inspect_wal()
    return [
        (event.oid, json.dumps(dict(event.document), sort_keys=True)) for event in snapshot.events
    ]


def seen(git_repo: GitFixture) -> list[tuple[str, str]]:
    snapshot = Coordinator(git_repo.root).wal.read()
    return [
        (event.oid, json.dumps(dict(event.document), sort_keys=True)) for event in snapshot.events
    ]


@pytest.fixture
def cached_repo(git_repo: GitFixture) -> GitFixture:
    git_repo.git("config", "onetree.walCache", "true")
    return git_repo


def test_without_the_setting_no_copy_is_kept(git_repo: GitFixture) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    coordinator.claim("alpha", [str(git_repo.root / "a.txt")])
    assert not cache_file(git_repo).exists()


def test_the_copy_is_written_extended_and_matches_git(cached_repo: GitFixture) -> None:
    coordinator = Coordinator(cached_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    assert seen(cached_repo) == truth(cached_repo)
    assert cache_file(cached_repo).is_file()
    for name in ("a.txt", "b.txt"):
        coordinator.claim("alpha", [str(cached_repo.root / name)])
        assert seen(cached_repo) == truth(cached_repo)
    lines = cache_file(cached_repo).read_bytes().splitlines()
    assert len(lines) == 1 + len(truth(cached_repo))
    assert (
        run(
            [
                "check",
                str(cached_repo.root / "a.txt"),
                "--repo",
                str(cached_repo.root),
                "--session",
                "beta",
            ]
        )
        == 1
    )


def test_an_unsound_copy_is_ignored_and_rewritten(cached_repo: GitFixture) -> None:
    coordinator = Coordinator(cached_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    seen(cached_repo)
    path = cache_file(cached_repo)
    assert path.is_file()
    good = path.read_bytes()
    for broken in (
        b"not a cache\n",
        good[:-5],
        good.replace(b'"event":', b'"evnt":', 1) + b"[1]\n",
        good + b'["0",[],{}]\n',
    ):
        path.write_bytes(broken)
        assert seen(cached_repo) == truth(cached_repo)
        assert path.read_bytes() == good


def test_a_log_that_went_back_or_was_replaced_is_read_in_full(cached_repo: GitFixture) -> None:
    coordinator = Coordinator(cached_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    first = cached_repo.git("rev-parse", WAL_REF).stdout.strip()
    coordinator.claim("alpha", [str(cached_repo.root / "a.txt")])
    assert len(seen(cached_repo)) == 2
    cached_repo.git("update-ref", WAL_REF, first)
    assert seen(cached_repo) == truth(cached_repo)
    assert len(seen(cached_repo)) == 1


def test_inspection_reads_git_not_the_copy(
    cached_repo: GitFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    coordinator = Coordinator(cached_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    seen(cached_repo)
    path = cache_file(cached_repo)
    assert path.is_file()
    path.write_bytes(path.read_bytes().replace(b'"actor":"alpha"', b'"actor":"forged"'))
    assert run(["wal", "--repo", str(cached_repo.root), "--json"]) == 0
    events = json.loads(capsys.readouterr().out)["wal"]["events"]
    assert [event["actor"] for event in events] == ["alpha"]


def test_a_copy_that_cannot_be_written_is_not_needed(cached_repo: GitFixture) -> None:
    folder = cached_repo.root / ".git" / "onetree" / "cache"
    folder.parent.mkdir(parents=True, exist_ok=True)
    folder.write_text("a file where the folder would be\n", encoding="utf-8")
    coordinator = Coordinator(cached_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    assert seen(cached_repo) == truth(cached_repo)
