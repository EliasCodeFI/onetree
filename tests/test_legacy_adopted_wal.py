"""Task 70: persisted legacy ``adopted`` records (schema version 1) stay readable.

The 9d2ef3b writer recorded adoption at ``v: 1`` with one scalar ``operation``
that joined every ``Onetree-Operation`` trailer value of the adopted commit
with commas; a single trailer whose value itself contains a comma produced
the same scalar. The current reader refused every such record at the version
check, so a repository adopted under that writer could not be opened at all
(``/anvil/scratch/onetree-legacy-adoption-20260916/results.jsonl``, and the
fresh persisted reproduction in this round's evidence directory).

Contract under test. A ``v: 1`` ``adopted`` document is read as a historical
record and verified against the commit it names under the historical
writer's own semantics: the commit's trailer block, parsed and comma-joined
the way 9d2ef3b did, must equal the recorded scalar exactly, and the parent
and tree checks every adopted record gets still apply. The scalar is never
split. A verified legacy commit counts as recorded, so the branch audit does
not report it and adoption does not record it again. Everything else is
unchanged: new events are written at the current version only, a document
whose version disagrees with its kind is refused unless it is exactly the
recognised historical version of that kind, unsupported versions and
malformed data are refused as before, and a broker commit the WAL never
recorded is still reported and adoptable.

Fixtures write the legacy document with Git plumbing, byte for byte the
shape the historical writer persisted (six document keys, five data keys,
``v: 1``); the current writer cannot produce it, which the writer case
asserts. One case replays the actual 9d2ef3b writer when its extracted
source is present on this host and skips otherwise.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import GitFixture
from test_land import _register_and_stage

from onetree.core import Coordinator
from onetree.errors import GitError, WalCorruptError
from onetree.git import _legacy_landing_operation
from onetree.wal import WAL_REF, Wal

_HISTORICAL_WRITER = Path("/anvil/scratch/onetree-legacy-adoption-20260916/legacy/src")
_FIRST, _SECOND = "a" * 32, "b" * 32
#: Trailer values the historical fixtures used, keyed by shape, and the scalar
#: the 9d2ef3b writer persisted for each. Repeated and literal-comma store
#: the same string and mean different things; that is the whole difficulty.
_SHAPES = {
    "single": ([_FIRST], _FIRST),
    "repeated": ([_FIRST, _SECOND], f"{_FIRST},{_SECOND}"),
    "literal_comma": ([f"{_FIRST},{_SECOND}"], f"{_FIRST},{_SECOND}"),
}


def _legacy_commit(git_repo: GitFixture, values: list[str]) -> str:
    message = "legacy adoption\n\n" + "\n".join(f"Onetree-Operation: {value}" for value in values)
    return git_repo.external_commit("a.txt", "legacy bytes\n", message)


def _legacy_document(git_repo: GitFixture, commit: str, operation: str) -> dict[str, object]:
    """The document the 9d2ef3b writer persisted for an adopted commit."""
    return {
        "v": 1,
        "event": "adopted",
        "event_id": uuid.uuid4().hex,
        "actor": "operator",
        "time": datetime.now(UTC).isoformat(timespec="microseconds"),
        "data": {
            "commit": commit,
            "expected_head": git_repo.git("rev-parse", f"{commit}^").stdout.strip(),
            "operation": operation,
            "target": "refs/heads/main",
            "tree": git_repo.git("rev-parse", f"{commit}^{{tree}}").stdout.strip(),
        },
    }


def _persist(git_repo: GitFixture, document: dict[str, object]) -> str:
    """Append one raw WAL event commit with Git plumbing, outside the current writer."""
    encoded = json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    blob = git_repo.git("hash-object", "-w", "--stdin", input_text=encoded).stdout.strip()
    tree = git_repo.git("mktree", input_text=f"100644 blob {blob}\tevent.json\n").stdout.strip()
    head = git_repo.git("rev-parse", "--verify", "--quiet", WAL_REF, check=False).stdout.strip()
    arguments = ["commit-tree", tree, *(("-p", head) if head else ())]
    event = git_repo.git(*arguments, input_text="onetree wal: adopted legacy\n").stdout.strip()
    git_repo.git("update-ref", WAL_REF, event, *((head,) if head else ()))
    return event


def _wal_head(git_repo: GitFixture) -> str:
    return git_repo.git("rev-parse", "--verify", WAL_REF).stdout.strip()


def _adopt_legacy(git_repo: GitFixture, shape: str) -> tuple[str, str]:
    values, scalar = _SHAPES[shape]
    commit = _legacy_commit(git_repo, values)
    _persist(git_repo, _legacy_document(git_repo, commit, scalar))
    return commit, scalar


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_legacy_adopted_record_opens_and_verifies_against_its_commit(
    git_repo: GitFixture, shape: str,
) -> None:
    commit, scalar = _adopt_legacy(git_repo, shape)
    coordinator = Coordinator(git_repo.root)
    # The persisted history's identity, captured before anything reads it.
    wal_before = _wal_head(git_repo)

    report = coordinator.inspect_wal()
    assert report.consistent
    assert report.divergences == ()
    assert report.pending_intents == 0
    # Raw history is retained: the record is read as written, version and scalar intact.
    assert [event.kind for event in report.events] == ["adopted"]
    assert report.events[0].document["v"] == 1
    assert report.events[0].data["operation"] == scalar
    assert report.events[0].data["commit"] == commit
    recovery = coordinator.recover()
    assert recovery.consistent
    assert (recovery.pending, recovery.repaired, recovery.refused) == (0, 0, 0)
    assert git_repo.oid() == commit
    # Neither inspection nor recovery rewrote, upgraded or appended to the history.
    assert _wal_head(git_repo) == wal_before
    # Known-bad for that check: one more raw event on the same history moves the
    # head the check compares, so it can fail; the preserved identity is then
    # still the parent of what was appended, and nothing below it changed.
    appended = _persist(git_repo, _legacy_document(git_repo, commit, scalar))
    assert _wal_head(git_repo) != wal_before
    assert git_repo.git("rev-parse", f"{appended}^").stdout.strip() == wal_before


#: Records that name a real commit whose historical trailer scalar is not the
#: recorded one: the exact forgeries and mismatches the historical reader
#: would have refused.
_FORGED = [
    pytest.param([_FIRST, _SECOND], _FIRST, id="repeated-commit-recorded-as-one"),
    pytest.param([_FIRST], f"{_FIRST},{_SECOND}", id="single-commit-recorded-as-pair"),
    pytest.param([f"{_FIRST},{_SECOND}"], _FIRST, id="literal-comma-recorded-as-one"),
    pytest.param([_SECOND], _FIRST, id="other-operation"),
    pytest.param([], _FIRST, id="no-trailer-at-all"),
]


@pytest.mark.parametrize(("values", "recorded"), _FORGED)
def test_legacy_record_is_refused_when_the_commit_does_not_carry_it(
    git_repo: GitFixture, values: list[str], recorded: str,
) -> None:
    commit = _legacy_commit(git_repo, values) if values else git_repo.external_commit(
        "a.txt", "legacy bytes\n", "no trailer here\n",
    )
    _persist(git_repo, _legacy_document(git_repo, commit, recorded))
    coordinator = Coordinator(git_repo.root)
    with pytest.raises(WalCorruptError, match="legacy adopted commit does not carry"):
        coordinator.inspect_wal()
    with pytest.raises(WalCorruptError, match="legacy adopted commit does not carry"):
        coordinator.recover()
    with pytest.raises(WalCorruptError, match="legacy adopted commit does not carry"):
        coordinator.adopt_landings()


def test_legacy_record_keeps_the_parent_and_tree_checks(git_repo: GitFixture) -> None:
    commit, scalar = _adopt_legacy(git_repo, "repeated")
    # Replace the WAL with a record whose tree does not match the commit's.
    git_repo.git("update-ref", "-d", WAL_REF)
    document = _legacy_document(git_repo, commit, scalar)
    data = dict(document["data"])  # type: ignore[arg-type]
    data["tree"] = git_repo.git("rev-parse", f"{git_repo.base}^{{tree}}").stdout.strip()
    document["data"] = data
    _persist(git_repo, document)
    with pytest.raises(WalCorruptError, match="tree differs from WAL"):
        Coordinator(git_repo.root).inspect_wal()


def test_ordinary_work_continues_after_legacy_history(git_repo: GitFixture) -> None:
    legacy_commit, _ = _adopt_legacy(git_repo, "repeated")
    coordinator = Coordinator(git_repo.root)

    # Adoption finds nothing: the legacy commit is recorded, under its own scalar.
    adoption = coordinator.adopt_landings()
    assert adoption.adopted == ()
    assert adoption.report.consistent
    assert [event.kind for event in coordinator.inspect_wal().events] == ["adopted"]

    # New work lands at the current version on top of legacy history.
    _register_and_stage(git_repo, coordinator, "alpha", "a.txt", "after legacy\n")
    landed = coordinator.land("alpha", message="after legacy history")
    assert git_repo.oid() == landed.commit
    events = coordinator.inspect_wal().events
    assert [event.kind for event in events] == [
        "adopted", "session_register", "claim", "land_intent", "landed",
    ]
    assert all(event.document["v"] == 1 for event in events[1:] if event.kind != "adopted")
    assert coordinator.inspect_wal().consistent

    # A broker commit the WAL never recorded is still reported and adoptable:
    # legacy verification conceals nothing new.
    unrecorded = git_repo.external_commit(
        "a.txt", "unrecorded broker bytes\n",
        f"unrecorded landing\n\nOnetree-Operation: {uuid.uuid4().hex}",
    )
    report = coordinator.inspect_wal()
    assert not report.consistent
    assert [item.kind for item in report.divergences] == ["unrecorded_landing"]
    assert report.divergences[0].commit == unrecorded
    adoption = coordinator.adopt_landings()
    assert [event.data["commit"] for event in adoption.adopted] == [unrecorded]
    assert adoption.adopted[0].document["v"] == 2
    assert adoption.report.consistent
    assert legacy_commit not in {event.data["commit"] for event in adoption.adopted}


def test_current_writer_never_writes_the_legacy_version(git_repo: GitFixture) -> None:
    coordinator = Coordinator(git_repo.root)
    commit = _legacy_commit(git_repo, [_FIRST])
    current = Wal.document("adopted", "operator", {
        "commit": commit, "expected_head": git_repo.base, "operation": _FIRST,
        "target": "refs/heads/main",
        "tree": git_repo.git("rev-parse", f"{commit}^{{tree}}").stdout.strip(),
    })
    assert current["v"] == 2
    with pytest.raises(WalCorruptError, match="does not match its kind"):
        coordinator.wal.try_append([{**current, "v": 1}], coordinator.wal.head())
    assert coordinator.wal.head() is None


@pytest.mark.parametrize(("version", "message"), [
    (3, "does not match its kind"),
    (5, "unsupported WAL schema version"),
])
def test_other_adopted_versions_stay_refused(
    git_repo: GitFixture, version: int, message: str,
) -> None:
    commit = _legacy_commit(git_repo, [_FIRST])
    document = _legacy_document(git_repo, commit, _FIRST)
    document["v"] = version
    _persist(git_repo, document)
    with pytest.raises(WalCorruptError, match=message):
        Coordinator(git_repo.root).inspect_wal()


def test_malformed_legacy_data_stays_refused(git_repo: GitFixture) -> None:
    commit = _legacy_commit(git_repo, [_FIRST])
    document = _legacy_document(git_repo, commit, _FIRST)
    data = dict(document["data"])  # type: ignore[arg-type]
    del data["tree"]
    document["data"] = data
    _persist(git_repo, document)
    with pytest.raises(WalCorruptError, match="adopted event has unexpected fields"):
        Coordinator(git_repo.root).inspect_wal()


@pytest.mark.skipif(
    not (_HISTORICAL_WRITER / "onetree" / "cli.py").is_file(),
    reason="the extracted 9d2ef3b writer is not present on this host",
)
@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_actual_historical_writer_output_opens_with_the_current_reader(
    git_repo: GitFixture, shape: str,
) -> None:
    """Provenance: the record the real 9d2ef3b CLI persists, not a hand-built one."""
    values, scalar = _SHAPES[shape]
    commit = _legacy_commit(git_repo, values)
    environment = {**os.environ, "PYTHONPATH": str(_HISTORICAL_WRITER)}
    environment.pop("ONETREE_COMMIT_PAYLOAD", None)
    # The historical writer is the instrument here, not the product under
    # measurement: keep the subprocess coverage collector from reading its
    # source as ``onetree``, which would fold 9d2ef3b's lines into the gate.
    environment.pop("COVERAGE_PROCESS_CONFIG", None)
    environment.pop("COVERAGE_PROCESS_START", None)
    argv = [sys.executable, "-B", "-m", "onetree.cli", "wal", "--repo", str(git_repo.root)]
    adopted = subprocess.run(
        [*argv, "--adopt", "--json"], env=environment, capture_output=True, text=True,
        check=False,
    )
    assert adopted.returncode == 0, (adopted.stdout, adopted.stderr)
    document = json.loads(git_repo.git("show", f"{WAL_REF}:event.json").stdout)
    assert document["v"] == 1
    assert document["data"]["operation"] == scalar
    report = Coordinator(git_repo.root).inspect_wal()
    assert report.consistent
    assert report.events[0].data["commit"] == commit


#: The 9d2ef3b trailer-block subset, exercised directly on raw messages so the
#: port is shown to keep each rule it claims: all-trailer blocks, ``#`` comments,
#: folded continuation lines, Git's generated-prefix quorum, the ``---`` divider,
#: prose blocks, and the comma join itself.
_LEGACY_MESSAGES = [
    pytest.param(b"subject\n\nOnetree-Operation: " + _FIRST.encode(), _FIRST, id="single"),
    pytest.param(
        b"subject\n\nOnetree-Operation: " + _FIRST.encode() + b"\nOnetree-Operation: "
        + _SECOND.encode(), f"{_FIRST},{_SECOND}", id="repeated-joined",
    ),
    pytest.param(
        b"subject\n\nOnetree-Operation: " + f"{_FIRST},{_SECOND}".encode(),
        f"{_FIRST},{_SECOND}", id="literal-comma-kept",
    ),
    pytest.param(
        b"subject\n\n# comment\nOnetree-Operation: " + _FIRST.encode() + b"\r\n",
        _FIRST, id="comment-and-crlf",
    ),
    pytest.param(
        b"subject\n\nOnetree-Operation: " + _FIRST.encode() + b"\n  " + _SECOND.encode(),
        f"{_FIRST} {_SECOND}", id="folded-continuation",
    ),
    pytest.param(
        b"subject\n\nprose line\nSigned-off-by: x <x@example.invalid>\nOnetree-Operation: "
        + _FIRST.encode(), _FIRST, id="generated-prefix-quorum",
    ),
    pytest.param(
        b"subject\n\nOnetree-Operation: " + _FIRST.encode() + b"\n--- \nOnetree-Operation: "
        + _SECOND.encode(), _FIRST, id="divider-ends-message",
    ),
    pytest.param(b"subject\n\nprose only\nOnetree-Operation: " + _FIRST.encode(), None,
                 id="prose-block-is-not-trailers"),
    pytest.param(b"subject\n\nOther-Key: value", None, id="no-landing-key"),
    pytest.param(b"subject only", None, id="no-block"),
    pytest.param(b"", None, id="empty"),
    pytest.param(b"subject\n\nOnetree-Operation:   ", None, id="empty-value"),
]


@pytest.mark.parametrize(("raw_message", "expected"), _LEGACY_MESSAGES)
def test_legacy_parser_keeps_the_historical_writer_rules(
    raw_message: bytes, expected: str | None,
) -> None:
    assert _legacy_landing_operation(raw_message, commit="fixture") == expected


def test_legacy_parser_refuses_non_utf8_trailer_bytes() -> None:
    with pytest.raises(GitError, match="not UTF-8"):
        _legacy_landing_operation(b"subject\n\nOnetree-Operation: \xff", commit="fixture")
