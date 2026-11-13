"""Two submitters race under referee train order.

INV3 established that two real lander processes racing on one target produce
one first-attempt winner and land both payloads. Earlier tests established, with
callbacks inside one process, that a stale audit is invalidated by the WAL CAS
and that a retry rebuilds and re-tests against the new target. Neither shows
the composition: that the losing *process* of a real race rebuilds its
candidate atop the winner's commit and runs the referee against the winner's
state before anything of it is recorded.

These cases establish that composition from four surfaces that no single
process controls: the referee's own execution log (one durable line per run,
written by the referee process from inside its checkout, naming the candidate
and baseline it was given, the tree it saw and the chain of processes above
it, read from ``/proc`` while they are alive), each lander's
phase log, the candidate receipts each lander returns, and the branch and WAL
as the test reads them afterwards.

Rendezvous. Both landers are held at ``before_intent``, after their first
referee run and before their intent append, by a three-party barrier the
test itself joins; at that point the test observes both green against the
same baseline with nothing recorded. The ordered cases then hold one lander
on a gate the test opens at a chosen moment: after the winner has landed, so
the loser's retry deterministically sees a landed winner; or with the winner
parked at ``after_intent``, so the loser deterministically meets a pending
train. The unordered case releases both together, a real race whose loser
reaches one of the outcomes the landing contract allows, and the case asserts
whichever occurred against its own evidence rather than choosing.

Controls. The referee log's ``a``/``b`` fields are read from the candidate
checkout on every run, so the same predicate that reads ``a1`` on the retest
reads ``a0`` on the two first runs, and an instrument case runs the script by
hand in two checkouts. The combined-red pair is the known-bad input: its
referee is green for each payload alone and red only when both are present,
and the compatible pair with the same script is the positive control showing
the refusal is not unconditional. INV3's raw-CAS red control, which loses a
payload without the broker, stands in ``test_land.py`` and is not repeated.

Provenance. A referee run is attributed to a lander through the recorded
chain: the lander is the referee's parent, or an ancestor reached only
through processes whose argv names a file of the installed ``onetree``
package (the referee supervisor, where the checkout has one). The chain is
checked for consistency link by link, and the control case shows the same
helper rejecting an attribution that would pass through the test runner.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from conftest import GitFixture
from test_land import _register_and_stage

import onetree
from onetree.core import Coordinator
from onetree.errors import LandingRefusedError

_ALPHA = "a1\n"
_BETA = "b1\n"
_FIRST_PASS = ["after_audit", "after_tree", "before_intent"]
_ABANDONED_PASS = ["after_audit", "after_tree"]
_LANDING_PASS = [*_FIRST_PASS, "after_intent", "after_branch", "after_landed"]

#: Written by the referee process itself, one line per execution, from inside
#: the candidate checkout it was handed.
_REFEREE_SCRIPT = """\
import json, os, pathlib, sys
log = pathlib.Path(sys.argv[1]); mode = sys.argv[2]
a = pathlib.Path("a.txt").read_text(); b = pathlib.Path("b.txt").read_text()
red = mode == "combined-red" and a == "a1\\n" and b == "b1\\n"
def above(pid, depth=4):
    chain = []
    while pid > 1 and len(chain) < depth:
        try:
            stat = pathlib.Path(f"/proc/{pid}/stat").read_text()
            argv = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\\0")
        except OSError:
            break
        parent = int(stat.rsplit(")", 1)[1].split()[1])
        chain.append({"pid": pid, "ppid": parent,
                      "argv": [part.decode("utf-8", "replace") for part in argv if part]})
        pid = parent
    return chain
record = {"pid": os.getpid(), "ppid": os.getppid(), "candidate": os.environ["ONETREE_CANDIDATE"],
          "baseline": os.environ["ONETREE_BASELINE"], "a": a, "b": b,
          "verdict": "red" if red else "pass", "above": above(os.getppid())}
with log.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(record, sort_keys=True) + "\\n")
    handle.flush()
    os.fsync(handle.fileno())
sys.exit(1 if red else 0)
"""


def _append(path: Path, record: Mapping[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _lander_link(run: Mapping[str, Any], lander_pid: int) -> list[list[str]]:
    """The referee run was executed on behalf of ``lander_pid``.

    The recorded chain must be consistent link by link, must reach the lander,
    and every process between the referee and the lander must be one the
    installed ``onetree`` package launched, named by a file under the package
    directory in its argv. Returns those intermediates' argv, empty when the
    lander spawned the referee directly.
    """
    chain = list(run["above"])
    assert chain, run
    assert chain[0]["pid"] == run["ppid"]
    for lower, upper in zip(chain, chain[1:], strict=False):
        assert lower["ppid"] == upper["pid"], chain
    pids = [int(entry["pid"]) for entry in chain]
    assert lander_pid in pids, (lander_pid, chain)
    intermediates = chain[: pids.index(lander_pid)]
    package = str(Path(onetree.__file__).resolve().parent) + os.sep
    for entry in intermediates:
        assert any(str(part).startswith(package) for part in entry["argv"]), (package, entry)
    return [[str(part) for part in entry["argv"]] for entry in intermediates]


def _configure_referee(git_repo: GitFixture, tmp_path: Path, mode: str) -> Path:
    script = tmp_path / "referee.py"
    script.write_text(_REFEREE_SCRIPT, encoding="utf-8")
    log = tmp_path / "referee-log.jsonl"
    git_repo.git("config", "onetree.referee", shlex.join([
        sys.executable, "-I", "-S", str(script), str(log), mode,
    ]))
    return log


def _wait_for(condition: Any, what: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.02)


def _wait_for_gate(gate: str) -> None:
    deadline = time.monotonic() + 30
    while not Path(gate).exists():
        if time.monotonic() > deadline:
            raise TimeoutError(f"release gate never opened: {gate}")
        time.sleep(0.02)


def _train_lander(
    root: str, actor: str, barrier: Any, results: Any, holds: Mapping[str, str], phases: str,
) -> None:
    """One real submitter: green at the rendezvous, then released into the race.

    The first ``before_intent`` is the rendezvous; a later one is a retry and
    must not wait again. ``holds`` maps a seam to a gate file the test opens;
    each hold is honoured once, the first time its seam is crossed after the
    rendezvous, which is what makes an ordered case ordered.
    """
    coordinator = Coordinator(Path(root))
    rendezvoused = False
    honoured: set[str] = set()

    def checkpoint(point: str, context: Mapping[str, Any]) -> None:
        nonlocal rendezvoused
        _append(Path(phases), {
            "actor": actor, "phase": point, "pid": os.getpid(),
            "operation": context.get("operation"), "candidate": context.get("candidate"),
            "expected_head": context.get("expected_head"),
        })
        if point == "before_intent" and not rendezvoused:
            rendezvoused = True
            barrier.wait(timeout=30)
        if rendezvoused and point in holds and point not in honoured:
            honoured.add(point)
            _wait_for_gate(holds[point])

    try:
        landed = coordinator.land(actor, message=f"train {actor}", checkpoint=checkpoint)
    except LandingRefusedError as refusal:
        results.put({
            "actor": actor, "refused": str(refusal.details["reason"]),
            "receipt": dict(refusal.details.get("referee_receipt") or {}),
        })
    except Exception as error:  # pragma: no cover - asserted through the child result
        results.put({"actor": actor, "error": f"{type(error).__name__}: {error}"})
    else:
        results.put({
            "actor": actor, "attempts": landed.attempts, "commit": landed.commit,
            "intent": landed.intent, "receipt": dict(landed.referee_receipt or {}),
        })


class _Race:
    """Two staged submitters, their processes, and the logs they leave."""

    def __init__(self, git_repo: GitFixture, tmp_path: Path, mode: str) -> None:
        self.git_repo = git_repo
        self.coordinator = Coordinator(git_repo.root)
        _register_and_stage(git_repo, self.coordinator, "alpha", "a.txt", _ALPHA)
        _register_and_stage(git_repo, self.coordinator, "beta", "b.txt", _BETA)
        self.referee_log = _configure_referee(git_repo, tmp_path, mode)
        self.phases = tmp_path / "phases.jsonl"
        self.tmp_path = tmp_path
        context = multiprocessing.get_context("fork")
        self.barrier = context.Barrier(3)
        self.results = context.Queue()
        self.processes: dict[str, Any] = {}
        self.context = context
        # Registration and claim events are already on the WAL; the race is
        # measured as what it appends after this point.
        self.kinds_before = self.kinds()

    def gate(self, actor: str, point: str) -> Path:
        return self.tmp_path / f"gate-{actor}-{point}"

    def start(self, holds: Mapping[str, Mapping[str, str]] | None = None) -> None:
        """Start both landers; ``holds`` maps actor -> {seam: gate path}."""
        for actor in ("alpha", "beta"):
            process = self.context.Process(
                target=_train_lander,
                args=(str(self.git_repo.root), actor, self.barrier, self.results,
                      dict((holds or {}).get(actor, {})), str(self.phases)),
            )
            process.start()
            self.processes[actor] = process

    def open(self, actor: str, point: str) -> None:
        self.gate(actor, point).write_text("go\n", encoding="utf-8")

    def stop(self) -> None:
        for process in self.processes.values():
            if process.is_alive():
                process.kill()
                process.join(timeout=5)

    def referee_runs(self) -> list[dict[str, Any]]:
        return _records(self.referee_log)

    def phase_records(self, actor: str) -> list[dict[str, Any]]:
        return [record for record in _records(self.phases) if record["actor"] == actor]

    def passes(self, actor: str) -> list[list[str]]:
        """The actor's phases grouped by landing operation, in order."""
        grouped: dict[str, list[str]] = {}
        for record in self.phase_records(actor):
            grouped.setdefault(str(record["operation"]), []).append(str(record["phase"]))
        return list(grouped.values())

    def join(self, actor: str, timeout: float = 30.0) -> None:
        process = self.processes[actor]
        process.join(timeout=timeout)
        assert process.exitcode == 0, (actor, process.exitcode)

    def outcomes(self) -> dict[str, dict[str, Any]]:
        collected = [self.results.get(timeout=5) for _ in self.processes]
        return {str(outcome["actor"]): outcome for outcome in collected}

    def kinds(self) -> list[str]:
        return [event.kind for event in self.coordinator.inspect_wal().events]

    def new_kinds(self) -> list[str]:
        """Event kinds appended since the submitters were staged."""
        kinds = self.kinds()
        assert kinds[: len(self.kinds_before)] == self.kinds_before
        return kinds[len(self.kinds_before):]

    def new_events(self) -> list[Any]:
        return list(self.coordinator.inspect_wal().events)[len(self.kinds_before):]


def _assert_green_rendezvous(race: _Race) -> None:
    """Both submitters passed the referee against the same baseline; nothing is recorded."""
    git_repo = race.git_repo
    _wait_for(
        lambda: sum(
            1 for record in _records(race.phases) if record["phase"] == "before_intent"
        ) == 2,
        "both landers to reach before_intent",
    )
    runs = race.referee_runs()
    assert len(runs) == 2
    assert {run["baseline"] for run in runs} == {git_repo.base}
    assert {run["verdict"] for run in runs} == {"pass"}
    assert {(run["a"], run["b"]) for run in runs} == {(_ALPHA, "b0\n"), ("a0\n", _BETA)}
    candidates = {run["candidate"] for run in runs}
    assert len(candidates) == 2
    assert candidates == {
        str(race.phase_records(actor)[-1]["candidate"]) for actor in ("alpha", "beta")
    }
    # Each first run was executed for the live lander that built its candidate.
    for actor in ("alpha", "beta"):
        first = race.phase_records(actor)[-1]
        run = next(run for run in runs if run["candidate"] == first["candidate"])
        assert first["pid"] == race.processes[actor].pid
        assert race.processes[actor].is_alive()
        _lander_link(run, race.processes[actor].pid)
    assert git_repo.oid() == git_repo.base
    assert race.new_kinds() == []


def _assert_both_landed_in_train(race: _Race, winner: str, loser: str) -> None:
    git_repo = race.git_repo
    head = git_repo.oid()
    report = race.coordinator.inspect_wal()
    assert report.consistent
    assert report.pending_intents == 0
    assert report.divergences == ()
    assert race.new_kinds() == ["land_intent", "landed", "land_intent", "landed"]
    assert git_repo.git("rev-list", "--count", f"{git_repo.base}..{head}").stdout.strip() == "2"
    assert git_repo.git("show", f"{head}:a.txt").stdout == _ALPHA
    assert git_repo.git("show", f"{head}:b.txt").stdout == _BETA
    intents = [event for event in race.new_events() if event.kind == "land_intent"]
    assert [event.actor for event in intents] == [winner, loser]
    assert intents[0].data["expected_head"] == git_repo.base
    assert intents[1].data["expected_head"] == intents[0].data["candidate"]
    assert head == intents[1].data["candidate"]
    recovery = race.coordinator.recover()
    assert recovery.consistent
    assert (recovery.pending, recovery.repaired, recovery.refused) == (0, 0, 0)


def _assert_loser_retested_atop_winner(
    race: _Race, winner: dict[str, Any], loser: dict[str, Any], loser_actor: str,
    *, passes: int | None,
) -> None:
    """The loser's landing carries one referee run over a rebuilt candidate atop the winner.

    ``passes`` pins how many landing operations the loser opened; ``None``
    accepts any number of abandoned middle passes (audits invalidated before
    the referee ran), which a real race can produce, while still requiring
    exactly one retest and exactly one recorded intent.
    """
    assert winner["attempts"] == 1
    assert loser["attempts"] == 2
    runs = race.referee_runs()
    assert len(runs) == 3
    retest = runs[2]
    assert retest["baseline"] == winner["commit"]
    assert retest["candidate"] == loser["commit"]
    assert (retest["a"], retest["b"]) == (_ALPHA, _BETA)
    assert retest["verdict"] == "pass"
    # Executed for the losing lander, on its landing pass, for its landing candidate.
    _lander_link(retest, race.processes[loser_actor].pid)
    loser_passes = race.passes(loser_actor)
    assert loser_passes[0] == _FIRST_PASS
    assert loser_passes[-1] == _LANDING_PASS
    assert all(middle == _ABANDONED_PASS for middle in loser_passes[1:-1])
    if passes is not None:
        assert len(loser_passes) == passes
    loser_phases = race.phase_records(loser_actor)
    assert loser_phases[-1]["candidate"] == loser["commit"]
    assert loser_phases[-1]["expected_head"] == winner["commit"]
    # The first, green-at-base candidate is an unreachable object, not a landing.
    first_candidate = str(loser_phases[2]["candidate"])
    assert first_candidate != loser["commit"]
    assert first_candidate not in race.git_repo.git("rev-list", "--all").stdout.split()
    # The receipt the loser returned is the retest, and it is what the WAL recorded.
    assert loser["receipt"]["baseline"] == winner["commit"]
    assert loser["receipt"]["tested"] == [loser["commit"]]
    assert loser["receipt"]["result"] == "pass"
    recorded = [
        event for event in race.new_events()
        if event.kind == "land_intent" and event.actor == loser_actor
    ]
    assert len(recorded) == 1
    assert recorded[0].data["referee_receipt"]["baseline"] == winner["commit"]
    assert recorded[0].data["referee_receipt"]["tested"] == [loser["commit"]]


def _assert_refused_train_pending(race: _Race, loser: str, winner_commit: str) -> None:
    """The loser re-audited while the winner's intent was pending and was refused, recorded."""
    assert race.new_kinds() == ["land_intent", "refused", "landed"]
    refused = race.new_events()[1]
    assert refused.actor == loser
    assert refused.data["reason"] == "train_pending"
    assert race.passes(loser) == [_FIRST_PASS, _ABANDONED_PASS]
    assert len(race.referee_runs()) == 2
    assert race.git_repo.oid() == winner_commit


def _retry_after_refusal(race: _Race, loser: str, winner_commit: str) -> None:
    """The refused submitter's next attempt is the retest the contract promises."""
    git_repo = race.git_repo
    landed = race.coordinator.land(loser, message=f"train {loser} after refusal")
    runs = race.referee_runs()
    assert len(runs) == 3
    assert runs[2]["baseline"] == winner_commit
    assert runs[2]["candidate"] == landed.commit
    assert (runs[2]["a"], runs[2]["b"]) == (_ALPHA, _BETA)
    _lander_link(runs[2], os.getpid())
    assert landed.attempts == 1
    assert landed.referee_receipt is not None
    assert landed.referee_receipt["baseline"] == winner_commit
    head = git_repo.oid()
    assert head == landed.commit
    assert git_repo.git("rev-list", "--count", f"{git_repo.base}..{head}").stdout.strip() == "2"
    assert git_repo.git("show", f"{head}:a.txt").stdout == _ALPHA
    assert git_repo.git("show", f"{head}:b.txt").stdout == _BETA
    report = race.coordinator.inspect_wal()
    assert report.consistent
    assert report.pending_intents == 0
    assert race.new_kinds() == ["land_intent", "refused", "landed", "land_intent", "landed"]


def test_ordered_train_compatible_pair_loser_rebuilds_and_retests_atop_winner(
    git_repo: GitFixture, tmp_path: Path,
) -> None:
    race = _Race(git_repo, tmp_path, "compatible")
    race.start({"beta": {"before_intent": str(race.gate("beta", "before_intent"))}})
    try:
        _assert_green_rendezvous(race)
        race.barrier.wait(timeout=30)
        # alpha is released; beta is held on its gate with its stale audit.
        race.join("alpha")
        assert git_repo.oid() != git_repo.base
        assert race.new_kinds() == ["land_intent", "landed"]
        assert len(race.referee_runs()) == 2
        race.open("beta", "before_intent")
        race.join("beta")
    finally:
        race.stop()

    outcomes = race.outcomes()
    assert "error" not in outcomes["alpha"], outcomes
    assert "error" not in outcomes["beta"], outcomes
    _assert_both_landed_in_train(race, "alpha", "beta")
    _assert_loser_retested_atop_winner(race, outcomes["alpha"], outcomes["beta"], "beta", passes=2)


def test_ordered_train_pending_intent_refuses_the_loser_who_then_retests(
    git_repo: GitFixture, tmp_path: Path,
) -> None:
    """The winner is parked at ``after_intent``: its intent is on the WAL and its
    branch move has not happened. The loser, released, re-audits into that
    pending train and is refused ``train_pending`` with the refusal recorded.
    Once the winner lands, the loser's next attempt retests atop it."""
    race = _Race(git_repo, tmp_path, "compatible")
    # Both are held past the rendezvous: beta before its intent, alpha after
    # its own. Without beta's hold the two race for the intent CAS and either
    # may win it, which is the unordered case, not this one.
    race.start({
        "alpha": {"after_intent": str(race.gate("alpha", "after_intent"))},
        "beta": {"before_intent": str(race.gate("beta", "before_intent"))},
    })
    try:
        _assert_green_rendezvous(race)
        race.barrier.wait(timeout=30)
        _wait_for(
            lambda: race.passes("alpha") == [[*_FIRST_PASS, "after_intent"]],
            "alpha to park on its recorded intent",
        )
        assert race.new_kinds() == ["land_intent"]
        assert git_repo.oid() == git_repo.base
        # beta, released now, runs into alpha's pending intent and is refused
        # while alpha is still parked, so the refusal is recorded unraced.
        race.open("beta", "before_intent")
        race.join("beta")
        assert race.new_kinds() == ["land_intent", "refused"]
        assert git_repo.oid() == git_repo.base
        assert race.passes("alpha") == [[*_FIRST_PASS, "after_intent"]]
        race.open("alpha", "after_intent")
        race.join("alpha")
    finally:
        race.stop()

    outcomes = race.outcomes()
    assert outcomes["alpha"]["attempts"] == 1
    assert outcomes["beta"]["refused"] == "train_pending"
    winner_commit = str(outcomes["alpha"]["commit"])
    _assert_refused_train_pending(race, "beta", winner_commit)
    _retry_after_refusal(race, "beta", winner_commit)


def test_unordered_race_loser_is_refused_or_retests_and_both_payloads_land(
    git_repo: GitFixture, tmp_path: Path,
) -> None:
    """A real race. The loser's retry meets the winner's intent either still pending
    (refused ``train_pending``, recorded) or already landed (rebuilt and retested,
    possibly after abandoned audits whose refusal append lost the WAL CAS).
    Which one happens is timing; the case asserts the outcome that occurred
    against its own receipts and never lets both win."""
    race = _Race(git_repo, tmp_path, "compatible")
    race.start()
    try:
        _assert_green_rendezvous(race)
        race.barrier.wait(timeout=30)
        race.join("alpha")
        race.join("beta")
    finally:
        race.stop()

    outcomes = race.outcomes()
    assert all("error" not in outcome for outcome in outcomes.values()), outcomes
    winners = [actor for actor, outcome in outcomes.items() if outcome.get("attempts") == 1]
    assert len(winners) == 1
    winner = winners[0]
    loser = "beta" if winner == "alpha" else "alpha"
    winner_commit = str(outcomes[winner]["commit"])
    assert git_repo.git(
        "merge-base", "--is-ancestor", winner_commit, git_repo.oid(), check=False,
    ).returncode == 0

    if "refused" in outcomes[loser]:
        assert outcomes[loser]["refused"] == "train_pending"
        _assert_refused_train_pending(race, loser, winner_commit)
        _retry_after_refusal(race, loser, winner_commit)
    else:
        _assert_both_landed_in_train(race, winner, loser)
        _assert_loser_retested_atop_winner(
            race, outcomes[winner], outcomes[loser], loser, passes=None,
        )


def test_ordered_train_combined_red_pair_refuses_the_rebuilt_loser(
    git_repo: GitFixture, tmp_path: Path,
) -> None:
    """Known-bad input: each payload is green alone and the pair is red together.

    The loser's rebuilt candidate carries both, so its retest must refuse it
    with a receipt naming the winner as baseline, and its first, green candidate
    must not be what lands."""
    race = _Race(git_repo, tmp_path, "combined-red")
    race.start({"beta": {"before_intent": str(race.gate("beta", "before_intent"))}})
    try:
        _assert_green_rendezvous(race)
        race.barrier.wait(timeout=30)
        race.join("alpha")
        assert race.new_kinds() == ["land_intent", "landed"]
        race.open("beta", "before_intent")
        race.join("beta")
    finally:
        race.stop()

    outcomes = race.outcomes()
    alpha, beta = outcomes["alpha"], outcomes["beta"]
    assert alpha["attempts"] == 1
    assert beta["refused"] == "referee_red"
    head = git_repo.oid()
    assert head == alpha["commit"]
    assert git_repo.git("show", f"{head}:a.txt").stdout == _ALPHA
    assert git_repo.git("show", f"{head}:b.txt").stdout == "b0\n"

    runs = race.referee_runs()
    assert len(runs) == 3
    retest = runs[2]
    assert retest["baseline"] == alpha["commit"]
    assert (retest["a"], retest["b"]) == (_ALPHA, _BETA)
    assert retest["verdict"] == "red"
    _lander_link(retest, race.processes["beta"].pid)
    # The second pass ends at after_tree: the referee refused before any seam
    # past it, so the rebuilt candidate is named by the receipt, not a seam.
    assert race.passes("beta") == [_FIRST_PASS, _ABANDONED_PASS]
    assert retest["candidate"] == beta["receipt"]["candidate"]
    assert beta["receipt"]["result"] == "red"
    assert beta["receipt"]["returncode"] == 1
    assert beta["receipt"]["tested"] == [retest["candidate"]]
    assert git_repo.git("show", f"{retest['candidate']}:a.txt").stdout == _ALPHA
    assert git_repo.git("show", f"{retest['candidate']}:b.txt").stdout == _BETA
    first_candidate = str(race.phase_records("beta")[2]["candidate"])
    reachable = git_repo.git("rev-list", "--all").stdout.split()
    assert first_candidate not in reachable
    assert retest["candidate"] not in reachable

    report = race.coordinator.inspect_wal()
    assert report.consistent
    assert report.pending_intents == 0
    assert report.divergences == ()
    assert race.new_kinds() == ["land_intent", "landed", "refused"]
    refused = race.new_events()[-1]
    assert refused.actor == "beta"
    assert refused.data["reason"] == "referee_red"
    assert refused.data["referee_receipt"]["baseline"] == alpha["commit"]
    assert refused.data["referee_receipt"]["tested"] == [retest["candidate"]]
    recovery = race.coordinator.recover()
    assert recovery.consistent
    assert (recovery.pending, recovery.repaired, recovery.refused) == (0, 0, 0)


def test_referee_log_reads_the_checkout_it_runs_in(git_repo: GitFixture, tmp_path: Path) -> None:
    """Instrument control: the log's tree fields come from the candidate checkout.

    Run the same referee script by hand in two checkouts with different bytes
    and read two different records; a script that reported something other
    than its checkout would make every ``a``/``b`` assertion above vacuous."""
    log = _configure_referee(git_repo, tmp_path, "combined-red")
    script = tmp_path / "referee.py"
    for label, a_content, b_content in (("base", "a0\n", "b0\n"), ("both", _ALPHA, _BETA)):
        checkout = tmp_path / label
        checkout.mkdir()
        (checkout / "a.txt").write_text(a_content, encoding="utf-8")
        (checkout / "b.txt").write_text(b_content, encoding="utf-8")
        completed = subprocess.run(
            [sys.executable, "-I", "-S", str(script), str(log), "combined-red"],
            cwd=checkout, check=False, capture_output=True,
            env={"ONETREE_CANDIDATE": label, "ONETREE_BASELINE": "control", "PATH": os.defpath},
        )
        assert completed.returncode == (1 if label == "both" else 0)
    runs = _records(log)
    assert [(run["candidate"], run["a"], run["b"], run["verdict"]) for run in runs] == [
        ("base", "a0\n", "b0\n", "pass"),
        ("both", _ALPHA, _BETA, "red"),
    ]
    assert {run["ppid"] for run in runs} == {os.getpid()}
    # Run by hand there is nothing between the script and this process, and an
    # attribution to this process's own parent would have to pass through the
    # test runner, which the provenance helper refuses.
    assert [_lander_link(run, os.getpid()) for run in runs] == [[], []]
    with pytest.raises(AssertionError):
        _lander_link(runs[0], os.getppid())


@pytest.mark.parametrize("held", ["beta", "none"])
def test_rendezvous_holds_both_landers_before_any_intent(
    git_repo: GitFixture, tmp_path: Path, held: str,
) -> None:
    """The barrier itself: with the test never joining it, neither lander records
    an intent, however long it waits. Without this, "both green at the same
    baseline" would be a claim about a window nothing held open."""
    race = _Race(git_repo, tmp_path, "compatible")
    holds = {"beta": {"before_intent": str(race.gate("beta", "before_intent"))}}
    race.start(holds if held == "beta" else None)
    try:
        _assert_green_rendezvous(race)
        time.sleep(1.0)
        assert race.new_kinds() == []
        assert git_repo.oid() == git_repo.base
        assert all(process.is_alive() for process in race.processes.values())
    finally:
        race.stop()
    assert race.new_kinds() == []
