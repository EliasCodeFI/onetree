"""The raw event log: one JSON object per line, the only input the metrics need.

Every metric in ``metrics.py`` is a pure function of a system's event list, so the committed
``events.jsonl`` is the raw data the protocol requires a stranger to recompute from. No metric
reads a repository; what a repository would have said (which paths and blobs a commit
changed, which tokens a blob held, which tokens the final tree held) is written into the
events at the moment it was observed, by the process that observed it.

Kinds and the fields the metrics read:

- ``setup``   writer null; ``duration_s``, ``disk_bytes`` -- the coordination layer's own cost.
- ``write``   ``write_id``, ``task``, ``path``, ``token``, ``content_sha256``, ``t``.
- ``landed``  ``task``, ``write_ids``, ``commit``, ``payload`` (list of ``path``, ``blob``,
  ``content_sha256``, ``tokens``), ``green`` (referee verdict on the landed tree), ``t``.
- ``refusal`` ``task``, ``write_ids``, ``reason`` (machine-readable), ``t`` -- an explicit,
  attributed receipt from the coordination layer.
- ``error``   ``task``, ``write_ids``, ``detail``, ``t`` -- a failure with NO coordination
  layer behind it (stock git said no); it is not a refusal receipt.
- ``referee`` writer null; ``commit``, ``result``, ``returncode``, ``duration_s`` -- the harness
  referee run after the writers' window over a commit the incumbent or floor system landed,
  which land without a referee of their own; ``t`` is taken when the verdict is complete.
  The onetree arm's referee is inside its landing and its verdict rides on the ``landed``
  event as ``green``.
- ``abandon`` ``task``, ``attempts`` -- the writer gave up after its attempt bound; no metric
  reads it, and it exists so a stranger can see which intents never landed.
- ``final``   writer null; ``tokens_by_path`` for the target branch's final tree, ``head``,
  ``disk_bytes``.
- ``run``     writer null; ``started``, ``ended`` -- wall-clock bounds of the writers' work;
  ``complete`` is false when a writer did not exit cleanly before the deadline.

The SHA-derived policy-v3 format and the combined SeedSequence v4 format add two kinds,
so commit inclusion and byte disappearance are recorded where they
happen instead of inferred from outcomes, and ``write`` names the ``checkout`` its bytes are
written in and the ``index`` they are staged in (the checkout's own, or the onetree session's):

- ``commit``   ``commit``, ``scope``, ``payload`` as ``landed`` carries it -- a Git commit the
  harness obtained. ``target``, ``local`` and ``candidate`` (a refused landing's candidate the
  refusal names) are taken when the operation that produced the commit returned. ``found`` is
  taken when a failed operation's listing of the object store completed, for a token-holding
  commit that appeared while the operation ran. ``run_end`` comes from the harness's run-end
  census of the object store, for a token-holding commit no writer event recorded, so its
  inclusion instant is unobserved; ``final`` then lists those commits as
  ``unrecorded_commits``.
- ``observed`` ``checkout`` and ``index`` (each null when not read), ``after`` (the operation
  just performed, or ``run_end``), ``paths`` -- the tokens read back per path: ``file`` from
  the working tree (null when the file is absent) and ``index`` (null when the path is not
  staged); ``unreadable`` marks a file whose bytes were not a well-formed corpus file.

Under policy-v3 and v4 these fields have exact shapes, never coerced to the nearest one.
Published v2 and the separate SeedSequence A1-v3 format retain the earlier kinds and rules;
the schema/derivation table in ``docs/BENCH-RECEIPT-VERSIONS.md`` distinguishes them.

- ``commit``: ``commit`` a non-empty string; ``scope`` one of ``COMMIT_SCOPES``, ``run_end``
  only from the harness (writer null) and every other scope only from a writer; ``t`` a finite
  number; ``payload`` a list of entries whose ``path`` is a non-empty string and whose ``blob``
  and ``content_sha256`` are both strings with ``tokens`` a list of strings, or both null with
  no tokens (a path the commit deleted). A ``found`` or ``run_end`` commit holds a token, and
  no writer's ``commit`` event records a ``run_end`` commit.
- ``observed``: ``t`` a finite number; ``after`` a non-empty string that is ``run_end`` exactly
  when the harness emits it; ``checkout`` and ``index`` each null or a non-empty string;
  ``paths`` an object whose entries carry ``file`` exactly when ``checkout`` names a checkout
  and ``index`` exactly when ``index`` names an index, each null or a list of strings, and
  ``unreadable`` only as ``true`` beside a ``file`` list.
- ``write``: ``checkout`` and ``index`` non-empty strings; ``t`` a finite number.
- ``landed`` and ``referee``: ``commit`` a non-empty string.

Writer events also carry ``attempt``: every attempt at a task writes its own token, so a
retry can never stand in for an earlier attempt's bytes.

Times are ``time.monotonic()`` readings, system-wide on Linux so comparable across the
writer processes; ``clock`` names the source so a stranger knows what was measured.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

CLOCK = "time.monotonic (CLOCK_MONOTONIC, system-wide)"
KINDS_V2 = frozenset(
    {"setup", "write", "landed", "refusal", "error", "referee", "abandon", "final", "run"}
)
KINDS = KINDS_V2 | {"commit", "observed"}
COMMIT_SCOPES = ("target", "local", "candidate", "found", "run_end")


def now() -> float:
    return time.monotonic()


class EventWriter:
    """Append-only JSONL for one process; lines are written whole and flushed."""

    def __init__(self, path: Path, system: str, writer: int | None) -> None:
        self.path = path
        self.system = system
        self.writer = writer
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, kind: str, **fields: Any) -> dict[str, Any]:
        if kind not in KINDS:
            raise ValueError(f"unknown event kind {kind!r}")
        event: dict[str, Any] = {"system": self.system, "writer": self.writer, "kind": kind}
        event.setdefault("t", fields.pop("t", now()))
        event.update(fields)
        line = json.dumps(event, sort_keys=True, ensure_ascii=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
        return event


def read(path: Path, kinds: frozenset[str] = KINDS) -> list[dict[str, Any]]:
    """Parse one JSONL file; a malformed line or a kind outside ``kinds`` is an error."""

    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                raise ValueError(f"{path}:{number}: blank line in event log")
            loaded = json.loads(line)
            if not isinstance(loaded, dict) or loaded.get("kind") not in kinds:
                raise ValueError(f"{path}:{number}: not an event object")
            events.append(loaded)
    return events


def read_parts(parts: Iterable[Path], kinds: frozenset[str] = KINDS) -> list[dict[str, Any]]:
    """Read per-process events in time and input order without writing files."""

    combined: list[tuple[float, int, dict[str, Any]]] = []
    order = 0
    for part in parts:
        for event in read(part, kinds):
            combined.append((float(event["t"]), order, event))
            order += 1
    combined.sort(key=lambda item: (item[0], item[1]))
    return [event for _, _, event in combined]


def merge(parts: Iterable[Path], destination: Path) -> list[dict[str, Any]]:
    """Combine per-process logs into one, ordered by time then by input order."""

    merged = read_parts(parts)
    with destination.open("w", encoding="utf-8") as handle:
        for event in merged:
            handle.write(json.dumps(event, sort_keys=True, ensure_ascii=True) + "\n")
    return merged


def of_kind(events: Iterable[dict[str, Any]], kind: str) -> Iterator[dict[str, Any]]:
    return (event for event in events if event.get("kind") == kind)


__all__ = [
    "CLOCK",
    "COMMIT_SCOPES",
    "KINDS",
    "KINDS_V2",
    "EventWriter",
    "merge",
    "now",
    "of_kind",
    "read",
    "read_parts",
]
