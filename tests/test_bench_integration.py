"""The combined receipt must have its own identity and reproduce the accepted A1 corpus."""

import hashlib
import shutil
from pathlib import Path

import pytest
from test_bench import _edit_json, _refreeze, _synthetic_run

from onetree.bench import SCHEMA, corpus, receipts

HISTORY = Path(__file__).parent / "fixtures" / "bench-history"


def test_combined_format_has_a_distinct_identity() -> None:
    assert SCHEMA == "onetree-bench-v4"


def test_current_corpus_retains_the_accepted_numpy_reference() -> None:
    grid = corpus.Grid(writers=2, tasks=2, files=3, conflict=0.5, jitter_ms=10)
    actual = corpus.build(grid, 7)
    assert hashlib.sha256(receipts.canonical(actual.payload()).encode()).hexdigest() == (
        "f03fc77652fe3ecb70af33cdd9ba669b0c483d3ad8f48a78ea1b941f26a8b3c5"
    )


@pytest.mark.parametrize("name", ["sha-v2", "policy-v3", "a1-v3"])
def test_retained_predecessor_receipts_keep_their_recorded_results(name: str) -> None:
    run = HISTORY / name
    before = {path: path.read_bytes() for path in run.rglob("*") if path.is_file()}
    recorded = receipts.read_json(run / receipts.FREEZE)
    result = receipts.verify(run)
    assert result["ok"], result
    assert result["schema"] == recorded["schema"]
    assert receipts.summary_of(result["recomputed"], ["naive"]) == recorded["summary"]
    assert ("undetermined" in result) is (name == "policy-v3")
    assert before == {path: path.read_bytes() for path in run.rglob("*") if path.is_file()}


def test_same_version_different_derivation_is_not_a_reproduction() -> None:
    result = receipts.compare(HISTORY / "policy-v3", HISTORY / "a1-v3")
    assert result["verified"] == {"first": True, "second": True}
    assert result["ok"] is False
    assert result["comparable"] is False
    assert result["counts"] == {}
    assert result["voids"][0]["what"] == "seed_derivation"


@pytest.mark.parametrize("name", ["sha-v2", "policy-v3", "a1-v3"])
def test_relabelling_history_does_not_upgrade_it(tmp_path: Path, name: str) -> None:
    run = tmp_path / "run"
    shutil.copytree(HISTORY / name, run)
    for filename in ("run.json", receipts.FREEZE):
        _edit_json(run / filename, lambda record: record.__setitem__("schema", SCHEMA))
    _refreeze(run)
    result = receipts.verify(run)
    assert result["ok"] is False
    if name == "a1-v3":
        assert any(void["what"] == "events" for void in result["voids"])
    else:
        assert result["recomputed"] == {}
        assert result["voids"][0]["what"] == "seed_derivation"


@pytest.mark.parametrize("name", ["sha-v2", "policy-v3", "a1-v3"])
def test_historical_manifest_exclusions_are_not_reinterpreted(tmp_path: Path, name: str) -> None:
    run = tmp_path / "run"
    shutil.copytree(HISTORY / name, run)
    nested = run / "nested" / "FREEZE.json"
    nested.parent.mkdir()
    nested.write_text("unrecorded evidence\n", encoding="utf-8")
    report = receipts.verify(run)
    assert report["ok"] is (name != "policy-v3")
    if name == "policy-v3":
        assert any(void.get("path") == "nested/FREEZE.json" for void in report["voids"])


@pytest.mark.parametrize("derivation", [None, 1, {}, [], "unknown"])
def test_unknown_derivation_never_selects_a_fallback(tmp_path: Path, derivation: object) -> None:
    run = _synthetic_run(tmp_path / "run", schema=SCHEMA)
    for filename in ("run.json", receipts.FREEZE):
        _edit_json(run / filename, lambda record: record.__setitem__("seed_derivation", derivation))
    result = receipts.verify(run)
    assert result["ok"] is False
    assert result["scope"] == []
    assert result["recomputed"] == {}
    assert result["voids"][0]["what"] == "seed_derivation"


def test_receipt_derivation_must_agree_before_scoring(tmp_path: Path) -> None:
    run = _synthetic_run(tmp_path / "run", schema=SCHEMA)
    from onetree.bench.legacy_seeds import DERIVATION

    _edit_json(run / "run.json", lambda record: record.__setitem__("seed_derivation", DERIVATION))
    result = receipts.verify(run)
    assert result["ok"] is False
    assert result["recomputed"] == {}
    assert result["voids"][0]["what"] == "seed_derivation"


@pytest.mark.parametrize("field", ["writers", "tasks", "files", "jitter_ms"])
def test_new_grid_retains_the_accepted_single_word_domain(field: str) -> None:
    arguments = {"writers": 1, "tasks": 1, "files": 1, "conflict": 0.0}
    arguments[field] = 2**32
    with pytest.raises(ValueError, match="below 2"):
        corpus.Grid(**arguments)
