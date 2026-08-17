"""Append-only event commits stored below ``refs/onetree/wal``."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from onetree.errors import GitError, RefCorruptError, WalCorruptError
from onetree.git import Repository

WAL_REF = "refs/onetree/wal"
EVENT_KINDS = frozenset(
    {
        "session_register",
        "claim",
        "release",
        "land_intent",
        "landed",
        "refused",
        "gc",
    }
)
_DOCUMENT_KEYS = frozenset({"v", "event", "event_id", "actor", "time", "data"})


@dataclass(frozen=True)
class EventRecord:
    """One validated event document plus its immutable Git identity."""

    oid: str
    parents: tuple[str, ...]
    document: Mapping[str, Any]

    @property
    def kind(self) -> str:
        return str(self.document["event"])

    @property
    def actor(self) -> str:
        return str(self.document["actor"])

    @property
    def event_id(self) -> str:
        return str(self.document["event_id"])

    @property
    def data(self) -> Mapping[str, Any]:
        value = self.document["data"]
        if not isinstance(value, dict):  # already established by validation
            raise AssertionError("validated event data changed type")
        return value

    def payload(self) -> dict[str, Any]:
        return {
            "oid": self.oid,
            "parents": list(self.parents),
            **dict(self.document),
        }


@dataclass(frozen=True)
class WalSnapshot:
    """A complete verified first-to-last WAL view."""

    head: str | None
    events: tuple[EventRecord, ...]


class Wal:
    """Build and CAS-append immutable event commit chains."""

    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    @staticmethod
    def document(
        kind: str,
        actor: str,
        data: Mapping[str, Any],
        *,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        if kind not in EVENT_KINDS:
            raise WalCorruptError("unknown WAL event kind", event=kind)
        if not actor:
            raise WalCorruptError("WAL actor is empty", event=kind)
        return {
            "v": 1,
            "event": kind,
            "event_id": event_id or uuid.uuid4().hex,
            "actor": actor,
            "time": datetime.now(UTC).isoformat(timespec="microseconds"),
            "data": dict(data),
        }

    @staticmethod
    def _validate_document(value: Any, *, oid: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise WalCorruptError("WAL event document is not an object", oid=oid)
        if frozenset(value) != _DOCUMENT_KEYS:
            raise WalCorruptError("WAL event document has unexpected fields", oid=oid)
        if value.get("v") != 1:
            raise WalCorruptError("unsupported WAL schema version", oid=oid)
        kind = value.get("event")
        if not isinstance(kind, str) or kind not in EVENT_KINDS:
            raise WalCorruptError("unknown WAL event kind", oid=oid)
        actor = value.get("actor")
        if not isinstance(actor, str) or not actor:
            raise WalCorruptError("WAL event actor is invalid", oid=oid)
        event_id = value.get("event_id")
        if not isinstance(event_id, str) or re.fullmatch(r"[0-9a-f]{32}", event_id) is None:
            raise WalCorruptError("WAL event id is invalid", oid=oid)
        timestamp = value.get("time")
        if not isinstance(timestamp, str):
            raise WalCorruptError("WAL event time is invalid", oid=oid)
        try:
            parsed = datetime.fromisoformat(timestamp)
        except ValueError as error:
            raise WalCorruptError("WAL event time is invalid", oid=oid) from error
        if parsed.tzinfo is None:
            raise WalCorruptError("WAL event time has no timezone", oid=oid)
        if not isinstance(value.get("data"), dict):
            raise WalCorruptError("WAL event data is not an object", oid=oid)
        return value

    def _read_event(self, oid: str, parents: tuple[str, ...]) -> EventRecord:
        listing = self.repo.git("ls-tree", "-z", oid).stdout
        records = [record for record in listing.split(b"\0") if record]
        if len(records) != 1:
            raise WalCorruptError("WAL event tree must contain exactly event.json", oid=oid)
        metadata, separator, path = records[0].partition(b"\t")
        if not separator or metadata.split()[:2] != [b"100644", b"blob"] or path != b"event.json":
            raise WalCorruptError("WAL event tree has an invalid entry", oid=oid)
        blob_oid = metadata.split()[2]
        try:
            blob = self.repo.git("cat-file", "blob", blob_oid.decode("ascii")).stdout
            value = json.loads(blob.decode("utf-8", errors="strict"))
        except (GitError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise WalCorruptError("WAL event.json is not valid UTF-8 JSON", oid=oid) from error
        document = self._validate_document(value, oid=oid)
        return EventRecord(oid=oid, parents=parents, document=document)

    def head(self) -> str | None:
        """Resolve the WAL ref, reporting a non-commit ref as WAL corruption.

        A ref that exists but is not a commit is the one shape that must never
        read as "no WAL yet": that reading answers every claim question with a
        clean bill of health over the corruption.
        """

        try:
            return self.repo.resolve_ref(WAL_REF, missing_ok=True)
        except RefCorruptError as error:
            raise WalCorruptError(
                f"{WAL_REF} is corrupt: {error}",
                **error.details,
            ) from error

    def read(self) -> WalSnapshot:
        head = self.head()
        if head is None:
            return WalSnapshot(head=None, events=())
        # Traverse only the candidate chain. ``--parents`` still exposes every
        # parent on a merge commit, so the exact-parent check below refuses it
        # without first attempting to parse the unrelated side history as WAL.
        output = self.repo.git("rev-list", "--first-parent", "--reverse", "--parents", head).stdout
        events: list[EventRecord] = []
        previous: str | None = None
        for raw_line in output.splitlines():
            try:
                fields = raw_line.decode("ascii", errors="strict").split()
            except UnicodeDecodeError as error:
                raise WalCorruptError("WAL history is not ASCII") from error
            if not fields:
                raise WalCorruptError("WAL history contains an empty row")
            oid = self.repo.validate_oid(fields[0], label="WAL commit")
            parents = tuple(
                self.repo.validate_oid(parent, label="WAL parent") for parent in fields[1:]
            )
            expected = () if previous is None else (previous,)
            if parents != expected:
                raise WalCorruptError(
                    "each WAL commit must have exactly one parent except the root",
                    oid=oid,
                    parents=list(parents),
                    expected=list(expected),
                )
            events.append(self._read_event(oid, parents))
            previous = oid
        if previous != head:
            raise WalCorruptError("WAL traversal did not end at the ref head", head=head)
        return WalSnapshot(head=head, events=tuple(events))

    def build_chain(
        self,
        documents: Sequence[Mapping[str, Any]],
        parent: str | None,
    ) -> tuple[str, tuple[EventRecord, ...]]:
        if not documents:
            raise WalCorruptError("cannot build an empty WAL append")
        records: list[EventRecord] = []
        current = parent
        for raw_document in documents:
            document = self._validate_document(dict(raw_document), oid="uncommitted")
            encoded = (
                json.dumps(
                    document,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("ascii")
                + b"\n"
            )
            blob = self.repo.hash_blob(encoded)
            tree = self.repo.make_single_blob_tree("event.json", blob)
            parents = () if current is None else (current,)
            commit = self.repo.commit_tree(
                tree,
                f"onetree wal: {document['event']} {document['event_id']}",
                parents,
            )
            record = EventRecord(commit, parents, document)
            records.append(record)
            current = commit
        if current is None:  # guarded by the non-empty check
            raise AssertionError("WAL chain has no head")
        return current, tuple(records)

    def try_append(
        self,
        documents: Sequence[Mapping[str, Any]],
        expected_head: str | None,
    ) -> tuple[EventRecord, ...] | None:
        new_head, records = self.build_chain(documents, expected_head)
        try:
            won = self.repo.update_ref(WAL_REF, new_head, expected_head)
        except RefCorruptError as error:
            raise WalCorruptError(
                f"{WAL_REF} is corrupt: {error}",
                **error.details,
            ) from error
        if not won:
            return None
        return records

    def append(
        self,
        kind: str,
        actor: str,
        data: Mapping[str, Any],
        *,
        retries: int = 32,
    ) -> EventRecord:
        document = self.document(kind, actor, data)
        for _ in range(retries):
            snapshot = self.read()
            records = self.try_append((document,), snapshot.head)
            if records is not None:
                return records[0]
        raise GitError("WAL CAS retry limit exceeded", retries=retries)
