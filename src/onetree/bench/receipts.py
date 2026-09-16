"""Recomputable receipts in the ``FREEZE.json`` + ``MANIFEST.sha256`` format the protocol names.

A run directory holds raw data (per-process event logs, the corpus, the run inputs in
``run.json``, the harness referee script), derived data (merged event logs, ``metrics.json``)
and the two receipts. ``MANIFEST.sha256`` lists every file except the receipts themselves and
the working repositories; ``FREEZE.json`` carries the manifest digest, the declared scope and
inputs, the completeness flag and the metric summary.

``verify`` treats no field of ``FREEZE.json`` as authoritative. Every claim that committed raw
data can check is recomputed and reconciled, and each inconsistency is its own void: the
manifest against the files, the declared systems against ``run.json`` and against the evidence
present, the seed, grid, labels and digests against ``run.json`` and the files they name, the
corpus against the seed, the merged logs against their parts, every write against the seeded
corpus, the metrics and the summary against the events, and the completeness flag against the
run events. Claims no committed input can check are listed as unchecked, never passed as
verified.

``compare`` issues a reproduction comparison only when both runs verify; otherwise it names the
unverified input and compares nothing. For two verified runs of the same seed, grid and systems
it reports each count that differs as a void: outcomes may legitimately differ between runs
because processes race, and the protocol's answer is to report the void, not to re-run until it
disappears. Timing numbers are never compared because they cannot reproduce bit-for-bit;
``compare`` says so in its output rather than omitting them silently.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeGuard

from onetree.bench import SCHEMA, SYSTEMS
from onetree.bench import corpus as corpus_module
from onetree.bench import events as events_module
from onetree.bench import metrics as metrics_module
from onetree.bench.corpus import Corpus
from onetree.bench.seeds import DERIVATION, SeedKey
from onetree.bench.systems import REFEREE_SOURCE

FREEZE = "FREEZE.json"
MANIFEST = "MANIFEST.sha256"
RUN_INPUTS = "run.json"
CORPUS = "corpus.json"
METRICS = "metrics.json"
REFEREE_SCRIPT = "harness/referee_check.py"
EXCLUDED_DIRS = ("repos", "tmp")
NOT_A_RESULT = "hermetic validation leg; grid values are placeholders until task 25"
GRID_FIELDS = ("writers", "tasks", "files", "conflict", "paths_per_task", "jitter_ms", "amended")
COUNTED = (
    ("lost_work", "lost"),
    ("collision", "collisions"),
    ("refusal", "refusals"),
    ("refusal", "errors"),
    ("integrity", "commits"),
    ("integrity", "failed"),
    ("throughput", "landed"),
    ("throughput", "green"),
)
TIMED = (
    ("throughput", "green_per_hour"),
    ("throughput", "window_seconds"),
    ("throughput", "writers_window_seconds"),
    ("throughput", "referee_seconds"),
    ("overhead", "setup_seconds"),
    ("overhead", "peak_disk_bytes"),
)
UNCHECKED = (
    "FREEZE.json frozen_utc",
    "run.json gate.path and the gate file's bytes, which live outside the run",
    "run.json message_sha256, whose message is not committed",
    "run.json harness settings, python, platform and git notes",
)
_PART = re.compile(r"^(?P<system>[a-z]+)-(?:harness|w(?P<writer>[0-9]+))\.jsonl$")


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True) + "\n", encoding="utf-8"
    )


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def manifest_entries(run_dir: Path) -> list[tuple[str, str]]:
    """(sha256, relative path) for every receipt-covered file, sorted by path."""

    entries: list[tuple[str, str]] = []
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(run_dir)
        if relative.parts[0] in EXCLUDED_DIRS or relative.name in {FREEZE, MANIFEST}:
            continue
        entries.append((digest_file(path), relative.as_posix()))
    return entries


def manifest_text(entries: list[tuple[str, str]]) -> str:
    return "".join(f"{digest}  {name}\n" for digest, name in entries)


def parse_manifest(text: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), 1):
        digest, separator, name = line.partition("  ")
        if separator != "  " or len(digest) != 64 or not name:
            raise ValueError(f"{MANIFEST}:{number}: malformed line")
        parsed[name] = digest
    return parsed


def summary_of(metrics: Mapping[str, Any], systems: Sequence[str]) -> dict[str, dict[str, Any]]:
    return {
        system: {f"{metric}.{field}": metrics[system][metric][field] for metric, field in COUNTED}
        for system in systems
    }


def freeze_fields(
    inputs: Mapping[str, Any], metrics: Mapping[str, Any], complete: bool
) -> dict[str, Any]:
    """The claims ``FREEZE.json`` makes beyond its manifest; ``verify`` recomputes each one."""

    systems = list(inputs["systems"])
    return {
        "seed": inputs["seed"],
        "grid": inputs["grid"],
        "systems": systems,
        "seed_derivation": inputs["seed_derivation"],
        "complete": complete,
        "gate_sha256": inputs["gate"]["sha256"],
        "referee_sha256": inputs["referee_sha256"],
        "summary": summary_of(metrics, systems),
    }


def freeze(run_dir: Path, fields: dict[str, Any]) -> dict[str, Any]:
    """Write ``MANIFEST.sha256`` then ``FREEZE.json``; the freeze names the manifest's digest."""

    text = manifest_text(manifest_entries(run_dir))
    (run_dir / MANIFEST).write_text(text, encoding="utf-8")
    record = {
        "schema": SCHEMA,
        "frozen_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "file_count": text.count("\n"),
        "manifest_sha256": digest_text(text),
        "excluded_dirs": list(EXCLUDED_DIRS),
        **fields,
    }
    write_json(run_dir / FREEZE, record)
    return record


def _load_object(run_dir: Path, name: str, voids: list[dict[str, Any]]) -> dict[str, Any] | None:
    try:
        loaded = read_json(run_dir / name)
    except (OSError, ValueError) as error:
        voids.append({"what": "unreadable", "path": name, "detail": f"{type(error).__name__}"})
        return None
    if not isinstance(loaded, dict):
        voids.append({"what": "unreadable", "path": name, "detail": "not a JSON object"})
        return None
    return loaded


def _check_manifest(run_dir: Path, record: Mapping[str, Any], voids: list[dict[str, Any]]) -> None:
    present = manifest_entries(run_dir)
    if digest_text(manifest_text(present)) != record.get("manifest_sha256"):
        voids.append(
            {"what": "manifest_sha256", "detail": "manifest does not match the files present"}
        )
    try:
        text = (run_dir / MANIFEST).read_text(encoding="utf-8")
    except OSError as error:
        voids.append({"what": "unreadable", "path": MANIFEST, "detail": type(error).__name__})
        return
    if digest_text(text) != record.get("manifest_sha256"):
        voids.append(
            {"what": "manifest_file", "detail": f"{MANIFEST} is not the manifest {FREEZE} names"}
        )
    try:
        listed = parse_manifest(text)
    except ValueError as error:
        voids.append({"what": "manifest_file", "detail": str(error)})
        listed = {}
    if canonical(record.get("file_count")) != canonical(text.count("\n")):
        voids.append(
            {
                "what": "file_count",
                "recorded": record.get("file_count"),
                "recomputed": text.count("\n"),
            }
        )
    if canonical(record.get("excluded_dirs")) != canonical(list(EXCLUDED_DIRS)):
        voids.append(
            {
                "what": "excluded_dirs",
                "recorded": record.get("excluded_dirs"),
                "expected": list(EXCLUDED_DIRS),
            }
        )
    for name, digest in sorted(listed.items()):
        path = run_dir / name
        if not path.is_file():
            voids.append({"what": "file", "path": name, "detail": "listed but absent"})
        elif digest_file(path) != digest:
            voids.append({"what": "file", "path": name, "detail": "content differs from manifest"})
    for name in sorted({name for _, name in present} - set(listed)):
        voids.append({"what": "file", "path": name, "detail": "present but not in manifest"})


def _check_scope(
    run_dir: Path,
    record: Mapping[str, Any],
    inputs: Mapping[str, Any],
    metrics: Mapping[str, Any],
    voids: list[dict[str, Any]],
) -> list[str]:
    """The declared systems, validated; evidence for any other system is a void."""

    declared = record.get("systems")
    if not isinstance(declared, list) or not declared:
        voids.append({"what": "scope", "detail": "systems must be a non-empty list"})
        declared = []
    unknown = sorted({str(system) for system in declared} - set(SYSTEMS))
    if unknown:
        voids.append({"what": "scope", "detail": f"unsupported systems: {', '.join(unknown)}"})
    if len(set(map(str, declared))) != len(declared):
        voids.append({"what": "scope", "detail": "systems repeat"})
    if canonical(inputs.get("systems")) != canonical(record.get("systems")):
        voids.append(
            {
                "what": "inputs",
                "field": "systems",
                "detail": f"{FREEZE} and {RUN_INPUTS} disagree",
            }
        )
    scope = [system for system in SYSTEMS if system in declared]
    parts = run_dir / "events" / "parts"
    part_names = sorted(path.name for path in parts.glob("*.jsonl")) if parts.is_dir() else []
    evidence: set[str] = set(metrics)
    for name in part_names:
        match = _PART.match(name)
        evidence.add(match.group("system") if match else name)
    events_dir = run_dir / "events"
    if events_dir.is_dir():
        evidence.update(path.stem for path in events_dir.glob("*.jsonl"))
    for system in sorted(evidence - set(scope)):
        voids.append(
            {
                "what": "scope",
                "system": system,
                "detail": "evidence present for a system the run does not declare",
            }
        )
    return scope


def _check_inputs(
    run_dir: Path, record: Mapping[str, Any], inputs: Mapping[str, Any], voids: list[dict[str, Any]]
) -> None:
    for field in ("schema", "seed", "grid", "seed_derivation"):
        if canonical(record.get(field)) != canonical(inputs.get(field)):
            voids.append(
                {"what": "inputs", "field": field, "detail": f"{FREEZE} and {RUN_INPUTS} disagree"}
            )
    if record.get("seed_derivation") != DERIVATION:
        voids.append(
            {"what": "inputs", "field": "seed_derivation", "detail": "not this schema's derivation"}
        )
    if inputs.get("clock") != events_module.CLOCK:
        voids.append({"what": "inputs", "field": "clock", "detail": "not this schema's clock"})
    grid: Any = record.get("grid")
    try:
        rebuilt = corpus_module.Grid(**{key: grid[key] for key in GRID_FIELDS})
    except (KeyError, TypeError, ValueError) as error:
        voids.append({"what": "grid", "detail": f"grid fields are invalid: {error}"})
    else:
        if canonical(rebuilt.payload()) != canonical(grid):
            voids.append({"what": "grid", "detail": "grid record is not what its fields produce"})
        if not rebuilt.amended and inputs.get("not_a_result") != NOT_A_RESULT:
            voids.append(
                {"what": "label", "detail": "an unamended grid must carry the not-a-result label"}
            )
    gate = inputs.get("gate")
    if not isinstance(gate, dict):
        voids.append({"what": "gate", "detail": f"{RUN_INPUTS} gate record is not an object"})
    else:
        if record.get("gate_sha256") != gate.get("sha256"):
            voids.append(
                {
                    "what": "inputs",
                    "field": "gate_sha256",
                    "detail": f"{FREEZE} and {RUN_INPUTS} disagree",
                }
            )
        matches = gate.get("sha256") is not None and gate.get("sha256") == gate.get(
            "admitted_pin_sha256"
        )
        if gate.get("matches_admitted_pin") is not matches:
            voids.append(
                {"what": "gate", "detail": "matches_admitted_pin disagrees with the two digests"}
            )
    if record.get("referee_sha256") != inputs.get("referee_sha256"):
        voids.append(
            {
                "what": "inputs",
                "field": "referee_sha256",
                "detail": f"{FREEZE} and {RUN_INPUTS} disagree",
            }
        )
    script = run_dir / REFEREE_SCRIPT
    if not script.is_file():
        voids.append({"what": "referee", "detail": f"{REFEREE_SCRIPT} is absent"})
        return
    if digest_file(script) != inputs.get("referee_sha256"):
        voids.append({"what": "referee", "detail": f"{REFEREE_SCRIPT} is not the recorded digest"})
    if script.read_text(encoding="utf-8") != REFEREE_SOURCE:
        voids.append(
            {"what": "referee", "detail": f"{REFEREE_SCRIPT} is not the referee this schema ships"}
        )


def _check_corpus(
    run_dir: Path, record: Mapping[str, Any], voids: list[dict[str, Any]]
) -> Corpus | None:
    grid: Any = record.get("grid")
    try:
        rebuilt = corpus_module.Grid(**{key: grid[key] for key in GRID_FIELDS})
        corpus = corpus_module.build(rebuilt, SeedKey(record["seed"]))
    except (KeyError, TypeError, ValueError) as error:
        voids.append({"what": "corpus", "detail": f"cannot regenerate: {error}"})
        return None
    recorded = _load_object(run_dir, CORPUS, voids)
    if recorded is not None and canonical(corpus.payload()) != canonical(recorded):
        voids.append({"what": "corpus", "detail": "corpus.json differs from the seed's corpus"})
    return corpus


def _merge_parts(run_dir: Path, system: str) -> list[dict[str, Any]] | None:
    parts = sorted((run_dir / "events" / "parts").glob(f"{system}-*.jsonl"))
    if not parts:
        return None
    scratch = run_dir / "tmp"
    scratch.mkdir(exist_ok=True)
    return events_module.merge(parts, scratch / f"{system}.recomputed.jsonl")


def _parts_disagreeing_with_names(run_dir: Path, system: str) -> list[str]:
    wrong: list[str] = []
    for part in sorted((run_dir / "events" / "parts").glob(f"{system}-*.jsonl")):
        match = _PART.match(part.name)
        writer = None if match is None or match.group("writer") is None else int(match["writer"])
        if match is None or any(
            event.get("system") != system or event.get("writer") != writer
            for event in events_module.read(part)
        ):
            wrong.append(part.name)
    return wrong


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _writes_off_corpus(events: Sequence[dict[str, Any]], corpus: Corpus) -> list[str]:
    """write_ids whose writer, task, path or token the seeded corpus does not produce."""

    wrong: list[str] = []
    for event in events_module.of_kind(events, "write"):
        writer, index = event.get("writer"), event.get("task")
        attempt, path = event.get("attempt"), event.get("path")
        tasks = corpus.tasks.get(writer) if _is_int(writer) else None
        valid = (
            tasks is not None
            and _is_int(index)
            and 0 <= index < len(tasks)
            and _is_int(attempt)
            and attempt >= 1
            and path in tasks[index].paths
            and event.get("token") == tasks[index].token(str(path), attempt)
        )
        if not valid:
            wrong.append(str(event.get("write_id")))
    return wrong


def _check_events(
    run_dir: Path,
    scope: Sequence[str],
    corpus: Corpus | None,
    metrics: Mapping[str, Any],
    voids: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, bool | None]]:
    recomputed: dict[str, Any] = {}
    completes: dict[str, bool | None] = {}
    for system in scope:
        merged_path = run_dir / "events" / f"{system}.jsonl"
        if not merged_path.is_file():
            voids.append({"what": "events", "system": system, "detail": "merged log absent"})
            continue
        try:
            merged = events_module.read(merged_path)
            remerged = _merge_parts(run_dir, system)
            disagreeing = _parts_disagreeing_with_names(run_dir, system)
        except (OSError, ValueError) as error:
            voids.append({"what": "events", "system": system, "detail": f"unreadable: {error}"})
            continue
        if remerged is None or canonical(remerged) != canonical(merged):
            voids.append(
                {"what": "events", "system": system, "detail": "merged log differs from parts"}
            )
        if disagreeing:
            voids.append(
                {
                    "what": "events",
                    "system": system,
                    "detail": "events disagree with the system or writer their log names",
                    "parts": disagreeing,
                }
            )
        if corpus is not None:
            off_corpus = _writes_off_corpus(merged, corpus)
            if off_corpus:
                voids.append(
                    {
                        "what": "events",
                        "system": system,
                        "detail": "writes the seeded corpus does not produce",
                        "write_ids": off_corpus[:10],
                        "count": len(off_corpus),
                    }
                )
        runs = list(events_module.of_kind(merged, "run"))
        if len(runs) == 1 and isinstance(runs[0].get("complete"), bool):
            completes[system] = runs[0]["complete"]
        else:
            completes[system] = None
            voids.append(
                {"what": "events", "system": system, "detail": "expected exactly one run event"}
            )
        try:
            recomputed[system] = metrics_module.compute(merged)
        except (KeyError, TypeError, ValueError) as error:
            voids.append(
                {"what": "metrics", "system": system, "detail": f"cannot compute: {error}"}
            )
            continue
        if canonical(recomputed[system]) != canonical(metrics.get(system)):
            voids.append(
                {
                    "what": "metrics",
                    "system": system,
                    "detail": "metrics.json differs from metrics recomputed from events",
                }
            )
    return recomputed, completes


def _check_claims(
    record: Mapping[str, Any],
    scope: Sequence[str],
    recomputed: Mapping[str, Any],
    completes: Mapping[str, bool | None],
    voids: list[dict[str, Any]],
) -> None:
    summary = record.get("summary")
    if not isinstance(summary, dict):
        voids.append({"what": "summary", "detail": "summary is not an object"})
        summary = {}
    for system in sorted(set(summary) - set(scope)):
        voids.append(
            {"what": "summary", "system": system, "detail": "summary for an undeclared system"}
        )
    expected = summary_of(recomputed, [system for system in scope if system in recomputed])
    for system, values in expected.items():
        recorded = summary.get(system)
        if not isinstance(recorded, dict):
            voids.append({"what": "summary", "system": system, "detail": "summary absent"})
            continue
        for key in sorted(set(values) | set(recorded)):
            if (
                key not in values
                or key not in recorded
                or (canonical(recorded[key]) != canonical(values[key]))
            ):
                voids.append(
                    {
                        "what": "summary",
                        "system": system,
                        "metric": key,
                        "recorded": recorded.get(key),
                        "recomputed": values.get(key),
                    }
                )
    if scope and all(completes.get(system) is not None for system in scope):
        complete = all(completes[system] for system in scope)
        if record.get("complete") is not complete:
            voids.append(
                {"what": "complete", "recorded": record.get("complete"), "recomputed": complete}
            )


def verify(run_dir: Path) -> dict[str, Any]:
    """Recompute every checkable claim of a run from its raw data; each mismatch is a void."""

    voids: list[dict[str, Any]] = []
    record = _load_object(run_dir, FREEZE, voids)
    inputs = _load_object(run_dir, RUN_INPUTS, voids)
    recomputed: dict[str, Any] = {}
    scope: list[str] = []
    if record is not None and inputs is not None:
        if record.get("schema") != SCHEMA:
            voids.append({"what": "schema", "expected": SCHEMA, "observed": record.get("schema")})
        _check_manifest(run_dir, record, voids)
        metrics = _load_object(run_dir, METRICS, voids) or {}
        scope = _check_scope(run_dir, record, inputs, metrics, voids)
        _check_inputs(run_dir, record, inputs, voids)
        corpus = _check_corpus(run_dir, record, voids)
        recomputed, completes = _check_events(run_dir, scope, corpus, metrics, voids)
        _check_claims(record, scope, recomputed, completes, voids)
    return {
        "run": str(run_dir),
        "schema": None if record is None else record.get("schema"),
        "ok": not voids,
        "scope": scope,
        "voids": voids,
        "unchecked": list(UNCHECKED),
        "recomputed": recomputed,
    }


def compare(first: Path, second: Path) -> dict[str, Any]:
    """Two verified runs of one seed, grid and scope: differing counts are voids."""

    checks = {"first": verify(first), "second": verify(second)}
    report: dict[str, Any] = {
        "first": str(first),
        "second": str(second),
        "verified": {label: bool(check["ok"]) for label, check in checks.items()},
        "not_compared": [f"{metric}.{field}" for metric, field in TIMED],
    }
    unverified = [
        {
            "what": "unverified",
            "run": label,
            "void_count": len(check["voids"]),
            "detail": "verify reports voids for this run; nothing was compared",
        }
        for label, check in checks.items()
        if not check["ok"]
    ]
    if unverified:
        return {**report, "comparable": None, "counts": {}, "ok": False, "voids": unverified}
    left, right = read_json(first / FREEZE), read_json(second / FREEZE)
    voids: list[dict[str, Any]] = []
    differing = [
        field
        for field in ("seed", "grid", "systems")
        if canonical(left.get(field)) != canonical(right.get(field))
    ]
    if differing:
        voids.append(
            {
                "what": "inputs",
                "fields": differing,
                "detail": "seed, grid or systems differ; runs are not reproductions",
            }
        )
    left_metrics, right_metrics = checks["first"]["recomputed"], checks["second"]["recomputed"]
    counts: dict[str, dict[str, Any]] = {}
    for system in [name for name in SYSTEMS if name in left_metrics or name in right_metrics]:
        if system not in left_metrics or system not in right_metrics:
            voids.append({"what": "system", "system": system, "detail": "absent from one run"})
            continue
        counts[system] = {}
        for metric, field in COUNTED:
            a, b = left_metrics[system][metric][field], right_metrics[system][metric][field]
            counts[system][f"{metric}.{field}"] = {"first": a, "second": b, "void": a != b}
            if a != b:
                voids.append(
                    {
                        "what": "count",
                        "system": system,
                        "metric": f"{metric}.{field}",
                        "first": a,
                        "second": b,
                    }
                )
    return {
        **report,
        "comparable": not differing,
        "counts": counts,
        "ok": not voids,
        "voids": voids,
    }


__all__ = [
    "COUNTED",
    "EXCLUDED_DIRS",
    "FREEZE",
    "MANIFEST",
    "NOT_A_RESULT",
    "RUN_INPUTS",
    "TIMED",
    "UNCHECKED",
    "canonical",
    "compare",
    "digest_file",
    "digest_text",
    "freeze",
    "freeze_fields",
    "manifest_entries",
    "manifest_text",
    "parse_manifest",
    "read_json",
    "summary_of",
    "verify",
    "write_json",
]
