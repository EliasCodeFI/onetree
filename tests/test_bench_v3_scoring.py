"""Schema v3 collision intervals over scripted logs: a write stops being uncommitted when a
Git commit contains it (decision b62102092057) or when its bytes disappear (decision
d680c759ca85)."""

from __future__ import annotations

from typing import Any

import pytest

from onetree.bench import metrics

HOT = "corpus/f000.txt"


def _write(writer: int, token: str, t: float, checkout: str = "main", index: str = "main") -> Any:
    return {
        "system": "fixture",
        "writer": writer,
        "kind": "write",
        "t": t,
        "task": 0,
        "attempt": 1,
        "write_id": f"w{writer}-{token}",
        "path": HOT,
        "token": token,
        "content_sha256": "0" * 64,
        "checkout": checkout,
        "index": index,
    }


def _commit(t: float, tokens: list[str], scope: str = "target") -> Any:
    return {
        "system": "fixture",
        "writer": 0,
        "kind": "commit",
        "t": t,
        "scope": scope,
        "commit": "c",
        "payload": [{"path": HOT, "blob": "b", "content_sha256": "0" * 64, "tokens": tokens}],
    }


def _seen(
    t: float,
    file: list[str] | None = None,
    index: list[str] | None = None,
    *,
    checkout: str | None = "main",
    staging: str | None = "main",
    after: str = "write",
    unreadable: bool = False,
) -> Any:
    entry: dict[str, Any] = {}
    if checkout is not None:
        entry["file"] = file
        if unreadable:
            entry["unreadable"] = True
    if staging is not None:
        entry["index"] = index
    return {
        "system": "fixture",
        "writer": None if after == "run_end" else 0,
        "kind": "observed",
        "t": t,
        "checkout": checkout,
        "index": staging,
        "after": after,
        "paths": {HOT: entry},
    }


def _run(ended: float = 10.0) -> Any:
    return {
        "system": "fixture",
        "writer": None,
        "kind": "run",
        "t": ended,
        "started": 0.0,
        "ended": ended,
        "complete": True,
    }


def _end(t: float, file: list[str], index: list[str] | None = None) -> Any:
    return _seen(t, file, [] if index is None else index, after="run_end")


def _score(events: list[dict[str, Any]]) -> tuple[Any, int]:
    ordered = sorted(events, key=lambda event: float(event["t"]))
    return metrics.collisions_v3(ordered), metrics.collisions(ordered)["collisions"]


def _one(first: str, second: str) -> dict[str, Any]:
    return {"path": HOT, "writes": sorted([first, second]), "writers": [0, 1]}


@pytest.mark.parametrize(("scope", "holds", "expected"), [
    ("target", True, 0),
    ("local", True, 0),
    ("candidate", True, 0),
    ("found", True, 0),
    ("target", False, 1),
])
def test_a_commit_that_contains_the_token_ends_the_interval(
    scope: str, holds: bool, expected: int
) -> None:
    events = [
        _write(0, "a", 1.0),
        _commit(2.0, ["a"] if holds else ["other"], scope),
        _write(1, "b", 3.0),
        _run(),
        _end(11.0, ["a", "b"]),
    ]
    v3, v2 = _score(events)
    assert (v3["collisions"], v3["undetermined"], v2) == (expected, [], 1)


@pytest.mark.parametrize(("committed_at", "expected"), [(3.0, 1), (1.5, 0)])
def test_a_commit_after_the_last_read_back_still_ends_the_interval(
    committed_at: float, expected: int
) -> None:
    events = [_write(0, "a", 1.0), _write(1, "b", 2.0), _commit(committed_at, ["a"]), _run()]
    v3, _ = _score(events)
    assert (v3["collisions"], v3["undetermined"]) == (expected, [])


@pytest.mark.parametrize(("file", "expected"), [([], 0), (["a"], 1)])
def test_bytes_seen_gone_end_the_interval_and_a_failure_ends_nothing(
    file: list[str], expected: int
) -> None:
    error = {"system": "fixture", "writer": 0, "kind": "error", "t": 1.5, "write_ids": ["w0-a"]}
    events = [
        _write(0, "a", 1.0),
        error,
        _seen(2.0, file, []),
        _write(1, "b", 3.0),
        _run(),
        _end(11.0, [*file, "b"]),
    ]
    v3, v2 = _score(events)
    assert (v3["collisions"], v3["undetermined"], v2) == (expected, [], 0)


def test_a_carried_earlier_token_keeps_its_own_interval() -> None:
    events = [
        _write(0, "a", 1.0),
        {**_write(0, "a2", 2.0), "attempt": 2},
        _write(1, "b", 3.0),
        _run(),
        _end(11.0, ["a", "a2", "b"]),
    ]
    v3, _ = _score(events)
    assert v3["pairs"] == [_one("w0-a", "w1-b"), _one("w0-a2", "w1-b")]
    assert v3["collisions"] == 2


def test_a_read_back_of_another_checkout_or_index_ends_nothing() -> None:
    other = _seen(2.0, [], [], checkout="wt/w1", staging="wt/w1")
    own = _seen(2.0, [], [], checkout="wt/w0", staging="wt/w0")
    base = [
        _write(0, "a", 1.0, checkout="wt/w0", index="wt/w0"),
        _write(1, "b", 3.0, checkout="wt/w1", index="wt/w1"),
        _run(),
        _seen(11.0, [], [], checkout="wt/w0", staging="wt/w0", after="run_end"),
        _seen(11.0, ["b"], [], checkout="wt/w1", staging="wt/w1", after="run_end"),
    ]
    unrelated = _score([*base, other])[0]
    assert unrelated["collisions"] is None
    assert [item["cause"] for item in unrelated["undetermined"]] == ["unobserved_end"]
    assert _score([*base, own])[0] == {"collisions": 0, "pairs": [], "undetermined": []}


@pytest.mark.parametrize(("staged_at_peer_write", "expected"), [(True, 1), (False, 0)])
def test_bytes_left_only_in_an_index_keep_the_write_present(
    staged_at_peer_write: bool, expected: int
) -> None:
    events = [
        _write(0, "a", 1.0),
        _seen(1.5, ["a"], ["a"], after="add"),
        _seen(2.0, [], ["a"] if staged_at_peer_write else []),
        _write(1, "b", 3.0),
        _seen(4.0, ["b"], ["b"], after="add"),
        _run(),
        _end(11.0, ["b"], ["b"]),
    ]
    v3, _ = _score(events)
    assert v3["collisions"] == expected
    assert v3["undetermined"] == []
    assert v3["pairs"] == ([_one("w0-a", "w1-b")] if expected else [])


@pytest.mark.parametrize(("elsewhere_holds", "expected"), [(True, 1), (False, 0)])
def test_a_copy_in_an_index_the_write_never_named_keeps_it_present(
    elsewhere_holds: bool, expected: int
) -> None:
    events = [
        _write(0, "a", 1.0, checkout="main", index="origin"),
        _seen(1.5, None, ["a"] if elsewhere_holds else [], checkout=None, staging="peer"),
        _seen(2.0, [], [], checkout="main", staging="origin"),
        _write(1, "b", 3.0, checkout="main", index="origin"),
        _run(),
        _seen(11.0, ["b"], [], checkout="main", staging="origin", after="run_end"),
        _seen(11.0, None, ["a"] if elsewhere_holds else [], checkout=None, staging="peer",
              after="run_end"),
    ]
    v3, _ = _score(events)
    assert v3["collisions"] == expected
    assert v3["undetermined"] == []
    assert v3["pairs"] == ([_one("w0-a", "w1-b")] if expected else [])


def test_a_place_that_never_held_the_bytes_neither_ends_nor_extends_a_write() -> None:
    events = [
        _write(0, "a", 1.0, checkout="main", index="main"),
        _write(1, "b", 1.2, checkout="main", index="main"),
        _run(),
        _seen(11.0, [], [], checkout="main", staging="main", after="run_end"),
    ]
    elsewhere = _seen(2.0, [], [], checkout="other", staging="other")
    alone, _ = _score(events)
    assert alone["collisions"] is None
    assert [item["cause"] for item in alone["undetermined"]] == ["unobserved_end"]
    assert _score([*events, elsewhere])[0] == alone


def _census(observed: str, cleared: bool) -> Any:
    events = [
        _write(0, "a", 1.0, checkout="main", index="origin"),
        _write(1, "b", 3.0, checkout="main", index="origin"),
        _run(),
        _seen(11.0, [], [], checkout="main", staging="origin", after="run_end"),
        _seen(11.5, [], ["a"], checkout="main", staging=observed, after="run_end"),
    ]
    if cleared:
        events.append(_seen(12.0, [], [], checkout="main", staging=observed, after="run_end"))
    return _score(events)[0]


def test_the_closing_census_clears_a_place_whatever_index_name_it_carries() -> None:
    own = _census("origin", cleared=True)
    assert own["collisions"] is None
    assert [item["cause"] for item in own["undetermined"]] == ["unobserved_end"]
    assert _census("peer", cleared=True) == own


def test_a_closing_census_that_still_holds_the_bytes_ends_the_write_whatever_it_is_called() -> None:
    own = _census("origin", cleared=False)
    assert own["collisions"] == 1
    assert own["undetermined"] == []
    assert _census("peer", cleared=False) == own


def test_a_copy_elsewhere_ends_once_every_place_holding_it_is_observed_without_it() -> None:
    events = [
        _write(0, "a", 1.0, checkout="main", index="origin"),
        _seen(1.5, None, ["a"], checkout=None, staging="peer"),
        _seen(2.0, [], [], checkout="main", staging="origin"),
        _seen(2.5, None, [], checkout=None, staging="peer"),
        _write(1, "b", 3.0, checkout="main", index="origin"),
        _run(),
        _end(11.0, ["b"], []),
    ]
    v3, _ = _score(events)
    assert v3 == {"collisions": 0, "pairs": [], "undetermined": []}


def test_bytes_left_only_in_an_index_change_nothing_without_a_peer_in_the_window() -> None:
    events = [
        _write(0, "a", 1.0),
        _seen(2.0, [], ["a"]),
        _seen(3.0, [], []),
        _write(1, "b", 4.0),
        _run(),
        _end(11.0, ["b"]),
    ]
    assert _score(events)[0] == {"collisions": 0, "pairs": [], "undetermined": []}


@pytest.mark.parametrize("returns", [True, False])
def test_bytes_that_return_after_disappearing_do_not_reopen_the_interval(returns: bool) -> None:
    events = [
        _write(0, "a", 1.0),
        _seen(2.0, [], []),
        _seen(3.0, ["a"] if returns else [], []),
        _write(1, "b", 4.0),
        _commit(5.0, ["a", "b"] if returns else ["b"]),
        _run(),
        _end(11.0, ["a", "b"] if returns else ["b"]),
    ]
    assert _score(events)[0] == {"collisions": 0, "pairs": [], "undetermined": []}
    unseen = [event for event in events if event["t"] != 2.0]
    assert _score(unseen)[0]["collisions"] == (1 if returns else 0)


@pytest.mark.parametrize(("peer_at", "holds", "expected", "causes"), [
    (3.0, True, None, ["unobserved_inclusion"]),
    (0.5, True, 1, []),
    (3.0, False, 1, []),
])
def test_a_commit_first_found_at_run_end_has_no_observed_inclusion(
    peer_at: float, holds: bool, expected: int | None, causes: list[str]
) -> None:
    events = [
        _write(0, "a", 1.0),
        _write(1, "b", peer_at),
        _run(),
        _end(11.0, ["a", "b"]),
        {**_commit(12.0, ["a"] if holds else ["other"], "run_end"), "writer": None},
    ]
    v3, _ = _score(events)
    assert (v3["collisions"], [item["cause"] for item in v3["undetermined"]]) == (expected, causes)


@pytest.mark.parametrize("peer_at", [0.5, 1.5, 3.0])
@pytest.mark.parametrize("seen_at", [None, 2.0])
def test_both_extremes_decide_what_every_mixture_of_unobserved_ends_decides(
    peer_at: float, seen_at: float | None
) -> None:
    events = [
        _write(0, "a", 1.0, checkout="wt/w0", index="wt/w0"),
        _write(1, "b", peer_at, checkout="wt/w1", index="wt/w1"),
        _run(),
        _seen(11.0, [], [], checkout="wt/w0", staging="wt/w0", after="run_end"),
        _seen(11.0, [], [], checkout="wt/w1", staging="wt/w1", after="run_end"),
    ]
    if seen_at is not None:
        events.append(_seen(seen_at, ["a"], [], checkout="wt/w0", staging="wt/w0"))
    ends = {0: (seen_at or 1.0, 10.0), 1: (peer_at, 10.0)}
    outcomes = {
        ends[1][late_peer] >= 1.0 and ends[0][late_first] >= peer_at
        for late_first in (0, 1)
        for late_peer in (0, 1)
    }
    v3, _ = _score(events)
    expected = int(next(iter(outcomes))) if len(outcomes) == 1 else None
    assert v3["collisions"] == expected
    assert [item["cause"] for item in v3["undetermined"]] == (
        [] if expected is not None else ["unobserved_end"]
    )


@pytest.mark.parametrize(("at_run_end", "peer_at", "expected"), [
    ([], 3.0, None),
    (["a"], 3.0, 1),
    ([], 0.5, 1),
])
def test_bytes_first_seen_gone_at_run_end_have_no_observed_end(
    at_run_end: list[str], peer_at: float, expected: int | None
) -> None:
    events = [
        _write(0, "a", 1.0),
        _write(1, "b", peer_at),
        _run(),
        _end(11.0, [*at_run_end, "b"]),
    ]
    v3, _ = _score(events)
    assert v3["collisions"] == expected
    if expected is None:
        assert [item["cause"] for item in v3["undetermined"]] == ["unobserved_end"]


@pytest.mark.parametrize(("unreadable", "expected"), [(True, 1), (False, 0)])
def test_an_unreadable_read_back_observes_nothing(unreadable: bool, expected: int) -> None:
    events = [
        _write(0, "a", 1.0),
        _seen(2.0, [], [], unreadable=unreadable),
        _write(1, "b", 3.0),
        _seen(4.0, ["a", "b"] if unreadable else ["b"], []),
        _run(),
        _end(11.0, ["a", "b"] if unreadable else ["b"]),
    ]
    v3, _ = _score(events)
    assert (v3["collisions"], v3["undetermined"]) == (expected, [])


def test_a_session_index_read_at_run_end_keeps_a_restored_onetree_write_present() -> None:
    session = "bench-w0-t0-a1"
    events = [
        _write(0, "a", 1.0, index=session),
        _seen(2.0, [], ["a"], staging=session, after="restore"),
        _write(1, "b", 3.0, index="bench-w1-t0-a1"),
        _commit(4.0, ["b"]),
        _run(),
        _seen(11.0, ["b"], None, staging=None, after="run_end"),
        _seen(11.0, None, ["a"], checkout=None, staging=session, after="run_end"),
    ]
    v3, v2 = _score(events)
    assert v3 == {"collisions": 1, "pairs": [_one("w0-a", "w1-b")], "undetermined": []}
    emptied = [*events[:-1], _seen(11.0, None, [], checkout=None, staging=session, after="run_end")]
    emptied[1] = _seen(2.0, [], [], staging=session, after="restore")
    assert _score(emptied)[0] == {"collisions": 0, "pairs": [], "undetermined": []}
    assert v2 == 1
