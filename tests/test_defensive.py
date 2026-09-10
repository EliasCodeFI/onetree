"""Focused refusal, recovery, and malformed-state coverage."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from conftest import GitFixture

from onetree import git as git_module
from onetree.cli import main
from onetree.core import Coordinator
from onetree.errors import (
    GitError,
    IdentityError,
    InvalidPathError,
    LandingRefusedError,
    ReleaseRefusedError,
    WalCorruptError,
)
from onetree.git import Repository
from onetree.wal import EventRecord, Wal


def test_registration_is_idempotent_without_resetting_index_and_identity_refuses(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    session = coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    git_repo.write("a.txt", "staged survives\n")
    git_repo.stage(session.index_path, "a.txt")
    staged_tree = coordinator.repo.write_tree(session.index_path)

    again = coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    assert again.generation == session.generation
    assert coordinator.repo.write_tree(again.index_path) == staged_tree

    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        with pytest.raises(IdentityError, match="already registered"):
            coordinator.register_session("alpha", pid=sleeper.pid, ttl=300)
    finally:
        sleeper.kill()
        sleeper.wait(timeout=5)

    for actor in ("../escape", ".", "space actor"):
        with pytest.raises(IdentityError):
            coordinator.register_session(actor, pid=os.getpid(), ttl=300)
    with pytest.raises(IdentityError, match="TTL"):
        coordinator.register_session("bad-ttl", pid=os.getpid(), ttl=0)
    with pytest.raises(IdentityError, match="not alive"):
        coordinator.register_session("dead-pid", pid=999_999_999, ttl=300)
    with pytest.raises(InvalidPathError, match="at least one"):
        coordinator.claim("alpha", [])
    with pytest.raises(IdentityError, match="not active"):
        coordinator.claim("unknown", [str(git_repo.root / "a.txt")])


def test_release_refusal_and_dead_state_gc(git_repo: GitFixture) -> None:
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    coordinator = Coordinator(git_repo.root)
    try:
        coordinator.register_session("alpha", pid=sleeper.pid, ttl=300)
        coordinator.claim("alpha", [str(git_repo.root / "a.txt")])
        with pytest.raises(ReleaseRefusedError) as refused:
            coordinator.release("alpha", [str(git_repo.root / "b.txt")])
        assert refused.value.details["reason"] == "not_held"
    finally:
        sleeper.kill()
        sleeper.wait(timeout=5)

    result = coordinator.gc("collector")
    assert result.removed_claims == 1
    assert result.removed_sessions == 1
    report = coordinator.inspect_wal()
    assert report.active_claims == ()
    assert report.active_sessions == ()
    with pytest.raises(IdentityError, match="not active"):
        coordinator.release("alpha")


def test_land_refuses_nothing_staged_wrong_target_and_diverged_target(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    session = coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    coordinator.claim("alpha", [str(git_repo.root / "a.txt")])
    with pytest.raises(LandingRefusedError) as empty:
        coordinator.land("alpha")
    assert empty.value.details["reason"] == "nothing_staged"

    git_repo.write("a.txt", "a1\n")
    git_repo.stage(session.index_path, "a.txt")
    git_repo.git("update-ref", "refs/heads/other", git_repo.base)
    with pytest.raises(LandingRefusedError) as wrong:
        coordinator.land("alpha", target="refs/heads/other")
    assert wrong.value.details["reason"] == "wrong_target"

    base_tree = git_repo.git("show", "-s", "--format=%T", git_repo.base).stdout.strip()
    orphan = git_repo.git("commit-tree", base_tree, input_text="unrelated root\n").stdout.strip()
    git_repo.git("update-ref", "refs/heads/main", orphan, git_repo.base)
    with pytest.raises(LandingRefusedError) as diverged:
        coordinator.land("alpha")
    assert diverged.value.details["reason"] == "target_diverged"


def test_claim_expiry_at_transaction_fence_refuses(
    git_repo: GitFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [1_000_000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    coordinator = Coordinator(git_repo.root)
    session = coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    coordinator.claim("alpha", [str(git_repo.root / "a.txt")], ttl=0.2)
    git_repo.write("a.txt", "expires before CAS\n")
    git_repo.stage(session.index_path, "a.txt")

    def expire(point: str, _context: Mapping[str, Any]) -> None:
        if point == "before_intent":
            clock[0] += 0.25

    with pytest.raises(LandingRefusedError) as refused:
        coordinator.land("alpha", checkpoint=expire)
    assert refused.value.details["reason"] == "claim_expired_at_fence"
    assert refused.value.details["paths"] == ["a.txt"]
    assert git_repo.oid() == git_repo.base
    # The refusal is a WAL fact, not just an exception.
    kinds = [
        event.data.get("reason")
        for event in coordinator.inspect_wal().events
        if event.kind == "refused"
    ]
    assert "claim_expired_at_fence" in kinds


def test_directory_claim_allows_addition_and_deletion_payload(git_repo: GitFixture) -> None:
    coordinator = Coordinator(git_repo.root)
    session = coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    coordinator.claim("alpha", [str(git_repo.root / "nested")])
    git_repo.write("nested/new.txt", "new\n")
    (git_repo.root / "nested/c.txt").unlink()
    git_repo.stage(session.index_path, "nested")

    checkpoints: list[str] = []

    def record(point: str, context: Mapping[str, Any]) -> None:
        assert context["actor"] == "alpha"
        checkpoints.append(point)

    result = coordinator.land("alpha", checkpoint=record)
    assert checkpoints == [
        "after_audit",
        "after_tree",
        "before_intent",
        "after_intent",
        "after_branch",
        "after_landed",
    ]
    assert result.payload == ("nested/c.txt", "nested/new.txt")
    assert git_repo.git("show", f"{result.commit}:nested/new.txt").stdout == "new\n"
    assert (
        git_repo.git("cat-file", "-e", f"{result.commit}:nested/c.txt", check=False).returncode != 0
    )


def _expose_intent(
    git_repo: GitFixture,
    coordinator: Coordinator,
    *,
    move_target: bool,
) -> tuple[str, str]:
    session = coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    claim = coordinator.claim("alpha", [str(git_repo.root / "a.txt")]).claims[0]
    git_repo.write("a.txt", "pending\n")
    git_repo.stage(session.index_path, "a.txt")
    tree = coordinator.repo.write_tree(session.index_path)
    base_entry = coordinator.repo.tree_entries(git_repo.base)["a.txt"].payload()
    new_entry = coordinator.repo.tree_entries(tree)["a.txt"].payload()
    candidate = coordinator.repo.commit_tree(tree, "pending candidate", (git_repo.base,))
    operation = "f" * 32
    intent = coordinator.wal.document(
        "land_intent",
        "alpha",
        {
            "operation": operation,
            "attempt": 1,
            "target": "refs/heads/main",
            "expected_head": git_repo.base,
            "candidate": candidate,
            "tree": tree,
            "session_generation": session.generation,
            # The lander this intent names is gone -- a pid no process holds --
            # which is what makes the intent recoverable at all. Round AF2 asks
            # liveness of the landing, not of the session that started it.
            "lander": {"pid": 999_999_999, "start": 1},
            "fences": [
                {"path": "a.txt", "claim_generation": claim.generation},
            ],
            "payload": [{"path": "a.txt", "old": base_entry, "new": new_entry}],
        },
    )
    snapshot = coordinator.wal.read()
    assert coordinator.wal.try_append((intent,), snapshot.head) is not None
    if move_target:
        assert coordinator.repo.update_ref("refs/heads/main", candidate, git_repo.base)
    return candidate, claim.generation


@pytest.mark.parametrize("move_target", [False, True])
def test_recovery_resolves_an_exposed_intent(
    git_repo: GitFixture,
    move_target: bool,
) -> None:
    coordinator = Coordinator(git_repo.root)
    candidate, _ = _expose_intent(git_repo, coordinator, move_target=move_target)
    report = coordinator.inspect_wal()
    assert not report.consistent
    assert report.pending_intents == 1

    if not move_target:
        with pytest.raises(ReleaseRefusedError) as fenced:
            coordinator.release("alpha", [str(git_repo.root / "a.txt")])
        assert fenced.value.details["reason"] == "pending_land"
    else:
        # Recovery may be delayed by an unrelated append and must still close
        # the exposed intent against its immutable event id.
        with pytest.raises(ReleaseRefusedError):
            coordinator.release("alpha", [str(git_repo.root / "b.txt")])

    recovered = coordinator.recover()
    assert recovered.consistent
    assert recovered.pending == 0
    if move_target:
        assert recovered.repaired == 1
        assert git_repo.oid() == candidate
    else:
        assert recovered.refused == 1
        coordinator.release("alpha", [str(git_repo.root / "a.txt")])


def test_wal_document_version_tracks_the_event_kind() -> None:
    """`v` names the lowest reader a kind needs, and a document may not understate it."""

    adopted = Wal.document("adopted", "operator", {"operation": "a" * 32})
    collected = Wal.document(
        "gc",
        "alpha",
        {"claim_generations": [], "session_generations": []},
    )

    assert adopted["v"] == 2
    assert collected["v"] == 1
    protected = Wal.document("workspace_protect", "operator", {})
    assert protected["v"] == 3
    assert Wal._validate_document(dict(protected), oid="fixture") == protected
    assert Wal._validate_document(dict(adopted), oid="fixture") == adopted

    with pytest.raises(WalCorruptError, match="does not match its kind"):
        Wal._validate_document({**adopted, "v": 1}, oid="fixture")
    with pytest.raises(WalCorruptError, match="unsupported WAL schema version"):
        Wal._validate_document({**collected, "v": 4}, oid="fixture")
    with pytest.raises(WalCorruptError, match="does not match its kind"):
        Wal._validate_document({**protected, "v": 2}, oid="fixture")
    # ``True`` is the one that matters: it compares equal to 1, so a bool would
    # otherwise pass the per-kind check for every version-1 kind.
    for understated in (0, -1, True, "1", 1.0):
        with pytest.raises(WalCorruptError, match="WAL schema version is invalid"):
            Wal._validate_document({**collected, "v": understated}, oid="fixture")


def test_wal_document_validator_and_empty_wal_paths(git_repo: GitFixture) -> None:
    coordinator = Coordinator(git_repo.root)
    wal = coordinator.wal
    assert wal.read().events == ()
    assert coordinator.inspect_wal().consistent
    with pytest.raises(WalCorruptError, match="unknown"):
        Wal.document("unknown", "alpha", {})
    with pytest.raises(WalCorruptError, match="actor"):
        Wal.document("gc", "", {})
    with pytest.raises(WalCorruptError, match="empty"):
        wal.build_chain((), None)

    valid = Wal.document(
        "gc",
        "alpha",
        {"claim_generations": [], "session_generations": []},
    )
    invalid_documents: list[Any] = [
        [],
        {**valid, "extra": True},
        {**valid, "v": 2},
        {**valid, "event": "unknown"},
        {**valid, "actor": ""},
        {**valid, "event_id": "bad"},
        {**valid, "time": 1},
        {**valid, "time": "not-a-time"},
        {**valid, "time": "2026-01-01T00:00:00"},
        {**valid, "data": []},
    ]
    for document in invalid_documents:
        with pytest.raises(WalCorruptError):
            Wal._validate_document(document, oid="fixture")

    record_document = dict(valid)
    record = EventRecord("a" * 40, (), record_document)
    assert record.payload()["event"] == "gc"
    record_document["data"] = []
    with pytest.raises(AssertionError, match="changed type"):
        _ = record.data

    appended = wal.append(
        "gc",
        "alpha",
        {"claim_generations": [], "session_generations": []},
    )
    assert appended.kind == "gc"


@pytest.mark.parametrize(
    ("kind", "data", "match"),
    [
        (
            "session_register",
            {
                "generation": "g",
                "pid": True,
                "pid_start": None,
                "expires_at": 1.0,
                "base": "a" * 40,
                "target": "refs/heads/main",
            },
            "pid",
        ),
        (
            "session_register",
            {
                "generation": "g",
                "pid": 1,
                "pid_start": True,
                "expires_at": 1.0,
                "base": "a" * 40,
                "target": "refs/heads/main",
            },
            "pid_start",
        ),
        (
            "session_register",
            {
                "generation": "g",
                "pid": 1,
                "pid_start": None,
                "expires_at": "bad",
                "base": "a" * 40,
                "target": "refs/heads/main",
            },
            "numeric",
        ),
        (
            "session_register",
            {
                "generation": "g",
                "pid": 1,
                "pid_start": None,
                "expires_at": -1.0,
                "base": "a" * 40,
                "target": "refs/heads/main",
            },
            "positive",
        ),
        (
            "claim",
            {
                "generation": "g",
                "path": "",
                "session_generation": "s",
                "expires_at": 1.0,
            },
            "path",
        ),
        (
            "claim",
            {
                "generation": "g",
                "path": "../escape",
                "session_generation": "s",
                "expires_at": 1.0,
            },
            "repository-relative",
        ),
        ("release", {"generations": ["missing"], "paths": []}, "owned claim"),
        (
            "gc",
            {"claim_generations": "bad", "session_generations": []},
            "claim_generations",
        ),
        (
            "land_intent",
            {
                "operation": "op",
                "attempt": 1,
                "target": "refs/heads/main",
                "expected_head": "c" * 40,
                "candidate": "a" * 40,
                "tree": "b" * 40,
                "session_generation": "s",
                "fences": "bad",
                "payload": [],
            },
            "audit data",
        ),
        (
            "landed",
            {"operation": "op", "intent": "none", "commit": "a" * 40},
            "matching intent",
        ),
        (
            "refused",
            {"operation": 1, "action": "land", "reason": "bad"},
            "operation",
        ),
    ],
)
def test_fold_refuses_malformed_event_data(
    git_repo: GitFixture,
    kind: str,
    data: dict[str, Any],
    match: str,
) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.wal.append(kind, "alpha", data)
    with pytest.raises((WalCorruptError, GitError), match=match):
        coordinator.inspect_wal()


@pytest.mark.parametrize(
    "mutation",
    [
        "attempt",
        "empty_fences",
        "fence_shape",
        "fence_value",
        "payload_shape",
        "payload_path",
        "entry_shape",
        "entry_value",
        "entry_oid",
        "fence_mismatch",
        "duplicate_path",
    ],
)
def test_fold_refuses_each_malformed_intent_component(
    git_repo: GitFixture,
    mutation: str,
) -> None:
    coordinator = Coordinator(git_repo.root)
    tree = git_repo.git("show", "-s", "--format=%T", git_repo.base).stdout.strip()
    entry: Any = {"mode": "100644", "oid": git_repo.base}
    data: dict[str, Any] = {
        "operation": "op",
        "attempt": 1,
        "target": "refs/heads/main",
        "expected_head": git_repo.base,
        "candidate": git_repo.base,
        "tree": tree,
        "session_generation": "session",
        "fences": [{"path": "a.txt", "claim_generation": "claim"}],
        "payload": [{"path": "a.txt", "old": entry, "new": None}],
    }
    if mutation == "attempt":
        data["attempt"] = 0
    elif mutation == "empty_fences":
        data["fences"] = []
    elif mutation == "fence_shape":
        data["fences"] = [1]
    elif mutation == "fence_value":
        data["fences"] = [{"path": "", "claim_generation": "claim"}]
    elif mutation == "payload_shape":
        data["payload"] = [1]
    elif mutation == "payload_path":
        data["payload"] = [{"path": "", "old": entry, "new": None}]
    elif mutation == "entry_shape":
        data["payload"] = [{"path": "a.txt", "old": {"bad": 1}, "new": None}]
    elif mutation == "entry_value":
        data["payload"] = [{"path": "a.txt", "old": {"mode": 1, "oid": git_repo.base}, "new": None}]
    elif mutation == "entry_oid":
        data["payload"] = [{"path": "a.txt", "old": {"mode": "100644", "oid": "bad"}, "new": None}]
    elif mutation == "fence_mismatch":
        data["fences"] = [{"path": "b.txt", "claim_generation": "claim"}]
    elif mutation == "duplicate_path":
        data["fences"] = [
            {"path": "a.txt", "claim_generation": "claim"},
            {"path": "a.txt", "claim_generation": "claim"},
        ]
        data["payload"] = [
            {"path": "a.txt", "old": entry, "new": None},
            {"path": "a.txt", "old": entry, "new": None},
        ]
    coordinator.wal.append("land_intent", "alpha", data)
    with pytest.raises((WalCorruptError, GitError)):
        coordinator.inspect_wal()


@pytest.mark.parametrize("duplicate", ["event", "session", "claim", "release", "gc"])
def test_fold_refuses_reused_ids_and_generations(
    git_repo: GitFixture,
    duplicate: str,
) -> None:
    coordinator = Coordinator(git_repo.root)
    wal = coordinator.wal
    now = time.time() + 300
    if duplicate == "event":
        event_id = "d" * 32
        documents = (
            wal.document(
                "gc",
                "alpha",
                {"claim_generations": [], "session_generations": []},
                event_id=event_id,
            ),
            wal.document(
                "gc",
                "alpha",
                {"claim_generations": [], "session_generations": []},
                event_id=event_id,
            ),
        )
    elif duplicate == "session":
        session_data = {
            "generation": "same",
            "pid": os.getpid(),
            "pid_start": None,
            "expires_at": now,
            "base": git_repo.base,
            "target": "refs/heads/main",
        }
        documents = (
            wal.document("session_register", "alpha", session_data),
            wal.document("session_register", "beta", session_data),
        )
    elif duplicate == "claim":
        claim_data = {
            "generation": "same",
            "path": "a.txt",
            "session_generation": "session",
            "expires_at": now,
        }
        documents = (
            wal.document("claim", "alpha", claim_data),
            wal.document("claim", "alpha", claim_data),
        )
    elif duplicate == "release":
        documents = (
            wal.document("release", "alpha", {"generations": ["same", "same"], "paths": []}),
        )
    else:
        documents = (
            wal.document(
                "gc",
                "alpha",
                {"claim_generations": ["same", "same"], "session_generations": []},
            ),
        )
    snapshot = wal.read()
    assert wal.try_append(documents, snapshot.head) is not None
    with pytest.raises(WalCorruptError):
        coordinator.inspect_wal()


def test_git_wrapper_refusals_and_index_type(git_repo: GitFixture, tmp_path: Path) -> None:
    repository = Repository(git_repo.root)
    with pytest.raises(GitError):
        Repository(tmp_path / "not-a-repository")
    with pytest.raises(GitError, match="malformed"):
        repository.validate_oid("xyz")
    assert repository.resolve_ref("refs/heads/missing", missing_ok=True) is None
    with pytest.raises(GitError, match="cannot resolve"):
        repository.resolve_ref("refs/heads/missing")
    with pytest.raises(GitError, match="refs/heads"):
        repository.validate_branch_ref("HEAD")
    with pytest.raises(GitError, match="invalid landing"):
        repository.validate_branch_ref("refs/heads/bad..name")
    with pytest.raises(GitError, match="event.json"):
        repository.make_single_blob_tree("other.json", "a" * 40)

    index = repository.ensure_index("alpha", git_repo.base, reset=True)
    index.unlink()
    index.symlink_to(git_repo.root / "a.txt")
    with pytest.raises(GitError, match="regular file"):
        repository.ensure_index("alpha", git_repo.base)
    index.unlink()
    os.link(git_repo.root / ".git" / "index", index)
    with pytest.raises(GitError, match="regular file"):
        repository.ensure_index("alpha", git_repo.base)

    git_repo.git("checkout", "--quiet", "--detach")
    with pytest.raises(GitError, match="detached"):
        repository.head_ref()


def test_head_ref_reports_a_failed_symbolic_ref_as_a_failure_not_as_detached(
    git_repo: GitFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`symbolic-ref --quiet` exits 1 for a detached HEAD; any other exit is git failing."""
    repository = Repository(git_repo.root)
    failed = subprocess.CompletedProcess(
        ["git", "symbolic-ref"], 128, stdout=b"",
        stderr=b"fatal: unable to parse command-line config\n",
    )
    # Control: the real detached exit keeps its diagnosis.
    git_repo.git("checkout", "--quiet", "--detach")
    with pytest.raises(GitError, match="detached"):
        repository.head_ref()
    assert repository.symbolic_head() is None
    # A real input reaches the arm (Round BF3 DUO: it was proven against a fake only):
    # HEAD naming a ref git refuses to resolve. The repository still opens.
    (git_repo.root / ".git" / "HEAD").write_text("ref: refs/heads/a..b\n", encoding="utf-8")
    reopened = Repository(git_repo.root)
    for read in (reopened.head_ref, reopened.symbolic_head):
        with pytest.raises(GitError, match="cannot read HEAD: fatal: No such ref: HEAD") as real:
            read()
        assert "detached" not in str(real.value)
    monkeypatch.setattr(repository, "git", lambda *arguments, **options: failed)
    with pytest.raises(GitError, match="unable to parse command-line config") as caught:
        repository.head_ref()
    assert "detached" not in str(caught.value)
    with pytest.raises(GitError, match="unable to parse command-line config"):
        repository.symbolic_head()


def test_git_invocation_relabels_only_operating_system_errors(
    git_repo: GitFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_invoke` may relabel an OSError as a git-exec failure, and nothing else.

    Every typed onetree error subclasses RuntimeError, so a handler widened to catch
    RuntimeError would rewrite any precise refusal raised under the subprocess call into
    "could not execute git" -- and the caller would then act on an inability to start git
    rather than on the refusal that actually happened. The two arms below differ only in
    the class of the injected exception, so what they pin is the width of that handler.
    """

    repository = Repository(git_repo.root)
    sentinel = WalCorruptError("a typed refusal raised under the subprocess call")

    def raise_sentinel(*_arguments: object, **_keywords: object) -> None:
        raise sentinel

    monkeypatch.setattr(git_module.subprocess, "run", raise_sentinel)
    with pytest.raises(WalCorruptError) as crossed:
        repository.head_ref()
    assert crossed.value is sentinel

    # Reached before the patched call above, so the two arms do not interact.
    monkeypatch.setattr(git_module, "_GIT_EXECUTABLE", None)
    with pytest.raises(GitError, match="could not execute git") as relabelled:
        repository.head_ref()
    assert relabelled.value.details["operation"] == "symbolic-ref"


def test_cli_human_errors_and_environment_identity(
    git_repo: GitFixture,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    absolute = str(git_repo.root / "a.txt")
    for name in ("ONETREE_SESSION_ID", "CODEX_THREAD_ID", "CLAUDE_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)

    assert main(["check", absolute, "--repo", str(git_repo.root)]) == 2
    missing = capsys.readouterr()
    assert "session id is required" in missing.err
    assert missing.out == ""

    monkeypatch.setenv("CODEX_THREAD_ID", "alpha")
    assert main(["check", absolute, "--repo", str(git_repo.root)]) == 0
    clear = capsys.readouterr()
    assert clear.out == "CLEAR a.txt\n"
    assert clear.err == ""

    monkeypatch.setenv("CLAUDE_SESSION_ID", "beta")
    assert main(["check", absolute, "--repo", str(git_repo.root), "--json"]) == 2
    ambiguous = capsys.readouterr()
    assert '"type": "invalid_identity"' in ambiguous.out
    assert ambiguous.err == ""
