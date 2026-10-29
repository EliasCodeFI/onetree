"""A WAL append during the referee re-tests a landing only when it changes what was tested (#245).

The referee here is a real configured command. It records every execution and, on its first
execution only, makes one WAL or repository change from inside the referee process, so each
change happens while the landing's referee is running.
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
from conftest import _CLEAN_PAYLOAD_GATE_SOURCE, GitFixture

from onetree import core, workspace
from onetree.core import Coordinator
from onetree.errors import GitError, LandingRefusedError

_SRC = str(Path(__file__).parents[1] / "src")

_REFEREE = """
import os, subprocess, sys, time
log, src, root, action, pid, git, other_gate = sys.argv[1:8]
sys.path.insert(0, src)
with open(log, "a", encoding="utf-8") as handle:
    first = handle.tell() == 0
    handle.write(os.environ["ONETREE_CANDIDATE"] + "\\n")
if first:
    from onetree.core import Coordinator
    from onetree.errors import LandingRefusedError
    from onetree import workspace
    coordinator = Coordinator(root)
    def config(*arguments):
        subprocess.run([git, "-C", root, "config", "--local", *arguments], check=True)
    for step in action.split("+"):
        if step == "none":
            pass
        elif step == "claim_unrelated":
            coordinator.claim("gamma", [os.path.join(root, "c.txt")])
        elif step == "register_unrelated":
            coordinator.register_session("delta", pid=int(pid))
        elif step == "refusal_unrelated":
            try:
                coordinator.land("gamma")
            except LandingRefusedError:
                pass
        elif step == "release_own":
            coordinator.release("alpha")
        elif step == "claim_own":
            coordinator.claim("alpha", [os.path.join(root, "a.txt")])
        elif step == "sleep":
            time.sleep(3.5)
        elif step == "gc":
            coordinator.gc("sweeper")
        elif step == "land_peer":
            coordinator.land("beta")
        elif step == "reconcile":
            epoch = coordinator._fold(coordinator.wal.read()).workspace_epoch_id
            coordinator.reconcile_workspace("operator", expected_epoch=epoch, reason="mid-referee")
        elif step == "protect_raw":
            document = coordinator.wal.document(
                "workspace_protect", "operator", workspace.baseline(coordinator.repo)
            )
            assert coordinator.wal.try_append([document], coordinator.wal.head()) is not None
        elif step == "referee_timeout":
            config("onetree.refereeTimeout", "61")
        elif step == "admit_other":
            import hashlib
            digest = hashlib.sha256(open(other_gate, "rb").read()).hexdigest()
            config("--replace-all", "onetree.payloadGateSha256", digest)
            config("--replace-all", "onetree.payloadGate", other_gate)
        else:
            raise SystemExit(f"unknown step {step}")
"""


@pytest.fixture(autouse=True)
def _no_named_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ONETREE_COMMIT_PAYLOAD", raising=False)


def _configure(git_repo: GitFixture, tmp_path: Path, action: str) -> Path:
    script = tmp_path / "referee.py"
    script.write_text(_REFEREE, encoding="utf-8")
    log = tmp_path / "referee.log"
    other = tmp_path / "other_gate.py"
    other.write_text(_CLEAN_PAYLOAD_GATE_SOURCE + "# another gate\n", encoding="utf-8")
    git = shutil.which("git") or "git"
    command = [sys.executable, "-I", str(script), str(log), _SRC, str(git_repo.root)]
    command += [action, str(os.getpid()), git, str(other)]
    git_repo.git("config", "onetree.referee", shlex.join(command))
    git_repo.git("config", "onetree.refereeTimeout", "60")
    return log


def _stage(
    coordinator: Coordinator,
    git_repo: GitFixture,
    actor: str,
    path: str,
    session_ttl: float = 300,
    claim_ttl: float = 300,
) -> None:
    session = coordinator.register_session(actor, pid=os.getpid(), ttl=session_ttl)
    coordinator.claim(actor, [str(git_repo.root / path)], ttl=claim_ttl)
    git_repo.write(path, f"{actor}\n")
    git_repo.stage(session.index_path, path)


def _land(
    git_repo: GitFixture,
    tmp_path: Path,
    action: str,
    *,
    protect: bool = False,
    session_ttl: float = 300,
    claim_ttl: float = 300,
    peer: bool = False,
    before_offer: Callable[[Coordinator], None] | None = None,
) -> dict[str, Any]:
    git_repo.write("c.txt", "c0\n")
    git_repo.git("add", "c.txt")
    git_repo.git("commit", "--quiet", "-m", "c")
    log = _configure(git_repo, tmp_path, action)
    coordinator = Coordinator(git_repo.root)
    if protect:
        coordinator.protect_workspace("operator")
    coordinator.register_session("gamma", pid=os.getpid())
    if peer or "land_peer" in action:
        _stage(coordinator, git_repo, "beta", "b.txt")
    _stage(coordinator, git_repo, "alpha", "a.txt", session_ttl, claim_ttl)
    if before_offer is not None:
        _before_first_offer(coordinator, before_offer)
    before = git_repo.oid()
    outcome: dict[str, Any] = {}
    try:
        landed = coordinator.land("alpha")
    except LandingRefusedError as error:
        outcome.update(landed=False, refused=error.details["reason"])
    else:
        outcome.update(landed=True, attempts=landed.attempts, alpha_on_main=True)
        assert git_repo.git("show", "main:a.txt").stdout == "alpha\n"
    report = Coordinator(git_repo.root).inspect_wal()
    outcome.update(
        referee_runs=len(log.read_text(encoding="utf-8").splitlines()),
        intents=sum(event.kind == "land_intent" for event in report.events),
        wal_consistent=report.consistent,
        moved=git_repo.oid() != before,
    )
    return outcome


# --- done item 3: an unrelated append no longer re-runs the referee ------------------------


@pytest.mark.parametrize("action", ["claim_unrelated", "register_unrelated", "refusal_unrelated"])
def test_an_unrelated_append_during_the_referee_keeps_the_tested_candidate(
    git_repo: GitFixture, tmp_path: Path, action: str
) -> None:
    assert _land(git_repo, tmp_path, action) == {
        "landed": True,
        "attempts": 1,
        "alpha_on_main": True,
        "referee_runs": 1,
        "intents": 1,
        "wal_consistent": True,
        "moved": True,
    }


def test_an_unrelated_append_under_protection_keeps_the_tested_candidate(
    git_repo: GitFixture, tmp_path: Path
) -> None:
    outcome = _land(git_repo, tmp_path, "claim_unrelated", protect=True)
    assert (outcome["landed"], outcome["referee_runs"], outcome["wal_consistent"]) == (
        True,
        1,
        True,
    )


# --- done item 2: changes that must still re-test or refuse --------------------------------


_SHORT = 3.0  # the referee's "sleep" step outlasts it


@pytest.mark.parametrize(
    ("action", "protect", "session_ttl", "claim_ttl", "expected"),
    [
        # The landing's own claim released, or expired: with no WAL change, swept by gc,
        # or beside an unrelated append.
        ("release_own", False, 300, 300, {"landed": False, "refused": "unclaimed_paths"}),
        ("sleep", False, 300, _SHORT, {"landed": False, "refused": "claim_expired_at_fence"}),
        ("sleep+gc", False, 300, _SHORT, {"landed": False, "refused": "unclaimed_paths"}),
        (
            "sleep+claim_unrelated",
            False,
            300,
            _SHORT,
            {"landed": False, "refused": "unclaimed_paths"},
        ),
        # The landing's session ended: expired, swept by gc, or beside an unrelated append.
        (
            "sleep",
            False,
            _SHORT,
            _SHORT,
            {"landed": False, "refused": "session_died_at_fence"},
        ),
        ("sleep+gc", False, _SHORT, _SHORT, {"landed": False, "refused": "session_inactive"}),
        (
            "sleep+claim_unrelated",
            False,
            _SHORT,
            _SHORT,
            {"landed": False, "refused": "session_inactive"},
        ),
        # The target tip moved: a peer landed on disjoint paths during the referee, so the
        # candidate is re-tested on the new tip (the peer's own landing is run 2).
        ("land_peer", False, 300, 300, {"landed": True, "attempts": 2, "referee_runs": 3}),
        # The workspace epoch changed: reconciled (which needs the claim released first, and
        # the claim retaken), or a protection appended outside the broker's busy check.
        (
            "release_own+reconcile+claim_own",
            True,
            300,
            300,
            {"landed": True, "attempts": 2, "referee_runs": 2},
        ),
        ("protect_raw", False, 300, 300, {"landed": True, "attempts": 2, "referee_runs": 2}),
        # The referee configuration or the gate admission changed: alone it refuses after
        # the run; beside an unrelated append the refusal's own record loses the CAS, so the
        # candidate is re-audited and re-tested under the new one before any intent.
        (
            "referee_timeout",
            False,
            300,
            300,
            {"landed": False, "refused": "referee_config_changed"},
        ),
        (
            "referee_timeout+claim_unrelated",
            False,
            300,
            300,
            {"landed": True, "attempts": 1, "referee_runs": 2},
        ),
        (
            "admit_other",
            False,
            300,
            300,
            {"landed": False, "refused": "payload_gate_config_changed"},
        ),
        (
            "admit_other+claim_unrelated",
            False,
            300,
            300,
            {"landed": True, "attempts": 1, "referee_runs": 2},
        ),
    ],
)
def test_a_change_to_what_was_tested_still_re_tests_or_refuses(
    git_repo: GitFixture,
    tmp_path: Path,
    action: str,
    protect: bool,
    session_ttl: float,
    claim_ttl: float,
    expected: dict[str, Any],
) -> None:
    outcome = _land(
        git_repo, tmp_path, action, protect=protect, session_ttl=session_ttl, claim_ttl=claim_ttl
    )
    assert outcome["wal_consistent"] is True
    assert {key: outcome[key] for key in expected} == expected
    if not expected["landed"]:
        assert (outcome["moved"], outcome["referee_runs"], outcome["intents"]) == (False, 1, 0)


def test_the_raw_protection_append_is_reachable_only_outside_the_broker(
    git_repo: GitFixture,
) -> None:
    # The broker's own protect refuses while the landing holds claims; the fold alone would
    # accept the epoch, which is why the landing must check the epoch it was tested under.
    coordinator = Coordinator(git_repo.root)
    _stage(coordinator, git_repo, "alpha", "a.txt")
    with pytest.raises(LandingRefusedError) as busy:
        coordinator.protect_workspace("operator")
    assert busy.value.details["reason"] == "workspace_busy"
    document = coordinator.wal.document(
        "workspace_protect", "operator", workspace.baseline(coordinator.repo)
    )
    assert coordinator.wal.try_append([document], coordinator.wal.head()) is not None
    assert coordinator.inspect_wal().consistent


# --- fix round 1: each re-offer condition decides its own case -----------------------------


def _before_first_offer(coordinator: Coordinator, stimulus: Callable[[Coordinator], None]) -> None:
    """Run ``stimulus`` once, just before alpha's first intent CAS, so the CAS loses to it.

    Every check before the CAS has already passed, so only the re-offer check can see it.
    """

    real = coordinator._try_append
    fired: list[bool] = []

    def offer(state: Any, snapshot: Any, documents: Any) -> Any:
        if not fired and any(document["event"] == "land_intent" for document in documents):
            fired.append(True)
            stimulus(Coordinator(coordinator.repo.root))
        return real(state, snapshot, documents)

    coordinator._try_append = offer  # type: ignore[method-assign]


class _CrashError(Exception):
    pass


def _crash_after_intent(name: str, context: Mapping[str, Any]) -> None:
    if name == "after_intent":
        raise _CrashError


def _peer_lands(peer: Coordinator) -> None:
    peer.land("beta")


def _peer_intent_pending(peer: Coordinator) -> None:
    with pytest.raises(_CrashError):
        peer.land("beta", checkpoint=_crash_after_intent)


def _unrelated_claim(peer: Coordinator) -> None:
    peer.claim("gamma", [str(peer.repo.root / "c.txt")])


def _referee_reconfigured(peer: Coordinator) -> None:
    peer.repo.git("config", "--local", "onetree.refereeTimeout", "61")
    _unrelated_claim(peer)


def _other_gate_admitted(peer: Coordinator) -> None:
    import hashlib

    other = peer.repo.root.parent / "other_gate.py"
    digest = hashlib.sha256(other.read_bytes()).hexdigest()
    peer.repo.git("config", "--local", "--replace-all", "onetree.payloadGateSha256", digest)
    peer.repo.git("config", "--local", "--replace-all", "onetree.payloadGate", str(other))
    _unrelated_claim(peer)


def _protected_raw(peer: Coordinator) -> None:
    document = peer.wal.document("workspace_protect", "operator", workspace.baseline(peer.repo))
    assert peer.wal.try_append([document], peer.wal.head()) is not None


def _alpha_record(git_repo: GitFixture) -> dict[str, Any]:
    report = Coordinator(git_repo.root).inspect_wal()
    return {
        "alpha_intents": sum(
            event.kind == "land_intent" and event.actor == "alpha" for event in report.events
        ),
        "alpha_refusals": [
            event.data["reason"]
            for event in report.events
            if event.kind == "refused" and event.actor == "alpha"
        ],
        "pending_intents": report.pending_intents,
        "divergences": len(report.divergences),
    }


@pytest.mark.parametrize(
    ("condition", "stimulus", "peer", "expected"),
    [
        # Without the tip condition the stale intent is appended and the branch CAS
        # refuses it (target_moved) before the re-test.
        (
            "target_tip",
            _peer_lands,
            True,
            {"landed": True, "attempts": 2, "alpha_intents": 1, "alpha_refusals": []},
        ),
        # Without the pending condition alpha lands beside the peer's pending intent.
        (
            "pending_intent",
            _peer_intent_pending,
            True,
            {
                "landed": False,
                "refused": "train_pending",
                "alpha_intents": 0,
                "pending_intents": 1,
            },
        ),
        # Without the referee or gate condition the in-loop check refuses the change
        # instead of re-testing under it.
        (
            "referee_configuration",
            _referee_reconfigured,
            False,
            {"landed": True, "attempts": 2, "alpha_intents": 1, "alpha_refusals": []},
        ),
        (
            "gate_admission",
            _other_gate_admitted,
            False,
            {"landed": True, "attempts": 2, "alpha_intents": 1, "alpha_refusals": []},
        ),
        # Without the epoch condition the tested intent lands under the new epoch untested.
        (
            "workspace_epoch",
            _protected_raw,
            False,
            {"landed": True, "attempts": 2, "alpha_intents": 1, "alpha_refusals": []},
        ),
    ],
)
def test_a_change_that_beats_the_intent_is_caught_by_the_re_offer_check(
    git_repo: GitFixture,
    tmp_path: Path,
    condition: str,
    stimulus: Callable[[Coordinator], None],
    peer: bool,
    expected: dict[str, Any],
) -> None:
    outcome = _land(git_repo, tmp_path, "none", peer=peer, before_offer=stimulus)
    outcome.update(_alpha_record(git_repo))
    assert outcome["divergences"] == 0
    assert {key: outcome.get(key) for key in expected} == expected
    if condition == "pending_intent":
        # The peer's own intent stays pending: its lander, this process, is alive.
        assert (outcome["moved"], outcome["referee_runs"]) == (False, 2)
    else:
        assert outcome["wal_consistent"] is True
        # alpha's first run, alpha's re-test, and the peer's landing where there is one
        assert outcome["referee_runs"] == 2 + peer


class _Clock:
    """``time`` for onetree.core, moved forward by ``skip`` seconds once told to."""

    def __init__(self) -> None:
        self.skip = 0.0

    def time(self) -> float:
        return time.time() + self.skip

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)


def test_a_fenced_claim_that_expires_after_the_re_offer_check_refuses_at_the_fence(
    git_repo: GitFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    monkeypatch.setattr(core, "time", clock)
    real = Coordinator._tested_candidate_holds

    def holds(self: Coordinator, *arguments: Any) -> bool:
        verdict = real(self, *arguments)
        assert verdict, "the re-offer check must see the claim alive"
        clock.skip = 61.0  # past alpha's 60 s claim, inside its 300 s session
        return verdict

    monkeypatch.setattr(Coordinator, "_tested_candidate_holds", holds)
    outcome = _land(git_repo, tmp_path, "none", claim_ttl=60, before_offer=_unrelated_claim)
    outcome.update(_alpha_record(git_repo))
    assert {
        key: outcome.get(key)
        for key in ("landed", "refused", "alpha_intents", "moved", "wal_consistent")
    } == {
        "landed": False,
        "refused": "claim_expired_at_fence",
        "alpha_intents": 0,
        "moved": False,
        "wal_consistent": True,
    }


def test_the_retry_limit_reports_attempts_and_intent_offers(
    git_repo: GitFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(core, "MAX_CAS_RETRIES", 2)
    git_repo.write("c.txt", "c0\n")
    git_repo.git("add", "c.txt")
    git_repo.git("commit", "--quiet", "-m", "c")
    log = _configure(git_repo, tmp_path, "none")
    coordinator = Coordinator(git_repo.root)
    _stage(coordinator, git_repo, "alpha", "a.txt")
    real = coordinator._try_append
    peers: list[str] = []

    def offer(state: Any, snapshot: Any, documents: Any) -> Any:
        if any(document["event"] == "land_intent" for document in documents):
            # an unrelated append beats every intent offer
            peers.append(f"delta{len(peers)}")
            Coordinator(git_repo.root).register_session(peers[-1], pid=os.getpid())
        return real(state, snapshot, documents)

    coordinator._try_append = offer  # type: ignore[method-assign]
    with pytest.raises(GitError, match="landing CAS retry limit exceeded") as limit:
        coordinator.land("alpha")
    assert limit.value.details == {"retries": 2, "attempts": 2, "offers": 4}
    assert len(peers) == 4
    assert len(log.read_text(encoding="utf-8").splitlines()) == 2
    assert Coordinator(git_repo.root).inspect_wal().consistent
