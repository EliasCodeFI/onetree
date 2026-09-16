"""Every metric of ``docs/PREREGISTRATION.md`` as a pure function of one system's event list.

The protocol fixes the definitions; where its text leaves an operational choice, the choice is
made here once and named so a reader disputes it against the frozen wording, not the code:

- Lost-work event: one per *write* (one path of one task) whose token reaches neither a
  landed payload blob at that path, nor the target branch's final tree at that path, nor a
  ``refusal`` event that lists its ``write_id``. An ``error`` event is a stock-git failure
  with no coordination layer behind it and is NOT a receipt, so the write it names can still
  be lost. Counted per run, as the protocol says.
- Collision: two writes to the same path by different writers whose uncommitted intervals
  overlap. A write is uncommitted from its ``write`` event until the first ``landed``,
  ``refusal`` or ``error`` event of its writer that lists its ``write_id``; a write with no
  such outcome stays uncommitted until ``run.ended``. Counted per unordered pair of writes,
  however the pair was resolved.
- Refusal: one per ``refusal`` event, grouped by its machine-readable reason. Errors are
  reported beside refusals, never among them.
- Landed-commit integrity: per landed commit, pass iff the set of payload paths equals the
  set of paths its ``write_ids`` wrote and every payload blob's content digest equals the
  corresponding write's. A payload path or content the writer did not produce is a foreign
  hunk; a written path absent from the payload is a missing hunk. Binary per commit.
- Throughput: green landings per wall-clock hour of a window that starts when the writers are
  released and ends only after every arm's green-verdict work is done. The onetree arm's referee
  runs inside each landing, so its window ends with its writers (``run.ended``). The incumbent and
  floor arms land without a referee and the harness referees their commits after their writers
  exit, so their window ends at the later of ``run.ended`` and the last ``referee`` event, whose
  time is taken when that verdict is complete; ``referee_seconds`` sums those runs. A landing is
  green when its ``landed`` event carries ``green: true`` or a ``referee`` event for the same
  commit reports ``pass``. A landing with no verdict is unverified and is not green. Serial
  after-the-window refereeing is this harness's choice, experimental until task 25 fixes how a
  baseline fleet referees.
- Overhead: the ``setup`` event's duration and the larger of the disk samples taken at setup
  end and run end. Two samples, not a continuous peak; the receipt says so.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from typing import Any

from onetree.bench.events import of_kind


def _outcomes(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """write_id -> the first outcome event (landed/refusal/error) listing it."""

    outcome: dict[str, dict[str, Any]] = {}
    for event in events:
        if event["kind"] not in {"landed", "refusal", "error"}:
            continue
        for write_id in event.get("write_ids", ()):
            outcome.setdefault(str(write_id), event)
    return outcome


def _run_bounds(events: list[dict[str, Any]]) -> tuple[float, float] | None:
    runs = list(of_kind(events, "run"))
    if len(runs) != 1:
        return None
    return float(runs[0]["started"]), float(runs[0]["ended"])


def _writes(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        of_kind(events, "write"), key=lambda event: (float(event["t"]), str(event["write_id"]))
    )


def lost_work(events: list[dict[str, Any]]) -> dict[str, Any]:
    landed_tokens: dict[str, set[str]] = {}
    for event in of_kind(events, "landed"):
        for entry in event.get("payload", ()):
            landed_tokens.setdefault(str(entry["path"]), set()).update(
                map(str, entry.get("tokens", ()))
            )
    final_tokens: dict[str, set[str]] = {}
    for event in of_kind(events, "final"):
        for path, tokens in event.get("tokens_by_path", {}).items():
            final_tokens.setdefault(str(path), set()).update(map(str, tokens))
    refused: set[str] = set()
    for event in of_kind(events, "refusal"):
        refused.update(map(str, event.get("write_ids", ())))
    lost: list[dict[str, Any]] = []
    writes = _writes(events)
    for write in writes:
        path, token, write_id = str(write["path"]), str(write["token"]), str(write["write_id"])
        reached_commit = token in landed_tokens.get(path, set()) or token in final_tokens.get(
            path, set()
        )
        if reached_commit or write_id in refused:
            continue
        lost.append(
            {
                "write_id": write_id,
                "writer": write["writer"],
                "task": write["task"],
                "path": path,
                "token": token,
            }
        )
    return {"writes": len(writes), "lost": len(lost), "events": lost}


def collisions(events: list[dict[str, Any]]) -> dict[str, Any]:
    bounds = _run_bounds(events)
    end_default = bounds[1] if bounds else float("inf")
    outcome = _outcomes(events)
    intervals: list[tuple[str, int, float, float, str]] = []
    for write in _writes(events):
        write_id = str(write["write_id"])
        out = outcome.get(write_id)
        end = float(out["t"]) if out is not None else end_default
        intervals.append(
            (str(write["path"]), int(write["writer"]), float(write["t"]), end, write_id)
        )
    pairs: list[dict[str, Any]] = []
    by_path: dict[str, list[tuple[str, int, float, float, str]]] = {}
    for interval in intervals:
        by_path.setdefault(interval[0], []).append(interval)
    for path, group in sorted(by_path.items()):
        for index, first in enumerate(group):
            for second in group[index + 1 :]:
                if first[1] == second[1]:
                    continue
                if first[2] <= second[3] and second[2] <= first[3]:
                    pairs.append(
                        {
                            "path": path,
                            "writes": sorted((first[4], second[4])),
                            "writers": sorted((first[1], second[1])),
                        }
                    )
    return {"collisions": len(pairs), "pairs": pairs}


def refusals(events: list[dict[str, Any]]) -> dict[str, Any]:
    reasons = Counter(str(event.get("reason", "")) for event in of_kind(events, "refusal"))
    errors = Counter(str(event.get("detail", ""))[:80] for event in of_kind(events, "error"))
    return {
        "refusals": sum(reasons.values()),
        "by_reason": dict(sorted(reasons.items())),
        "errors": sum(errors.values()),
        "error_details": dict(sorted(errors.items())),
    }


def integrity(events: list[dict[str, Any]]) -> dict[str, Any]:
    writes_by_id = {str(write["write_id"]): write for write in _writes(events)}
    commits: list[dict[str, Any]] = []
    for event in sorted(of_kind(events, "landed"), key=lambda item: float(item["t"])):
        intended: dict[str, str] = {}
        for write_id in event.get("write_ids", ()):
            write = writes_by_id.get(str(write_id))
            if write is not None:
                intended[str(write["path"])] = str(write["content_sha256"])
        payload = {
            str(entry["path"]): str(entry["content_sha256"]) for entry in event.get("payload", ())
        }
        foreign = sorted(path for path, digest in payload.items() if intended.get(path) != digest)
        missing = sorted(path for path in intended if path not in payload)
        commits.append(
            {
                "commit": event["commit"],
                "writer": event["writer"],
                "task": event["task"],
                "pass": not foreign and not missing,
                "foreign": foreign,
                "missing": missing,
            }
        )
    return {
        "commits": len(commits),
        "passed": sum(1 for item in commits if item["pass"]),
        "failed": sum(1 for item in commits if not item["pass"]),
        "per_commit": commits,
    }


def throughput(events: list[dict[str, Any]]) -> dict[str, Any]:
    verdicts: dict[str, bool] = {}
    verdict_times: list[float] = []
    referee_seconds = 0.0
    for event in of_kind(events, "referee"):
        verdicts[str(event["commit"])] = event.get("result") == "pass"
        verdict_times.append(float(event["t"]))
        referee_seconds += float(event.get("duration_s", 0.0))
    green = unverified = red = 0
    for event in of_kind(events, "landed"):
        own = event.get("green")
        verdict = own if isinstance(own, bool) else verdicts.get(str(event["commit"]))
        if verdict is None:
            unverified += 1
        elif verdict:
            green += 1
        else:
            red += 1
    bounds = _run_bounds(events)
    window: float | None = None
    writers_window: float | None = None
    window_end: str | None = None
    if bounds is not None:
        started, writers_ended = bounds
        ended = max([writers_ended, *verdict_times])
        window = ended - started
        writers_window = writers_ended - started
        window_end = "last harness referee verdict" if ended > writers_ended else "writers' window"
    hours = window / 3600.0 if window else None
    return {
        "landed": green + red + unverified,
        "green": green,
        "red": red,
        "unverified": unverified,
        "writers_window_seconds": writers_window,
        "referee_seconds": referee_seconds,
        "window_seconds": window,
        "window_end": window_end,
        "green_per_hour": green / hours if hours else None,
    }


def overhead(events: list[dict[str, Any]]) -> dict[str, Any]:
    setups = list(of_kind(events, "setup"))
    finals = list(of_kind(events, "final"))
    setup_seconds = sum(float(event.get("duration_s", 0.0)) for event in setups)
    samples = [int(event.get("disk_bytes", 0)) for event in (*setups, *finals)]
    return {
        "setup_seconds": setup_seconds,
        "peak_disk_bytes": max(samples) if samples else 0,
        "disk_samples": samples,
        "sampling": "setup end and run end only",
    }


def compute(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """All six metrics for one system's events, in the protocol's order."""

    listed = list(events)
    return {
        "lost_work": lost_work(listed),
        "collision": collisions(listed),
        "refusal": refusals(listed),
        "integrity": integrity(listed),
        "throughput": throughput(listed),
        "overhead": overhead(listed),
    }


__all__ = ["collisions", "compute", "integrity", "lost_work", "overhead", "refusals", "throughput"]
