"""Round A coordination state, claims, payload audit, and CAS landing."""

from __future__ import annotations

import math
import os
import re
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from onetree.errors import (
    ClaimConflictError,
    GitError,
    IdentityError,
    InvalidPathError,
    LandingRefusedError,
    RefCorruptError,
    RefLockedError,
    ReleaseRefusedError,
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
    consistent: bool
    pending: int
    repaired: int
    refused: int
    diverged: int = 0


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
    #: Positions the WAL has observed for each target ref. They bound the
    #: history a divergence scan has to walk.
    target_positions: dict[str, set[str]]

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
        state = _FoldedState(
            sessions={},
            claims={},
            pending={},
            landed=[],
            landed_operations={},
            refused_intents={},
            refused_operations=set(),
            target_positions={},
        )
        event_ids: set[str] = set()
        session_generations: set[str] = set()
        claim_generations: set[str] = set()
        for event in snapshot.events:
            data = event.data
            if event.event_id in event_ids:
                raise WalCorruptError("duplicate WAL event id", oid=event.oid)
            event_ids.add(event.event_id)
            if event.kind == "session_register":
                _validate_actor(event.actor)
                generation = _string(data, "generation", event)
                if generation in session_generations:
                    raise WalCorruptError("duplicate session generation", oid=event.oid)
                session_generations.add(generation)
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
                state.target_positions.setdefault(target, set()).add(base)
            elif event.kind == "claim":
                generation = _string(data, "generation", event)
                if generation in claim_generations:
                    raise WalCorruptError("duplicate claim generation", oid=event.oid)
                claim_generations.add(generation)
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
                state.target_positions.setdefault(intent_target, set()).add(intent_head)
                self.repo.validate_oid(_string(data, "candidate", event), label="candidate")
                self.repo.validate_oid(_string(data, "tree", event), label="candidate tree")
                _string(data, "session_generation", event)
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
                    data.get(field) != intent.data.get(source)
                    for field, source in (
                        ("commit", "candidate"),
                        ("target", "target"),
                        ("expected_head", "expected_head"),
                        ("tree", "tree"),
                    )
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
                positions = state.target_positions.setdefault(landed_target, set())
                positions.update((landed_head, landed_commit))
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
                _string(data, "action", event)
                _string(data, "reason", event)
            else:  # all event kinds are covered above
                raise AssertionError(f"unhandled WAL event: {event.kind}")
        return state

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
            if self.wal.try_append((document,), snapshot.head) is not None:
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
                if self.wal.try_append((refusal,), snapshot.head) is None:
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
            if self.wal.try_append(documents, snapshot.head) is not None:
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
                if self.wal.try_append((refusal,), snapshot.head) is None:
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
            if self.wal.try_append((release,), snapshot.head) is not None:
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
        return self.wal.try_append((document,), snapshot.head) is not None

    def _append_document(self, document: Mapping[str, Any], *, what: str) -> EventRecord:
        """CAS-append one prepared document, re-reading the head on every loss."""

        for _ in range(MAX_CAS_RETRIES):
            snapshot = self.wal.read()
            records = self.wal.try_append((document,), snapshot.head)
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
                if not self._record_land_refusal(snapshot, actor, operation, "session_inactive"):
                    continue
                raise LandingRefusedError(
                    "landing session is inactive",
                    reason="session_inactive",
                    paths=[],
                )
            if session.target != target_ref:
                if not self._record_land_refusal(snapshot, actor, operation, "wrong_target"):
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
                if not self._record_land_refusal(snapshot, actor, operation, "nothing_staged"):
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
                    snapshot, actor, operation, "unclaimed_paths", missing
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
                if not self._record_land_refusal(snapshot, actor, operation, "target_diverged"):
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
                    snapshot, actor, operation, "payload_conflict", conflicts
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
                    snapshot, actor, operation, "session_died_at_fence"
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
                    snapshot, actor, operation, "claim_expired_at_fence", expired
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
            if self.wal.try_append((intent,), snapshot.head) is None:
                continue
            if checkpoint is not None:
                checkpoint("after_intent", context)

            # Write-ahead step 2. A single-ref CAS, so a crash inside it leaves
            # the branch either moved or not, never half-moved.
            if not self.repo.update_ref(target_ref, commit, current):
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

    def _close_intent(self, actor: str, operation: str, intent_id: str, reason: str) -> None:
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
        except GitError as error:
            raise GitError(
                "the candidate is on the branch but its landed mark could not be "
                "appended; run `onetree wal --recover`",
                operation=operation,
                commit=commit,
                target=target_ref,
            ) from error

    def _verify_landed(self, state: _FoldedState) -> None:
        for event in state.landed:
            commit = _string(event.data, "commit", event)
            expected_tree = _string(event.data, "tree", event)
            try:
                self.repo.require_commit(commit)
            except (GitError, RefCorruptError) as error:
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
            excludes = self.repo.existing_commits(state.target_positions[target])
            for marked in self.repo.marked_commits(target, excludes):
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
        locks = tuple(
            str(path)
            for path in dict.fromkeys(
                lock
                for reference in (WAL_REF, *sorted(state.target_positions))
                for lock in self.repo.ref_lock_paths(reference)
            )
            if path.exists()
        )
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
            if not state.pending:
                self._verify_landed(state)
                divergences = self._divergences(state)
                if not divergences:
                    return RecoveryResult(True, 0, repaired, refused)
                document = self.wal.document(
                    "refused",
                    "recovery",
                    {
                        "action": "recover",
                        "reason": "branch_diverged",
                        "divergences": [item.payload() for item in divergences],
                    },
                )
                if self.wal.try_append((document,), snapshot.head) is None:
                    continue
                raise WalDivergedError(
                    "the branch and the WAL disagree about what the broker landed",
                    divergences=[item.payload() for item in divergences],
                    repaired=repaired,
                    refused=refused,
                )
            operation, intent = next(iter(state.pending.items()))
            target = _string(intent.data, "target", intent)
            candidate = _string(intent.data, "candidate", intent)
            target_head = self.repo.resolve_ref(target, missing_ok=True)
            if target_head is not None and self.repo.is_ancestor(candidate, target_head):
                document = self.wal.document(
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
                was_landed = True
            else:
                document = self.wal.document(
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
                was_landed = False
            if self.wal.try_append((document,), snapshot.head) is None:
                continue
            if was_landed:
                repaired += 1
            else:
                refused += 1
        raise GitError("recovery CAS retry limit exceeded")

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
            records = self.wal.try_append((document,), snapshot.head)
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
