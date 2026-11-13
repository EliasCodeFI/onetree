"""The admitted payload gate is protected workspace configuration (onetree #238, #237)."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import _CLEAN_PAYLOAD_GATE_SOURCE, GATE_GIT_LITERAL, GitFixture, admit_gate

from onetree import workspace
from onetree.core import Coordinator
from onetree.errors import (
    LandingRefusedError,
    PayloadGateError,
    WalAppendRefusedError,
    WalCorruptError,
)
from onetree.git import Repository
from onetree.wal import WAL_REF, EventRecord

_KEYS = ("onetree.payloadGate", "onetree.payloadGateSha256")
_NO_GATE = (
    "the protected workspace epoch names no payload gate; admit one with "
    "`onetree gate --admit` and record it with `onetree reconcile`"
)
_SRC = str(Path(__file__).parents[1] / "src")


@pytest.fixture(autouse=True)
def _no_named_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    # Landing reads the admitted path, so a swapped admission is exactly what runs.
    monkeypatch.delenv("ONETREE_COMMIT_PAYLOAD", raising=False)


def _gate(tmp_path: Path, name: str, extra: str = "") -> Path:
    gate = tmp_path / name
    gate.write_text(_CLEAN_PAYLOAD_GATE_SOURCE + extra, encoding="utf-8")
    return gate


def _stage(coordinator: Coordinator, git_repo: GitFixture, actor: str, content: str) -> None:
    session = coordinator.register_session(actor, pid=os.getpid(), ttl=300)
    coordinator.claim(actor, [str(git_repo.root / "a.txt")])
    git_repo.write("a.txt", content)
    git_repo.stage(session.index_path, "a.txt")


def _epoch(coordinator: Coordinator) -> str:
    return [
        event.event_id
        for event in coordinator.wal.read().events
        if event.kind in ("workspace_protect", "workspace_reconcile")
    ][-1]


def _refuses(coordinator: Coordinator, git_repo: GitFixture, actor: str, reason: str) -> str:
    before = git_repo.oid()
    with pytest.raises(LandingRefusedError) as refusal:
        coordinator.land(actor)
    assert refusal.value.details["reason"] == reason
    assert git_repo.oid() == before
    last = coordinator.wal.read().events[-1]
    assert (last.kind, last.data["reason"]) == ("refused", reason)
    return str(refusal.value)


def _reconcile_and_land(coordinator: Coordinator, git_repo: GitFixture, actor: str) -> None:
    coordinator.release(actor)
    reconciled = coordinator.reconcile_workspace(
        "operator", expected_epoch=_epoch(coordinator), reason="Reviewed the admitted gate"
    )
    admission = Repository(git_repo.root).payload_gate_admission()
    assert admission is not None
    assert reconciled["baseline"]["gate"] == admission.payload()
    _stage(coordinator, git_repo, actor + "-after", "after reconciliation\n")
    landed = coordinator.land(actor + "-after")
    assert git_repo.oid() == landed.commit
    assert landed.gate_receipt is not None
    assert landed.gate_receipt["gate_source_sha256"] == admission.sha256


# --- done item 1: admission is protected configuration -------------------------------------


@pytest.mark.parametrize("change", ["different_gate", "path_key", "sha256_key", "unadmitted"])
def test_changing_the_protected_admission_quarantines_landing_until_reconcile(
    git_repo: GitFixture, tmp_path: Path, change: str
) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.protect_workspace("operator")
    other = _gate(tmp_path, "other_gate.py", "# a different reviewed gate\n")
    if change == "different_gate":
        admit_gate(git_repo.root, other)
    elif change == "path_key":
        copy = tmp_path / "copy_of_admitted.py"
        shutil.copyfile(tmp_path / "clean_commit_payload.py", copy)
        git_repo.git("config", "--local", _KEYS[0], str(copy))
    elif change == "sha256_key":
        git_repo.git("config", "--local", _KEYS[1], hashlib.sha256(other.read_bytes()).hexdigest())
    else:
        for key in _KEYS:
            git_repo.git("config", "--local", "--unset-all", key)
    _stage(coordinator, git_repo, "alpha", "after the admission changed\n")

    message = _refuses(coordinator, git_repo, "alpha", "workspace_changed")
    assert message == "protected payload gate admission changed"
    # Still refused on a second attempt: only an explicit reconcile accepts the change.
    _refuses(coordinator, git_repo, "alpha", "workspace_changed")
    if change in ("unadmitted", "sha256_key"):
        # Reconcile observes a whole admission; these two changes leave none or a broken one.
        admit_gate(git_repo.root, other)
    _reconcile_and_land(coordinator, git_repo, "alpha")


def test_readmitting_the_identical_gate_is_not_a_change(git_repo: GitFixture) -> None:
    coordinator = Coordinator(git_repo.root)
    epoch = coordinator.protect_workspace("operator")
    admission = Repository(git_repo.root).payload_gate_admission()
    assert admission is not None
    readmitted = Repository(git_repo.root).admit_payload_gate(
        admission.path, expected_sha256=admission.sha256
    )
    assert readmitted == admission
    _stage(coordinator, git_repo, "alpha", "identical admission\n")
    landed = coordinator.land("alpha")
    assert git_repo.oid() == landed.commit
    assert coordinator.protect_workspace("operator") == epoch


def test_a_malformed_admission_under_protection_is_a_workspace_change(
    git_repo: GitFixture,
) -> None:
    coordinator = Coordinator(git_repo.root)
    coordinator.protect_workspace("operator")
    git_repo.git("config", "--local", "--add", _KEYS[1], "0" * 64)
    assert workspace.violation(
        coordinator.repo, coordinator._fold(coordinator.wal.read()).workspace_epoch or {}, ()
    ) == (
        "protected payload gate admission is unreadable: payload gate admission must have one value"
    )


def test_a_baseline_with_a_malformed_gate_record_is_invalid(git_repo: GitFixture) -> None:
    repo = Repository(git_repo.root)
    baseline = workspace.baseline(repo)
    workspace.validate(baseline, repo)
    for gate in (
        {"path": "x"},
        {"path": "", "sha256": "0" * 64},
        {"path": "x", "sha256": "z"},
        "x",
    ):
        with pytest.raises(ValueError, match="invalid protected payload gate admission"):
            workspace.validate({**baseline, "gate": gate}, repo)
    with pytest.raises(ValueError, match="invalid workspace baseline"):
        workspace.validate(baseline, repo, legacy=True)


# --- done item 2: a repository protected before this change -------------------------------


def _persist(git_repo: GitFixture, document: Mapping[str, Any]) -> None:
    """Append one raw WAL event with Git plumbing, as the pre-change writer persisted it."""

    encoded = json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    blob = git_repo.git("hash-object", "-w", "--stdin", input_text=encoded + "\n").stdout.strip()
    tree = git_repo.git("mktree", input_text=f"100644 blob {blob}\tevent.json\n").stdout.strip()
    head = git_repo.git("rev-parse", "--verify", "--quiet", WAL_REF, check=False).stdout.strip()
    arguments = ["commit-tree", tree, *(("-p", head) if head else ())]
    event = git_repo.git(*arguments, input_text="onetree wal: legacy\n").stdout.strip()
    git_repo.git("update-ref", WAL_REF, event, *((head,) if head else ()))


def _legacy_document(kind: str, version: int, data: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "v": version,
        "event": kind,
        "event_id": os.urandom(16).hex(),
        "actor": "operator",
        "time": datetime.now(UTC).isoformat(timespec="microseconds"),
        "data": dict(data),
    }


def _legacy_baseline(git_repo: GitFixture) -> dict[str, Any]:
    baseline = workspace.baseline(Repository(git_repo.root))
    baseline.pop("gate", None)
    return baseline


@pytest.mark.parametrize("history", ["protect_v3", "protect_v3_reconcile_v4"])
def test_a_repository_protected_before_admission_verifies_and_brings_it_under_reconcile(
    git_repo: GitFixture, clean_commit_payload_gate: Path, history: str
) -> None:
    for key in _KEYS:
        git_repo.git("config", "--local", "--unset-all", key)
    protect = _legacy_document("workspace_protect", 3, _legacy_baseline(git_repo))
    _persist(git_repo, protect)
    if history == "protect_v3_reconcile_v4":
        _persist(
            git_repo,
            _legacy_document(
                "workspace_reconcile",
                4,
                {
                    "previous_epoch": protect["event_id"],
                    "reason": "legacy reconcile",
                    "violation": None,
                    "baseline": _legacy_baseline(git_repo),
                },
            ),
        )
    coordinator = Coordinator(git_repo.root)

    report = coordinator.inspect_wal()
    assert report.consistent
    assert [event.document["v"] for event in report.events] == (
        [3] if history == "protect_v3" else [3, 4]
    )
    # A pre-change epoch reads as "no gate admitted" ...
    assert coordinator.protect_workspace("operator").get("gate") is None

    # ... so landing refuses until a gate is admitted and reconciled, and the first
    # admission is itself a protected change ...
    _stage(coordinator, git_repo, "alpha", "first admission\n")
    assert _refuses(coordinator, git_repo, "alpha", "workspace_gate_unprotected") == _NO_GATE
    admit_gate(git_repo.root, clean_commit_payload_gate)
    assert _refuses(coordinator, git_repo, "alpha", "workspace_gate_unprotected") == _NO_GATE
    report = coordinator.inspect_wal()
    assert [(item.kind, item.detail) for item in report.divergences] == [
        ("workspace_changed", "protected payload gate admission changed")
    ]

    # ... and an explicit reconcile records it in a current-version baseline.
    _reconcile_and_land(coordinator, git_repo, "alpha")
    reconcile = [e for e in coordinator.wal.read().events if e.kind == "workspace_reconcile"][-1]
    assert reconcile.document["v"] == 5


# --- done item 3 (M1): admission changed while a landing is in flight -----------------------


def _self_changing_gate(tmp_path: Path, change: str, other: Path) -> Path:
    digest = hashlib.sha256(other.read_bytes()).hexdigest()
    commands = {
        "unadmit": [
            ["config", "--local", "--unset-all", _KEYS[0]],
            ["config", "--local", "--unset-all", _KEYS[1]],
        ],
        "readmit": [
            ["config", "--local", "--replace-all", _KEYS[1], digest],
            ["config", "--local", "--replace-all", _KEYS[0], str(other)],
        ],
    }[change]
    prefix = (
        "import subprocess as _subprocess\n"
        "import sys as _sys\n"
        f"for _arguments in {commands!r}:\n"
        f"    _subprocess.run([{GATE_GIT_LITERAL}, '-C', _sys.argv[4], *_arguments],\n"
        "                    check=True, capture_output=True)\n"
    )
    gate = tmp_path / f"self_{change}.py"
    gate.write_text(prefix + _CLEAN_PAYLOAD_GATE_SOURCE, encoding="utf-8")
    return gate


@pytest.mark.parametrize("change", ["unadmit", "readmit"])
def test_an_admission_change_during_the_gate_run_refuses_that_landing(
    git_repo: GitFixture, tmp_path: Path, change: str
) -> None:
    other = _gate(tmp_path, "other_gate.py", "# other\n")
    admit_gate(git_repo.root, _self_changing_gate(tmp_path, change, other))
    coordinator = Coordinator(git_repo.root)
    _stage(coordinator, git_repo, "alpha", "admission changes mid-audit\n")

    message = _refuses(coordinator, git_repo, "alpha", "payload_gate_config_changed")
    assert message == "payload gate admission changed during candidate audit"


@pytest.mark.parametrize("change", ["unadmit", "readmit", "malformed"])
def test_an_admission_change_before_the_intent_refuses_that_landing(
    git_repo: GitFixture, tmp_path: Path, change: str
) -> None:
    other = _gate(tmp_path, "other_gate.py", "# other\n")
    coordinator = Coordinator(git_repo.root)
    _stage(coordinator, git_repo, "alpha", "admission changes before the intent\n")

    def checkpoint(name: str, _: Mapping[str, Any]) -> None:
        if name != "before_intent":
            return
        if change == "unadmit":
            for key in _KEYS:
                git_repo.git("config", "--local", "--unset-all", key)
        elif change == "readmit":
            admit_gate(git_repo.root, other)
        else:
            git_repo.git("config", "--local", "--add", _KEYS[1], "0" * 64)

    before = git_repo.oid()
    with pytest.raises(LandingRefusedError) as refusal:
        coordinator.land("alpha", checkpoint=checkpoint)
    assert refusal.value.details["reason"] == "payload_gate_config_changed"
    assert str(refusal.value) == "payload gate admission changed before admission"
    assert git_repo.oid() == before


# --- done item 4 (S3): an admitted symlink is repointed ------------------------------------


def test_a_repointed_admitted_symlink_refuses_until_readmission(
    git_repo: GitFixture, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first = _gate(tmp_path, "first_gate.py")
    second = _gate(tmp_path, "second_gate.py", "# repointed\n")
    link = tmp_path / "current_gate.py"
    link.symlink_to(first)
    admitted = Repository(git_repo.root).admit_payload_gate(link)
    assert admitted.path == str(link)
    coordinator = Coordinator(git_repo.root)
    _stage(coordinator, git_repo, "alpha", "through the link\n")
    coordinator.land("alpha")
    coordinator.release("alpha")

    link.unlink()
    link.symlink_to(second)
    _stage(coordinator, git_repo, "beta", "after the link moved\n")
    before = git_repo.oid()
    with pytest.raises(PayloadGateError, match="does not match admitted") as failure:
        coordinator.land("beta")
    assert failure.value.details["gate"] == str(second.resolve())
    assert git_repo.oid() == before
    from onetree.cli import main

    assert main(["gate", "--repo", str(git_repo.root), "--json"]) == 1
    report = json.loads(capsys.readouterr().out)["gate"]
    assert (report["matches"], report["source"]["resolved"]) == (False, str(second.resolve()))

    Repository(git_repo.root).admit_payload_gate(link)
    landed = coordinator.land("beta")
    assert git_repo.oid() == landed.commit


# --- done item 5 (M2/S4): `onetree gate` applies landing's file rules -----------------------


def _cli(*arguments: str, env: Mapping[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    environment = {**os.environ, "PYTHONPATH": _SRC, **(env or {})}
    completed = subprocess.run(
        [sys.executable, "-m", "onetree.cli", *arguments, "--json"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=environment,
    )
    payload = json.loads(completed.stdout)
    assert isinstance(payload, dict)
    return completed.returncode, payload


def test_a_fifo_admission_is_refused_without_hanging(git_repo: GitFixture, tmp_path: Path) -> None:
    fifo = tmp_path / "gate.fifo"
    os.mkfifo(fifo)
    git_repo.git("config", "--local", "--replace-all", _KEYS[0], str(fifo))
    code, report = _cli("gate", "--repo", str(git_repo.root))
    assert code == 1
    assert report["gate"]["matches"] is False
    assert report["gate"]["source"]["error"] == "commit payload gate is not a regular file"

    coordinator = Coordinator(git_repo.root)
    _stage(coordinator, git_repo, "alpha", "fifo gate\n")
    code, refused = _cli("land", "--repo", str(git_repo.root), "--session", "alpha")
    assert code == 2
    assert refused["error"]["type"] == "payload_gate_error"
    assert refused["error"]["message"] == "commit payload gate is not a regular file"
    with pytest.raises(PayloadGateError, match="not a regular file"):
        Repository(git_repo.root).admit_payload_gate(fifo)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
def test_an_unreadable_admitted_gate_names_the_error(git_repo: GitFixture, tmp_path: Path) -> None:
    gate = _gate(tmp_path, "locked_gate.py")
    admit_gate(git_repo.root, gate)
    gate.chmod(0)
    try:
        code, report = _cli("gate", "--repo", str(git_repo.root))
    finally:
        gate.chmod(0o600)
    assert code == 1
    assert "could not be read" in report["gate"]["source"]["error"]
    assert "Permission denied" in report["gate"]["source"]["error"]


def test_the_report_follows_the_gate_landing_would_read(
    git_repo: GitFixture, tmp_path: Path
) -> None:
    admitted = _gate(tmp_path, "admitted_gate.py")
    admit_gate(git_repo.root, admitted)
    copy = tmp_path / "named_copy.py"
    shutil.copyfile(admitted, copy)
    different = _gate(tmp_path, "named_other.py", "# other\n")
    admitted.unlink()
    digest = hashlib.sha256(copy.read_bytes()).hexdigest()

    code, report = _cli("gate", "--repo", str(git_repo.root))
    assert (code, report["gate"]["source"]["origin"]) == (1, "admission")
    code, report = _cli("gate", "--repo", str(git_repo.root), "--commit-payload", str(copy))
    assert code == 0
    assert report["gate"]["source"] == {
        "path": str(copy),
        "origin": "commit_payload",
        "resolved": str(copy.resolve()),
        "sha256": digest,
        "error": None,
    }
    code, report = _cli(
        "gate", "--repo", str(git_repo.root), env={"ONETREE_COMMIT_PAYLOAD": str(different)}
    )
    assert code == 1
    assert report["gate"]["source"]["origin"] == "environment"
    assert report["gate"]["matches"] is False

    # The named copy really does land, as the report said.
    coordinator = Coordinator(git_repo.root, commit_payload=copy)
    _stage(coordinator, git_repo, "alpha", "named copy\n")
    landed = coordinator.land("alpha")
    assert git_repo.oid() == landed.commit


def test_the_human_report_names_a_named_gate(
    git_repo: GitFixture, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from onetree.cli import main

    admitted = _gate(tmp_path, "admitted_gate.py")
    admit_gate(git_repo.root, admitted)
    assert main(["gate", "--repo", str(git_repo.root), "--commit-payload", str(admitted)]) == 0
    assert capsys.readouterr().out.endswith(
        f"; landing reads {admitted} (commit_payload); source matches\n"
    )


# --- a protected intent is bound to the epoch's gate on replay ------------------


def _protected_landing(git_repo: GitFixture) -> tuple[Coordinator, Any]:
    coordinator = Coordinator(git_repo.root)
    coordinator.protect_workspace("operator")
    _stage(coordinator, git_repo, "alpha", "protected landing\n")
    coordinator.land("alpha")
    intent = next(e for e in coordinator.wal.read().events if e.kind == "land_intent")
    return coordinator, intent


def _forged(intent: Any, mutate: Any) -> EventRecord:
    document = copy.deepcopy(dict(intent.document))
    document["data"]["operation"] = "f" * 32
    document["event_id"] = "forged-intent"
    mutate(document["data"])
    return EventRecord("forged", (), document)


def _other_gate(data: dict[str, Any]) -> None:
    data["gate_receipt"]["gate_source_sha256"] = "0" * 64


def _no_gate(data: dict[str, Any]) -> None:
    del data["gate_receipt"]


def test_a_protected_intent_carrying_another_gate_or_none_does_not_replay(
    git_repo: GitFixture,
) -> None:
    coordinator, intent = _protected_landing(git_repo)
    state = coordinator._fold(coordinator.wal.read())
    assert state.workspace_epoch is not None
    assert (
        intent.data["gate_receipt"]["gate_source_sha256"] == state.workspace_epoch["gate"]["sha256"]
    )
    for mutate, expected in (
        (_other_gate, "protected intent evidence names another payload gate"),
        (_no_gate, "protected intent has no payload gate evidence"),
    ):
        with pytest.raises(WalCorruptError, match=expected):
            coordinator._apply(copy.deepcopy(state), _forged(intent, mutate))
    # Control: the unforged document replays.
    coordinator._apply(copy.deepcopy(state), _forged(intent, lambda data: None))


def test_an_epoch_that_names_no_gate_binds_none(git_repo: GitFixture) -> None:
    # Protected intents written under a version 3/4 epoch carry gate receipts from the
    # pinned-digest era; that epoch reads as gate null, and they keep replaying.
    coordinator, intent = _protected_landing(git_repo)
    state = coordinator._fold(coordinator.wal.read())
    assert state.workspace_epoch is not None
    state.workspace_epoch = {**state.workspace_epoch, "gate": None}
    for mutate in (_other_gate, _no_gate):
        coordinator._apply(copy.deepcopy(state), _forged(intent, mutate))


def _flip_b_a_b(
    git_repo: GitFixture, coordinator: Coordinator, a: Path, b: Path, trace: list[str]
) -> Any:
    """Admit B after the first workspace check, A for the second, B again before the last
    admission check, and A after the intent: the interleave of onetree #238's racer."""

    armed = False
    original = coordinator._workspace_violation

    def violation(state: Any) -> str | None:
        nonlocal armed
        result = original(state)
        if armed:
            armed = False
            admit_gate(git_repo.root, b)
            trace.append("B after the second workspace check")
        return result

    def checkpoint(name: str, _: Mapping[str, Any]) -> None:
        nonlocal armed
        if name == "after_tree":
            admit_gate(git_repo.root, b)
            trace.append("B after the first workspace check")
        elif name == "before_intent":
            admit_gate(git_repo.root, a)
            trace.append("A before the second workspace check")
            armed = True
        elif name == "after_intent":
            admit_gate(git_repo.root, a)
            trace.append("A after the intent")

    coordinator._workspace_violation = violation  # type: ignore[method-assign]
    try:
        return coordinator.land("alpha", checkpoint=checkpoint)
    finally:
        del coordinator._workspace_violation
        admit_gate(git_repo.root, a)


def test_a_b_a_b_admission_flip_during_one_landing_does_not_land(
    git_repo: GitFixture, clean_commit_payload_gate: Path, tmp_path: Path
) -> None:
    a = clean_commit_payload_gate
    b = _gate(tmp_path, "gate_b.py", "# gate B: the same verdicts, other bytes\n")
    digests = {hashlib.sha256(p.read_bytes()).hexdigest(): name for name, p in (("A", a), ("B", b))}
    coordinator = Coordinator(git_repo.root)
    epoch = coordinator.protect_workspace("operator")
    _stage(coordinator, git_repo, "alpha", "landed under a flipped gate\n")
    before = git_repo.oid()

    outcome: dict[str, Any] = {"epoch_gate": digests[epoch["gate"]["sha256"]]}
    trace: list[str] = []
    try:
        landed = _flip_b_a_b(git_repo, coordinator, a, b, trace)
    except WalAppendRefusedError as error:
        outcome.update(landed=None, refused=str(error))
    else:
        assert landed.gate_receipt is not None
        outcome.update(
            landed=landed.commit,
            refused=None,
            landed_gate=digests[landed.gate_receipt["gate_source_sha256"]],
        )
    outcome["trace"] = trace
    report = coordinator.inspect_wal()
    outcome.update(
        branch_moved=git_repo.oid() != before,
        wal_consistent=report.consistent,
        intents=sum(event.kind == "land_intent" for event in report.events),
    )
    assert outcome == {
        "epoch_gate": "A",
        "landed": None,
        "refused": "refusing to append a land_intent event that the WAL cannot replay: "
        "protected intent evidence names another payload gate",
        "trace": [
            "B after the first workspace check",
            "A before the second workspace check",
            "B after the second workspace check",
        ],
        "branch_moved": False,
        "wal_consistent": True,
        "intents": 0,
    }


# --- no landing under a protected epoch that names no gate ---------------------


def _unadmit(git_repo: GitFixture) -> None:
    for key in _KEYS:
        git_repo.git("config", "--local", "--unset-all", key, check=False)


def _protect_without_gate(git_repo: GitFixture, coordinator: Coordinator, epoch: str) -> None:
    _unadmit(git_repo)
    if epoch == "v5":
        assert coordinator.protect_workspace("operator")["gate"] is None
        return
    protect = _legacy_document("workspace_protect", 3, _legacy_baseline(git_repo))
    _persist(git_repo, protect)
    if epoch == "legacy_v4":
        reconcile = {
            "previous_epoch": protect["event_id"],
            "reason": "legacy reconcile",
            "violation": None,
            "baseline": _legacy_baseline(git_repo),
        }
        _persist(git_repo, _legacy_document("workspace_reconcile", 4, reconcile))
    folded = coordinator._fold(coordinator.wal.read())
    assert folded.workspace_epoch is not None
    assert folded.workspace_epoch["gate"] is None


def _null_gate_land(
    git_repo: GitFixture, coordinator: Coordinator, b: Path, flip: bool, trace: list[str]
) -> Any:
    """The #238 interleave with "unadmitted" as the protected value: B after the first
    workspace check, none for the second, B again straight after it, none after the intent."""

    armed = False
    original = coordinator._workspace_violation

    def violation(state: Any) -> str | None:
        nonlocal armed
        result = original(state)
        if armed:
            armed = False
            admit_gate(git_repo.root, b)
            trace.append("B after the second workspace check")
        return result

    def checkpoint(name: str, _: Mapping[str, Any]) -> None:
        nonlocal armed
        if not flip:
            trace.append(name)
        elif name == "after_tree":
            admit_gate(git_repo.root, b)
            trace.append("B after the first workspace check")
        elif name == "before_intent":
            _unadmit(git_repo)
            trace.append("unadmitted before the second workspace check")
            armed = True
        elif name == "after_intent":
            _unadmit(git_repo)
            trace.append("unadmitted after the intent")

    coordinator._workspace_violation = violation  # type: ignore[method-assign]
    try:
        return coordinator.land("alpha", checkpoint=checkpoint)
    finally:
        del coordinator._workspace_violation


@pytest.mark.parametrize("admission", ["flip", "steady_b", "steady_none"])
@pytest.mark.parametrize("epoch", ["v5", "legacy_v3", "legacy_v4"])
def test_no_landing_under_a_protected_epoch_that_names_no_gate(
    git_repo: GitFixture, tmp_path: Path, epoch: str, admission: str
) -> None:
    b = _gate(tmp_path, "gate_b.py", "# gate B: the same verdicts, other bytes\n")
    coordinator = Coordinator(git_repo.root)
    _protect_without_gate(git_repo, coordinator, epoch)
    _stage(coordinator, git_repo, "alpha", "landed under an unprotected gate\n")
    if admission == "steady_b":
        admit_gate(git_repo.root, b)
    before = git_repo.oid()

    outcome: dict[str, Any] = {}
    trace: list[str] = []
    try:
        landed = _null_gate_land(git_repo, coordinator, b, admission == "flip", trace)
    except LandingRefusedError as error:
        outcome.update(landed=None, refused=error.details["reason"], message=str(error))
    else:
        assert landed.gate_receipt is not None
        b_sha256 = hashlib.sha256(b.read_bytes()).hexdigest()
        outcome.update(
            landed=landed.commit,
            refused=None,
            landed_gate_is_b=landed.gate_receipt["gate_source_sha256"] == b_sha256,
        )
    outcome["trace"] = trace
    events = coordinator.wal.read().events
    outcome.update(
        branch_moved=git_repo.oid() != before,
        intents=sum(event.kind == "land_intent" for event in events),
        last_event=(events[-1].kind, events[-1].data.get("reason")),
    )
    # Refused before the payload audit, so no admission read the landing makes can matter.
    assert outcome == {
        "landed": None,
        "refused": "workspace_gate_unprotected",
        "message": _NO_GATE,
        "trace": [],
        "branch_moved": False,
        "intents": 0,
        "last_event": ("refused", "workspace_gate_unprotected"),
    }
