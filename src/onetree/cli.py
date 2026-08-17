"""Command-line adapter for the Round A broker."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from onetree import __version__
from onetree.core import Coordinator
from onetree.errors import IdentityError, OnetreeError, WalDivergedError


def _add_output(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Emit one JSON object on stdout.")


def _add_repo(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", default=".", help="Path inside the target repository.")


def _add_actor(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--session", dest="actor", help="Stable session id.")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="onetree", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status", help="Report version and build phase.")
    _add_output(status)

    session = subparsers.add_parser("session", help="Register or inspect a session index.")
    _add_repo(session)
    _add_actor(session)
    session.add_argument("--pid", type=int, help="Long-lived session body PID (default: parent).")
    session.add_argument("--ttl", type=float, default=86400.0, help="Session lease seconds.")
    session.add_argument("--target", help="Full refs/heads target (default: symbolic HEAD).")
    _add_output(session)

    claim = subparsers.add_parser("claim", help="Claim one or more absolute existing paths.")
    claim.add_argument("paths", nargs="+")
    _add_repo(claim)
    _add_actor(claim)
    claim.add_argument("--ttl", type=float, default=86400.0, help="Claim lease seconds.")
    _add_output(claim)

    release = subparsers.add_parser("release", help="Release selected paths or all held paths.")
    release.add_argument("paths", nargs="*")
    _add_repo(release)
    _add_actor(release)
    _add_output(release)

    check = subparsers.add_parser("check", help="Check one absolute existing path.")
    check.add_argument("path")
    _add_repo(check)
    _add_actor(check)
    _add_output(check)

    land = subparsers.add_parser("land", help="Audit and CAS-land the session index.")
    _add_repo(land)
    _add_actor(land)
    land.add_argument("--target", help="Full refs/heads target (default: symbolic HEAD).")
    land.add_argument("-m", "--message", help="Commit message.")
    _add_output(land)

    wal = subparsers.add_parser("wal", help="Verify and inspect refs/onetree/wal.")
    _add_repo(wal)
    wal.add_argument("--recover", action="store_true", help="Resolve any exposed intent.")
    _add_output(wal)

    gc = subparsers.add_parser("gc", help="Append a stale-session/claim collection event.")
    _add_repo(gc)
    _add_actor(gc)
    _add_output(gc)
    return parser


def _actor(explicit: str | None) -> str:
    if explicit:
        return explicit
    candidates = {
        value
        for name in ("ONETREE_SESSION_ID", "CODEX_THREAD_ID", "CLAUDE_SESSION_ID")
        if (value := os.environ.get(name))
    }
    if not candidates:
        raise IdentityError(
            "session id is required (--session or a supported session environment variable)"
        )
    if len(candidates) != 1:
        raise IdentityError("session environment is ambiguous", sessions=sorted(candidates))
    return candidates.pop()


def _emit(payload: Mapping[str, Any], json_output: bool, human: str) -> None:
    if json_output:
        sys.stdout.write(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")
    else:
        sys.stdout.write(human.rstrip("\n") + "\n")


def _event_payload(event: Any) -> dict[str, Any]:
    payload = event.payload()
    return dict(payload)


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    json_output = bool(args.json)
    return_code = 0
    if args.command == "status":
        payload = {
            "ok": True,
            "name": "onetree",
            "version": __version__,
            "phase": "gate-1-round-a",
        }
        _emit(payload, json_output, f"onetree {__version__} (phase: gate-1-round-a)")
        return 0

    try:
        coordinator = Coordinator(args.repo)
        if args.command == "session":
            session = coordinator.register_session(
                _actor(args.actor),
                pid=args.pid,
                ttl=args.ttl,
                target=args.target,
            )
            payload = {"ok": True, "session": session.payload()}
            human = f"registered {session.actor}; index {session.index_path}"
            if session.warnings:
                human += f" [warning: {', '.join(session.warnings)}]"
        elif args.command == "claim":
            claim_result = coordinator.claim(_actor(args.actor), args.paths, ttl=args.ttl)
            payload = {
                "ok": True,
                "claim": {"claims": [claim.payload() for claim in claim_result.claims]},
            }
            human = f"claimed {len(claim_result.claims)} path(s)"
        elif args.command == "release":
            release_result = coordinator.release(_actor(args.actor), args.paths)
            payload = {
                "ok": True,
                "release": {"released": list(release_result.released)},
            }
            human = f"released {len(release_result.released)} path(s)"
        elif args.command == "check":
            check_result = coordinator.check(_actor(args.actor), args.path)
            return_code = 1 if check_result.blocked else 0
            payload = {
                "ok": True,
                "check": {
                    "path": check_result.path,
                    "blocked": check_result.blocked,
                    "holders": [claim.payload() for claim in check_result.holders],
                    "owned": [claim.payload() for claim in check_result.owned],
                },
            }
            human = (
                f"BLOCKED {check_result.path} by "
                f"{', '.join(claim.actor for claim in check_result.holders)}"
                if check_result.blocked
                else f"CLEAR {check_result.path}"
            )
        elif args.command == "land":
            land_result = coordinator.land(
                _actor(args.actor),
                message=args.message,
                target=args.target,
            )
            payload = {
                "ok": True,
                "land": {
                    "commit": land_result.commit,
                    "tree": land_result.tree,
                    "target": land_result.target,
                    "attempts": land_result.attempts,
                    "payload": list(land_result.payload),
                    "operation": land_result.operation,
                    "intent": land_result.intent,
                    "warnings": list(land_result.warnings),
                },
            }
            human = f"landed {land_result.commit} on {land_result.target}"
            if land_result.warnings:
                human += f" [warning: {', '.join(land_result.warnings)}]"
        elif args.command == "wal" and args.recover:
            # Exit codes differ between the two `wal` modes on purpose. Inspection
            # is a report: 1 means "I looked and the state is not clean", the way
            # a diff reports difference. Recovery is an action: a divergence it
            # cannot repair raises, and a raised OnetreeError exits 2, because the
            # command it was asked to complete did not complete.
            recovery_result = coordinator.recover()
            return_code = 0 if recovery_result.consistent else 1
            payload = {
                "ok": recovery_result.consistent,
                "recovery": {
                    "consistent": recovery_result.consistent,
                    "pending": recovery_result.pending,
                    "repaired": recovery_result.repaired,
                    "refused": recovery_result.refused,
                    "locks": list(recovery_result.locks),
                    "divergences": [],
                },
            }
            human = (
                f"recovery: repaired={recovery_result.repaired}, "
                f"refused={recovery_result.refused}, pending={recovery_result.pending}"
            )
            if recovery_result.pending:
                human += " (landing(s) still in flight, left alone)"
            if recovery_result.locks:
                human += (
                    f"; {len(recovery_result.locks)} ref lock(s) held, "
                    f"remove by hand if no process holds them: "
                    f"{', '.join(recovery_result.locks)}"
                )
        elif args.command == "wal":
            wal_result = coordinator.inspect_wal()
            return_code = 0 if wal_result.consistent else 1
            payload = {
                "ok": True,
                "wal": {
                    "ref": "refs/onetree/wal",
                    "head": wal_result.head,
                    "consistent": wal_result.consistent,
                    "pending_intents": wal_result.pending_intents,
                    "active_sessions": list(wal_result.active_sessions),
                    "active_claims": list(wal_result.active_claims),
                    "divergences": [item.payload() for item in wal_result.divergences],
                    "ref_locks": list(wal_result.ref_locks),
                    "temp_indexes": [residue.payload() for residue in wal_result.temp_indexes],
                    "events": [_event_payload(event) for event in wal_result.events],
                },
            }
            # The verdict leads. A human line that reads identically for a healthy
            # WAL and for the commonest post-crash state -- an exposed intent,
            # which is what exit 1 and `consistent: false` are reporting -- tells
            # the only reader who sees it nothing at all.
            human = (
                f"WAL {wal_result.head or '(empty)'}: {len(wal_result.events)} event(s); "
                f"{'consistent' if wal_result.consistent else 'INCONSISTENT'}"
            )
            if wal_result.pending_intents:
                human += f"; {wal_result.pending_intents} pending intent(s)"
            if wal_result.divergences:
                human += f"; DIVERGED on {len(wal_result.divergences)} record(s)"
            # Residue is not inconsistency -- a ref lock is held for milliseconds
            # during every healthy append, so folding it into `consistent` would
            # fail a sound repo whenever a peer happens to be writing. It still
            # has to reach the human line: it is what makes the next land refuse.
            if wal_result.ref_locks:
                human += f"; {len(wal_result.ref_locks)} ref lock(s) held"
            if wal_result.temp_indexes:
                human += f"; {len(wal_result.temp_indexes)} temp index(es) present"
        elif args.command == "gc":
            gc_result = coordinator.gc(_actor(args.actor))
            payload = {
                "ok": True,
                "gc": {
                    "event": gc_result.event,
                    "removed_claims": gc_result.removed_claims,
                    "removed_sessions": gc_result.removed_sessions,
                    "removed_temp_indexes": gc_result.removed_temp_indexes,
                },
            }
            human = (
                f"gc removed {gc_result.removed_claims} claim(s), "
                f"{gc_result.removed_sessions} session(s), "
                f"{gc_result.removed_temp_indexes} temp index(es)"
            )
        else:
            raise AssertionError("unreachable: subparser is required")
    except OnetreeError as error:
        payload = {"ok": False, "error": error.payload()}
        pending_divergence = 0
        if args.command == "wal" and args.recover and isinstance(error, WalDivergedError):
            pending_divergence = int(error.details.get("pending", 0))
            payload["recovery"] = {
                "consistent": False,
                "pending": pending_divergence,
                "repaired": error.details.get("repaired", 0),
                "refused": error.details.get("refused", 0),
                "locks": error.details.get("locks", []),
                "divergences": error.details.get("divergences", []),
            }
        if json_output:
            _emit(payload, True, "")
        else:
            suffix = (
                f"; pending={pending_divergence} landing(s) still in flight"
                if pending_divergence
                else ""
            )
            sys.stderr.write(f"onetree: {error}{suffix}\n")
        return error.exit_code

    _emit(payload, json_output, human)
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
