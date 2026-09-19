"""Old receipts keep the old manifest and scoring interpretation, new receipts select the new
one (decision 0969cbbc24d1), and an unknown schema is handled explicitly, never certified."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest
from test_bench import _edit_json, _rederive, _refreeze, _rewrite_events, _synthetic_run

from onetree.bench import SCHEMA, SCHEMA_V2, SCHEMA_V3, receipts

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = [SCHEMA_V2, SCHEMA]


def _origins() -> dict[str, str]:
    origins = {}
    for name, module in sorted(sys.modules.items()):
        if name == "onetree" or name.startswith("onetree."):
            source = Path(str(module.__file__)).resolve()
            assert source.is_relative_to(ROOT / "src"), (name, source)
            origins[name] = hashlib.sha256(source.read_bytes()).hexdigest()
    assert {"onetree.bench.receipts", "onetree.bench.metrics"} <= set(origins)
    return origins


def _relabel(
    run: Path, schema: object, names: tuple[str, ...] = ("run.json", "FREEZE.json")
) -> None:
    for name in names:
        _edit_json(run / name, lambda record: record.__setitem__("schema", schema))


@pytest.mark.parametrize("schema", SCHEMAS)
def test_each_schema_verifies_under_its_own_rules(tmp_path: Path, schema: str) -> None:
    run = _synthetic_run(tmp_path / "run", schema=schema)
    report = receipts.verify(run)
    assert (report["ok"], report["schema"], report["voids"]) == (True, schema, [])
    kinds = {
        json.loads(line)["kind"]
        for line in (run / "events" / "naive.jsonl").read_text(encoding="utf-8").splitlines()
    }
    if schema == SCHEMA:
        assert {"commit", "observed"} <= kinds
        assert report["unchecked"] == [*receipts.UNCHECKED, *receipts.UNCHECKED_V3]
        assert report["undetermined"] == []
    else:
        assert not {"commit", "observed"} & kinds
        assert report["unchecked"] == list(receipts.UNCHECKED)
        assert "undetermined" not in report
    _origins()


def test_a_v2_run_relabelled_v3_is_voided_not_upgraded(tmp_path: Path) -> None:
    run = _synthetic_run(tmp_path / "run")
    _relabel(run, SCHEMA_V3)
    _refreeze(run)
    report = receipts.verify(run)
    assert report["ok"] is False
    assert [(void["what"], void["detail"]) for void in report["voids"]] == [
        ("events", "events outside the v3 event contract"),
        ("events", "writes whose checkout or index has no run-end read-back"),
        ("events", "no run-end commit census"),
        ("metrics", "cannot compute: 'checkout'"),
    ]
    assert [(item["kind"], item["field"]) for item in report["voids"][0]["events"]] == [
        ("write", "checkout"),
        ("write", "checkout"),
    ]


@pytest.mark.parametrize(
    ("census", "detail"),
    [
        (None, "no run-end commit census"),
        ("absent", "no run-end commit census"),
        (["c9"], "run-end commit census differs from its run_end commit events"),
    ],
)
def test_a_v3_run_needs_a_run_end_commit_census_that_matches_its_events(
    tmp_path: Path, census: object, detail: str
) -> None:
    run = _synthetic_run(tmp_path / "run", schema=SCHEMA)
    assert receipts.verify(run)["ok"]

    def recensus(row: dict[str, Any]) -> None:
        if row["kind"] == "final":
            if census == "absent":
                del row["unrecorded_commits"]
            else:
                row["unrecorded_commits"] = census

    _rewrite_events(run / "events" / "parts" / "naive-harness.jsonl", recensus)
    _rederive(run)
    report = receipts.verify(run)
    assert [(void["what"], void["detail"]) for void in report["voids"]] == [("events", detail)]


def test_a_census_inclusion_is_reported_undetermined_and_never_compared(
    tmp_path: Path,
) -> None:
    run = _synthetic_run(tmp_path / "run", seed=13, schema=SCHEMA)
    summary = receipts.read_json(run / receipts.FREEZE)["summary"]["naive"]
    assert summary["collision.collisions"] == 1
    write = json.loads(
        (run / "events" / "parts" / "naive-w0.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    harness = run / "events" / "parts" / "naive-harness.jsonl"

    def recensus(row: dict[str, Any]) -> None:
        if row["kind"] == "final":
            row["unrecorded_commits"] = ["c9"]

    _rewrite_events(harness, recensus)
    census = {
        "system": "naive",
        "writer": None,
        "kind": "commit",
        "t": 9.7,
        "commit": "c9",
        "scope": "run_end",
        "payload": [
            {
                "path": write["path"],
                "blob": "b9",
                "content_sha256": write["content_sha256"],
                "tokens": [write["token"]],
            }
        ],
    }
    with harness.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(census, sort_keys=True) + "\n")
    _rederive(run)
    report = receipts.verify(run)
    assert (report["ok"], report["voids"]) == (True, [])
    assert report["undetermined"] == [
        {"system": "naive", "metric": "collision.collisions", "causes": ["unobserved_inclusion"]}
    ]
    summary = receipts.read_json(run / receipts.FREEZE)["summary"]["naive"]
    assert summary["collision.collisions"] is None
    compared = receipts.compare(run, run)
    assert compared["ok"] is False
    assert compared["counts"]["naive"]["collision.collisions"] == {
        "first": None,
        "second": None,
        "void": True,
    }
    assert compared["voids"] == [
        {
            "what": "count",
            "system": "naive",
            "metric": "collision.collisions",
            "first": None,
            "second": None,
            "detail": "undetermined in at least one run; nothing was compared",
        }
    ]


def test_a_v3_run_relabelled_v2_is_voided_not_read_as_v2(tmp_path: Path) -> None:
    run = _synthetic_run(tmp_path / "run", schema=SCHEMA)
    _relabel(run, SCHEMA_V2)
    _refreeze(run)
    voids = receipts.verify(run)["voids"]
    assert [void["what"] for void in voids] == ["seed_derivation"]
    assert "nothing was verified" in voids[0]["detail"]


@pytest.mark.parametrize(
    ("names", "value", "expected"),
    [
        (
            ("run.json", "FREEZE.json"),
            "onetree-bench-v9",
            {
                "what": "schema",
                "expected": [SCHEMA_V2, SCHEMA_V3, SCHEMA],
                "observed": "onetree-bench-v9",
                "detail": "no interpretation is known for this schema; nothing was verified",
            },
        ),
        (
            ("FREEZE.json",),
            SCHEMA,
            {
                "what": "schema",
                "recorded": SCHEMA,
                "inputs": SCHEMA_V2,
                "detail": "FREEZE.json and run.json name different schemas; nothing was verified",
            },
        ),
        (
            ("run.json",),
            SCHEMA,
            {
                "what": "schema",
                "recorded": SCHEMA_V2,
                "inputs": SCHEMA,
                "detail": "FREEZE.json and run.json name different schemas; nothing was verified",
            },
        ),
    ],
)
def test_an_unknown_or_disagreeing_schema_verifies_nothing(
    tmp_path: Path, names: tuple[str, ...], value: str, expected: dict[str, Any]
) -> None:
    run = _synthetic_run(tmp_path / "run")
    _edit_json(run / "metrics.json", lambda m: m["naive"]["lost_work"].update(lost=999))
    control = receipts.verify(run)
    assert [void["what"] for void in control["voids"]] == ["manifest_sha256", "file", "metrics"]
    _relabel(run, value, names)
    report = receipts.verify(run)
    assert report["voids"] == [expected]
    assert (report["ok"], report["scope"], report["recomputed"]) == (False, [], {})


def test_compare_refuses_runs_read_under_different_schemas(tmp_path: Path) -> None:
    old = _synthetic_run(tmp_path / "old")
    new = _synthetic_run(tmp_path / "new", schema=SCHEMA)
    control = receipts.compare(new, _synthetic_run(tmp_path / "again", schema=SCHEMA))
    assert (control["ok"], control["comparable"], control["voids"]) == (True, True, [])
    result = receipts.compare(old, new)
    assert result["verified"] == {"first": True, "second": True}
    assert (result["ok"], result["comparable"], result["counts"]) == (False, False, {})
    assert result["voids"] == [
        {
            "what": "schema",
            "first": SCHEMA_V2,
            "second": SCHEMA,
            "detail": "the runs are read under different schemas; no count is comparable",
        }
    ]


@pytest.mark.parametrize("staged_only", [True, False])
def test_bytes_left_only_in_an_index_keep_a_comparable_count(
    tmp_path: Path, staged_only: bool
) -> None:
    run = _synthetic_run(tmp_path / "run", seed=13, schema=SCHEMA)
    part = run / "events" / "parts" / "naive-w0.jsonl"
    write = json.loads(part.read_text(encoding="utf-8").splitlines()[0])
    assert write["kind"] == "write"
    staged = [write["token"]] if staged_only else []

    def uncommitted(row: dict[str, Any]) -> None:
        for entry in row.get("payload", ()):
            entry["tokens"] = []

    def left_in_index(row: dict[str, Any]) -> None:
        if row["kind"] == "observed":
            entry = row["paths"][write["path"]]
            entry.update(
                file=[token for token in entry["file"] if token != write["token"]], index=staged
            )

    _rewrite_events(part, uncommitted)
    _rewrite_events(run / "events" / "parts" / "naive-harness.jsonl", left_in_index)
    readback = {
        "system": "naive",
        "writer": 0,
        "kind": "observed",
        "t": 1.5,
        "task": 0,
        "attempt": 1,
        "checkout": "main",
        "index": "main",
        "after": "write",
        "paths": {write["path"]: {"file": [], "index": staged}},
    }
    with part.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(readback, sort_keys=True) + "\n")
    _rederive(run)
    report = receipts.verify(run)
    assert (report["ok"], report["voids"]) == (True, [])
    summary = receipts.read_json(run / receipts.FREEZE)["summary"]["naive"]
    compared = receipts.compare(run, run)
    assert report["undetermined"] == []
    assert summary["collision.collisions"] == (1 if staged_only else 0)
    assert (compared["ok"], compared["voids"]) == (True, [])


CASES = [
    ("notes.txt", True, True),
    ("nested/notes.txt", True, True),
    ("tmp", False, True),
    ("repos", False, True),
    ("tmp/evidence.txt", False, False),
    ("repos/evidence.txt", False, False),
    ("nested/tmp", True, True),
    ("nested/repos", True, True),
    ("nested/tmp/evidence.txt", True, True),
    ("nested/repos/evidence.txt", True, True),
    ("nested/FREEZE.json", False, True),
    ("nested/MANIFEST.sha256", False, True),
    ("deep/nested/FREEZE.json", False, True),
    ("deep/nested/MANIFEST.sha256", False, True),
    ("FREEZE.json.backup", True, True),
    ("MANIFEST.sha256.backup", True, True),
]


@pytest.mark.parametrize(("name", "v2_covered", "v3_covered"), CASES)
@pytest.mark.parametrize("operation", ["change", "delete", "add"])
@pytest.mark.parametrize("schema", SCHEMAS)
def test_manifest_coverage_follows_the_recorded_schema(
    tmp_path: Path, name: str, v2_covered: bool, v3_covered: bool, operation: str, schema: str
) -> None:
    covered = v3_covered if schema == SCHEMA else v2_covered
    run = _synthetic_run(tmp_path / "run", schema=schema)
    target = run / name
    target.parent.mkdir(parents=True, exist_ok=True)
    if operation != "add":
        target.write_bytes(b"original retained evidence\n")
    _refreeze(run)
    manifest = (run / receipts.MANIFEST).read_bytes()
    listed = receipts.parse_manifest(manifest.decode())
    before = receipts.verify(run)
    assert before["ok"], before
    assert (name in listed) is (covered and operation != "add")
    if operation == "delete":
        target.unlink()
    else:
        target.write_bytes(b"changed evidence after freeze\n")
    after = receipts.verify(run)
    assert (run / receipts.MANIFEST).read_bytes() == manifest
    assert after["ok"] is not covered, after
    if covered:
        assert any(void.get("path") == name and void["what"] == "file" for void in after["voids"])
    else:
        assert after["voids"] == []


def test_no_manifest_rule_is_guessed_for_an_unknown_schema(tmp_path: Path) -> None:
    run = _synthetic_run(tmp_path / "run", schema=SCHEMA)
    with pytest.raises(ValueError, match="no manifest rule"):
        receipts.manifest_entries(run, "onetree-bench-v9")
    assert "run.json" in [path for _, path in receipts.manifest_entries(run, SCHEMA)]


@pytest.mark.parametrize("schema", SCHEMAS)
def test_an_unlisted_ordinary_file_is_void_despite_consistent_receipts(
    tmp_path: Path, schema: str
) -> None:
    run = _synthetic_run(tmp_path / "run", schema=schema)
    (run / "notes.txt").write_bytes(b"retained evidence\n")
    _refreeze(run)
    assert receipts.verify(run)["ok"]
    entries = receipts.manifest_entries(run, schema)
    text = receipts.manifest_text([entry for entry in entries if entry[1] != "notes.txt"])
    (run / receipts.MANIFEST).write_text(text, encoding="utf-8")
    record = receipts.read_json(run / receipts.FREEZE)
    record.update(manifest_sha256=receipts.digest_text(text), file_count=text.count("\n"))
    receipts.write_json(run / receipts.FREEZE, record)
    after = receipts.verify(run)
    assert after["ok"] is False
    assert {"what": "file", "path": "notes.txt", "detail": "present but not in manifest"} in (
        after["voids"]
    )


@pytest.mark.parametrize("valid", [True, False])
@pytest.mark.parametrize("operation", ["verify", "compare"])
@pytest.mark.parametrize("scratch_shape", ["existing", "missing", "read_only", "file", "symlink"])
def test_v3_verification_preserves_inputs_and_covers_a_root_tmp_file(
    tmp_path: Path, valid: bool, operation: str, scratch_shape: str
) -> None:
    run = _synthetic_run(tmp_path / "run", schema=SCHEMA)
    scratch = run / "tmp"
    outside = tmp_path / "outside.txt"
    outside.write_text("outside scratch evidence\n", encoding="utf-8")
    if scratch_shape == "existing":
        scratch.mkdir()
        (scratch / "naive.recomputed.jsonl").write_text("retained\n", encoding="utf-8")
    elif scratch_shape == "file":
        scratch.write_text("retained scratch file\n", encoding="utf-8")
    elif scratch_shape == "symlink":
        scratch.mkdir()
        (scratch / "naive.recomputed.jsonl").symlink_to(outside)
    if not valid:
        _edit_json(run / "metrics.json", lambda m: m["naive"]["lost_work"].update(lost=999))

    def snapshot() -> dict[Path, tuple[int, int, bytes | None, str | None]]:
        return {
            path: (
                path.lstat().st_mode,
                path.lstat().st_mtime_ns,
                path.read_bytes() if path.is_file() else None,
                os.readlink(path) if path.is_symlink() else None,
            )
            for path in (run, *run.rglob("*"), outside)
        }

    mode = stat.S_IMODE(run.stat().st_mode)
    try:
        if scratch_shape == "read_only":
            run.chmod(0o555)
            if os.access(run, os.W_OK):
                pytest.skip("requires enforced directory write permissions")
        before = snapshot()
        result = receipts.verify(run) if operation == "verify" else receipts.compare(run, run)
        assert result["ok"] is (valid and scratch_shape != "file")
        assert snapshot() == before
    finally:
        if scratch_shape == "read_only":
            run.chmod(mode)
