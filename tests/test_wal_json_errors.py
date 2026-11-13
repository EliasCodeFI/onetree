from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import GitFixture

from onetree.core import Coordinator, SessionInfo
from onetree.errors import WalCorruptError
from onetree.wal import WAL_REF, Wal

_PAYLOADS = {
    "integer_limit": (b"9" * 5000, ValueError),
    "deep_array": (b"[" * 100_000 + b"0" + b"]" * 100_000, RecursionError),
    "deep_object": (b'{"a":' * 100_000 + b"0" + b"}" * 100_000, RecursionError),
    "invalid_json": (b"[", json.JSONDecodeError),
    "invalid_utf8": (b"\xff", UnicodeDecodeError),
    "valid": (b"0", None),
}
_LIMIT_SHAPES = {"integer_limit", "deep_array", "deep_object"}
_LIMIT_MESSAGE = "WAL event.json could not be decoded within the active interpreter limits"


@pytest.fixture
def integer_limit() -> Iterator[None]:
    original = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(4300)
    try:
        yield
    finally:
        sys.set_int_max_str_digits(original)


def _append_raw(wal: Wal, value: bytes, session: SessionInfo) -> str:
    document = wal.document("claim", "alpha", {
        "path": "a.txt",
        "generation": uuid.uuid4().hex,
        "session_generation": session.generation,
        "expires_at": session.expires_at,
        "probe": "__probe__",
    })
    body = json.dumps(document, sort_keys=True).encode("utf-8")
    assert body.count(b'"__probe__"') == 1
    body = body.replace(b'"__probe__"', value)
    blob = wal.repo.git("hash-object", "-w", "--stdin", input_bytes=body).stdout.strip()
    tree = wal.repo.git(
        "mktree", input_bytes=b"100644 blob " + blob + b"\tevent.json\n"
    ).stdout.strip().decode("ascii")
    parent = wal.head()
    assert parent is not None
    commit = wal.repo.git(
        "commit-tree", tree, "-p", parent, input_bytes=b"input fixture\n"
    ).stdout.strip().decode("ascii")
    wal.repo.git("update-ref", WAL_REF, commit, parent)
    return commit


@pytest.mark.usefixtures("integer_limit")
@pytest.mark.parametrize("shape", _PAYLOADS)
@pytest.mark.parametrize("caller", ["api", "cli"])
def test_wal_json_failures_are_typed_and_leave_the_ref_unchanged(
    git_repo: GitFixture, tmp_path: Path, shape: str, caller: str
) -> None:
    coordinator = Coordinator(git_repo.root)
    session = coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    raw, cause_type = _PAYLOADS[shape]
    commit = _append_raw(coordinator.wal, raw, session)
    refs_before = git_repo.git("show-ref").stdout
    source = Path(__file__).resolve().parents[1] / "src"
    assert Path(sys.modules[Wal.__module__].__file__).resolve() == source / "onetree/wal.py"

    if caller == "api":
        caught = None
        snapshot = None
        try:
            snapshot = coordinator.wal.read()
        except Exception as error:
            caught = error
        observation = {
            "shape": shape,
            "caller": caller,
            "oid": commit,
            "exception": None if caught is None else type(caught).__name__,
            "cause": None if caught is None else type(caught.__cause__).__name__,
            "details": getattr(caught, "details", None),
        }
        (tmp_path / "observation.json").write_text(json.dumps(observation) + "\n")
        if cause_type is None:
            assert caught is None
            assert snapshot is not None
            assert len(snapshot.events) == 2
        else:
            assert type(caught) is WalCorruptError, observation
            assert type(caught.__cause__) is cause_type
            if shape in _LIMIT_SHAPES:
                assert caught.details == {"oid": commit, "reason": "json_decode_limit"}
                assert str(caught) == _LIMIT_MESSAGE
            else:
                assert caught.details == {"oid": commit}
                assert str(caught) == "WAL event.json is not valid UTF-8 JSON"
    else:
        environment = {**os.environ, "PYTHONPATH": str(source), "PYTHONINTMAXSTRDIGITS": "4300"}
        environment.pop("PYTHONVERBOSE", None)
        argv = [
            sys.executable, "-B", "-m", "onetree.cli", "wal", "--repo", str(git_repo.root),
            "--json",
        ]
        completed = subprocess.run(
            argv, capture_output=True, text=True, env=environment, check=False
        )
        (tmp_path / "observation.json").write_text(json.dumps({
            "shape": shape, "caller": caller, "oid": commit, "argv": argv,
            "pythonpath": str(source), "integer_limit": 4300,
            "rc": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr,
        }) + "\n")
        assert completed.returncode == (0 if cause_type is None else 2), completed.stderr
        assert completed.stderr == ""
        assert completed.stdout.count("\n") == 1
        payload = json.loads(completed.stdout)
        assert isinstance(payload, dict)
        if cause_type is None:
            assert payload["ok"] is True
            assert payload["wal"]["consistent"] is True
        else:
            assert payload == {
                "ok": False,
                "error": {
                    "type": "wal_corrupt",
                    "message": (
                        _LIMIT_MESSAGE if shape in _LIMIT_SHAPES
                        else "WAL event.json is not valid UTF-8 JSON"
                    ),
                    "oid": commit,
                    **({"reason": "json_decode_limit"} if shape in _LIMIT_SHAPES else {}),
                },
            }
    assert git_repo.git("show-ref").stdout == refs_before


@pytest.mark.usefixtures("integer_limit")
@pytest.mark.parametrize("first", ["integer_limit", "deep_array"])
def test_decoder_failure_reports_the_first_bad_wal_commit(
    git_repo: GitFixture, first: str
) -> None:
    coordinator = Coordinator(git_repo.root)
    session = coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    first_oid = _append_raw(coordinator.wal, _PAYLOADS[first][0], session)
    _append_raw(coordinator.wal, _PAYLOADS["invalid_json"][0], session)
    with pytest.raises(WalCorruptError) as caught:
        coordinator.wal.read()
    assert caught.value.details == {"oid": first_oid, "reason": "json_decode_limit"}
    assert type(caught.value.__cause__) is _PAYLOADS[first][1]


def test_valid_json_can_exhaust_a_deep_callers_decoder_budget(git_repo: GitFixture) -> None:
    coordinator = Coordinator(git_repo.root)
    session = coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    commit = _append_raw(coordinator.wal, b"[" * 40 + b"0" + b"]" * 40, session)
    assert len(coordinator.wal.read().events) == 2
    refs_before = git_repo.git("show-ref").stdout

    def descend(remaining: int) -> None:
        if remaining:
            descend(remaining - 1)
        else:
            coordinator.wal.read()

    original_limit = sys.getrecursionlimit()
    decoded = 0
    limited = []
    sys.setrecursionlimit(1000)
    try:
        for depth in range(850, 1000):
            try:
                descend(depth)
            except WalCorruptError as error:
                limited.append((type(error.__cause__), error.details, str(error)))
            except RecursionError:
                pass
            else:
                decoded += 1
    finally:
        sys.setrecursionlimit(original_limit)
    assert decoded > 0
    for cause, details, message in limited:
        assert cause is RecursionError
        assert details == {"oid": commit, "reason": "json_decode_limit"}
        assert message == _LIMIT_MESSAGE
    if sys.version_info[:2] == (3, 11):
        assert limited
    assert git_repo.git("show-ref").stdout == refs_before


def test_stack_exhaustion_in_git_is_not_a_json_decode_limit(
    git_repo: GitFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator = Coordinator(git_repo.root)
    session = coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    _append_raw(coordinator.wal, b"0", session)
    event = coordinator.wal.read().events[-1]
    original_git = coordinator.repo.git

    def git_command(*arguments: str, **kwargs: Any) -> Any:
        if arguments[:2] == ("cat-file", "blob"):
            raise RecursionError("git call exhausted the caller stack")
        return original_git(*arguments, **kwargs)

    monkeypatch.setattr(coordinator.repo, "git", git_command)
    with pytest.raises(RecursionError, match="git call exhausted the caller stack"):
        coordinator.wal._read_event(event.oid, event.parents)
