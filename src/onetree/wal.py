"""Append-only event commits stored below ``refs/onetree/wal``."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from onetree.errors import GitError, RefCorruptError, WalCorruptError
from onetree.git import Repository

WAL_REF = "refs/onetree/wal"
#: Repository setting that lets readers keep a copy of the verified log (``onetree init``
#: sets it). Without it every read verifies the whole chain, as before.
CACHE_CONFIG_KEY = "onetree.walCache"
_CACHE_HEADER = b'{"onetree-wal-cache":1}\n'

# ``v`` is the lowest reader version that understands the document's event
# kind. It is not a version of the document shape: the six keys below have
# never changed, and a reader that does not know a kind cannot act on the log
# whatever the shape says. Versioning per kind rather than per log keeps an
# older binary working against a WAL that has not used a newer kind yet, and
# makes its refusal accurate when one appears -- an unsupported version, not a
# corrupt log. A kind's version is frozen once released; adding a kind adds a
# row here and, if the kind is new, raises WAL_READER_VERSION.
_KIND_VERSIONS = {
    "session_register": 1,
    "claim": 1,
    "release": 1,
    "land_intent": 1,
    "landed": 1,
    "adopted": 2,
    "refused": 1,
    "gc": 1,
    "workspace_protect": 5,
    "workspace_reconcile": 5,
}
WAL_READER_VERSION = max(_KIND_VERSIONS.values())
EVENT_KINDS = frozenset(_KIND_VERSIONS)
# Historical versions a reader still accepts for a kind, on read only. The
# 9d2ef3b writer released ``adopted`` at v=1 with a scalar ``operation`` that
# comma-joined every landing trailer value of the adopted commit (task 70).
# Such a record is verified under that writer's semantics by the coordinator
# and is never written again: the current writer refuses it.
# ``workspace_protect`` v3 and ``workspace_reconcile`` v4 baselines predate payload gate
# admission and carry no ``gate`` field; the coordinator reads them as "no gate admitted".
_LEGACY_KIND_VERSIONS: dict[str, frozenset[int]] = {
    "adopted": frozenset({1}),
    "workspace_protect": frozenset({3}),
    "workspace_reconcile": frozenset({4}),
}
_DOCUMENT_KEYS = frozenset({"v", "event", "event_id", "actor", "time", "data"})
# The raw body of the tree ``build_chain`` writes, ahead of the blob's binary object ID.
_CANONICAL_TREE_PREFIX = b"100644 event.json\0"


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
    def version(self) -> int:
        return int(self.document["v"])

    @property
    def legacy(self) -> bool:
        """Recorded at a historical version of its kind, verified under that version's semantics."""
        return self.version != _KIND_VERSIONS[self.kind]

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
        self._cache_enabled: bool | None = None

    def cache_enabled(self) -> bool:
        """Whether the repository lets readers keep a copy of the verified log."""

        if self._cache_enabled is None:
            result = self.repo.git("config", "--bool", "--get", CACHE_CONFIG_KEY, check=False)
            self._cache_enabled = result.returncode == 0 and result.stdout.strip() == b"true"
        return self._cache_enabled

    def cache_path(self) -> Path:
        return self.repo.git_dir / "onetree" / "cache" / "wal.jsonl"

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
            "v": _KIND_VERSIONS[kind],
            "event": kind,
            "event_id": event_id or uuid.uuid4().hex,
            "actor": actor,
            "time": datetime.now(UTC).isoformat(timespec="microseconds"),
            "data": dict(data),
        }

    @staticmethod
    def _validate_document(value: Any, *, oid: str, writing: bool = False) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise WalCorruptError("WAL event document is not an object", oid=oid)
        if frozenset(value) != _DOCUMENT_KEYS:
            raise WalCorruptError("WAL event document has unexpected fields", oid=oid)
        version = value.get("v")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise WalCorruptError("WAL schema version is invalid", oid=oid)
        if version > WAL_READER_VERSION:
            raise WalCorruptError("unsupported WAL schema version", oid=oid, version=version)
        kind = value.get("event")
        if not isinstance(kind, str) or kind not in EVENT_KINDS:
            raise WalCorruptError("unknown WAL event kind", oid=oid)
        # A document may not understate the reader its kind needs: accepting a
        # downgraded ``v`` would let a writer smuggle a newer kind past a reader
        # that refuses the version but not the kind.
        # A historical version of the kind is readable, never writable.
        if version != _KIND_VERSIONS[kind] and (
            writing or version not in _LEGACY_KIND_VERSIONS.get(kind, frozenset())
        ):
            raise WalCorruptError("WAL event version does not match its kind", oid=oid, event=kind)
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
            text = blob.decode("utf-8", errors="strict")
        except (GitError, UnicodeDecodeError) as error:
            raise WalCorruptError("WAL event.json is not valid UTF-8 JSON", oid=oid) from error
        try:
            value = json.loads(text)
        except json.JSONDecodeError as error:
            raise WalCorruptError("WAL event.json is not valid UTF-8 JSON", oid=oid) from error
        except (ValueError, RecursionError) as error:
            raise WalCorruptError(
                "WAL event.json could not be decoded within the active interpreter limits",
                oid=oid,
                reason="json_decode_limit",
            ) from error
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

    def read(self, *, verify: bool = False) -> WalSnapshot:
        """The whole log, first event to last.

        With the cache setting on (``onetree.walCache``), a reader keeps a copy of the
        chain it has verified in ``.git/onetree/cache`` and reads from Git only the events
        appended since. The copy is used only when the log's head descends from the copy's
        last event by first parents, and every new event is read and checked as a full
        read checks it; anything else is a full read, which writes a new copy. ``verify``
        reads and checks the whole chain whatever the setting.
        """

        head = self.head()
        if head is None:
            return WalSnapshot(head=None, events=())
        if not self.cache_enabled():
            return self._read_chain(head)
        if not verify:
            cached = self._read_cached(head)
            if cached is not None:
                return cached
        snapshot = self._read_chain(head)
        self._write_cache(snapshot.events, rewrite=True)
        return snapshot

    def _read_cached(self, head: str) -> WalSnapshot | None:
        loaded = self._load_cache()
        if loaded is None:
            return None
        events, last = loaded
        if last == head:
            return WalSnapshot(head=head, events=events)
        listed = self.repo.git(
            "rev-list", "--first-parent", "--reverse", "--parents", head, f"^{last}", check=False
        )
        if listed.returncode != 0:
            return None
        added = self._read_canonical(head, listed.stdout, last)
        if added is None or not added.events:
            return None
        self._write_cache(added.events, rewrite=False)
        return WalSnapshot(head=head, events=(*events, *added.events))

    def _load_cache(self) -> tuple[tuple[EventRecord, ...], str] | None:
        """The cached chain and its last commit, or None when the copy is absent or unsound."""

        try:
            data = self.cache_path().read_bytes()
        except OSError:
            return None
        if not data.startswith(_CACHE_HEADER) or not data.endswith(b"\n"):
            return None
        body = data[len(_CACHE_HEADER) : -1]
        if not body:
            return None
        try:
            # One JSON value per line; JSON text never holds a raw line break.
            rows = json.loads(b"[" + body.replace(b"\n", b",") + b"]")
        except (ValueError, RecursionError):
            return None
        events: list[EventRecord] = []
        previous: str | None = None
        for row in rows:
            if (
                not isinstance(row, list)
                or len(row) != 3
                or not isinstance(row[0], str)
                or not isinstance(row[1], list)
                or not isinstance(row[2], dict)
            ):
                return None
            parents = tuple(row[1])
            if parents != (() if previous is None else (previous,)):
                return None
            events.append(EventRecord(oid=row[0], parents=parents, document=row[2]))
            previous = row[0]
        if previous is None:
            return None
        try:  # the one cached name that reaches a git command line
            self.repo.validate_oid(previous, label="cached WAL commit")
        except GitError:
            return None
        return tuple(events), previous

    def _write_cache(self, events: Sequence[EventRecord], *, rewrite: bool) -> None:
        """Store or extend the copy. A copy that cannot be written is simply not kept."""

        lines = b"".join(
            json.dumps(
                [event.oid, list(event.parents), dict(event.document)],
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("ascii")
            + b"\n"
            for event in events
        )
        path = self.cache_path()
        with contextlib.suppress(OSError):
            path.parent.mkdir(parents=True, exist_ok=True)
            if not rewrite:
                with path.open("ab") as stream:
                    stream.write(lines)
                return
            temporary = path.with_name(f".wal.{os.getpid()}.{uuid.uuid4().hex}.tmp")
            try:
                temporary.write_bytes(_CACHE_HEADER + lines)
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)

    def _read_chain(self, head: str) -> WalSnapshot:
        # Traverse only the candidate chain. ``--parents`` still exposes every
        # parent on a merge commit, so the exact-parent check below refuses it
        # without first attempting to parse the unrelated side history as WAL.
        output = self.repo.git("rev-list", "--first-parent", "--reverse", "--parents", head).stdout
        snapshot = self._read_canonical(head, output)
        if snapshot is not None:
            return snapshot
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

    def _read_canonical(
        self, head: str, output: bytes, previous: str | None = None
    ) -> WalSnapshot | None:
        """The snapshot the sequential reader in ``read`` would return, or None.

        A snapshot is returned only for a chain whose every record has the canonical shape
        the writer produces; anything else is None, and the sequential reader decides it over
        the same head and history, keeping its per-record commit, exact tree, document, object
        identity, ordering and typed failure contracts. Nothing is retained across calls
        unless the repository turns the cache on (see ``read``).

        With ``previous``, the rows must continue a chain that ends at that commit, and only
        their events are returned: the cache's extension, held to the same checks.
        """

        rows: list[tuple[str, tuple[str, ...]]] = []
        for raw_line in output.splitlines():
            try:
                fields = raw_line.decode("ascii", errors="strict").split()
                oid = self.repo.validate_oid(fields[0], label="WAL commit")
                parents = tuple(
                    self.repo.validate_oid(parent, label="WAL parent") for parent in fields[1:]
                )
            except (UnicodeDecodeError, IndexError, GitError):
                return None
            if parents != (() if previous is None else (previous,)):
                return None
            rows.append((oid, parents))
            previous = oid
        if previous != head:
            return None
        width = self.repo.oid_length // 2
        events: list[EventRecord] = []
        # `ls-tree` refuses a commit or tree whose bytes do not hash to its name, while
        # `cat-file --batch` returns them unchecked (task 194 identity probe), so every
        # certified object's name is recomputed here. The batch also spools to a temporary
        # file the sequential reader never opens, so an OSError declines the read.
        try:
            trees: list[str] = []
            for oid, kind, body in self.repo.cat_file_batch(
                [oid for oid, _ in rows], reference=WAL_REF
            ):
                if kind != "commit" or not self._hashes_to(kind, body, oid):
                    return None
                trees.append(self.repo.commit_object_tree(body, commit=oid))
            blobs: list[str] = []
            for tree, kind, body in self.repo.cat_file_batch(trees, reference=WAL_REF):
                if (
                    kind != "tree"
                    or len(body) != len(_CANONICAL_TREE_PREFIX) + width
                    or not body.startswith(_CANONICAL_TREE_PREFIX)
                    or not self._hashes_to(kind, body, tree)
                ):
                    return None
                blobs.append(body[len(_CANONICAL_TREE_PREFIX) :].hex())
            for index, (blob, kind, body) in enumerate(
                self.repo.cat_file_batch(blobs, reference=WAL_REF)
            ):
                if kind != "blob" or not self._hashes_to(kind, body, blob):
                    return None
                oid, parents = rows[index]
                # Decoded in this frame, at the call depth `_read_event` decodes at, so the
                # recursion limit that refuses a nested document refuses it on both paths.
                try:
                    value = json.loads(body.decode("utf-8", errors="strict"))
                    document = self._validate_document(value, oid=oid)
                except (WalCorruptError, RecursionError, ValueError):
                    return None
                events.append(EventRecord(oid=oid, parents=parents, document=document))
        except (GitError, OSError):
            return None
        return WalSnapshot(head=head, events=tuple(events))

    def _hashes_to(self, kind: str, body: bytes, oid: str) -> bool:
        digest = hashlib.new(self.repo.object_format)
        digest.update(f"{kind} {len(body)}\0".encode("ascii"))
        digest.update(body)
        return digest.hexdigest() == oid

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
            document = self._validate_document(dict(raw_document), oid="uncommitted", writing=True)
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
