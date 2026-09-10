from __future__ import annotations

import copy
import hashlib
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from conftest import SHIPPED_PAYLOAD_GATE_SOURCE_SHA256, GitFixture
from test_cli import _json_call
from test_defensive import _expose_intent
from test_payload_gate import _register_and_stage, _write_gate

from onetree import git as git_module
from onetree.core import Coordinator
from onetree.errors import LandingRefusedError, PayloadGateError, WalCorruptError
from onetree.wal import WAL_REF


@pytest.mark.parametrize("verdict", ["CLEAN", "REFUSE", "DEGRADED"])
def test_exact_gate_evidence_survives_the_call(
    git_repo: GitFixture, tmp_path: Path, verdict: str
) -> None:
    suites = [
        {
            "hook_path": "hooks/demo.js",
            "suite_path": "hooks/demo.test.mjs",
            "status": "NO_COMPANION",
            "detail": "no indexed companion",
            "checks": None,
            "evidence": "not-established",
            "weakening": "unchecked",
            "runtime": "sandboxed",
        }
    ]
    report = {
        "repository": str(git_repo.root),
        "staged_files": [] if verdict == "DEGRADED" else ["a.txt"],
        "findings": ["known bad"] if verdict == "REFUSE" else [],
        "degraded": ["census unavailable"] if verdict == "DEGRADED" else [],
        "advisories": ["suite absence remains visible"],
        "hook_suites": suites,
        "hook_suite_state": "evaluated",
        "verdict": verdict,
    }
    gate = _write_gate(
        tmp_path / "gate.py", report, {"CLEAN": 0, "REFUSE": 1, "DEGRADED": 2}[verdict]
    )
    coordinator = _register_and_stage(git_repo, "alpha", "receipt payload\n", commit_payload=gate)
    if verdict == "CLEAN":
        result = coordinator.land("alpha")
        events = coordinator.inspect_wal().events
        carrier = next(event for event in events if event.kind == "land_intent")
        candidate = result.commit
    else:
        with pytest.raises(LandingRefusedError) as refused:
            coordinator.land("alpha")
        carrier = coordinator.inspect_wal().events[-1]
        assert carrier.kind == "refused"
        assert refused.value.details["reason"] == (
            "payload_gate_refused" if verdict == "REFUSE" else "payload_gate_degraded"
        )
        candidate = carrier.data.get("gate_receipt", {}).get("report", {}).get("candidate")
    receipt = carrier.data.get("gate_receipt")
    assert receipt is not None, carrier.payload()
    assert receipt == {
        "version": 1,
        "gate_source_sha256": hashlib.sha256(gate.read_bytes()).hexdigest(),
        "expected_paths": ["a.txt"],
        "report": {**report, "candidate": candidate},
    }
    assert coordinator.inspect_wal().gate_evidence()[-1] == {
        "event": coordinator.inspect_wal().events[-1].event_id,
        "state": "recorded",
        "receipt_event": carrier.event_id,
    }
    if verdict == "CLEAN":
        assert result.gate_receipt == receipt
    else:
        assert refused.value.details["gate_receipt"] == receipt
        assert git_repo.oid() == git_repo.base


@pytest.mark.parametrize("case", ["no_companion", "green", "red"])
def test_real_hook_gate_evidence(
    git_repo: GitFixture,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    case: str,
) -> None:
    configured = os.environ.get("ONETREE_TEST_ESTATE_GATE")
    if configured is None:
        pytest.skip("requires explicit ONETREE_TEST_ESTATE_GATE")
    gate = Path(configured).resolve(strict=True)
    monkeypatch.setattr(
        git_module, "PAYLOAD_GATE_SOURCE_SHA256", SHIPPED_PAYLOAD_GATE_SOURCE_SHA256
    )
    for name in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_THREAD_ID"):
        monkeypatch.delenv(name, raising=False)
    identity = "11111111-1111-4111-8111-111111111111"
    monkeypatch.setenv("CODEX_THREAD_ID", identity)
    coordinator = Coordinator(git_repo.root, commit_payload=gate)
    session = coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    coordinator.claim("alpha", [str(git_repo.root)])
    # Producer: commit_payload.py's admitted selftest fixtures at lines7759-7783
    # and10685-10691; the companion reads the hook and fails when its answer is wrong.
    git_repo.write("hooks/demo.js", "module.exports = { ok: true };\n")
    paths = ["hooks/demo.js"]
    if case != "no_companion":
        git_repo.write(
            "hooks/demo.test.mjs",
            (
                "import { createRequire } from 'node:module';\n"
                "const hook = createRequire(import.meta.url)('./demo.js');\n"
                f"const ok = hook.ok === {'true' if case == 'green' else 'false'};\n"
                'console.log(ok ? "2/2 passed, 0 failed" : "0/2 passed, 2 failed");\n'
                "process.exit(ok ? 0 : 1);\n"
            ),
        )
        paths.append("hooks/demo.test.mjs")
    git_repo.stage(session.index_path, *paths)
    message = (
        f"Hook evidence\n\nCo-Authored-By: Codex <noreply@openai.com>\nCodex-Session: {identity}\n"
    )
    if case == "red":
        with pytest.raises(LandingRefusedError):
            coordinator.land("alpha", message=message)
        receipt = coordinator.inspect_wal().events[-1].data["gate_receipt"]
        assert git_repo.oid() == git_repo.base
    else:
        result = coordinator.land("alpha", message=message)
        receipt = result.gate_receipt
    assert receipt is not None
    report = receipt["report"]
    request.node.user_properties.extend(
        [
            ("gate", str(gate)),
            ("source_sha256", receipt["gate_source_sha256"]),
            ("report", str(report)),
            ("implementation", str(git_module.__file__)),
        ]
    )
    assert set(report) == {
        "candidate",
        "repository",
        "staged_files",
        "findings",
        "degraded",
        "advisories",
        "hook_suites",
        "hook_suite_state",
        "verdict",
    }
    assert report["hook_suite_state"] == "evaluated"
    assert {row["status"] for row in report["hook_suites"]} == {
        {"no_companion": "NO_COMPANION", "green": "GREEN", "red": "RED"}[case]
    }
    assert report["verdict"] == ("REFUSE" if case == "red" else "CLEAN")
    assert report["hook_suites"][0]["runtime"] == "sandboxed"
    assert receipt["gate_source_sha256"] == SHIPPED_PAYLOAD_GATE_SOURCE_SHA256


def test_recovery_links_original_receipt_without_reauditing(
    git_repo: GitFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    coordinator = _register_and_stage(git_repo, "alpha", "crash after target\n")

    def stop(point: str, context: Mapping[str, Any]) -> None:
        if point == "after_branch":
            raise RuntimeError("test crash after branch CAS")

    with pytest.raises(RuntimeError, match="test crash"):
        coordinator.land("alpha", checkpoint=stop)
    intent = coordinator.inspect_wal().events[-1]
    receipt = copy.deepcopy(intent.data["gate_receipt"])

    def unexpected_gate(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("recovery reran the gate")

    monkeypatch.setattr(git_module.Repository, "audit_commit_payload", unexpected_gate)
    fresh = Coordinator(git_repo.root)
    assert fresh.recover().repaired == 1
    report = fresh.inspect_wal()
    landed = report.events[-1]
    assert landed.data["intent"] == intent.event_id
    assert "gate_receipt" not in landed.data
    assert (
        next(event for event in report.events if event.event_id == intent.event_id).data[
            "gate_receipt"
        ]
        == receipt
    )
    code, payload = _json_call(capsys, ["wal", "--repo", str(git_repo.root)])
    assert code == 0
    assert payload["wal"]["gate_evidence"][-1] == {
        "event": landed.event_id,
        "state": "recorded",
        "receipt_event": intent.event_id,
    }


def test_old_records_remain_explicitly_unrecorded(git_repo: GitFixture) -> None:
    coordinator = Coordinator(git_repo.root)
    _expose_intent(git_repo, coordinator, move_target=True)
    assert coordinator.recover().repaired == 1
    evidence = coordinator.inspect_wal().gate_evidence()
    assert len(evidence) == 2
    assert all(row["state"] == "unrecorded" for row in evidence)
    assert all(row["receipt_event"] is None for row in evidence)


@pytest.mark.parametrize("case", ["shape", "partial", "advisory", "suite", "count", "nonfinite"])
def test_malformed_new_evidence_is_operational_failure(
    git_repo: GitFixture,
    tmp_path: Path,
    case: str,
) -> None:
    report: dict[str, Any] = {
        "repository": str(git_repo.root),
        "staged_files": ["a.txt"],
        "findings": [],
        "degraded": [],
        "verdict": "CLEAN",
        "advisories": [],
        "hook_suites": [],
        "hook_suite_state": "evaluated",
    }
    if case == "shape":
        report["hook_suites"] = "GREEN"
    elif case == "partial":
        report.pop("hook_suite_state")
    elif case == "advisory":
        report["advisories"] = [None]
    elif case in {"suite", "count"}:
        report["hook_suites"] = (
            [{}]
            if case == "suite"
            else [
                {
                    **dict.fromkeys(
                        (
                            "hook_path",
                            "suite_path",
                            "status",
                            "detail",
                            "evidence",
                            "weakening",
                            "runtime",
                        ),
                        "sample",
                    ),
                    "checks": True,
                }
            ]
        )
    else:
        report["extension"] = float("inf")
    gate = _write_gate(tmp_path / "malformed.py", report, 0)
    if case == "nonfinite":
        gate.write_text(gate.read_text().replace("'extension': inf", "'extension': float('inf')"))
    coordinator = _register_and_stage(git_repo, "alpha", "bad evidence\n", commit_payload=gate)
    before = coordinator.wal.read()
    with pytest.raises(PayloadGateError, match="malformed retained gate evidence"):
        coordinator.land("alpha")
    assert coordinator.wal.read() == before
    assert git_repo.oid() == git_repo.base


@pytest.mark.parametrize(
    "case", ["candidate", "paths", "verdict", "digest", "version", "report", "misplaced"]
)
def test_wal_rejects_forged_receipt_input(git_repo: GitFixture, case: str) -> None:
    coordinator = _register_and_stage(git_repo, "alpha", "receipt baseline\n")
    result = coordinator.land("alpha")
    snapshot = coordinator.wal.read()
    documents = [copy.deepcopy(event.document) for event in snapshot.events]
    intent = next(document for document in documents if document["event"] == "land_intent")
    receipt = intent["data"]["gate_receipt"]
    if case == "candidate":
        receipt["report"]["candidate"] = git_repo.base
    elif case == "paths":
        receipt["expected_paths"] = ["b.txt"]
        receipt["report"]["staged_files"] = ["b.txt"]
    elif case == "verdict":
        receipt["report"]["verdict"] = "REFUSE"
    elif case == "digest":
        receipt["gate_source_sha256"] = "unverified"
    elif case == "version":
        receipt["version"] = True
    elif case == "report":
        receipt["report"] = []
    else:
        documents[-1]["data"]["gate_receipt"] = receipt
    forged, _ = coordinator.wal.build_chain(documents, None)
    assert coordinator.repo.update_ref(WAL_REF, forged, snapshot.head)
    with pytest.raises(WalCorruptError, match="gate (receipt|evidence)|retained gate evidence"):
        Coordinator(git_repo.root).inspect_wal()
    assert git_repo.oid() == result.commit


def test_historical_gate_origin_is_not_rebound_to_current_clone(
    git_repo: GitFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = _register_and_stage(git_repo, "alpha", "portable receipt\n")
    result = coordinator.land("alpha")
    original = result.gate_receipt
    clone = tmp_path / "relocated"
    git_repo.git("clone", "--quiet", str(git_repo.root), str(clone))
    relocated = GitFixture(root=clone, base=git_repo.base)
    relocated.git("fetch", "--quiet", "origin", f"{WAL_REF}:{WAL_REF}")
    monkeypatch.setattr(git_module, "PAYLOAD_GATE_SOURCE_SHA256", "0" * 64)
    report = Coordinator(clone).inspect_wal()
    assert report.consistent
    intent = next(event for event in report.events if event.kind == "land_intent")
    assert intent.data["gate_receipt"] == original
    assert intent.data["gate_receipt"]["report"]["repository"] == str(git_repo.root)


@pytest.mark.parametrize("intent_kind", ["other_operation", "malformed"])
def test_unrelated_refusal_cannot_borrow_gate_evidence(
    git_repo: GitFixture, intent_kind: str
) -> None:
    coordinator = _register_and_stage(git_repo, "alpha", "audited\n")
    result = coordinator.land("alpha")
    coordinator.wal.append(
        "refused",
        "alpha",
        {
            "action": "land",
            "reason": "unrecorded_attempt",
            "operation": "other-operation",
            "intent": result.intent if intent_kind == "other_operation" else [],
            "paths": [],
        },
    )
    report = coordinator.inspect_wal()
    assert report.gate_evidence()[-1] == {
        "event": report.events[-1].event_id,
        "state": "unrecorded",
        "receipt_event": None,
    }


def test_landing_retry_retains_the_successful_candidates_receipt(git_repo: GitFixture) -> None:
    coordinator = _register_and_stage(git_repo, "alpha", "retry evidence\n")
    candidates: list[str] = []

    def contend(point: str, context: Mapping[str, Any]) -> None:
        if point == "before_intent":
            candidates.append(context["candidate"])
            if len(candidates) == 1:
                Coordinator(git_repo.root).wal.append(
                    "refused",
                    "peer",
                    {
                        "action": "claim",
                        "reason": "concurrent event",
                    },
                )

    result = coordinator.land("alpha", checkpoint=contend)
    assert len(candidates) == 2
    assert candidates[0] != candidates[1]
    assert result.gate_receipt is not None
    assert result.gate_receipt["report"]["candidate"] == candidates[1] == result.commit
    intents = [event for event in coordinator.inspect_wal().events if event.kind == "land_intent"]
    assert len(intents) == 1
    assert intents[0].data["gate_receipt"] == result.gate_receipt


def test_land_cli_exposes_the_recorded_receipt(
    git_repo: GitFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    coordinator = _register_and_stage(git_repo, "alpha", "CLI receipt\n")
    code, payload = _json_call(capsys, ["land", "--repo", str(git_repo.root), "--session", "alpha"])
    assert code == 0
    receipt = payload["land"]["gate_receipt"]
    assert receipt["report"]["candidate"] == payload["land"]["commit"]
    intent = next(
        event for event in coordinator.inspect_wal().events if event.kind == "land_intent"
    )
    assert receipt == intent.data["gate_receipt"]
