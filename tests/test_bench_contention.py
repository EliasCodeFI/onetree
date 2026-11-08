"""Same-target landing contention: the offer bound on a tested candidate, and the instrument.

``docs/ROUND-CONTENTION.md`` records the measurements. The first two tests pin the shape of
the gap it found: commit 187147e keeps a tested candidate across unrelated WAL appends, but
only for ``MAX_CAS_RETRIES`` intent offers per attempt. When every offer of an attempt loses
the WAL CAS to an unrelated append, the attempt is abandoned and the next one re-runs the
payload gate and the referee on an unchanged candidate. The appends here are injected at the
offer seam, so the loss count is exact rather than a race. A fix that keeps the candidate past
the bound flips the second test; it is a fixture to flip, not a property to keep.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from conftest import GitFixture

from onetree.bench import contention
from onetree.core import MAX_CAS_RETRIES, Coordinator

_COUNTING_REFEREE = """\
import pathlib, sys
counter = pathlib.Path(sys.argv[1])
counter.write_text(str(int(counter.read_text()) + 1 if counter.exists() else 1))
"""


def _land_against_unrelated_appends(
    git_repo: GitFixture, tmp_path: Path, lost_offers: int,
) -> tuple[int, int, int]:
    """Land a.txt while a peer session's claim or release beats the first ``lost_offers``
    intent offers. Returns (referee runs, attempts, offers)."""

    counter = tmp_path / "referee.count"
    script = tmp_path / "referee.py"
    script.write_text(_COUNTING_REFEREE, encoding="utf-8")
    git_repo.git("config", "onetree.referee",
                 shlex.join([sys.executable, "-I", "-S", str(script), str(counter)]))
    coordinator = Coordinator(git_repo.root)
    session = coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    coordinator.register_session("noise", pid=os.getpid(), ttl=300)
    coordinator.claim("alpha", [str(git_repo.root / "a.txt")])
    git_repo.write("a.txt", "a1\n")
    git_repo.stage(session.index_path, "a.txt")
    noise = str(git_repo.root / "b.txt")
    offers = 0

    def checkpoint(point: str, context: Mapping[str, Any]) -> None:
        nonlocal offers
        if point not in {"before_intent", "before_rebased_intent"}:
            return
        offers += 1
        if offers <= lost_offers:
            # One unrelated append after the offer's WAL read: its CAS must lose.
            if offers % 2:
                coordinator.claim("noise", [noise])
            else:
                coordinator.release("noise", [noise])

    landed = coordinator.land("alpha", message="contention offer bound", checkpoint=checkpoint)
    assert git_repo.git("show", f"{landed.commit}:a.txt").stdout == "a1\n"
    assert coordinator.inspect_wal().consistent
    return int(counter.read_text()), landed.attempts, offers


def test_unrelated_appends_within_the_offer_bound_keep_the_tested_candidate(
    git_repo: GitFixture, tmp_path: Path,
) -> None:
    """Control: 187147e's guarantee. Every offer but the last loses; one referee run."""

    runs, attempts, offers = _land_against_unrelated_appends(
        git_repo, tmp_path, MAX_CAS_RETRIES - 1,
    )
    assert (runs, attempts, offers) == (1, 1, MAX_CAS_RETRIES)


def test_unrelated_appends_exhausting_the_offer_bound_rerun_the_referee(
    git_repo: GitFixture, tmp_path: Path,
) -> None:
    """Known gap shape (ROUND-CONTENTION): one more lost offer and the unchanged candidate
    is abandoned; the second attempt re-runs gate and referee. A fix flips this to 1 run."""

    runs, attempts, offers = _land_against_unrelated_appends(git_repo, tmp_path, MAX_CAS_RETRIES)
    assert (runs, attempts, offers) == (2, 2, MAX_CAS_RETRIES + 1)


def test_jain_index_bounds() -> None:
    assert contention.jain([]) is None
    assert contention.jain([0.0, 0.0]) is None
    assert contention.jain([3.0, 3.0, 3.0]) == pytest.approx(1.0)
    assert contention.jain([1.0, 0.0, 0.0, 0.0]) == pytest.approx(0.25)


def test_grid_is_complete_and_seeded() -> None:
    cells = contention.grid([2, 4], [0.2, 2.0], 3, [False, True], seed=7)
    assert len(cells) == 2 * 2 * 3 * 2
    assert len(set(cells)) == len(cells)
    assert cells == contention.grid([2, 4], [0.2, 2.0], 3, [False, True], seed=7)
    assert cells != sorted(cells, key=lambda c: (c.landers, c.referee_seconds, c.control,
                                                 c.repetition))


@pytest.mark.parametrize("control", [False, True], ids=["plain", "unrelated-appends"])
def test_run_cell_lands_every_lander_with_real_processes(tmp_path: Path, control: bool) -> None:
    """Instrument: N real CLI landers, each its own session body, all land; the referee log
    attributes every run to a lander, and runs are at least one per landing."""

    cell = contention.Cell(2, 0.05, control, 0, control_interval=0.2)
    result = contention.run_cell(cell, tmp_path, deadline=300)
    summary, rows = result["summary"], result["rows"]
    assert summary["landed"] == 2
    assert summary["branch_commits"] == 2
    assert summary["wal_consistent"] is True
    assert summary["referee_runs"] == sum(row["referee_runs"] for row in rows)
    assert all(row["referee_runs"] >= 1 for row in rows)
    assert all(row["outcome"] == "landed" and row["exit"] == 0 for row in rows)
    assert {row["actor"] for row in rows} == {"lander0", "lander1"}
    assert (summary["control_appends"] is not None) == control
    if control:
        assert summary["control_appends"] > 0


def test_lander_resubmits_only_the_retryable_refusals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes = iter([
        {"outcome": "refused", "reason": "train_pending"},
        {"outcome": "refused", "reason": "retry_limit"},
        {"outcome": "refused", "reason": "unclaimed_paths"},
        {"outcome": "landed"},
    ])
    monkeypatch.setattr(contention, "_submit", lambda *args: next(outcomes))
    go, out = tmp_path / "go", tmp_path / "out.json"
    go.write_text("go")
    assert contention.lander_main(["root", "alpha", "gate", str(go), str(out), "10"]) == 0
    report = json.loads(out.read_text())
    assert [item.get("reason") for item in report["submissions"]] == [
        "train_pending", "retry_limit", "unclaimed_paths",
    ]


def test_main_json_rejects_a_bad_grid(capsys: pytest.CaptureFixture[str]) -> None:
    assert contention.main(["--out", "x", "--landers", "two", "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False


def test_main_runs_a_cell_and_writes_raw_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    out = tmp_path / "out"
    assert contention.main(["--out", str(out), "--landers", "2", "--referee-seconds", "0.05",
                            "--repetitions", "1", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True
    assert report["cells"] == 1
    assert len((out / "landings.jsonl").read_text().splitlines()) == 2
    assert json.loads((out / "runs.jsonl").read_text())["landed"] == 2
