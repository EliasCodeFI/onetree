"""Round A coordination state, claims, payload audit, and CAS landing."""

from __future__ import annotations

import contextlib
import math
import os
import re
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from onetree import referee, workspace
from onetree.errors import (
    ClaimConflictError,
    GitError,
    IdentityError,
    InvalidPathError,
    LandingRefusedError,
    LegacyGraftError,
    NarrowRefusedError,
    OnetreeError,
    RefCorruptError,
    RefLockedError,
    ReleaseRefusedError,
    ShallowHistoryError,
    UnrecordedLandingError,
    WalAppendRefusedError,
    WalCorruptError,
    WalDivergedError,
)
from onetree.git import (
    MarkedCommit,
    Repository,
    TempIndex,
    TreeEntry,
    validate_payload_gate_receipt,
)
from onetree.proc import process_alive as _pid_alive
from onetree.proc import process_start as _pid_start
from onetree.wal import WAL_REF, EventRecord, Wal, WalSnapshot

DEFAULT_TTL = 24 * 60 * 60.0
MAX_CAS_RETRIES = 32
_ACTOR_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
#: Liveness degrades to bare pid existence when the kernel exposes no birth
#: tick; a recycled pid then keeps a dead session fencing its peers.
DEGRADED_LIVENESS = "degraded_liveness"

Checkpoint = Callable[[str, Mapping[str, Any]], None]
_AuditHeads = tuple[tuple[str, str | None], ...]

#: The fields a ``landed`` mark must repeat from its intent, field for field.
_LANDED_INTENT_FIELDS = (
    ("commit", "candidate"),
    ("target", "target"),
    ("expected_head", "expected_head"),
    ("tree", "tree"),
)
_DIVERGENCE_KEYS = frozenset({"kind", "target", "detail", "commit", "operation"})


@dataclass(frozen=True)
class SessionInfo:
    actor: str
    generation: str
    pid: int
    pid_start: int | None
    expires_at: float
    base: str
    target: str
    index_path: Path
    warnings: tuple[str, ...] = ()

    def payload(self) -> dict[str, Any]:
        return {
            "actor": self.actor,
            "generation": self.generation,
            "pid": self.pid,
            "pid_start": self.pid_start,
            "expires_at": self.expires_at,
            "base": self.base,
            "target": self.target,
            "index": str(self.index_path),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class ClaimInfo:
    actor: str
    path: str
    generation: str
    session_generation: str
    expires_at: float

    def payload(self) -> dict[str, Any]:
        return {
            "actor": self.actor,
            "path": self.path,
            "generation": self.generation,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True)
class ClaimResult:
    claims: tuple[ClaimInfo, ...]


@dataclass(frozen=True)
class NarrowResult:
    released: tuple[str, ...]
    claims: tuple[ClaimInfo, ...]


@dataclass(frozen=True)
class CheckResult:
    path: str
    blocked: bool
    holders: tuple[ClaimInfo, ...]
    owned: tuple[ClaimInfo, ...]


@dataclass(frozen=True)
class ReleaseResult:
    released: tuple[str, ...]


@dataclass(frozen=True)
class LandResult:
    commit: str
    tree: str
    target: str
    attempts: int
    payload: tuple[str, ...]
    operation: str
    intent: str
    warnings: tuple[str, ...] = ()
    gate_receipt: Mapping[str, Any] | None = None
    referee_receipt: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class Divergence:
    """One disagreement between a target branch and the WAL's landing record."""

    kind: str
    target: str
    detail: str
    commit: str | None = None
    operation: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "target": self.target,
            "detail": self.detail,
            "commit": self.commit,
            "operation": self.operation,
        }


@dataclass(frozen=True)
class WalReport:
    head: str | None
    events: tuple[EventRecord, ...]
    consistent: bool
    pending_intents: int
    active_sessions: tuple[str, ...]
    active_claims: tuple[str, ...]
    divergences: tuple[Divergence, ...] = ()
    ref_locks: tuple[str, ...] = ()
    temp_indexes: tuple[TempIndex, ...] = ()

    def gate_evidence(self) -> list[dict[str, Any]]:
        carriers = {
            event.event_id: event
            for event in self.events
            if event.kind in {"land_intent", "refused"} and "gate_receipt" in event.data
        }
        evidence: list[dict[str, Any]] = []
        for event in self.events:
            if event.kind not in {"land_intent", "landed", "adopted", "refused"}:
                continue
            if event.kind == "refused" and event.data.get("action") != "land":
                continue
            reference = (
                event.event_id
                if event.event_id in carriers
                else event.data.get("intent")
                if event.kind in {"landed", "refused"}
                else None
            )
            carrier = carriers.get(reference) if isinstance(reference, str) else None
            recorded = carrier is not None and (
                carrier.event_id == event.event_id
                or (
                    carrier.kind == "land_intent"
                    and carrier.actor == event.actor
                    and carrier.data.get("operation") == event.data.get("operation")
                )
            )
            evidence.append(
                {
                    "event": event.event_id,
                    "state": "recorded" if recorded else "unrecorded",
                    "receipt_event": reference if recorded else None,
                }
            )
        return evidence


@dataclass(frozen=True)
class RecoveryResult:
    """What one recovery pass measured, not what it hoped.

    ``pending`` counts intents recovery deliberately left alone because the
    landing writing them is still in flight, so ``consistent`` is false exactly
    when the repository still has landings to finish. ``locks`` names the ref
    locks that will refuse the next landing; recovery reports them and never
    deletes one.
    """

    consistent: bool
    pending: int
    repaired: int
    refused: int
    locks: tuple[str, ...] = ()


@dataclass(frozen=True)
class GcResult:
    event: str
    removed_claims: int
    removed_sessions: int
    removed_temp_indexes: int = 0


@dataclass(frozen=True)
class AdoptionResult:
    adopted: tuple[EventRecord, ...]
    report: WalReport


@dataclass(frozen=True)
class _PayloadChange:
    path: str
    old: TreeEntry | None
    new: TreeEntry | None

    def event_payload(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "old": None if self.old is None else self.old.payload(),
            "new": None if self.new is None else self.new.payload(),
        }


@dataclass
class _FoldedState:
    sessions: dict[str, SessionInfo]
    claims: dict[str, ClaimInfo]
    pending: dict[str, EventRecord]
    landed: list[EventRecord]
    #: Operations the WAL has closed as landed, keyed by operation id.
    landed_operations: dict[str, EventRecord]
    #: Intents the WAL has closed as refused, keyed by operation id. Their
    #: candidates must never appear on the target branch.
    refused_intents: dict[str, EventRecord]
    #: Every operation a ``refused`` event named, whether or not it had an intent.
    refused_operations: set[str]
    #: Positions the WAL has observed for each target ref. They name the targets
    #: a branch-side audit has to visit; they never bound how far down it looks.
    target_positions: dict[str, set[str]]
    #: Divergence sets recovery has already recorded a refusal for, so probing a
    #: diverged repository does not append one event per probe.
    diverged_refusals: set[tuple[tuple[str, ...], ...]]
    #: Commits named by ``adopted`` records at the historical v=1 schema. Their
    #: ``operation`` is the 9d2ef3b writer's comma-joined scalar, not an identity,
    #: so recorded-ness is by commit; ``_verify_landed`` checks each against its
    #: commit under that writer's semantics before any audit consults this.
    legacy_adopted: set[str] = field(default_factory=set)
    event_ids: set[str] = field(default_factory=set)
    session_generations: set[str] = field(default_factory=set)
    claim_generations: set[str] = field(default_factory=set)
    session_payloads: dict[tuple[str, str, str], dict[str, TreeEntry | None]] = field(
        default_factory=dict
    )
    workspace_epoch: Mapping[str, Any] | None = None
    workspace_epoch_id: str | None = None
    workspace_transitions: list[tuple[str, str]] = field(default_factory=list)

    @classmethod
    def empty(cls) -> _FoldedState:
        return cls(
            sessions={},
            claims={},
            pending={},
            landed=[],
            landed_operations={},
            refused_intents={},
            refused_operations=set(),
            target_positions={},
            diverged_refusals=set(),
        )

    def copy(self) -> _FoldedState:
        """A private replica, so a rejected trial fold cannot alter the real one."""

        return _FoldedState(
            sessions=dict(self.sessions),
            claims=dict(self.claims),
            pending=dict(self.pending),
            landed=list(self.landed),
            landed_operations=dict(self.landed_operations),
            refused_intents=dict(self.refused_intents),
            refused_operations=set(self.refused_operations),
            target_positions={key: set(value) for key, value in self.target_positions.items()},
            diverged_refusals=set(self.diverged_refusals),
            event_ids=set(self.event_ids),
            session_generations=set(self.session_generations),
            claim_generations=set(self.claim_generations),
            session_payloads={key: dict(value) for key, value in self.session_payloads.items()},
            workspace_epoch=self.workspace_epoch,
            workspace_epoch_id=self.workspace_epoch_id,
            workspace_transitions=list(self.workspace_transitions),
        )

    def observe_position(self, target: str, *positions: str) -> None:
        seen = self.target_positions.setdefault(target, set())
        seen.update(positions)

    def terminal(self, operation: str) -> bool:
        return operation in self.landed_operations or operation in self.refused_operations

    def session_active(self, actor: str, now: float) -> bool:
        session = self.sessions.get(actor)
        return session is not None and _session_alive(session, now)

    def active_claims(self, now: float) -> tuple[ClaimInfo, ...]:
        active: list[ClaimInfo] = []
        for claim in self.claims.values():
            session = self.sessions.get(claim.actor)
            if (
                session is not None
                and session.generation == claim.session_generation
                and claim.expires_at > now
                and _session_alive(session, now)
            ):
                active.append(claim)
        return tuple(sorted(active, key=lambda item: (item.path, item.actor, item.generation)))


def _validate_actor(actor: str) -> str:
    if _ACTOR_PATTERN.fullmatch(actor) is None or actor in {".", ".."}:
        raise IdentityError(
            "session id must be 1-128 path-safe characters",
            session=actor,
        )
    return actor


def _positive_number(value: Any, name: str, *, event_oid: str | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WalCorruptError(f"{name} must be numeric", oid=event_oid)
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise WalCorruptError(f"{name} must be finite and positive", oid=event_oid)
    return number


def _string(data: Mapping[str, Any], name: str, event: EventRecord) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value:
        raise WalCorruptError(f"{event.kind}.{name} is invalid", oid=event.oid)
    return value


def _string_list(data: Mapping[str, Any], name: str, event: EventRecord) -> tuple[str, ...]:
    value = data.get(name)
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise WalCorruptError(f"{event.kind}.{name} is invalid", oid=event.oid)
    return tuple(value)


def _liveness_warnings(pid_start: int | None) -> tuple[str, ...]:
    return () if pid_start is not None else (DEGRADED_LIVENESS,)


def _validate_lander(value: Any, event: EventRecord) -> None:
    """Type the optional process tag an intent carries for its own landing."""

    if value is None:
        return
    if not isinstance(value, dict) or frozenset(value) not in (
        {"pid", "start"},
        {"pid", "start", "warnings"},
    ):
        raise WalCorruptError("land_intent.lander is invalid", oid=event.oid)
    pid = value.get("pid")
    start = value.get("start")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise WalCorruptError("land_intent.lander is invalid", oid=event.oid)
    if start is not None and (isinstance(start, bool) or not isinstance(start, int) or start <= 0):
        raise WalCorruptError("land_intent.lander is invalid", oid=event.oid)
    if "warnings" in value:
        warnings = value.get("warnings")
        if not isinstance(warnings, list) or tuple(warnings) != _liveness_warnings(start):
            raise WalCorruptError("land_intent.lander.warnings is invalid", oid=event.oid)


def _divergence_key(value: Any, event: EventRecord) -> tuple[tuple[str, ...], ...]:
    """Canonical identity of one divergence set, order-independent."""

    if not isinstance(value, list):
        raise WalCorruptError("recovery refusal divergences are invalid", oid=event.oid)
    rows: list[tuple[str, ...]] = []
    for item in value:
        if not isinstance(item, dict) or frozenset(item) != _DIVERGENCE_KEYS:
            raise WalCorruptError("recovery refusal divergences are invalid", oid=event.oid)
        rows.append(
            tuple(
                "" if item[name] is None else str(item[name])
                for name in ("kind", "target", "commit", "operation")
            )
        )
    return tuple(sorted(rows))


def _session_alive(session: SessionInfo, now: float) -> bool:
    return session.expires_at > now and _pid_alive(session.pid, session.pid_start)


def _covers(claimed: str, target: str) -> bool:
    return claimed == "." or claimed == target or target.startswith(f"{claimed}/")


def _overlaps(first: str, second: str) -> bool:
    return _covers(first, second) or _covers(second, first)


class Coordinator:
    """The daemon-free broker; every mutation is serialized by a Git ref CAS."""

    def __init__(
        self,
        repository: Path | str,
        *,
        commit_payload: Path | str | None = None,
    ) -> None:
        self.repo = Repository(repository)
        self.wal = Wal(self.repo)
        self.commit_payload = (
            commit_payload
            if commit_payload is not None
            else os.environ.get("ONETREE_COMMIT_PAYLOAD")
        )

    def _audit_heads(
        self,
        state: _FoldedState,
        target: str | None = None,
    ) -> _AuditHeads:
        """One repository-wide view: every local branch plus HEAD."""

        current = self.repo.audit_heads()
        targets = set(current).union(state.target_positions)
        if target is not None:
            targets.add(self.repo.validate_branch_ref(target))
        ordered = sorted(targets, key=lambda reference: (reference == "HEAD", reference))
        return tuple((reference, current.get(reference)) for reference in ordered)

    @staticmethod
    def _audit_target_names(heads: _AuditHeads) -> tuple[str, ...]:
        return tuple(reference for reference, _ in heads)

    def _unrecorded_landings(
        self,
        state: _FoldedState,
        heads: _AuditHeads,
    ) -> tuple[Divergence, ...]:
        """Broker-marked branch commits for which this WAL has no terminal record."""

        found: list[Divergence] = []
        seen: set[str] = set()
        walked_heads: set[str] = set()
        recorded = {
            (str(event.data.get("commit")), str(event.data.get("operation")))
            for event in state.landed
        }
        recorded.update(
            (str(event.data.get("candidate")), operation)
            for operation, event in state.pending.items()
        )
        recorded.update(
            (str(event.data.get("candidate")), operation)
            for operation, event in state.refused_intents.items()
        )
        for target, head in heads:
            if head is None or head in walked_heads:
                continue
            walked_heads.add(head)
            for marked in self._audit_marked_commits(
                head,
                self._scan_floor(state, target),
                recorded,
                state.pending,
            ):
                if not marked.operations or marked.commit in seen:
                    continue
                seen.add(marked.commit)
                # A commit a verified legacy record names is recorded, whatever
                # identities the current parser reads from it; any other commit
                # carrying those identities is still judged on its own record.
                if marked.commit in state.legacy_adopted or any(
                    (marked.commit, operation) in recorded
                    for operation in marked.operations
                ):
                    continue
                # Divergence and adopted-event schemas are intentionally scalar.
                # The repository has already reduced this unrecorded identity
                # stream to the deterministic nonpending witness (or overall
                # minimum) that the scalar schema can carry.
                if len(marked.operations) != 1:
                    raise AssertionError("audit identity reduction returned multiple witnesses")
                operation = next(iter(marked.operations))
                found.append(
                    Divergence(
                        "unrecorded_landing",
                        target,
                        "a broker landing is on the branch with no WAL record of it",
                        commit=marked.commit,
                        operation=operation,
                    )
                )
        return tuple(found)

    def _read_wal(self, target: str | None = None) -> WalSnapshot:
        """Read a WAL for mutation only after auditing the histories it would adopt."""

        for _ in range(MAX_CAS_RETRIES):
            snapshot = self.wal.read()
            state = self._fold(snapshot)
            self._verify_landed(state)
            heads = self._audit_heads(state, target)
            targets = self._audit_target_names(heads)
            self._refuse_legacy_grafts(targets)
            if self.repo.is_shallow():
                raise ShallowHistoryError(
                    "the repository is shallow, so broker landing history cannot be audited "
                    "completely; deepen it before mutating",
                    targets=list(targets),
                    repair="deepen the repository, then run `onetree wal --adopt` if needed",
                )
            unrecorded = self._unrecorded_landings(state, heads)
            if self.wal.head() != snapshot.head or self._audit_heads(state, target) != heads:
                continue
            if unrecorded:
                raise UnrecordedLandingError(
                    "the repository carries broker landings that refs/onetree/wal does not "
                    "record; run `onetree wal --adopt` before mutating",
                    targets=sorted({item.target for item in unrecorded}),
                    commits=[item.commit for item in unrecorded],
                    divergences=[item.payload() for item in unrecorded],
                    repair="onetree wal --adopt",
                )
            return snapshot
        raise GitError("audit snapshot retry limit exceeded", retries=MAX_CAS_RETRIES)

    def _refuse_legacy_grafts(self, targets: Sequence[str]) -> None:
        graft_file = self.repo.nonempty_graft_file()
        if graft_file is None:
            return
        raise LegacyGraftError(
            "a non-empty legacy Git graft file can hide repository history; "
            "convert or remove it before mutating",
            targets=list(targets),
            graft_file=str(graft_file),
            repair="git replace --convert-graft-file, then review the replacement refs",
        )

    @staticmethod
    def _resolve_release_path(candidate: Path) -> Path:
        """A vanished suffix may still name a recorded claim; unresolved aliases may not."""

        suffix: list[str] = []
        prefix = candidate
        while True:
            try:
                resolved = prefix.resolve(strict=True)
            except RuntimeError as error:
                # Older supported Python versions use RuntimeError for a symlink loop.
                raise OSError(str(error)) from error
            except FileNotFoundError:
                if prefix.is_symlink() or prefix.name in {"", ".."}:
                    raise
                suffix.append(prefix.name)
                prefix = prefix.parent
            else:
                return resolved.joinpath(*reversed(suffix))

    def _canonical_path(self, raw_path: str, *, for_release: bool = False) -> str:
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            raise InvalidPathError(
                "claim paths must be absolute",
                path=raw_path,
                reason="relative",
            )
        try:
            resolved = (
                self._resolve_release_path(candidate)
                if for_release
                else candidate.resolve(strict=True)
            )
        except FileNotFoundError as error:
            raise InvalidPathError(
                "claim path does not exist",
                path=raw_path,
                reason="nonexistent",
            ) from error
        except OSError as error:
            raise InvalidPathError(
                f"claim path cannot be resolved: {error}",
                path=raw_path,
                reason="unresolvable",
            ) from error
        git_dir = self.repo.git_dir
        if resolved == git_dir or git_dir in resolved.parents:
            raise InvalidPathError(
                "claim path is Git's own metadata, not repository content",
                path=raw_path,
                reason="git_internal",
            )
        try:
            relative = resolved.relative_to(self.repo.root)
        except ValueError as error:
            raise InvalidPathError(
                "claim path is outside the repository",
                path=raw_path,
                reason="outside_repository",
            ) from error
        if ".git" in relative.parts:
            raise InvalidPathError(
                "claim path is Git's own metadata, not repository content",
                path=raw_path,
                reason="git_internal",
            )
        return relative.as_posix()

    @staticmethod
    def _ttl_deadline(ttl: float, now: float, limit: float | None = None) -> float:
        if not math.isfinite(ttl) or ttl <= 0:
            raise IdentityError("TTL must be finite and positive", ttl=ttl)
        deadline = now + ttl
        return deadline if limit is None else min(deadline, limit)

    def _fold(self, snapshot: WalSnapshot) -> _FoldedState:
        state = _FoldedState.empty()
        for event in snapshot.events:
            self._apply(state, event)
        return state

    def _try_append(
        self,
        state: _FoldedState,
        snapshot: WalSnapshot,
        documents: Sequence[Mapping[str, Any]],
    ) -> tuple[EventRecord, ...] | None:
        """CAS-append documents that fold, or refuse them before they are written.

        Every read of the WAL is a fold, so an append that does not fold is a
        write that poisons every subsequent read -- from all five entry points,
        permanently, with an error naming corruption rather than the interleave
        that caused it. The documents are therefore folded into a replica of the
        state this CAS would extend before the CAS is attempted: winning the CAS
        makes that replica the truth, and losing it writes nothing at all.
        """

        probe = state.copy()
        for document in documents:
            record = EventRecord(oid="(unwritten)", parents=(), document=dict(document))
            try:
                self._apply(probe, record)
            except (GitError, WalCorruptError) as error:
                raise WalAppendRefusedError(
                    f"refusing to append a {record.kind} event that the WAL cannot replay: {error}",
                    event=record.kind,
                    event_id=record.event_id,
                    actor=record.actor,
                    **error.details,
                ) from error
        return self.wal.try_append(documents, snapshot.head)

    def _apply(self, state: _FoldedState, event: EventRecord) -> None:  # noqa: C901 - one arm per event kind
        """Replay one event into ``state``, refusing every shape replay cannot trust.

        Reading and writing share this function on purpose: an append is
        validated by folding it into the state it will produce, so no accepted
        write can leave the WAL unreadable.
        """

        data = event.data
        if "gate_receipt" in data and event.kind not in {"land_intent", "refused"}:
            raise WalCorruptError("gate receipt is on the wrong event kind", oid=event.oid)
        if "referee_receipt" in data and event.kind not in {"land_intent", "refused"}:
            raise WalCorruptError("referee receipt is on the wrong event kind", oid=event.oid)
        if event.event_id in state.event_ids:
            raise WalCorruptError("duplicate WAL event id", oid=event.oid)
        state.event_ids.add(event.event_id)
        if event.kind == "session_register":
            _validate_actor(event.actor)
            generation = _string(data, "generation", event)
            if generation in state.session_generations:
                raise WalCorruptError("duplicate session generation", oid=event.oid)
            state.session_generations.add(generation)
            pid = data.get("pid")
            pid_start = data.get("pid_start")
            if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
                raise WalCorruptError("session_register.pid is invalid", oid=event.oid)
            if pid_start is not None and (
                isinstance(pid_start, bool) or not isinstance(pid_start, int) or pid_start <= 0
            ):
                raise WalCorruptError("session_register.pid_start is invalid", oid=event.oid)
            expires_at = _positive_number(
                data.get("expires_at"), "expires_at", event_oid=event.oid
            )
            base = self.repo.validate_oid(_string(data, "base", event), label="session base")
            target = self.repo.validate_branch_ref(_string(data, "target", event))
            state.sessions[event.actor] = SessionInfo(
                actor=event.actor,
                generation=generation,
                pid=pid,
                pid_start=pid_start,
                expires_at=expires_at,
                base=base,
                target=target,
                index_path=self.repo.index_path(event.actor),
                warnings=_liveness_warnings(pid_start),
            )
            state.observe_position(target, base)
        elif event.kind == "claim":
            generation = _string(data, "generation", event)
            if generation in state.claim_generations:
                raise WalCorruptError("duplicate claim generation", oid=event.oid)
            state.claim_generations.add(generation)
            path = _string(data, "path", event)
            if path.startswith("/") or ".." in Path(path).parts:
                raise WalCorruptError("claim path is not repository-relative", oid=event.oid)
            state.claims[generation] = ClaimInfo(
                actor=event.actor,
                path=path,
                generation=generation,
                session_generation=_string(data, "session_generation", event),
                expires_at=_positive_number(
                    data.get("expires_at"), "expires_at", event_oid=event.oid
                ),
            )
        elif event.kind == "release":
            generations = _string_list(data, "generations", event)
            if len(set(generations)) != len(generations):
                raise WalCorruptError("release repeats a claim generation", oid=event.oid)
            for generation in generations:
                claim = state.claims.get(generation)
                if claim is None or claim.actor != event.actor:
                    raise WalCorruptError(
                        "release does not match an owned claim", oid=event.oid
                    )
                state.claims.pop(generation)
        elif event.kind == "gc":
            removed_claims = _string_list(data, "claim_generations", event)
            removed_sessions = _string_list(data, "session_generations", event)
            # The swept file names are the only record of what gc deleted from
            # disk, so they are held to the same shape as everything else it
            # claims: an event nobody validates cannot testify to anything.
            _string_list(data, "temp_indexes", event)
            if len(set(removed_claims)) != len(removed_claims) or len(
                set(removed_sessions)
            ) != len(removed_sessions):
                raise WalCorruptError("gc repeats a generation", oid=event.oid)
            for generation in removed_claims:
                state.claims.pop(generation, None)
            for generation in removed_sessions:
                for actor, session in tuple(state.sessions.items()):
                    if session.generation == generation:
                        state.sessions.pop(actor)
        elif event.kind == "land_intent":
            operation = _string(data, "operation", event)
            if operation in state.pending or state.terminal(operation):
                raise WalCorruptError("duplicate landing operation", oid=event.oid)
            attempt = data.get("attempt")
            if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt <= 0:
                raise WalCorruptError("land_intent.attempt is invalid", oid=event.oid)
            intent_target = self.repo.validate_branch_ref(_string(data, "target", event))
            intent_head = self.repo.validate_oid(
                _string(data, "expected_head", event), label="expected target"
            )
            state.observe_position(intent_target, intent_head)
            self.repo.validate_oid(_string(data, "candidate", event), label="candidate")
            self.repo.validate_oid(_string(data, "tree", event), label="candidate tree")
            _string(data, "session_generation", event)
            _validate_lander(data.get("lander"), event)
            fences = data.get("fences")
            payload = data.get("payload")
            if (
                not isinstance(fences, list)
                or not fences
                or not isinstance(payload, list)
                or not payload
            ):
                raise WalCorruptError("land_intent audit data is invalid", oid=event.oid)
            fence_paths: list[str] = []
            for fence in fences:
                if not isinstance(fence, dict):
                    raise WalCorruptError("land_intent fence is invalid", oid=event.oid)
                fence_path = fence.get("path")
                fence_generation = fence.get("claim_generation")
                if (
                    not isinstance(fence_path, str)
                    or not fence_path
                    or not isinstance(fence_generation, str)
                ):
                    raise WalCorruptError("land_intent fence is invalid", oid=event.oid)
                fence_paths.append(fence_path)
            intent_payload_paths: list[str] = []
            for change in payload:
                if not isinstance(change, dict) or frozenset(change) != {
                    "path",
                    "old",
                    "new",
                }:
                    raise WalCorruptError("land_intent payload is invalid", oid=event.oid)
                change_path = change.get("path")
                if not isinstance(change_path, str) or not change_path:
                    raise WalCorruptError("land_intent payload path is invalid", oid=event.oid)
                for side in (change.get("old"), change.get("new")):
                    if side is None:
                        continue
                    if not isinstance(side, dict) or frozenset(side) != {"mode", "oid"}:
                        raise WalCorruptError(
                            "land_intent payload entry is invalid", oid=event.oid
                        )
                    if not isinstance(side.get("mode"), str) or not isinstance(
                        side.get("oid"), str
                    ):
                        raise WalCorruptError(
                            "land_intent payload entry is invalid", oid=event.oid
                        )
                    self.repo.validate_oid(side["oid"], label="payload entry")
                intent_payload_paths.append(change_path)
            if sorted(fence_paths) != sorted(intent_payload_paths) or len(
                set(intent_payload_paths)
            ) != len(intent_payload_paths):
                raise WalCorruptError(
                    "land_intent fences do not match its payload", oid=event.oid
                )
            self._validate_gate_receipt(event, "CLEAN", intent_payload_paths, data["candidate"])
            if "referee_receipt" in data:
                self._validate_referee_receipt(event, data["candidate"])
                if data["referee_receipt"]["result"] not in {"pass", "not_configured"}:
                    raise WalCorruptError("intent has a failed referee", oid=event.oid)
                if data["referee_receipt"]["baseline"] != intent_head:
                    raise WalCorruptError("referee baseline differs from intent", oid=event.oid)
            if state.workspace_epoch is not None:
                # The epoch records which branch is protected and which referee
                # guards it. The broker that honours it refuses every landing outside
                # that branch and never writes an intent without that referee's
                # evidence, so an intent elsewhere, lacking the evidence, or carrying
                # another referee's, did not come from the broker.
                if intent_target != state.workspace_epoch["target"]:
                    raise WalCorruptError("protected intent targets outside the shared branch",
                                          oid=event.oid)
                if "referee_receipt" not in data:
                    raise WalCorruptError("protected intent has no referee evidence",
                                          oid=event.oid)
                receipt = data["referee_receipt"]
                if [receipt["command"], receipt["timeout_seconds"]] != list(
                    state.workspace_epoch["referee"]
                ):
                    raise WalCorruptError("protected intent evidence names another referee",
                                          oid=event.oid)
            state.pending[operation] = event
            if state.workspace_epoch is not None:
                state.workspace_transitions.append((intent_head, data["candidate"]))
        elif event.kind == "adopted":
            if frozenset(data) != {"operation", "target", "expected_head", "commit", "tree"}:
                raise WalCorruptError("adopted event has unexpected fields", oid=event.oid)
            _validate_actor(event.actor)
            operation = _string(data, "operation", event)
            # An over-detected commit may quote the id of an independent intent
            # that is still live. Adoption records this distinct commit/id pair
            # without consuming that intent; its later landed/refused event
            # closes pending in the ordinary way. Adopted commits already share
            # ids safely, with exact duplicate pairs rejected below.
            target_value = _string(data, "target", event)
            target = (
                "HEAD"
                if target_value == "HEAD"
                else self.repo.validate_branch_ref(target_value)
            )
            expected_head = self.repo.validate_oid(
                _string(data, "expected_head", event), label="adopted base"
            )
            commit = self.repo.validate_oid(
                _string(data, "commit", event), label="adopted commit"
            )
            if any(
                existing.data.get("operation") == operation
                and existing.data.get("commit") == commit
                for existing in state.landed
            ):
                raise WalCorruptError("duplicate adopted landing", oid=event.oid)
            self.repo.validate_oid(_string(data, "tree", event), label="adopted tree")
            state.observe_position(target, expected_head, commit)
            state.landed_operations.setdefault(operation, event)
            state.landed.append(event)
            if event.legacy:
                state.legacy_adopted.add(commit)
        elif event.kind == "landed":
            operation = _string(data, "operation", event)
            intent = state.pending.get(operation)
            if intent is None or data.get("intent") != intent.event_id:
                raise WalCorruptError("landed event has no matching intent", oid=event.oid)
            # Write-ahead ordering puts the branch CAS between the intent and
            # its landed mark, so an unrelated append may legitimately sit
            # between the two commits. Adjacency is therefore replaced by a
            # full field-for-field agreement with the intent.
            if any(
                data.get(name) != intent.data.get(source)
                for name, source in _LANDED_INTENT_FIELDS
            ):
                raise WalCorruptError("landed event differs from its intent", oid=event.oid)
            landed_target = self.repo.validate_branch_ref(_string(data, "target", event))
            landed_head = self.repo.validate_oid(
                _string(data, "expected_head", event), label="landed base"
            )
            landed_commit = self.repo.validate_oid(
                _string(data, "commit", event), label="landed commit"
            )
            self.repo.validate_oid(_string(data, "tree", event), label="landed tree")
            state.observe_position(landed_target, landed_head, landed_commit)
            landed_payload_paths = data.get("payload")
            if not isinstance(landed_payload_paths, list) or any(
                not isinstance(path, str) or not path for path in landed_payload_paths
            ):
                raise WalCorruptError("landed.payload is invalid", oid=event.oid)
            audited_paths = [
                change["path"] for change in intent.data["payload"] if isinstance(change, dict)
            ]
            if landed_payload_paths != audited_paths:
                raise WalCorruptError("landed payload differs from its intent", oid=event.oid)
            if not isinstance(data.get("recovered"), bool):
                raise WalCorruptError("landed.recovered is invalid", oid=event.oid)
            state.pending.pop(operation)
            state.landed_operations[operation] = event
            state.landed.append(event)
            progress = state.session_payloads.setdefault(
                (intent.actor, str(intent.data["session_generation"]), landed_target), {}
            )
            for change in intent.data["payload"]:
                entry = change["new"]
                progress[change["path"]] = (
                    None if entry is None else TreeEntry(entry["mode"], entry["oid"])
                )
        elif event.kind == "workspace_protect":
            try:
                workspace.validate(data, self.repo)
            except (ValueError, TypeError, KeyError, OnetreeError) as error:
                raise WalCorruptError("invalid workspace baseline", oid=event.oid) from error
            if state.workspace_epoch is not None:
                raise WalCorruptError("workspace baseline cannot be silently replaced",
                                      oid=event.oid)
            state.workspace_epoch = data
            state.workspace_epoch_id = event.event_id
            state.workspace_transitions = []
        elif event.kind == "workspace_reconcile":
            try:
                if set(data) != {"previous_epoch", "reason", "violation", "baseline"}:
                    raise ValueError("invalid reconciliation fields")
                if (state.workspace_epoch_id is None
                        or data["previous_epoch"] != state.workspace_epoch_id):
                    raise ValueError("reconciliation names a different prior epoch")
                if not isinstance(data["reason"], str) or not data["reason"].strip():
                    raise ValueError("reconciliation requires an operator reason")
                if data["violation"] is not None and (
                    not isinstance(data["violation"], str) or not data["violation"].strip()
                ):
                    raise ValueError("invalid prior violation")
                if state.pending or state.claims:
                    raise ValueError("reconciliation requires no claims or pending intents")
                workspace.validate(data["baseline"], self.repo, allow_empty=True)
            except (ValueError, TypeError, KeyError, OnetreeError) as error:
                raise WalCorruptError("invalid workspace reconciliation", oid=event.oid) from error
            state.workspace_epoch = data["baseline"]
            state.workspace_epoch_id = event.event_id
            state.workspace_transitions = []
        elif event.kind == "refused":
            refused_operation = data.get("operation")
            intent_id = data.get("intent")
            if refused_operation is not None:
                if not isinstance(refused_operation, str) or not refused_operation:
                    raise WalCorruptError("refused.operation is invalid", oid=event.oid)
                pending = state.pending.get(refused_operation)
                if pending is not None and intent_id == pending.event_id:
                    state.refused_intents[refused_operation] = state.pending.pop(
                        refused_operation
                    )
                state.refused_operations.add(refused_operation)
            action = _string(data, "action", event)
            reason = _string(data, "reason", event)
            if "referee_receipt" in data:
                self._validate_referee_receipt(event)
                result = data["referee_receipt"]["result"]
                if (action != "land" or result not in {"red", "timeout", "error"}
                        or reason != "referee_" + result):
                    raise WalCorruptError("referee receipt has no matching refusal", oid=event.oid)
            if "gate_receipt" in data:
                verdict = {
                    "payload_gate_refused": "REFUSE", "payload_gate_degraded": "DEGRADED"
                }.get(reason)
                if action != "land" or verdict is None:
                    raise WalCorruptError("gate receipt has no matching refusal", oid=event.oid)
                self._validate_gate_receipt(event, verdict, _string_list(data, "paths", event))
            if action == "recover" and reason == "branch_diverged":
                state.diverged_refusals.add(_divergence_key(data.get("divergences"), event))
        else:  # all event kinds are covered above
            raise AssertionError(f"unhandled WAL event: {event.kind}")

    @staticmethod
    def _validate_referee_receipt(event: EventRecord, candidate: str | None = None) -> None:
        try:
            referee.validate_receipt(event.data["referee_receipt"])
        except (ValueError, TypeError) as error:
            raise WalCorruptError("invalid referee evidence", oid=event.oid,
                                  detail=str(error)) from error
        if candidate is not None and event.data["referee_receipt"]["candidate"] != candidate:
            raise WalCorruptError("referee candidate differs from intent", oid=event.oid)

    @staticmethod
    def _validate_gate_receipt(
        event: EventRecord, verdict: str, paths: Sequence[str], candidate: str | None = None
    ) -> None:
        if "gate_receipt" not in event.data:
            return
        receipt = event.data["gate_receipt"]
        try:
            validate_payload_gate_receipt(receipt)
        except (ValueError, TypeError) as error:
            raise WalCorruptError(
                "invalid retained gate evidence", oid=event.oid, detail=str(error)
            ) from error
        if (
            receipt["report"]["verdict"] != verdict
            or set(receipt["expected_paths"]) != set(paths)
            or (candidate is not None and receipt["report"]["candidate"] != candidate)
        ):
            raise WalCorruptError("gate receipt differs from its WAL event", oid=event.oid)

    def _active_session(
        self,
        state: _FoldedState,
        actor: str,
        now: float,
    ) -> SessionInfo | None:
        session = state.sessions.get(actor)
        if session is None or not _session_alive(session, now):
            return None
        return session

    def register_session(
        self,
        actor: str,
        *,
        pid: int | None = None,
        ttl: float = DEFAULT_TTL,
        target: str | None = None,
    ) -> SessionInfo:
        actor = _validate_actor(actor)
        body_pid = os.getppid() if pid is None else pid
        if body_pid <= 0 or not _pid_alive(body_pid, _pid_start(body_pid)):
            raise IdentityError("session process is not alive", session=actor, pid=body_pid)
        process_start = _pid_start(body_pid)
        warnings = _liveness_warnings(process_start)
        target_ref = (
            self.repo.default_branch_ref()
            if target is None
            else self.repo.validate_branch_ref(target)
        )
        base = self.repo.resolve_ref(target_ref, missing_ok=True)
        if base is None:
            raise GitError("landing target is unborn", reference=target_ref)
        now = time.time()
        expires_at = self._ttl_deadline(ttl, now)
        generation = uuid.uuid4().hex
        document = self.wal.document(
            "session_register",
            actor,
            {
                "generation": generation,
                "pid": body_pid,
                "pid_start": process_start,
                "expires_at": expires_at,
                "base": base,
                "target": target_ref,
                "warnings": list(warnings),
            },
        )
        for _ in range(MAX_CAS_RETRIES):
            snapshot = self._read_wal(target_ref)
            state = self._fold(snapshot)
            existing = self._active_session(state, actor, time.time())
            if existing is not None:
                if (
                    existing.pid == body_pid
                    and existing.pid_start == process_start
                    and existing.target == target_ref
                ):
                    self.repo.ensure_index(actor, self._session_baseline(state, existing))
                    return existing
                raise IdentityError(
                    "session id is already registered to a live process",
                    session=actor,
                    pid=existing.pid,
                )
            if self._try_append(state, snapshot, (document,)) is not None:
                index = self.repo.ensure_index(actor, base, reset=True)
                return SessionInfo(
                    actor=actor,
                    generation=generation,
                    pid=body_pid,
                    pid_start=process_start,
                    expires_at=expires_at,
                    base=base,
                    target=target_ref,
                    index_path=index,
                    warnings=warnings,
                )
        raise GitError("session registration CAS retry limit exceeded")

    def _session_baseline(self, state: _FoldedState, session: SessionInfo) -> str:
        """Subtract only this incarnation's terminal payloads from its next submission.

        Peer changes never become private-index edits, and newly staged work must
        survive a landing. Existing indexes are therefore not refreshed or reset.
        """
        progress = state.session_payloads.get((session.actor, session.generation, session.target))
        return self.repo.overlay_tree(session.base, progress) if progress else session.base

    def protect_workspace(self, actor: str) -> Mapping[str, Any]:
        actor = _validate_actor(actor)
        for _ in range(MAX_CAS_RETRIES):
            snapshot = self._read_wal()
            state = self._fold(snapshot)
            if state.workspace_epoch is not None:
                detail = self._workspace_violation(state)
                if detail is not None:
                    raise LandingRefusedError(detail, reason="workspace_changed", paths=[])
                return state.workspace_epoch
            if state.pending or state.active_claims(time.time()):
                raise LandingRefusedError("protection requires no pending intents or active claims",
                                          reason="workspace_busy", paths=[])
            try:
                epoch = workspace.baseline(self.repo)
            except (ValueError, OSError, GitError) as error:
                raise LandingRefusedError(str(error), reason="workspace_unavailable",
                                          paths=[]) from error
            document = self.wal.document("workspace_protect", actor, epoch)
            if self._try_append(state, snapshot, (document,)) is not None:
                return epoch
        raise GitError("workspace protection CAS retry limit exceeded")

    def reconcile_workspace(
        self, actor: str, *, expected_epoch: str, reason: str,
    ) -> Mapping[str, Any]:
        """Accept an explicitly reviewed workspace without repairing branch or file state.

        The caller binds the current protection event and supplies the operator's
        reason. History remains in the WAL, and active claims, pending intents or
        unrelated branch/WAL divergence prevent replacing its baseline.
        """

        actor = _validate_actor(actor)
        if not isinstance(reason, str) or not reason.strip():
            raise LandingRefusedError("reconciliation requires an operator reason",
                                      reason="reconciliation_reason", paths=[])
        for _ in range(MAX_CAS_RETRIES):
            snapshot = self._read_wal()
            state = self._fold(snapshot)
            if state.workspace_epoch_id is None:
                raise LandingRefusedError("workspace is not protected",
                                          reason="workspace_unprotected", paths=[])
            if expected_epoch != state.workspace_epoch_id:
                raise LandingRefusedError("workspace epoch no longer matches the request",
                                          reason="workspace_epoch_changed", paths=[])
            if state.pending or state.active_claims(time.time()):
                raise LandingRefusedError(
                    "reconciliation requires no pending intents or active claims",
                    reason="workspace_busy", paths=[],
                )
            self._verify_landed(state)
            heads = self._audit_heads(state, None)
            divergences = self._divergences(state, heads)
            if divergences:
                raise WalDivergedError("reconciliation cannot resolve branch/WAL divergence",
                                       divergences=[item.payload() for item in divergences])
            violation = self._workspace_violation(state)
            try:
                baseline = workspace.baseline(self.repo, allow_empty=True)
                workspace.validate(baseline, self.repo, allow_empty=True)
                if (workspace.violation(self.repo, baseline, ()) is not None
                        or workspace.baseline(self.repo, allow_empty=True) != baseline):
                    raise ValueError("workspace changed during reconciliation observation")
            except (ValueError, OSError, GitError) as error:
                raise LandingRefusedError(str(error), reason="workspace_unavailable",
                                          paths=[]) from error
            if self.wal.head() != snapshot.head or self._audit_heads(state, None) != heads:
                continue
            documents: list[Mapping[str, Any]] = []
            if state.claims:
                documents.append(self.wal.document("gc", actor, {
                    "claim_generations": sorted(state.claims),
                    "session_generations": [], "temp_indexes": [],
                }))
            data = {"previous_epoch": expected_epoch, "reason": reason,
                    "violation": violation, "baseline": baseline}
            document = self.wal.document("workspace_reconcile", actor, data)
            documents.append(document)
            if self._try_append(state, snapshot, documents) is not None:
                return {"epoch": document["event_id"], **data}
        raise GitError("workspace reconciliation CAS retry limit exceeded")

    def _workspace_violation(self, state: _FoldedState) -> str | None:
        if state.workspace_epoch is None:
            return None
        refused = {
            (str(intent.data["expected_head"]), str(intent.data["candidate"]))
            for intent in state.refused_intents.values()
        }
        transitions = [item for item in state.workspace_transitions if item not in refused]
        return workspace.violation(self.repo, state.workspace_epoch, transitions)

    def claim(
        self,
        actor: str,
        paths: Sequence[str],
        *,
        ttl: float = DEFAULT_TTL,
    ) -> ClaimResult:
        actor = _validate_actor(actor)
        canonical = tuple(sorted({self._canonical_path(path) for path in paths}))
        if not canonical:
            raise InvalidPathError("at least one claim path is required", reason="empty")
        for _ in range(MAX_CAS_RETRIES):
            now = time.time()
            snapshot = self._read_wal()
            state = self._fold(snapshot)
            session = self._active_session(state, actor, now)
            if session is None:
                raise IdentityError("session is not active", session=actor)
            active = state.active_claims(now)
            conflicts = tuple(
                claim
                for claim in active
                if claim.actor != actor and any(_overlaps(claim.path, path) for path in canonical)
            )
            if conflicts:
                holders = sorted({claim.actor for claim in conflicts})
                refusal = self.wal.document(
                    "refused",
                    actor,
                    {
                        "action": "claim",
                        "reason": "claimed_by_other",
                        "paths": list(canonical),
                        "holders": holders,
                    },
                )
                if self._try_append(state, snapshot, (refusal,)) is None:
                    continue
                raise ClaimConflictError(
                    f"claim refused; held by {', '.join(holders)}",
                    paths=list(canonical),
                    holders=holders,
                )
            owned = tuple(claim for claim in active if claim.actor == actor)
            needed = tuple(
                path for path in canonical if not any(_covers(claim.path, path) for claim in owned)
            )
            if not needed:
                return ClaimResult(
                    tuple(
                        claim
                        for claim in owned
                        if any(_covers(claim.path, path) for path in canonical)
                    )
                )
            deadline = self._ttl_deadline(ttl, now, session.expires_at)
            claims = tuple(
                ClaimInfo(actor, path, uuid.uuid4().hex, session.generation, deadline)
                for path in needed
            )
            documents = tuple(
                self.wal.document(
                    "claim",
                    actor,
                    {
                        "path": claim.path,
                        "generation": claim.generation,
                        "session_generation": claim.session_generation,
                        "expires_at": claim.expires_at,
                    },
                )
                for claim in claims
            )
            if self._try_append(state, snapshot, documents) is not None:
                return ClaimResult(claims)
        raise GitError("claim CAS retry limit exceeded")

    def narrow(self, actor: str, parent: str, paths: Sequence[str]) -> NarrowResult:
        """Retain existing descendants without surrendering their coverage between writes.

        Only the named parent generation is surrendered. Other claims survive,
        and newly issued children inherit its remaining lease without renewal.
        """

        actor = _validate_actor(actor)
        canonical_parent = self._canonical_path(parent)
        canonical = tuple(sorted({self._canonical_path(path) for path in paths}))
        if not canonical:
            raise InvalidPathError("at least one retained path is required", reason="empty")
        invalid = [
            path
            for path in canonical
            if path == canonical_parent or not _covers(canonical_parent, path)
        ]
        if invalid:
            raise InvalidPathError(
                "retained paths must be proper descendants of the parent",
                reason="not_descendant",
                parent=canonical_parent,
                paths=invalid,
            )
        children = tuple(
            path
            for path in canonical
            if not any(other != path and _covers(other, path) for other in canonical)
        )
        selected_generation: str | None = None
        for _ in range(MAX_CAS_RETRIES):
            now = time.time()
            snapshot = self._read_wal()
            state = self._fold(snapshot)
            session = self._active_session(state, actor, now)
            if session is None:
                raise IdentityError("session is not active", session=actor)
            active = state.active_claims(now)
            owned = tuple(claim for claim in active if claim.actor == actor)
            parents = tuple(claim for claim in owned if claim.path == canonical_parent)
            selected = parents[0] if len(parents) == 1 else None
            reason: str | None = None
            if selected is None:
                reason = "not_held"
            elif selected_generation is not None and selected.generation != selected_generation:
                reason = "claim_changed"
            else:
                selected_generation = selected.generation
                fenced = {
                    str(fence.get("claim_generation"))
                    for intent in state.pending.values()
                    for fence in intent.data.get("fences", [])
                    if isinstance(fence, dict)
                }
                if selected.generation in fenced:
                    reason = "pending_land"
            if reason is not None:
                refusal = self.wal.document(
                    "refused",
                    actor,
                    {
                        "action": "narrow",
                        "reason": reason,
                        "parent": canonical_parent,
                        "paths": list(children),
                    },
                )
                if self._try_append(state, snapshot, (refusal,)) is None:
                    continue
                raise NarrowRefusedError(
                    "narrow refused",
                    reason=reason,
                    parent=canonical_parent,
                    paths=list(children),
                )
            assert selected is not None
            remaining = tuple(claim for claim in owned if claim.generation != selected.generation)
            needed = tuple(
                path
                for path in children
                if not any(_covers(claim.path, path) for claim in remaining)
            )
            claims = tuple(
                ClaimInfo(
                    actor,
                    path,
                    uuid.uuid4().hex,
                    session.generation,
                    min(selected.expires_at, session.expires_at),
                )
                for path in needed
            )
            documents = tuple(
                self.wal.document(
                    "claim",
                    actor,
                    {
                        "path": claim.path,
                        "generation": claim.generation,
                        "session_generation": claim.session_generation,
                        "expires_at": claim.expires_at,
                    },
                )
                for claim in claims
            ) + (
                self.wal.document(
                    "release",
                    actor,
                    {"generations": [selected.generation], "paths": [canonical_parent]},
                ),
            )
            if self._try_append(state, snapshot, documents) is not None:
                retained = tuple(
                    claim
                    for claim in (*remaining, *claims)
                    if any(_covers(claim.path, path) for path in children)
                )
                return NarrowResult(
                    (canonical_parent,), tuple(sorted(retained, key=lambda claim: claim.path))
                )
        raise GitError("narrow CAS retry limit exceeded")

    def check(self, actor: str, path: str) -> CheckResult:
        actor = _validate_actor(actor)
        canonical = self._canonical_path(path)
        # Checking ownership is read-only: an unrecorded landing is reported by
        # ``wal`` and blocks writes, but cannot erase the claims the WAL does have.
        state = self._fold(self.wal.read())
        relevant = tuple(
            claim for claim in state.active_claims(time.time()) if _overlaps(claim.path, canonical)
        )
        holders = tuple(claim for claim in relevant if claim.actor != actor)
        owned = tuple(claim for claim in relevant if claim.actor == actor)
        return CheckResult(canonical, bool(holders), holders, owned)

    def release(self, actor: str, paths: Sequence[str] = ()) -> ReleaseResult:
        actor = _validate_actor(actor)
        canonical = tuple(sorted({self._canonical_path(path, for_release=True) for path in paths}))
        for _ in range(MAX_CAS_RETRIES):
            now = time.time()
            snapshot = self._read_wal()
            state = self._fold(snapshot)
            if self._active_session(state, actor, now) is None:
                raise IdentityError("session is not active", session=actor)
            owned = tuple(claim for claim in state.active_claims(now) if claim.actor == actor)
            selected = (
                owned
                if not canonical
                else tuple(
                    claim
                    for claim in owned
                    if any(_covers(path, claim.path) or path == claim.path for path in canonical)
                )
            )
            fenced = {
                str(fence.get("claim_generation"))
                for intent in state.pending.values()
                for fence in intent.data.get("fences", [])
                if isinstance(fence, dict)
            }
            blocked = tuple(claim for claim in selected if claim.generation in fenced)
            if not selected or blocked:
                reason = "pending_land" if blocked else "not_held"
                refusal = self.wal.document(
                    "refused",
                    actor,
                    {
                        "action": "release",
                        "reason": reason,
                        "paths": list(canonical),
                    },
                )
                if self._try_append(state, snapshot, (refusal,)) is None:
                    continue
                raise ReleaseRefusedError(
                    "release refused",
                    reason=reason,
                    paths=list(canonical),
                )
            released_paths = tuple(sorted({claim.path for claim in selected}))
            release = self.wal.document(
                "release",
                actor,
                {
                    "generations": [claim.generation for claim in selected],
                    "paths": list(released_paths),
                },
            )
            if self._try_append(state, snapshot, (release,)) is not None:
                return ReleaseResult(released_paths)
        raise GitError("release CAS retry limit exceeded")

    @staticmethod
    def _changes(
        base_entries: Mapping[str, TreeEntry],
        session_entries: Mapping[str, TreeEntry],
    ) -> tuple[_PayloadChange, ...]:
        paths = sorted(set(base_entries) | set(session_entries))
        return tuple(
            _PayloadChange(path, base_entries.get(path), session_entries.get(path))
            for path in paths
            if base_entries.get(path) != session_entries.get(path)
        )

    def _record_land_refusal(
        self,
        state: _FoldedState,
        snapshot: WalSnapshot,
        actor: str,
        operation: str,
        reason: str,
        paths: Sequence[str] = (),
    ) -> bool:
        document = self.wal.document(
            "refused",
            actor,
            {
                "action": "land",
                "reason": reason,
                "operation": operation,
                "paths": list(paths),
            },
        )
        return self._try_append(state, snapshot, (document,)) is not None

    def _append_document(self, document: Mapping[str, Any], *, what: str) -> EventRecord:
        """CAS-append one terminal document, re-reading the WAL on every loss."""

        for _ in range(MAX_CAS_RETRIES):
            # Every caller has already crossed its mutation audit. A landing's
            # branch CAS may also have become irreversible, so a new unrelated
            # divergence must not prevent this terminal event from being
            # recorded or relabel success as failure.
            snapshot = self.wal.read()
            records = self._try_append(self._fold(snapshot), snapshot, (document,))
            if records is not None:
                return records[0]
        raise GitError(f"{what} CAS retry limit exceeded", retries=MAX_CAS_RETRIES)

    def land(
        self,
        actor: str,
        *,
        message: str | None = None,
        target: str | None = None,
        checkpoint: Checkpoint | None = None,
    ) -> LandResult:
        """Audit, then land write-ahead: intent, branch CAS, landed mark.

        Round A moved the branch and the WAL in one ``update-ref --stdin``
        transaction. On the files ref backend that transaction is not
        crash-atomic: Git commits the two loose refs as two sequential renames,
        branch first, so a death between them leaves the candidate on the branch
        with no WAL provenance at all. No two-loose-ref crash-atomic primitive
        exists on this backend, so the protocol stops needing one. Every ref
        update below is a single-ref CAS, which *is* atomic, and the order is
        chosen so that each crash window leaves a state that can be named.

        There are three such states, not two. A crash before the branch moves
        leaves an intent with no branch move, which recovery refuses. A crash
        after it moves leaves a branch move whose intent is already recorded,
        which recovery completes. And ``git update-ref`` itself can die in the
        window between the rename that applies the update and the exit status
        that reports it: the branch moved, this process is still alive, and it
        is holding a failure over its own success. That third state is named
        where it happens, by ``Repository._reapplied`` -- a target that
        contains this call's own candidate is a win whose report was lost, even
        if ordinary commits have advanced its tip. Reading it as a lost race
        refuses an intent whose candidate is on the branch, and nothing retires
        a ``refused`` event, so the WAL stays diverged on
        ``refused_candidate_on_target`` forever over correct data.
        """

        actor = _validate_actor(actor)
        target_ref = (
            self.repo.head_ref()
            if target is None
            else self.repo.validate_branch_ref(target)
        )
        attempts = 0
        for _ in range(MAX_CAS_RETRIES):
            now = time.time()
            snapshot = self._read_wal(target_ref)
            state = self._fold(snapshot)
            # A landing operation is one attempt. Retries mint a new id so the
            # WAL keeps every attempt's intent and its terminal event distinct.
            operation = uuid.uuid4().hex
            intent_id = uuid.uuid4().hex
            session = self._active_session(state, actor, now)
            if session is None:
                if not self._record_land_refusal(
                    state, snapshot, actor, operation, "session_inactive"
                ):
                    continue
                raise LandingRefusedError(
                    "landing session is inactive",
                    reason="session_inactive",
                    paths=[],
                )
            if session.target != target_ref:
                if not self._record_land_refusal(state, snapshot, actor, operation, "wrong_target"):
                    continue
                raise LandingRefusedError(
                    "session index belongs to another target",
                    reason="wrong_target",
                    paths=[],
                )
            baseline = self._session_baseline(state, session)
            index = self.repo.ensure_index(actor, baseline)
            session_tree = self.repo.write_tree(index)
            base_entries = self.repo.tree_entries(baseline)
            session_entries = self.repo.tree_entries(session_tree)
            changes = self._changes(base_entries, session_entries)
            if not changes:
                if not self._record_land_refusal(
                    state, snapshot, actor, operation, "nothing_staged"
                ):
                    continue
                raise LandingRefusedError(
                    "session index has no payload",
                    reason="nothing_staged",
                    paths=[],
                )
            invalidated = [change.path for change in changes]
            try:
                violation = self._workspace_violation(state)
            except LandingRefusedError:
                if not self._record_land_refusal(
                    state, snapshot, actor, operation, "referee_config", invalidated
                ):
                    continue
                raise LandingRefusedError("invalid referee configuration",
                                          reason="referee_config", paths=invalidated) from None
            if state.workspace_epoch is not None and target_ref != state.workspace_epoch["target"]:
                violation = "landing target is outside the protected shared branch"
            if violation is not None:
                if not self._record_land_refusal(
                    state, snapshot, actor, operation, "workspace_changed", invalidated
                ):
                    continue
                raise LandingRefusedError(violation, reason="workspace_changed", paths=invalidated)
            active_claims = state.active_claims(now)
            owned = tuple(claim for claim in active_claims if claim.actor == actor)
            missing = tuple(
                change.path
                for change in changes
                if not any(_covers(claim.path, change.path) for claim in owned)
            )
            if missing:
                if not self._record_land_refusal(
                    state, snapshot, actor, operation, "unclaimed_paths", missing
                ):
                    continue
                raise LandingRefusedError(
                    "payload contains unclaimed paths",
                    reason="unclaimed_paths",
                    paths=list(missing),
                )
            fences = tuple(
                {
                    "path": change.path,
                    "claim_generation": next(
                        claim.generation for claim in owned if _covers(claim.path, change.path)
                    ),
                }
                for change in changes
            )
            context: dict[str, Any] = {
                "actor": actor,
                "target": target_ref,
                "operation": operation,
                "intent": intent_id,
                "wal_head": snapshot.head,
                "payload": [change.path for change in changes],
            }
            if checkpoint is not None:
                checkpoint("after_audit", context)
            current = self.repo.resolve_ref(target_ref, missing_ok=True)
            if current is None:
                raise GitError("landing target is unborn", reference=target_ref)
            if not self.repo.is_ancestor(session.base, current):
                invalidated = [change.path for change in changes]
                if not self._record_land_refusal(
                    state, snapshot, actor, operation, "target_diverged", invalidated
                ):
                    continue
                raise LandingRefusedError(
                    "landing target no longer descends from the session base",
                    reason="target_diverged",
                    paths=invalidated,
                )
            current_entries = self.repo.tree_entries(current)
            conflicts = tuple(
                change.path for change in changes if current_entries.get(change.path) != change.old
            )
            if conflicts:
                if not self._record_land_refusal(
                    state, snapshot, actor, operation, "payload_conflict", conflicts
                ):
                    continue
                raise LandingRefusedError(
                    "target changed on payload paths",
                    reason="payload_conflict",
                    paths=list(conflicts),
                )
            replacements = {change.path: change.new for change in changes}
            candidate_tree = self.repo.overlay_tree(current, replacements)
            candidate_entries = self.repo.tree_entries(candidate_tree)
            actual = self._changes(current_entries, candidate_entries)
            expected = tuple(
                _PayloadChange(change.path, current_entries.get(change.path), change.new)
                for change in changes
            )
            if actual != expected:
                raise GitError(
                    "candidate tree differs from the audited payload",
                    expected=[change.path for change in expected],
                    actual=[change.path for change in actual],
                )
            context["expected_head"] = current
            context["tree"] = candidate_tree
            if checkpoint is not None:
                checkpoint("after_tree", context)
            commit = self.repo.commit_tree(
                candidate_tree,
                self.repo.landing_message(
                    message or f"onetree land: {actor}", operation, intent_id, actor
                ),
                (current,),
            )
            context["candidate"] = commit
            payload_paths = [change.path for change in changes]
            try:
                configured_referee = referee.configuration(self.repo)
            except LandingRefusedError:
                if not self._record_land_refusal(
                    state, snapshot, actor, operation, "referee_config", payload_paths
                ):
                    continue
                raise LandingRefusedError("invalid referee configuration",
                                          reason="referee_config", paths=payload_paths) from None
            if configured_referee[0] is not None and any(
                pending.data["target"] == target_ref for pending in state.pending.values()
            ):
                if not self._record_land_refusal(
                    state, snapshot, actor, operation, "train_pending", payload_paths
                ):
                    continue
                raise LandingRefusedError("an earlier target intent is still pending",
                                          reason="train_pending", paths=payload_paths)
            try:
                gate_receipt = self.repo.audit_commit_payload(
                    commit, payload_paths, self.commit_payload
                )
            except LandingRefusedError as refusal:
                # The verdict is terminal for this candidate. WAL contention may
                # retry its recording, but must never rerun the gate or discard
                # its evidence. A recording failure remains visible to the caller.
                rejected = self.wal.document("refused", actor, {
                    "action": "land", "reason": str(refusal.details["reason"]),
                    "operation": operation, "paths": list(payload_paths),
                    "gate_receipt": refusal.details["gate_receipt"],
                })
                try:
                    self._append_document(rejected, what="payload gate refusal")
                except OnetreeError as recording_error:
                    refusal.details["recording_error"] = recording_error.payload()
                    raise refusal from recording_error
                raise
            try:
                referee_receipt = referee.run(
                    self.repo, commit, current, payload_paths, configured_referee
                )
            except LandingRefusedError as refusal:
                rejected = self.wal.document("refused", actor, {
                    "action": "land", "reason": str(refusal.details["reason"]),
                    "operation": operation, "paths": list(payload_paths),
                    "referee_receipt": refusal.details["referee_receipt"],
                })
                try:
                    self._append_document(rejected, what="referee refusal")
                except OnetreeError as recording_error:
                    refusal.details["recording_error"] = recording_error.payload()
                    raise refusal from recording_error
                raise
            if not referee.configuration_matches(self.repo, configured_referee):
                if not self._record_land_refusal(
                    state, snapshot, actor, operation, "referee_config_changed", payload_paths
                ):
                    continue
                raise LandingRefusedError("referee configuration changed during candidate audit",
                                          reason="referee_config_changed", paths=payload_paths)
            lander_start = _pid_start(os.getpid())
            lander_warnings = _liveness_warnings(lander_start)
            intent = self.wal.document(
                "land_intent",
                actor,
                {
                    "operation": operation,
                    "attempt": attempts + 1,
                    "target": target_ref,
                    "expected_head": current,
                    "candidate": commit,
                    "tree": candidate_tree,
                    "session_generation": session.generation,
                    # The incarnation performing THIS landing, which is not the
                    # session body: a CLI invocation can die with its session
                    # still running, and recovery must be able to tell "this
                    # landing is in flight" from "its owner is still logged in".
                    "lander": {
                        "pid": os.getpid(),
                        "start": lander_start,
                        "warnings": list(lander_warnings),
                    },
                    "fences": list(fences),
                    "payload": [change.event_payload() for change in changes],
                    "gate_receipt": gate_receipt,
                    "referee_receipt": referee_receipt,
                },
                event_id=intent_id,
            )
            if checkpoint is not None:
                checkpoint("before_intent", context)
            try:
                violation = self._workspace_violation(state)
            except LandingRefusedError:
                # The configuration was readable when the candidate was audited, so an
                # unreadable one here is a change, the same refusal the unprotected
                # path records below.
                if not self._record_land_refusal(
                    state, snapshot, actor, operation, "referee_config_changed", payload_paths
                ):
                    continue
                raise LandingRefusedError("referee configuration changed before admission",
                                          reason="referee_config_changed",
                                          paths=payload_paths) from None
            if violation is not None:
                if not self._record_land_refusal(
                    state, snapshot, actor, operation, "workspace_changed", payload_paths
                ):
                    continue
                raise LandingRefusedError(violation, reason="workspace_changed",
                                          paths=payload_paths)
            if not referee.configuration_matches(self.repo, configured_referee):
                if not self._record_land_refusal(
                    state, snapshot, actor, operation, "referee_config_changed", payload_paths
                ):
                    continue
                raise LandingRefusedError("referee configuration changed before admission",
                                          reason="referee_config_changed", paths=payload_paths)

            # Fence sample. Everything above was audited against a WAL read that
            # is now old; this is the last look at the session and its claims
            # before anything becomes visible to peers.
            landing_time = time.time()
            fenced_generations = {str(fence["claim_generation"]) for fence in fences}
            if not _session_alive(session, landing_time):
                if not self._record_land_refusal(
                    state, snapshot, actor, operation, "session_died_at_fence", payload_paths
                ):
                    continue
                raise LandingRefusedError(
                    "landing session died between its audit and the fence",
                    reason="session_died_at_fence",
                    paths=list(payload_paths),
                )
            expired_generations = {
                claim.generation for claim in owned
                if claim.generation in fenced_generations and claim.expires_at <= landing_time
            }
            expired = tuple(sorted(
                str(fence["path"]) for fence in fences
                if fence["claim_generation"] in expired_generations
            ))
            if expired:
                if not self._record_land_refusal(
                    state, snapshot, actor, operation, "claim_expired_at_fence", expired
                ):
                    continue
                raise LandingRefusedError(
                    "a fenced claim expired between its audit and the fence",
                    reason="claim_expired_at_fence",
                    paths=list(expired),
                )

            attempts += 1
            # Write-ahead step 1. The WAL CAS is also the fence: any release,
            # reclamation, registration change, or unrelated append since the
            # snapshot invalidates this attempt and forces a fresh audit.
            if self._try_append(state, snapshot, (intent,)) is None:
                continue
            if checkpoint is not None:
                checkpoint("after_intent", context)

            # Write-ahead step 2. A single-ref CAS, so a crash inside it leaves
            # the branch either moved or not, never half-moved.
            try:
                moved = self.repo.update_ref(target_ref, commit, current)
            except (GitError, RefLockedError) as error:
                # Both of these leave the branch where it was -- update_ref only
                # raises after confirming the ref still holds the expected value
                # -- so the intent is refusable, and leaving it pending instead
                # would fence this session's own claims against release until an
                # operator ran recovery by hand.
                self._close_intent(
                    actor,
                    operation,
                    intent_id,
                    "target_ref_locked"
                    if isinstance(error, RefLockedError)
                    else "branch_cas_failed",
                    quiet=True,
                )
                raise
            if not moved:
                self._close_intent(actor, operation, intent_id, "target_moved")
                continue
            if checkpoint is not None:
                checkpoint("after_branch", context)

            # Write-ahead step 3. If this never runs, the intent stays pending
            # with its candidate already on the branch, which is exactly the
            # state recovery completes.
            self._mark_landed(
                actor, operation, intent_id, target_ref, current, commit, candidate_tree,
                payload_paths,
            )
            if checkpoint is not None:
                checkpoint("after_landed", context)
            return LandResult(
                commit,
                candidate_tree,
                target_ref,
                attempts,
                tuple(payload_paths),
                operation,
                intent_id,
                lander_warnings,
                gate_receipt,
                referee_receipt,
            )
        raise GitError("landing CAS retry limit exceeded", retries=MAX_CAS_RETRIES)

    def _close_intent(
        self,
        actor: str,
        operation: str,
        intent_id: str,
        reason: str,
        *,
        quiet: bool = False,
    ) -> None:
        document = self.wal.document(
            "refused",
            actor,
            {
                "action": "land",
                "reason": reason,
                "operation": operation,
                "intent": intent_id,
                "paths": [],
            },
        )
        if not quiet:
            self._append_document(document, what="landing refusal")
            return
        # Closing on the way out of a failure must not replace the failure being
        # reported. If the WAL will not take the refusal either, the intent stays
        # pending -- which `onetree wal` now names on its human line.
        with contextlib.suppress(OnetreeError):
            self._append_document(document, what="landing refusal")

    def _mark_landed(
        self,
        actor: str,
        operation: str,
        intent_id: str,
        target_ref: str,
        expected_head: str,
        commit: str,
        tree: str,
        payload_paths: Sequence[str],
    ) -> None:
        document = self.wal.document(
            "landed",
            actor,
            {
                "operation": operation,
                "intent": intent_id,
                "target": target_ref,
                "expected_head": expected_head,
                "commit": commit,
                "tree": tree,
                "payload": list(payload_paths),
                "recovered": False,
            },
        )
        try:
            self._append_document(document, what="landed mark")
        except WalAppendRefusedError as error:
            # Recovery may have completed this very landing while the mark was in
            # flight. An identical record already in the WAL is this call's own
            # outcome, not a conflict -- and it is the reason the refusal is
            # typed separately from corruption.
            if self._landing_recorded(operation, document):
                return
            raise WalAppendRefusedError(
                "the candidate is on the branch but the WAL refused its landed mark; "
                "run `onetree wal --recover`",
                operation=operation,
                commit=commit,
                target=target_ref,
                cause=str(error),
            ) from error
        except GitError as error:
            raise GitError(
                "the candidate is on the branch but its landed mark could not be "
                "appended; run `onetree wal --recover`",
                operation=operation,
                commit=commit,
                target=target_ref,
            ) from error

    def _landing_recorded(self, operation: str, document: Mapping[str, Any]) -> bool:
        """Is this exact landing already recorded, field for field, by someone else?"""

        recorded = self._fold(self.wal.read()).landed_operations.get(operation)
        if recorded is None:
            return False
        return all(
            recorded.data.get(name) == document["data"].get(name)
            for name, _ in (*_LANDED_INTENT_FIELDS, ("payload", "payload"))
        )

    def _verify_landed(self, state: _FoldedState) -> None:
        if not state.landed:
            return
        # One batch read establishes, for every ordinary commit the records
        # name, its parents, its tree and which recorded operations its message
        # carries; each body is reduced as it is read and not kept. A record
        # whose object Git did not hand back as a commit -- absent,
        # uninflatable, a blob, a tag, a name that is no object ID -- keeps the
        # per-object probes, the only readers that separate "not a commit I
        # have" (WAL corruption) from the plumbing failing (a GitError). Nothing
        # outlives this call: every mutation and inspection re-reads the store,
        # so an object that vanishes under a stable WAL head is refused by the
        # next call.
        requests: dict[str, set[str]] = {}
        for event in state.landed:
            named = event.data.get("commit")
            if not isinstance(named, str):
                continue
            operations = requests.setdefault(named, set())
            recorded_operation = event.data.get("operation")
            if event.kind == "adopted" and not event.legacy and isinstance(recorded_operation, str):
                operations.add(recorded_operation)
        evidence = self.repo.landed_evidence(requests, reference=WAL_REF)
        for event in state.landed:
            commit = _string(event.data, "commit", event)
            expected_tree = _string(event.data, "tree", event)
            known = evidence.get(commit)
            if known is None:
                try:
                    self.repo.require_commit(commit)
                except RefCorruptError as error:
                    # Only Git's own answer -- "this object is not a commit I have" --
                    # is WAL corruption. A GitError here is the plumbing failing, and
                    # blaming the WAL for it sends the reader to the wrong repair.
                    raise WalCorruptError(
                        "landed commit does not resolve; restore or fetch the recorded commit "
                        "object before running recovery again",
                        oid=event.oid,
                        commit=commit,
                    ) from error
            expected_parent = _string(event.data, "expected_head", event)
            parents = self.repo.commit_parents(commit) if known is None else known.parents
            if event.kind == "adopted":
                actual_expected = parents[0] if parents else self.repo.zero_oid
                parents_match = actual_expected == expected_parent
            else:
                parents_match = parents == (expected_parent,)
            if not parents_match:
                raise WalCorruptError("landed commit parent differs from WAL", oid=event.oid)
            actual_tree = (
                self.repo.git_text("show", "-s", "--format=%T", commit)
                if known is None
                else known.tree
            )
            if actual_tree != expected_tree:
                raise WalCorruptError("landed commit tree differs from WAL", oid=event.oid)
            if event.kind == "adopted" and event.legacy:
                # A v=1 record is verified under the writer that made it: the
                # commit's trailer block, parsed and comma-joined as 9d2ef3b did,
                # must equal the stored scalar exactly. The scalar is never split.
                operation = _string(event.data, "operation", event)
                if self.repo.legacy_landing_operation(commit) != operation:
                    raise WalCorruptError(
                        "legacy adopted commit does not carry the recorded landing trailer "
                        "under the historical writer's semantics",
                        oid=event.oid,
                        commit=commit,
                    )
            elif event.kind == "adopted":
                operation = _string(event.data, "operation", event)
                carried = (
                    self.repo.commit_has_landing_operation(commit, operation)
                    if known is None
                    else operation in known.operations
                )
                if not carried:
                    raise WalCorruptError(
                        "adopted commit does not carry the recorded landing trailer",
                        oid=event.oid,
                        commit=commit,
                    )

    def _divergences(
        self,
        state: _FoldedState,
        heads: _AuditHeads,
    ) -> tuple[Divergence, ...]:
        """Compare every target the WAL knows against what the WAL says landed.

        Two directions matter. The WAL can claim a landing the branch does not
        contain, and the branch can carry a broker landing the WAL never
        recorded — the shape a pre-write-ahead crash produced. Ordinary hand
        commits are still permitted on these branches, so the branch-side test
        keys on the landing trailer every broker candidate carries rather than
        on mere presence of an unrecognised commit.

        The branch-side walk starts at the head and stops only when it runs out
        of history. It used to be bounded by every position the WAL had seen,
        which retired the finding the moment any session registered on top of
        the unrecorded commit or landed above it, and then by a fixed
        genealogical distance below the first position it ever saw, which
        retired it behind that many permitted hand commits. An unrecorded
        landing that stops being reported by being used is worse than one that
        was never detected; see ``_scan_floor`` for why no floor survives.
        """

        found: list[Divergence] = []
        for target, head in heads:
            landed_here = (
                []
                if target == "HEAD"
                else [event for event in state.landed if event.data.get("target") == target]
            )
            if head is None:
                found.extend(
                    Divergence(
                        "target_missing",
                        target,
                        "the WAL recorded a landing on a target ref that no longer exists",
                        commit=str(event.data.get("commit")),
                        operation=str(event.data.get("operation")),
                    )
                    for event in landed_here
                )
                continue
            for event in landed_here:
                commit = str(event.data.get("commit"))
                if not self.repo.is_ancestor(commit, head):
                    found.append(
                        Divergence(
                            "landed_not_on_target",
                            target,
                            "the WAL records this commit as landed but the branch does not "
                            "contain it",
                            commit=commit,
                            operation=str(event.data.get("operation")),
                        )
                    )
            for operation, intent in sorted(state.refused_intents.items()):
                if intent.data.get("target") != target:
                    continue
                candidate = str(intent.data.get("candidate"))
                if self.repo.is_ancestor(candidate, head):
                    found.append(
                        Divergence(
                            "refused_candidate_on_target",
                            target,
                            "the WAL refused this candidate but the branch contains it",
                            commit=candidate,
                            operation=operation,
                        )
                    )
        found.extend(self._unrecorded_landings(state, heads))
        return tuple(found)

    def _audit_marked_commits(
        self,
        head: str,
        excludes: Sequence[str],
        recorded: set[tuple[str, str]],
        pending: Mapping[str, EventRecord],
    ) -> Iterator[MarkedCommit]:
        """Read one stable head while retaining only audit-relevant identities."""

        yield from self.repo.audit_marked_commits(
            head,
            excludes,
            recorded=recorded,
            pending=pending.keys(),
        )

    def _scan_floor(self, state: _FoldedState, target: str) -> tuple[str, ...]:
        """Git-visible history the branch-side audit may skip: none of it.

        Every exclusion this walk can express is ``git log ^X``, which drops X
        *and its entire ancestry*. The WAL has audited none of that ancestry, so
        any non-empty floor hides an unbounded amount of history behind one
        commit. That makes a floor unsound by construction rather than merely
        shallow, and two rounds measured the same class at successive depths:
        lead (c) found a crash-left landing masked by one permitted hand commit
        below first sight, and the round after it found the grandparent floor
        that replaced it masked by two and by three. A fixed genealogical
        distance d is always defeated by d hand commits, so the depth was never
        the defect.

        Retreating to the honest boundary is not available either: lead (c)
        settled that the commit below the WAL's first sighting of a branch is in
        scope, and that is precisely the history a first-sight floor drops. What
        is left is the empty floor. The history walk is one streamed Git process,
        so a complete walk costs linear traversal rather than repeated pages.

        A shallow boundary is different: the omitted objects are not visible to
        any local history walk. Inspection reports that boundary explicitly and
        every mutating entry point refuses until the repository is deepened.

        A repository holding broker landings its WAL does not record is not
        allowed to append a new event and thereby adopt that history. The same
        streamed trailer audit enforces that property for every repository
        shape; ``wal --adopt`` is the explicit, local operator escape.
        """

        return ()

    def inspect_wal(self, target: str | None = None) -> WalReport:
        """Inspect without mutating, including states that mutation must refuse."""

        for _ in range(MAX_CAS_RETRIES):
            snapshot = self.wal.read()
            state = self._fold(snapshot)
            self._verify_landed(state)
            heads = self._audit_heads(state, target)
            targets = self._audit_target_names(heads)
            boundaries: list[Divergence] = []
            graft_file = self.repo.nonempty_graft_file()
            if graft_file is not None:
                boundaries.extend(
                    Divergence(
                        "legacy_grafts",
                        audit_target,
                        f"the non-empty legacy graft file {graft_file} can hide history and "
                        "must be converted or removed before mutation",
                    )
                    for audit_target in targets
                )
            if self.repo.is_shallow():
                boundaries.extend(
                    Divergence(
                        "shallow_history",
                        audit_target,
                        "the repository is shallow, so history below its boundary was not audited",
                    )
                    for audit_target in targets
                )
            divergences = (*boundaries, *self._divergences(state, heads))
            try:
                violation = self._workspace_violation(state)
            except LandingRefusedError as refusal:
                divergences = (*divergences, Divergence("referee_config", "HEAD", str(refusal)))
            else:
                if violation is not None:
                    divergences = (
                        *divergences, Divergence("workspace_changed", "HEAD", violation)
                    )
            now = time.time()
            sessions = tuple(
                sorted(actor for actor in state.sessions if state.session_active(actor, now))
            )
            claims = tuple(sorted({claim.path for claim in state.active_claims(now)}))
            locks = self._held_locks(state)
            temp_indexes = self.repo.temp_indexes()
            if self.wal.head() != snapshot.head or self._audit_heads(state, target) != heads:
                continue
            return WalReport(
                snapshot.head,
                snapshot.events,
                not state.pending and not divergences,
                len(state.pending),
                sessions,
                claims,
                divergences,
                locks,
                temp_indexes,
            )
        raise GitError("audit snapshot retry limit exceeded", retries=MAX_CAS_RETRIES)

    def adopt_landings(self, target: str | None = None) -> AdoptionResult:
        """Explicitly record every unrecorded landing found by the local audit.

        Adoption is idempotent: an audit that finds nothing to adopt is the
        ordinary second-run outcome, not a failure.
        """

        target_ref = None if target is None else self.repo.validate_branch_ref(target)
        for _ in range(MAX_CAS_RETRIES):
            snapshot = self.wal.read()
            state = self._fold(snapshot)
            # Every recorded landing is verified before the audit may rely on
            # it; a legacy record in particular counts only once its commit is
            # shown to carry it.
            self._verify_landed(state)
            heads = self._audit_heads(state, target_ref)
            targets = self._audit_target_names(heads)
            self._refuse_legacy_grafts(targets)
            if self.repo.is_shallow():
                raise ShallowHistoryError(
                    "the repository is shallow, so hidden landings cannot be adopted",
                    targets=list(targets),
                    repair="deepen the repository before adopting",
                )
            adoption_heads = (
                heads
                if target_ref is None
                else tuple(item for item in heads if item[0] == target_ref)
            )
            unrecorded = self._unrecorded_landings(state, adoption_heads)
            if not unrecorded:
                return AdoptionResult((), self.inspect_wal(target_ref))
            documents: list[dict[str, Any]] = []
            for item in unrecorded:
                if item.commit is None or item.operation is None:
                    raise AssertionError("unrecorded landing has no commit or operation")
                parents = self.repo.commit_parents(item.commit)
                tree = self.repo.git_text("show", "-s", "--format=%T", item.commit)
                documents.append(
                    self.wal.document(
                        "adopted",
                        "operator",
                        {
                            "operation": item.operation,
                            "target": item.target,
                            "expected_head": parents[0] if parents else self.repo.zero_oid,
                            "commit": item.commit,
                            "tree": tree,
                        },
                    )
                )
            records = self._try_append(state, snapshot, documents)
            if records is None:
                continue
            return AdoptionResult(records, self.inspect_wal(target_ref))
        raise GitError("landing adoption CAS retry limit exceeded", retries=MAX_CAS_RETRIES)

    def _held_locks(self, state: _FoldedState) -> tuple[str, ...]:
        """Every ref lock, WAL and target, that will refuse the next write."""

        return tuple(
            str(path)
            for path in dict.fromkeys(
                lock
                for reference in (WAL_REF, *sorted(state.target_positions))
                for lock in self.repo.ref_lock_paths(reference)
            )
            if path.exists()
        )

    @staticmethod
    def _landing_in_flight(state: _FoldedState, intent: EventRecord, now: float) -> bool:
        """Is the process that wrote this intent still able to move the branch?

        Write-ahead made "intent appended, branch not yet moved" a normal
        transient state of every healthy landing, so refusing an intent purely
        because its candidate has not landed yet refuses live work: the lander
        then wins its branch CAS and marks a landing whose intent the WAL has
        recorded as refused. The bar is the gc bar -- (pid, birth tick)
        liveness -- asked of the landing rather than of the session, because a
        CLI invocation dies while its session body keeps running. An intent with
        no lander recorded falls back to the owning session, which is the only
        thing left to ask.
        """

        lander = intent.data.get("lander")
        if isinstance(lander, dict):
            start = lander.get("start")
            return _pid_alive(int(lander["pid"]), start if isinstance(start, int) else None)
        session = state.sessions.get(intent.actor)
        if session is None or session.generation != intent.data.get("session_generation"):
            return False
        return _session_alive(session, now)

    def _recovery_documents(
        self,
        state: _FoldedState,
        now: float,
    ) -> tuple[list[dict[str, Any]], int, int, int]:
        """Resolve every pending intent this pass may touch, in one batch.

        One CAS per intent would spend the retry budget on progress: thirty-three
        stranded intents could never be recovered however many times recovery
        ran. They are resolved together instead, so the budget covers contention
        only.
        """

        documents: list[dict[str, Any]] = []
        repaired = refused = in_flight = 0
        for operation, intent in sorted(state.pending.items()):
            target = _string(intent.data, "target", intent)
            candidate = _string(intent.data, "candidate", intent)
            target_head = self.repo.resolve_ref(target, missing_ok=True)
            if target_head is not None and self.repo.is_ancestor(candidate, target_head):
                documents.append(
                    self.wal.document(
                        "landed",
                        intent.actor,
                        {
                            "operation": operation,
                            "intent": intent.event_id,
                            "target": target,
                            "expected_head": intent.data.get("expected_head"),
                            "commit": candidate,
                            "tree": intent.data.get("tree"),
                            "payload": [
                                item.get("path")
                                for item in intent.data.get("payload", [])
                                if isinstance(item, dict)
                            ],
                            "recovered": True,
                        },
                    )
                )
                repaired += 1
            elif self._landing_in_flight(state, intent, now):
                in_flight += 1
            else:
                documents.append(
                    self.wal.document(
                        "refused",
                        intent.actor,
                        {
                            "action": "land",
                            "reason": "incomplete_intent",
                            "operation": operation,
                            "intent": intent.event_id,
                            "paths": [],
                        },
                    )
                )
                refused += 1
        return documents, repaired, refused, in_flight

    def recover(self, target: str | None = None) -> RecoveryResult:
        # A ref lock left by a crashed process names no owner, so it cannot be
        # liveness-checked and must not be deleted: a live peer may hold it.
        # Recovery names it in a typed refusal instead of letting the next WAL
        # append surface it as an unattributed Git failure.
        held = [str(lock) for lock in self.repo.ref_lock_paths(WAL_REF) if lock.exists()]
        if held:
            raise RefLockedError(
                "the WAL ref is locked; if no process holds it, remove the lock by hand",
                reference=WAL_REF,
                lock=held[0],
                locks=held,
            )
        repaired = 0
        refused = 0
        for _ in range(MAX_CAS_RETRIES):
            snapshot = self._read_wal(target)
            state = self._fold(snapshot)
            documents, batch_repaired, batch_refused, in_flight = self._recovery_documents(
                state, time.time()
            )
            if documents:
                if self._try_append(state, snapshot, tuple(documents)) is None:
                    continue
                repaired += batch_repaired
                refused += batch_refused
                continue
            # Locks are residue, not inconsistency, but they are what refuses the
            # next landing -- including on the target branch, which recovery used
            # to leave unexamined while reporting an all-clear.
            locks = self._held_locks(state)
            self._verify_landed(state)
            heads = self._audit_heads(state, target)
            divergences = self._divergences(state, heads)
            try:
                violation = self._workspace_violation(state)
            except LandingRefusedError as refusal:
                # The workspace cannot be audited until the setting is readable.
                # On its own that is the operator's typo, not a branch that
                # diverged from the WAL: refuse by type and record nothing, so
                # correcting the setting is the whole remedy and no
                # `branch_diverged` row outlives it. Beside a real divergence it
                # is one more thing the operator must see, not a reason to hide
                # the divergence recovery has already computed.
                if not divergences:
                    raise LandingRefusedError(
                        f"recovery cannot audit the protected workspace: {refusal}",
                        reason="referee_config", paths=[], repaired=repaired, refused=refused,
                        pending=in_flight, locks=list(locks),
                    ) from None
                divergences = (*divergences, Divergence("referee_config", "HEAD", str(refusal)))
            else:
                if violation is not None:
                    divergences = (
                        *divergences, Divergence("workspace_changed", "HEAD", violation)
                    )
            if self.wal.head() != snapshot.head or self._audit_heads(state, target) != heads:
                continue
            if not divergences:
                if in_flight:
                    return RecoveryResult(False, in_flight, repaired, refused, locks)
                return RecoveryResult(True, 0, repaired, refused, locks)
            payloads = [item.payload() for item in divergences]
            document = self.wal.document(
                "refused",
                "recovery",
                {
                    "action": "recover",
                    "reason": "branch_diverged",
                    "divergences": payloads,
                },
            )
            # One refusal per divergence set, not per probe: re-recording an
            # unchanged divergence grows the WAL without bound and fences every
            # concurrently landing peer into a fresh audit for nothing.
            key = _divergence_key(payloads, EventRecord("(unwritten)", (), document))
            unrecorded = key not in state.diverged_refusals
            if unrecorded and self._try_append(state, snapshot, (document,)) is None:
                continue
            raise WalDivergedError(
                "the branch and the WAL disagree about what the broker landed",
                divergences=payloads,
                pending=in_flight,
                repaired=repaired,
                refused=refused,
                locks=list(locks),
            )
        raise GitError("recovery CAS retry limit exceeded", retries=MAX_CAS_RETRIES)

    def _abandoned_temp_indexes(self) -> tuple[TempIndex, ...]:
        """Select temp-index residue whose creating process incarnation is gone.

        A file whose owner tag does not parse is reported by ``inspect`` but is
        never swept here: without an owner there is no liveness question to ask,
        and deleting an index a live process is mid-write on is worse than
        leaving it.
        """

        return tuple(
            residue
            for residue in self.repo.temp_indexes()
            if residue.pid is not None and not _pid_alive(residue.pid, residue.start)
        )

    def gc(self, actor: str) -> GcResult:
        actor = _validate_actor(actor)
        for _ in range(MAX_CAS_RETRIES):
            snapshot = self._read_wal()
            state = self._fold(snapshot)
            now = time.time()
            abandoned = self._abandoned_temp_indexes()
            active_claim_generations = {claim.generation for claim in state.active_claims(now)}
            stale_claims = tuple(
                sorted(
                    generation
                    for generation in state.claims
                    if generation not in active_claim_generations
                )
            )
            stale_sessions = tuple(
                sorted(
                    session.generation
                    for session in state.sessions.values()
                    if not _session_alive(session, now)
                )
            )
            document = self.wal.document(
                "gc",
                actor,
                {
                    "claim_generations": list(stale_claims),
                    "session_generations": list(stale_sessions),
                    "temp_indexes": [residue.path.name for residue in abandoned],
                },
            )
            records = self._try_append(state, snapshot, (document,))
            if records is not None:
                for residue in abandoned:
                    residue.path.unlink(missing_ok=True)
                return GcResult(
                    records[0].kind,
                    len(stale_claims),
                    len(stale_sessions),
                    len(abandoned),
                )
        raise GitError("gc CAS retry limit exceeded")
