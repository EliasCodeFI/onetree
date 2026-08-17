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
    ReleaseRefusedError,
    WalCorruptError,
)
from onetree.git import Repository, TreeEntry
from onetree.wal import WAL_REF, EventRecord, Wal, WalSnapshot

DEFAULT_TTL = 24 * 60 * 60.0
MAX_CAS_RETRIES = 32
_ACTOR_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")

Checkpoint = Callable[[str], None]


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


@dataclass(frozen=True)
class WalReport:
    head: str | None
    events: tuple[EventRecord, ...]
    consistent: bool
    pending_intents: int
    active_sessions: tuple[str, ...]
    active_claims: tuple[str, ...]


@dataclass(frozen=True)
class RecoveryResult:
    consistent: bool
    pending: int
    repaired: int
    refused: int


@dataclass(frozen=True)
class GcResult:
    event: str
    removed_claims: int
    removed_sessions: int


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


def _pid_start(pid: int) -> int | None:
    """Return Linux's process-birth tick, or None where the kernel does not expose it."""

    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        closing = text.rfind(")")
        if closing < 0:
            return None
        fields = text[closing + 2 :].split()
        return int(fields[19])
    except (FileNotFoundError, IndexError, OSError, UnicodeError, ValueError):
        return None


def _pid_alive(pid: int, expected_start: int | None) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    current_start = _pid_start(pid)
    return expected_start is None or current_start == expected_start


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
        try:
            relative = resolved.relative_to(self.repo.root)
        except ValueError as error:
            raise InvalidPathError(
                "claim path is outside the repository",
                path=raw_path,
                reason="outside_repository",
            ) from error
        return relative.as_posix()

    @staticmethod
    def _ttl_deadline(ttl: float, now: float, limit: float | None = None) -> float:
        if not math.isfinite(ttl) or ttl <= 0:
            raise IdentityError("TTL must be finite and positive", ttl=ttl)
        deadline = now + ttl
        return deadline if limit is None else min(deadline, limit)

    def _fold(self, snapshot: WalSnapshot) -> _FoldedState:
        state = _FoldedState(sessions={}, claims={}, pending={}, landed=[])
        terminal_operations: set[str] = set()
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
                )
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
                if operation in state.pending or operation in terminal_operations:
                    raise WalCorruptError("duplicate landing operation", oid=event.oid)
                attempt = data.get("attempt")
                if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt <= 0:
                    raise WalCorruptError("land_intent.attempt is invalid", oid=event.oid)
                self.repo.validate_branch_ref(_string(data, "target", event))
                self.repo.validate_oid(
                    _string(data, "expected_head", event), label="expected target"
                )
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
                if not data.get("recovered", False) and event.parents != (intent.oid,):
                    raise WalCorruptError(
                        "landed event is not adjacent to its intent", oid=event.oid
                    )
                if data.get("commit") != intent.data.get("candidate"):
                    raise WalCorruptError("landed commit differs from its intent", oid=event.oid)
                self.repo.validate_branch_ref(_string(data, "target", event))
                self.repo.validate_oid(_string(data, "expected_head", event), label="landed base")
                self.repo.validate_oid(_string(data, "tree", event), label="landed tree")
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
                terminal_operations.add(operation)
                state.landed.append(event)
            elif event.kind == "refused":
                refused_operation = data.get("operation")
                intent_id = data.get("intent")
                if refused_operation is not None:
                    if not isinstance(refused_operation, str) or not refused_operation:
                        raise WalCorruptError("refused.operation is invalid", oid=event.oid)
                    pending = state.pending.get(refused_operation)
                    if pending is not None and intent_id == pending.event_id:
                        state.pending.pop(refused_operation)
                    terminal_operations.add(refused_operation)
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
        target_ref = (
            self.repo.head_ref() if target is None else self.repo.validate_branch_ref(target)
        )
        base = self.repo.resolve_ref(target_ref)
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
                    actor,
                    generation,
                    body_pid,
                    process_start,
                    expires_at,
                    base,
                    target_ref,
                    index,
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

    def land(
        self,
        actor: str,
        *,
        message: str | None = None,
        target: str | None = None,
        checkpoint: Checkpoint | None = None,
    ) -> LandResult:
        actor = _validate_actor(actor)
        target_ref = (
            self.repo.head_ref() if target is None else self.repo.validate_branch_ref(target)
        )
        operation = uuid.uuid4().hex
        attempts = 0
        for _ in range(MAX_CAS_RETRIES):
            now = time.time()
            snapshot = self.wal.read()
            state = self._fold(snapshot)
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
            if checkpoint is not None:
                checkpoint("after_audit")
            current = self.repo.resolve_ref(target_ref)
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
            if checkpoint is not None:
                checkpoint("after_tree")
            commit = self.repo.commit_tree(
                candidate_tree,
                message or f"onetree land: {actor}",
                (current,),
            )
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
            )
            landed = self.wal.document(
                "landed",
                actor,
                {
                    "operation": operation,
                    "intent": intent["event_id"],
                    "target": target_ref,
                    "expected_head": current,
                    "commit": commit,
                    "tree": candidate_tree,
                    "payload": [change.path for change in changes],
                    "recovered": False,
                },
            )
            new_wal_head, _ = self.wal.build_chain((intent, landed), snapshot.head)
            if checkpoint is not None:
                checkpoint("before_transaction")
            landing_time = time.time()
            fenced_generations = {str(fence["claim_generation"]) for fence in fences}
            fences_live = all(
                claim.generation not in fenced_generations or claim.expires_at > landing_time
                for claim in owned
            )
            if not _session_alive(session, landing_time) or not fences_live:
                continue
            attempts += 1
            won = self.repo.update_refs_atomically(
                (
                    (target_ref, commit, current),
                    (WAL_REF, new_wal_head, snapshot.head),
                )
            )
            if not won:
                continue
            if checkpoint is not None:
                checkpoint("after_transaction")
            return LandResult(
                commit,
                candidate_tree,
                target_ref,
                attempts,
                tuple(change.path for change in changes),
                operation,
            )
        raise GitError("landing CAS retry limit exceeded", retries=MAX_CAS_RETRIES)

    def _verify_landed(self, state: _FoldedState) -> None:
        for event in state.landed:
            commit = _string(event.data, "commit", event)
            expected_tree = _string(event.data, "tree", event)
            resolved = self.repo.resolve_ref(commit)
            if resolved != commit:
                raise WalCorruptError("landed commit does not resolve", oid=event.oid)
            expected_parent = _string(event.data, "expected_head", event)
            if self.repo.commit_parents(commit) != (expected_parent,):
                raise WalCorruptError("landed commit parent differs from WAL", oid=event.oid)
            actual_tree = self.repo.git_text("show", "-s", "--format=%T", commit)
            if actual_tree != expected_tree:
                raise WalCorruptError("landed commit tree differs from WAL", oid=event.oid)

    def inspect_wal(self) -> WalReport:
        snapshot = self.wal.read()
        state = self._fold(snapshot)
        self._verify_landed(state)
        now = time.time()
        sessions = tuple(
            sorted(actor for actor in state.sessions if state.session_active(actor, now))
        )
        claims = tuple(sorted({claim.path for claim in state.active_claims(now)}))
        return WalReport(
            snapshot.head,
            snapshot.events,
            not state.pending,
            len(state.pending),
            sessions,
            claims,
        )

    def recover(self) -> RecoveryResult:
        repaired = 0
        refused = 0
        for _ in range(MAX_CAS_RETRIES):
            snapshot = self.wal.read()
            state = self._fold(snapshot)
            if not state.pending:
                self._verify_landed(state)
                return RecoveryResult(True, 0, repaired, refused)
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

    def gc(self, actor: str) -> GcResult:
        actor = _validate_actor(actor)
        for _ in range(MAX_CAS_RETRIES):
            snapshot = self.wal.read()
            state = self._fold(snapshot)
            now = time.time()
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
                },
            )
            records = self.wal.try_append((document,), snapshot.head)
            if records is not None:
                return GcResult(records[0].kind, len(stale_claims), len(stale_sessions))
        raise GitError("gc CAS retry limit exceeded")
