"""Round A coordination state, claims, payload audit, and CAS landing."""

from __future__ import annotations

import contextlib
import math
import os
import re
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from onetree.errors import (
    ClaimConflictError,
    GitError,
    IdentityError,
    InvalidPathError,
    LandingRefusedError,
    OnetreeError,
    RefCorruptError,
    RefLockedError,
    ReleaseRefusedError,
    WalAppendRefusedError,
    WalCorruptError,
    WalDivergedError,
)
from onetree.git import Repository, TempIndex, TreeEntry
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
    #: Positions the WAL has observed for each target ref.
    target_positions: dict[str, set[str]]
    #: The FIRST position the WAL ever observed for each target ref, in event
    #: order. It is the only honest floor for a branch-side audit: below it the
    #: WAL never saw this branch and never claimed to.
    target_origin: dict[str, str]
    #: Divergence sets recovery has already recorded a refusal for, so probing a
    #: diverged repository does not append one event per probe.
    diverged_refusals: set[tuple[tuple[str, ...], ...]]
    event_ids: set[str] = field(default_factory=set)
    session_generations: set[str] = field(default_factory=set)
    claim_generations: set[str] = field(default_factory=set)

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
            target_origin={},
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
            target_origin=dict(self.target_origin),
            diverged_refusals=set(self.diverged_refusals),
            event_ids=set(self.event_ids),
            session_generations=set(self.session_generations),
            claim_generations=set(self.claim_generations),
        )

    def observe_position(self, target: str, *positions: str) -> None:
        seen = self.target_positions.setdefault(target, set())
        for position in positions:
            self.target_origin.setdefault(target, position)
            seen.add(position)

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
    if not isinstance(value, dict) or frozenset(value) != {"pid", "start"}:
        raise WalCorruptError("land_intent.lander is invalid", oid=event.oid)
    pid = value.get("pid")
    start = value.get("start")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise WalCorruptError("land_intent.lander is invalid", oid=event.oid)
    if start is not None and (isinstance(start, bool) or not isinstance(start, int) or start <= 0):
        raise WalCorruptError("land_intent.lander is invalid", oid=event.oid)


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

    def __init__(self, repository: Path | str) -> None:
        self.repo = Repository(repository)
        self.wal = Wal(self.repo)

    def _canonical_path(self, raw_path: str) -> str:
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            raise InvalidPathError(
                "claim paths must be absolute",
                path=raw_path,
                reason="relative",
            )
        try:
            resolved = candidate.resolve(strict=True)
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
            except WalCorruptError as error:
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
            state.pending[operation] = event
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
            if action == "recover" and reason == "branch_diverged":
                state.diverged_refusals.add(_divergence_key(data.get("divergences"), event))
        else:  # all event kinds are covered above
            raise AssertionError(f"unhandled WAL event: {event.kind}")

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
            self.repo.head_ref() if target is None else self.repo.validate_branch_ref(target)
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
            snapshot = self.wal.read()
            state = self._fold(snapshot)
            existing = self._active_session(state, actor, time.time())
            if existing is not None:
                if (
                    existing.pid == body_pid
                    and existing.pid_start == process_start
                    and existing.target == target_ref
                ):
                    self.repo.ensure_index(actor, existing.base)
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
            snapshot = self.wal.read()
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

    def check(self, actor: str, path: str) -> CheckResult:
        actor = _validate_actor(actor)
        canonical = self._canonical_path(path)
        state = self._fold(self.wal.read())
        relevant = tuple(
            claim for claim in state.active_claims(time.time()) if _overlaps(claim.path, canonical)
        )
        holders = tuple(claim for claim in relevant if claim.actor != actor)
        owned = tuple(claim for claim in relevant if claim.actor == actor)
        return CheckResult(canonical, bool(holders), holders, owned)

    def release(self, actor: str, paths: Sequence[str] = ()) -> ReleaseResult:
        actor = _validate_actor(actor)
        canonical = tuple(sorted({self._canonical_path(path) for path in paths}))
        for _ in range(MAX_CAS_RETRIES):
            now = time.time()
            snapshot = self.wal.read()
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
        """CAS-append one prepared document, re-reading the head on every loss."""

        for _ in range(MAX_CAS_RETRIES):
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
        chosen so that each crash window leaves a state recovery can name:
        either an intent with no branch move (recovery refuses it) or a branch
        move whose intent is already recorded (recovery completes it).
        """

        actor = _validate_actor(actor)
        target_ref = (
            self.repo.head_ref() if target is None else self.repo.validate_branch_ref(target)
        )
        attempts = 0
        for _ in range(MAX_CAS_RETRIES):
            now = time.time()
            snapshot = self.wal.read()
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
            index = self.repo.ensure_index(actor, session.base)
            session_tree = self.repo.write_tree(index)
            base_entries = self.repo.tree_entries(session.base)
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
                if not self._record_land_refusal(
                    state, snapshot, actor, operation, "target_diverged"
                ):
                    continue
                raise LandingRefusedError(
                    "landing target no longer descends from the session base",
                    reason="target_diverged",
                    paths=[],
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
                    "lander": {"pid": os.getpid(), "start": _pid_start(os.getpid())},
                    "fences": list(fences),
                    "payload": [change.event_payload() for change in changes],
                },
                event_id=intent_id,
            )
            if checkpoint is not None:
                checkpoint("before_intent", context)

            # Fence sample. Everything above was audited against a WAL read that
            # is now old; this is the last look at the session and its claims
            # before anything becomes visible to peers.
            landing_time = time.time()
            fenced_generations = {str(fence["claim_generation"]) for fence in fences}
            if not _session_alive(session, landing_time):
                if not self._record_land_refusal(
                    state, snapshot, actor, operation, "session_died_at_fence"
                ):
                    continue
                raise LandingRefusedError(
                    "landing session died between its audit and the fence",
                    reason="session_died_at_fence",
                    paths=list(payload_paths),
                )
            expired = tuple(
                sorted(
                    claim.path
                    for claim in owned
                    if claim.generation in fenced_generations and claim.expires_at <= landing_time
                )
            )
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
        for event in state.landed:
            commit = _string(event.data, "commit", event)
            expected_tree = _string(event.data, "tree", event)
            try:
                self.repo.require_commit(commit)
            except RefCorruptError as error:
                # Only Git's own answer -- "this object is not a commit I have" --
                # is WAL corruption. A GitError here is the plumbing failing, and
                # blaming the WAL for it sends the reader to the wrong repair.
                raise WalCorruptError("landed commit does not resolve", oid=event.oid) from error
            expected_parent = _string(event.data, "expected_head", event)
            if self.repo.commit_parents(commit) != (expected_parent,):
                raise WalCorruptError("landed commit parent differs from WAL", oid=event.oid)
            actual_tree = self.repo.git_text("show", "-s", "--format=%T", commit)
            if actual_tree != expected_tree:
                raise WalCorruptError("landed commit tree differs from WAL", oid=event.oid)

    def _divergences(self, state: _FoldedState) -> tuple[Divergence, ...]:
        """Compare every target the WAL knows against what the WAL says landed.

        Two directions matter. The WAL can claim a landing the branch does not
        contain, and the branch can carry a broker landing the WAL never
        recorded — the shape a pre-write-ahead crash produced. Ordinary hand
        commits are still permitted on these branches, so the branch-side test
        keys on the landing trailer every broker candidate carries rather than
        on mere presence of an unrecognised commit.

        The branch-side walk is bounded by the WAL's recorded landing chain: it
        starts at the head and stops below the first position this WAL ever
        observed for the target. Bounding it by every position the WAL has *since*
        seen — which is what it used to do — retires the finding the moment any
        session registers on top of the unrecorded commit or lands above it, and
        an unrecorded landing that stops being reported by being used is worse
        than one that was never detected.
        """

        found: list[Divergence] = []
        for target in sorted(state.target_positions):
            head = self.repo.resolve_ref(target, missing_ok=True)
            landed_here = [
                event for event in state.landed if event.data.get("target") == target
            ]
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
            scan = self.repo.marked_commits(target, self._scan_floor(state, target))
            if scan.truncated:
                found.append(
                    Divergence(
                        "scan_truncated",
                        target,
                        "the branch-side audit stopped at the scan limit "
                        f"({scan.limit} commits) before reaching the WAL's first recorded "
                        "position, so an unrecorded landing below it is neither confirmed "
                        "nor ruled out",
                    )
                )
            for marked in scan.commits:
                operation = marked.operation or ""
                if not operation:
                    continue
                if (
                    operation in state.landed_operations
                    or operation in state.pending
                    or operation in state.refused_intents
                ):
                    continue
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

    def _scan_floor(self, state: _FoldedState, target: str) -> tuple[str, ...]:
        """History the branch-side audit may skip: below the WAL's first sight.

        The floor is the parents of that first position rather than the position
        itself, so the position is inside the walk. It has to be: when a session
        registers on top of a crash-left commit, that commit *is* the WAL's first
        position, and excluding it is exactly how the finding disappeared.
        """

        origin = state.target_origin.get(target)
        if origin is None or not self.repo.existing_commits((origin,)):
            return ()
        return self.repo.existing_commits(self.repo.commit_parents(origin))

    def inspect_wal(self) -> WalReport:
        snapshot = self.wal.read()
        state = self._fold(snapshot)
        self._verify_landed(state)
        divergences = self._divergences(state)
        now = time.time()
        sessions = tuple(
            sorted(actor for actor in state.sessions if state.session_active(actor, now))
        )
        claims = tuple(sorted({claim.path for claim in state.active_claims(now)}))
        locks = self._held_locks(state)
        return WalReport(
            snapshot.head,
            snapshot.events,
            not state.pending and not divergences,
            len(state.pending),
            sessions,
            claims,
            divergences,
            locks,
            self.repo.temp_indexes(),
        )

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

    def recover(self) -> RecoveryResult:
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
            snapshot = self.wal.read()
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
            if in_flight:
                return RecoveryResult(False, in_flight, repaired, refused, locks)
            self._verify_landed(state)
            divergences = self._divergences(state)
            if not divergences:
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
            snapshot = self.wal.read()
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
