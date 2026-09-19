"""Schema v3 evidence verifies only in the shapes the event contract states: a receipt holding
any other shape is void, not certified as the nearest valid value (Owner review of r3, F1/F2;
landed and referee identities and write times, task209)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from test_bench import _rederive, _rewrite_events, _synthetic_run

from onetree.bench import SCHEMA, SCHEMA_V2, events, receipts

ROOT = Path(__file__).resolve().parents[1]
OUTSIDE = "events outside the v3 event contract"
CENSUS = "run-end commit census differs from its run_end commit events"
Event = dict[str, Any]
Change = Callable[[Event], Event]


def _found(write: Event) -> Event:
    return {
        "system": "naive",
        "writer": 0,
        "kind": "commit",
        "t": 1.5,
        "task": 0,
        "attempt": 1,
        "write_ids": [write["write_id"]],
        "commit": "c9",
        "scope": "found",
        "payload": [
            {
                "path": write["path"],
                "blob": "b9",
                "content_sha256": write["content_sha256"],
                "tokens": [write["token"]],
            }
        ],
    }


def _census(write: Event) -> Event:
    event = {**_found(write), "writer": None, "t": 9.7, "scope": "run_end"}
    for key in ("task", "attempt", "write_ids"):
        del event[key]
    return event


def _seen(write: Event) -> Event:
    return {
        "system": "naive",
        "writer": 0,
        "kind": "observed",
        "t": 1.5,
        "task": 0,
        "attempt": 1,
        "checkout": "main",
        "index": "main",
        "after": "reset",
        "paths": {write["path"]: {"file": [], "index": []}},
    }


def _referee(identity: object) -> Event:
    return {
        "system": "naive",
        "writer": None,
        "kind": "referee",
        "t": 9.2,
        "commit": identity,
        "result": "pass",
        "returncode": 0,
        "duration_s": 0.1,
    }


def _with(**change: Any) -> Change:
    return lambda event: {**event, **change}


def _without(key: str) -> Change:
    return lambda event: {name: value for name, value in event.items() if name != key}


def _entry(**change: Any) -> Change:
    return lambda event: {**event, "payload": [{**event["payload"][0], **change}]}


def _entry_without(key: str) -> Change:
    return lambda event: {
        **event,
        "payload": [{name: value for name, value in event["payload"][0].items() if name != key}],
    }


def _read(entry: Event, **change: Any) -> Change:
    return lambda event: {**event, **change, "paths": dict.fromkeys(event["paths"], entry)}


def _unwritten(entry: object) -> Change:
    return lambda event: {**event, "paths": {**event["paths"], "corpus/unwritten.txt": entry}}


BASES: dict[str, Callable[[Event], Event]] = {"found": _found, "census": _census, "seen": _seen}
IDENTITY = (CENSUS,)
CASES: list[tuple[str, str, Change, str | None, tuple[str, ...]]] = [
    ("found commit", "found", _with(), None, ()),
    ("target commit deleting a path", "found", _with(scope="target", payload=[{
        "path": "corpus/gone.txt", "blob": None, "content_sha256": None, "tokens": []}]), None, ()),
    ("run_end census commit", "census", _with(), None, ()),
    ("unknown scope", "found", _with(scope="not-a-scope"), "scope", ()),
    ("found without identity", "found", _without("commit"), "commit", ()),
    ("found with empty identity", "found", _with(commit=""), "commit", ()),
    ("run_end without identity", "census", _without("commit"), "commit", IDENTITY),
    ("run_end with null identity", "census", _with(commit=None), "commit", IDENTITY),
    ("run_end with integer identity", "census", _with(commit=7), "commit", IDENTITY),
    ("run_end naming a commit a writer recorded", "census", _with(commit="c0"), "commit", ()),
    ("found from the harness", "found", _with(writer=None), "writer", ()),
    ("run_end from a writer", "census", _with(writer=0), "writer", ()),
    ("commit time as text", "found", _with(t="1.5"), "t", ()),
    ("commit time as boolean", "found", _with(t=True), "t", ()),
    ("commit time not a number", "found", _with(t=float("nan")), "t", ()),
    ("payload entry without blob", "found", _entry_without("blob"), "payload", ()),
    ("payload path empty", "found", _entry(path=""), "payload", ()),
    ("payload path integer", "found", _entry(path=7), "payload", ()),
    ("payload tokens as text", "found", _entry(tokens="aaaaaaaaaaaa"), "payload", ()),
    ("payload tokens integers", "found", _entry(tokens=[7]), "payload", ()),
    ("deleted path holding the token", "found", _entry(blob=None, content_sha256=None), "payload",
     ()),
    ("null blob with a digest", "found", _entry(blob=None), "payload", ()),
    ("integer blob", "found", _entry(blob=7), "payload", ()),
    ("found holding no token", "found", _entry(tokens=[]), "payload", ()),
    ("run_end holding no token", "census", _entry(tokens=[]), "payload", ()),
    ("read-back of gone bytes", "seen", _with(), None, ()),
    ("checkout-only read-back", "seen", _read({"file": []}, index=None), None, ()),
    ("index-only read-back", "seen", _read({"index": []}, checkout=None), None, ()),
    ("read-back of an absent file", "seen", _read({"file": None, "index": []}), None, ()),
    ("read-back of an unreadable file", "seen",
     _read({"file": [], "index": [], "unreadable": True}), None, ()),
    ("operation as integer", "seen", _with(after=7), "after", ()),
    ("operation empty", "seen", _with(after=""), "after", ()),
    ("run_end read-back from a writer", "seen", _with(after="run_end"), "writer", ()),
    ("operation read-back from the harness", "seen", _with(writer=None), "writer", ()),
    ("read-back time as text", "seen", _with(t="1.5"), "t", ()),
    ("read-back time as boolean", "seen", _with(t=True), "t", ()),
    ("checkout label integer", "seen", _read({"file": []}, checkout=5, index=None), "checkout", ()),
    ("checkout label empty", "seen", _with(checkout=""), "checkout", ()),
    ("index label integer", "seen", _with(index=7), "index", ()),
    ("file read without a checkout", "seen", _with(checkout=None), "paths", ()),
    ("index read without an index", "seen", _with(index=None), "paths", ()),
    ("file as text", "seen", _read({"file": "gone", "index": []}), "paths", ()),
    ("file tokens integers", "seen", _read({"file": [7], "index": []}), "paths", ()),
    ("index tokens as integer", "seen", _read({"file": [], "index": 7}), "paths", ()),
    ("unreadable as text", "seen", _read({"file": [], "index": [], "unreadable": "false"}),
     "paths", ()),
    ("unreadable false", "seen", _read({"file": [], "index": [], "unreadable": False}), "paths",
     ()),
    ("unreadable beside an absent file", "seen",
     _read({"file": None, "index": [], "unreadable": True}), "paths", ()),
    ("read-back of a path no write touched", "seen", _unwritten({"file": [], "index": []}), None,
     ()),
    ("read-back entry not an object", "seen", _unwritten("gone"), "paths", ()),
]
RECORDED: list[tuple[str, str, Change, str | None]] = [
    ("landing naming its commit", "landed", _with(), None),
    ("landing with null identity", "landed", _with(commit=None), "commit"),
    ("landing with empty identity", "landed", _with(commit=""), "commit"),
    ("landing with integer identity", "landed", _with(commit=7), "commit"),
    ("referee naming the landed commit", "referee", _with(), None),
    ("referee with null identity", "referee", _with(commit=None), "commit"),
    ("referee with empty identity", "referee", _with(commit=""), "commit"),
    ("referee with integer identity", "referee", _with(commit=7), "commit"),
    ("write time as integer", "write", _with(t=1), None),
    ("write time as text", "write", _with(t="1.0"), "t"),
    ("write time as boolean", "write", _with(t=True), "t"),
    ("write time as boolean false", "write", _with(t=False), "t"),
    ("write time not a number", "write", _with(t=float("nan")), "t"),
    ("write time infinite", "write", _with(t=float("inf")), "t"),
]


def _append(run: Path, event: Event) -> None:
    writer = event.get("writer")
    name = "naive-harness.jsonl" if writer is None else f"naive-w{writer}.jsonl"
    with (run / "events" / "parts" / name).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, sort_keys=True) + "\n")


def _list_in_census(run: Path, event: Event) -> None:
    if event.get("scope") != "run_end":
        return
    listed = [str(event.get("commit"))]

    def census(row: Event) -> None:
        if row["kind"] == "final":
            row["unrecorded_commits"] = listed

    _rewrite_events(run / "events" / "parts" / "naive-harness.jsonl", census)


def _line(run: Path, event: Event) -> int:
    merged = events.read(run / "events" / "naive.jsonl")
    lines = [
        number
        for number, row in enumerate(merged, 1)
        if receipts.canonical(row) == receipts.canonical(event)
    ]
    assert len(lines) == 1, lines
    return lines[0]


@pytest.mark.parametrize(
    ("case", "base", "change", "field", "also"), CASES, ids=[case[0] for case in CASES]
)
def test_v3_evidence_verifies_only_in_its_contract_shape(
    tmp_path: Path, case: str, base: str, change: Change, field: str | None, also: tuple[str, ...]
) -> None:
    assert Path(str(receipts.__file__)).resolve().is_relative_to(ROOT / "src")
    run = _synthetic_run(tmp_path / "run", seed=13, schema=SCHEMA)
    assert receipts.verify(run)["ok"]
    write = events.read(run / "events" / "parts" / "naive-w0.jsonl")[0]
    event = change(BASES[base](write))
    _append(run, event)
    _list_in_census(run, event)
    _rederive(run)
    report = receipts.verify(run)
    details = [void["detail"] for void in report["voids"]]
    if field is None:
        assert (report["ok"], report["voids"]) == (True, []), case
        return
    assert report["ok"] is False, case
    assert details == [OUTSIDE, *also], case
    assert report["voids"][0]["events"] == [
        {"line": _line(run, event), "kind": event["kind"], "field": field}
    ]
    assert report["voids"][0]["count"] == 1


@pytest.mark.parametrize(
    ("case", "kind", "change", "field"), RECORDED, ids=[case[0] for case in RECORDED]
)
def test_v3_identities_and_write_times_verify_only_in_their_contract_shape(
    tmp_path: Path, case: str, kind: str, change: Change, field: str | None
) -> None:
    run = _synthetic_run(tmp_path / "run", seed=13, schema=SCHEMA)
    part = run / "events" / "parts" / "naive-w0.jsonl"

    def recorded(row: Event) -> None:
        if row["kind"] == kind:
            row.update(change(row))

    _rewrite_events(part, recorded)
    referee = change(_referee("c0")) if kind == "referee" else _referee("c0")
    _append(run, referee)
    _rederive(run)
    report = receipts.verify(run)
    if field is None:
        assert (report["ok"], report["voids"]) == (True, []), case
        return
    event = next((row for row in events.read(part) if row["kind"] == kind), referee)
    assert report["ok"] is False, case
    assert [void["detail"] for void in report["voids"]] == [OUTSIDE], case
    assert report["voids"][0]["events"] == [
        {"line": _line(run, event), "kind": kind, "field": field}
    ]
    assert report["voids"][0]["count"] == 1


def test_an_identity_is_never_joined_through_its_text(tmp_path: Path) -> None:
    run = _synthetic_run(tmp_path / "run", seed=13, schema=SCHEMA)

    def numbered(row: Event) -> None:
        if row["kind"] == "landed":
            row["commit"] = 7

    _rewrite_events(run / "events" / "parts" / "naive-w0.jsonl", numbered)
    _append(run, _referee("7"))
    _rederive(run)
    report = receipts.verify(run)
    assert report["ok"] is False
    assert [void["detail"] for void in report["voids"]] == [OUTSIDE]
    assert [(item["kind"], item["field"]) for item in report["voids"][0]["events"]] == [
        ("landed", "commit")
    ]


def test_a_write_label_is_never_matched_through_its_text(tmp_path: Path) -> None:
    run = _synthetic_run(tmp_path / "run", seed=13, schema=SCHEMA)
    parts = run / "events" / "parts"
    closing = events.read(parts / "naive-harness.jsonl")[-2]
    assert (closing["kind"], closing["after"], closing["checkout"]) == (
        "observed",
        "run_end",
        "main",
    )

    def numbered(row: Event) -> None:
        if row["kind"] == "write":
            row["checkout"] = 5

    _rewrite_events(parts / "naive-w0.jsonl", numbered)
    _append(run, {**closing, "t": 9.6, "checkout": "5", "index": None, "paths": {
        path: {"file": entry["file"]} for path, entry in closing["paths"].items()
    }})
    _rederive(run)
    report = receipts.verify(run)
    assert report["ok"] is False
    assert [void["detail"] for void in report["voids"]] == [OUTSIDE]
    assert [(item["kind"], item["field"]) for item in report["voids"][0]["events"]] == [
        ("write", "checkout")
    ]


def test_the_void_names_the_first_ten_events_and_counts_them_all(tmp_path: Path) -> None:
    run = _synthetic_run(tmp_path / "run", seed=13, schema=SCHEMA)
    write = events.read(run / "events" / "parts" / "naive-w0.jsonl")[0]
    for step in range(12):
        _append(run, {**_found(write), "t": 1.5 + step / 100, "scope": "not-a-scope"})
    _rederive(run)
    voids = receipts.verify(run)["voids"]
    assert [void["detail"] for void in voids] == [OUTSIDE]
    assert (voids[0]["count"], len(voids[0]["events"])) == (12, 10)
    lines = [item["line"] for item in voids[0]["events"]]
    assert lines == sorted(lines)


def test_v2_receipts_are_not_read_against_the_v3_contract(tmp_path: Path) -> None:
    run = _synthetic_run(tmp_path / "run", seed=13, schema=SCHEMA_V2)

    def labelled(row: Event) -> None:
        if row["kind"] == "write":
            row.update(checkout=5, index="")

    _rewrite_events(run / "events" / "parts" / "naive-w0.jsonl", labelled)
    _rederive(run)
    report = receipts.verify(run)
    assert (report["ok"], report["schema"], report["voids"]) == (True, SCHEMA_V2, [])


def test_v2_identities_and_write_times_keep_their_historical_reading(tmp_path: Path) -> None:
    run = _synthetic_run(tmp_path / "run", seed=13, schema=SCHEMA_V2)

    def unnamed(row: Event) -> None:
        if row["kind"] == "landed":
            row["commit"] = None
        if row["kind"] == "write":
            row["t"] = str(row["t"])

    _rewrite_events(run / "events" / "parts" / "naive-w0.jsonl", unnamed)
    _append(run, _referee(None))
    _rederive(run)
    report = receipts.verify(run)
    summary = receipts.read_json(run / receipts.FREEZE)["summary"]["naive"]
    assert (report["ok"], report["schema"], report["voids"]) == (True, SCHEMA_V2, [])
    assert summary["throughput.green"] == 1
