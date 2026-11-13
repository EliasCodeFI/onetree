"""Command-line interface for shared-tree coordination."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from onetree import __version__, agents, autosave
from onetree.core import CheckResult, Coordinator
from onetree.errors import (
    IdentityError,
    InvalidPathError,
    OnetreeError,
    PayloadGateError,
    WalDivergedError,
)
from onetree.git import (
    PAYLOAD_GATE_PATH_KEY,
    PAYLOAD_GATE_SHA256_KEY,
    Repository,
    payload_gate_source,
    read_payload_gate_source,
)


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

    protect = subparsers.add_parser("protect", help="Protect the current shared branch baseline.")
    _add_repo(protect)
    _add_actor(protect)
    _add_output(protect)

    reconcile = subparsers.add_parser(
        "reconcile",
        help="Explicitly accept an inspected workspace baseline.",
    )
    _add_repo(reconcile)
    _add_actor(reconcile)
    reconcile.add_argument("--expected-epoch", required=True, help="Current protection event id.")
    reconcile.add_argument(
        "--reason",
        required=True,
        help="Operator's reason for accepting the state.",
    )
    _add_output(reconcile)

    session = subparsers.add_parser("session", help="Register or inspect a session index.")
    _add_repo(session)
    _add_actor(session)
    session.add_argument("--pid", type=int, help="Long-lived session body PID (default: parent).")
    session.add_argument("--ttl", type=float, default=86400.0, help="Session lease seconds.")
    session.add_argument(
        "--target",
        help="Full refs/heads target (default: HEAD branch or unique same-tip branch).",
    )
    _add_output(session)

    claim = subparsers.add_parser(
        "claim", help="Claim one or more absolute paths; a file may not exist yet."
    )
    claim.add_argument("paths", nargs="+")
    _add_repo(claim)
    _add_actor(claim)
    claim.add_argument("--ttl", type=float, default=86400.0, help="Claim lease seconds.")
    _add_output(claim)

    narrow = subparsers.add_parser("narrow", help="Atomically retain descendants of a held path.")
    narrow.add_argument("parent", help="Absolute path of the exact claim to surrender.")
    narrow.add_argument("paths", nargs="+", help="Absolute existing descendants to retain.")
    _add_repo(narrow)
    _add_actor(narrow)
    _add_output(narrow)

    release = subparsers.add_parser("release", help="Release selected paths or all held paths.")
    release.add_argument("paths", nargs="*")
    _add_repo(release)
    _add_actor(release)
    _add_output(release)

    check = subparsers.add_parser("check", help="Check one or more absolute paths.")
    check.add_argument("paths", nargs="+")
    _add_repo(check)
    _add_actor(check)
    _add_output(check)

    land = subparsers.add_parser("land", help="Audit and CAS-land the session index.")
    _add_repo(land)
    _add_actor(land)
    land.add_argument(
        "--target",
        help="Full refs/heads target (required while HEAD is detached).",
    )
    land.add_argument(
        "--commit-payload",
        help=(
            "Path to commit_payload.py (default: ONETREE_COMMIT_PAYLOAD, then the "
            "repository's admitted gate). Its bytes must match the admitted SHA-256; "
            "landing refuses when no gate is admitted."
        ),
    )
    land.add_argument("-m", "--message", help="Commit message.")
    land.add_argument(
        "-a",
        "--all",
        action="store_true",
        help=(
            "First move the session to the branch's tip (keeping its claims) and stage every "
            "file it holds; paths it does not hold are never staged."
        ),
    )
    _add_output(land)

    gate = subparsers.add_parser(
        "gate",
        help="Inspect or admit the repository's commit payload gate.",
    )
    _add_repo(gate)
    gate.add_argument(
        "--admit",
        metavar="PATH",
        help="Admit this gate file's current bytes as the repository's payload gate.",
    )
    gate.add_argument(
        "--sha256",
        help="With --admit: the digest the operator reviewed; admission refuses other bytes.",
    )
    gate.add_argument(
        "--commit-payload",
        help=(
            "Report the gate `land --commit-payload` would read (default: "
            "ONETREE_COMMIT_PAYLOAD, then the admitted gate)."
        ),
    )
    _add_output(gate)

    wal = subparsers.add_parser("wal", help="Verify and inspect refs/onetree/wal.")
    _add_repo(wal)
    wal_action = wal.add_mutually_exclusive_group()
    wal_action.add_argument("--recover", action="store_true", help="Resolve any exposed intent.")
    wal_action.add_argument(
        "--adopt",
        action="store_true",
        help="Record broker landings found by the local branch audit.",
    )
    wal.add_argument(
        "--target",
        help=(
            "One refs/heads branch. Only --adopt narrows anything: it limits which "
            "landings are recorded. In every mode the audit and the report stay "
            "repository-wide, and naming a branch only adds it to that audit."
        ),
    )
    _add_output(wal)

    saves = subparsers.add_parser(
        "autosave", help="Save, list or restore snapshots of claimed working bytes."
    )
    autosave_actions = saves.add_subparsers(dest="autosave_action", required=True)
    save = autosave_actions.add_parser("save", help="Snapshot the session's claimed paths.")
    save.add_argument("paths", nargs="*", help="Absolute claimed paths (default: all claims).")
    listing = autosave_actions.add_parser("list", help="List snapshots, newest first.")
    listing.add_argument("--limit", type=int, help="Show at most this many snapshots.")
    restore = autosave_actions.add_parser("restore", help="Restore or print a path's snapshot.")
    restore.add_argument("path", help="Absolute path to restore.")
    restore.add_argument("--from", dest="source", help="Read another session's snapshots.")
    restore.add_argument("--snapshot", help="Snapshot commit (default: newest holding the path).")
    restore.add_argument("--stdout", action="store_true", help="Print the bytes; write nothing.")
    restore.add_argument(
        "--force", action="store_true", help="Overwrite working bytes no snapshot holds."
    )
    autosave_status = autosave_actions.add_parser(
        "status", help="Report claimed paths whose working bytes no snapshot holds."
    )
    for action in (save, listing, restore, autosave_status):
        _add_repo(action)
        _add_actor(action)
        _add_output(action)

    init = subparsers.add_parser(
        "init", help="Set a repository up: payload gate, test command, log cache."
    )
    _add_repo(init)
    _add_actor(init)
    init.add_argument("--test", help='Test command run before every landing, e.g. "pytest -q".')
    init.add_argument("--gate", metavar="PATH", help="Admit this payload gate instead.")
    init.add_argument(
        "--protect",
        action="store_true",
        help="Also refuse landings after anyone moves the branch outside onetree.",
    )
    _add_output(init)

    claims = subparsers.add_parser("claims", help="Show who holds which paths.")
    _add_repo(claims)
    _add_actor(claims)
    _add_output(claims)

    hook = subparsers.add_parser(
        "hook", help="Run as a Claude Code or Codex hook (the plugin uses it)."
    )
    hook.add_argument("agent", choices=("claude", "codex"))
    hook.add_argument(
        "event", choices=("session-start", "pre-tool-use", "post-tool-use", "subagent-stop")
    )

    mcp = subparsers.add_parser(
        "mcp", help="Serve claim, release, claims and land to an agent over MCP."
    )
    _add_repo(mcp)
    _add_actor(mcp)

    gc = subparsers.add_parser("gc", help="Append a stale-session/claim collection event.")
    _add_repo(gc)
    _add_actor(gc)
    _add_output(gc)
    return parser


def _actor(explicit: str | None) -> str:
    if explicit:
        return explicit
    found = agents.session_id()
    if found is None:
        raise IdentityError(
            "session id is required (--session or a supported session environment variable)"
        )
    return found


def _one_line(text: str) -> str:
    """One stderr line per message: every boundary a line reader honours becomes a
    space, and the message's own spacing stays (a path with two spaces is that path).
    JSON carries the message as written."""
    return " ".join(part.strip() for part in text.splitlines() if part.strip())


def _emit(payload: Mapping[str, Any], json_output: bool, human: str) -> None:
    if json_output:
        sys.stdout.write(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")
    else:
        sys.stdout.write(human.rstrip("\n") + "\n")


def _event_payload(event: Any) -> dict[str, Any]:
    payload = event.payload()
    return dict(payload)


def _check_payload(result: CheckResult) -> dict[str, Any]:
    return {
        "path": result.path,
        "blocked": result.blocked,
        "holders": [claim.payload() for claim in result.holders],
        "owned": [claim.payload() for claim in result.owned],
    }


def _check_human(result: CheckResult) -> str:
    path = json.dumps(result.path, ensure_ascii=True)[1:-1]
    if result.blocked:
        holders = ", ".join(json.dumps(claim.actor) for claim in result.holders)
        line = f"BLOCKED {path} by {holders}"
    elif result.owned:
        line = f"OWNED {path}"
    else:
        line = f"CLEAR {path}"
    if result.owned:
        owners = ", ".join(json.dumps(claim.actor) for claim in result.owned)
        line += f"; owned by {owners}"
    return line


def _gate_payload(repo: Repository, named: str | None, origin: str | None) -> dict[str, Any]:
    admission = repo.payload_gate_admission()
    source = payload_gate_source(named, admission)
    if source is not None and source != named:
        origin = "admission"
    observed: str | None = None
    resolved: str | None = None
    error: str | None = None
    if source is not None:
        try:
            gate, observed = read_payload_gate_source(source)
            resolved = str(gate)
        except PayloadGateError as failure:
            error = str(failure)
    return {
        "admitted": admission is not None,
        "path": None if admission is None else admission.path,
        "sha256": None if admission is None else admission.sha256,
        "source": {
            "path": source,
            "origin": None if source is None else origin,
            "resolved": resolved,
            "sha256": observed,
            "error": error,
        },
        "source_sha256": observed,
        "matches": admission is not None and observed == admission.sha256,
        "config": {"path": PAYLOAD_GATE_PATH_KEY, "sha256": PAYLOAD_GATE_SHA256_KEY},
    }


def _gate_human(gate: Mapping[str, Any]) -> str:
    if not gate["admitted"]:
        return "no payload gate admitted; landing refuses"
    line = f"payload gate {gate['path']} admitted at sha256 {gate['sha256']}"
    source = gate["source"]
    if source["origin"] != "admission":
        line += f"; landing reads {source['path']} ({source['origin']})"
    if gate["matches"]:
        return line + "; source matches"
    if source["error"] is not None:
        return line + f"; SOURCE UNREADABLE ({source['error']}), landing refuses"
    return line + f"; SOURCE DIFFERS (sha256 {source['sha256']}), landing refuses"


def _check_paths(
    coordinator: Coordinator, actor: str, paths: Sequence[str]
) -> tuple[int, dict[str, Any], str]:
    if len(paths) == 1:
        result = coordinator.check(actor, paths[0])
        return (
            int(result.blocked),
            {"ok": True, "check": _check_payload(result)},
            _check_human(result),
        )

    rows: list[dict[str, Any]] = []
    lines: list[str] = []
    invalid = False
    blocked = False
    for path in paths:
        try:
            result = coordinator.check(actor, path)
        except InvalidPathError as error:
            invalid = True
            rows.append({"input": path, "ok": False, "error": error.payload()})
            lines.append(
                f"ERROR {json.dumps(path, ensure_ascii=True)}: "
                f"{error.kind}: {json.dumps(str(error), ensure_ascii=True)}"
            )
        else:
            blocked |= result.blocked
            rows.append({"input": path, "ok": True, "check": _check_payload(result)})
            lines.append(_check_human(result))
    code = 2 if invalid else int(blocked)
    return code, {"ok": not invalid, "blocked": blocked, "checks": rows}, "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "gate" and args.sha256 is not None and args.admit is None:
        parser.error("gate: --sha256 requires --admit")
    json_output = bool(getattr(args, "json", False))
    return_code = 0
    if args.command == "status":
        payload = {
            "ok": True,
            "name": "onetree",
            "version": __version__,
            "phase": "gate-3-dogfood",
        }
        _emit(payload, json_output, f"onetree {__version__} (phase: gate-3-dogfood)")
        return 0
    if args.command == "hook":
        from onetree import hooks

        outcome = hooks.run(args.agent, args.event, sys.stdin.read())
        sys.stdout.write(outcome.stdout)
        sys.stderr.write(outcome.stderr)
        return outcome.code
    if args.command == "mcp":
        from onetree import mcp

        try:
            identity = args.actor or agents.session_id()
        except IdentityError:
            identity = None
        repo = args.repo if args.repo != "." else os.environ.get("CLAUDE_PROJECT_DIR") or "."
        return mcp.serve(repo, identity, sys.stdin, sys.stdout)

    try:
        coordinator = Coordinator(
            args.repo,
            commit_payload=getattr(args, "commit_payload", None),
        )
        if args.command == "session":
            actor = _actor(args.actor)
            pid = args.pid if args.pid is not None else agents.default_session_pid(actor)
            try:
                session = coordinator.register_session(
                    actor, pid=pid, ttl=args.ttl, target=args.target
                )
            except IdentityError as error:
                if args.pid is None and pid is not None and "not alive" in str(error):
                    raise IdentityError(
                        "the Claude Code process is not visible from this shell (a sandbox hides "
                        "it), so no session can be tied to it here; use the onetree plugin's "
                        "tools, or run onetree outside the sandbox",
                        session=actor,
                        pid=pid,
                    ) from error
                raise
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
        elif args.command == "narrow":
            narrow_result = coordinator.narrow(_actor(args.actor), args.parent, args.paths)
            payload = {
                "ok": True,
                "narrow": {
                    "released": list(narrow_result.released),
                    "claims": [claim.payload() for claim in narrow_result.claims],
                },
            }
            human = f"narrowed {args.parent}; retained {len(narrow_result.claims)} claim(s)"
        elif args.command == "release":
            release_result = coordinator.release(_actor(args.actor), args.paths)
            payload = {
                "ok": True,
                "release": {"released": list(release_result.released)},
            }
            human = f"released {len(release_result.released)} path(s)"
        elif args.command == "check":
            return_code, payload, human = _check_paths(coordinator, _actor(args.actor), args.paths)
        elif args.command == "protect":
            epoch = coordinator.protect_workspace(_actor(args.actor))
            payload = {"ok": True, "workspace": dict(epoch)}
            human = f"protected {epoch['target']} at {epoch['tip']}"
        elif args.command == "reconcile":
            reconciled = coordinator.reconcile_workspace(
                _actor(args.actor),
                expected_epoch=args.expected_epoch,
                reason=args.reason,
            )
            payload = {"ok": True, "workspace": dict(reconciled)}
            human = f"reconciled workspace; epoch {reconciled['epoch']}"
        elif args.command == "init":
            from onetree import initialize

            try:
                operator = _actor(args.actor)
            except IdentityError:
                operator = "operator"
            setup = initialize.initialize(
                coordinator, operator, tests=args.test, gate=args.gate, protect=args.protect
            )
            payload = {"ok": True, "init": setup.payload()}
            gate_line = {
                "standard": "the standard gate",
                "updated": "the standard gate (updated)",
                "admitted": "the gate you named",
                "kept": "the gate already admitted",
            }[setup.gate]
            human = "\n".join(
                [
                    f"onetree is set up in {setup.root}",
                    f"  payload gate: {gate_line} ({setup.gate_path})",
                    f"  tests: {setup.tests or 'none'}",
                    "  log cache: on",
                    f"  branch protection: {'on' if setup.protected else 'off'}",
                    *(f"  note: {note}" for note in setup.notes),
                ]
            )
        elif args.command == "claims":
            try:
                me: str | None = _actor(args.actor)
            except IdentityError:
                me = None
            held = coordinator.claims()
            payload = {
                "ok": True,
                "claims": [{**claim.payload(), "mine": claim.actor == me} for claim in held],
            }
            human = "\n".join(
                f"{claim.path}  {claim.actor}{'  (you)' if claim.actor == me else ''}"
                for claim in held
            ) or "nobody holds a claim"
        elif args.command == "land":
            if args.all:
                coordinator.renew(_actor(args.actor))
                coordinator.stage_claimed(_actor(args.actor))
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
                    "gate_receipt": land_result.gate_receipt,
                    "referee_receipt": land_result.referee_receipt,
                },
            }
            human = f"landed {land_result.commit} on {land_result.target}"
            if land_result.warnings:
                human += f" [warning: {', '.join(land_result.warnings)}]"
        elif args.command == "wal" and args.adopt:
            adoption_result = coordinator.adopt_landings(args.target)
            wal_result = adoption_result.report
            return_code = 0 if wal_result.consistent else 1
            payload = {
                "ok": wal_result.consistent,
                "adoption": {
                    "ref": "refs/onetree/wal",
                    "head": wal_result.head,
                    "consistent": wal_result.consistent,
                    "pending_intents": wal_result.pending_intents,
                    "divergences": [item.payload() for item in wal_result.divergences],
                    "commits": [event.data["commit"] for event in adoption_result.adopted],
                },
            }
            human = (
                f"WAL adoption recorded {len(adoption_result.adopted)} landing(s): "
                f"{'consistent' if wal_result.consistent else 'INCONSISTENT'}"
            )
        elif args.command == "wal" and args.recover:
            # Exit codes differ between the two `wal` modes on purpose. Inspection
            # is a report: 1 means "I looked and the state is not clean", the way
            # a diff reports difference. Recovery is an action: a divergence it
            # cannot repair raises, and a raised OnetreeError exits 2, because the
            # command it was asked to complete did not complete.
            recovery_result = coordinator.recover(args.target)
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
            wal_result = coordinator.inspect_wal(args.target)
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
                    "gate_evidence": wal_result.gate_evidence(),
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
                human += f"; DIVERGED on {len(wal_result.divergences)} audit-target finding(s)"
            # Residue is not inconsistency -- a ref lock is held for milliseconds
            # during every healthy append, so folding it into `consistent` would
            # fail a sound repo whenever a peer happens to be writing. It still
            # has to reach the human line: it is what makes the next land refuse.
            if wal_result.ref_locks:
                human += f"; {len(wal_result.ref_locks)} ref lock(s) held"
            if wal_result.temp_indexes:
                human += f"; {len(wal_result.temp_indexes)} temp index(es) present"
        elif args.command == "gate":
            if args.admit is not None:
                coordinator.repo.admit_payload_gate(args.admit, expected_sha256=args.sha256)
            if args.commit_payload is not None:
                named, origin = args.commit_payload, "commit_payload"
            else:
                named, origin = os.environ.get("ONETREE_COMMIT_PAYLOAD"), "environment"
            gate_payload = _gate_payload(coordinator.repo, named, origin)
            return_code = 0 if gate_payload["matches"] else 1
            payload = {"ok": True, "gate": gate_payload}
            human = _gate_human(gate_payload)
        elif args.command == "autosave" and args.autosave_action == "save":
            save_result = autosave.save(coordinator, _actor(args.actor), args.paths)
            payload = {"ok": True, "autosave": save_result.payload()}
            human = (
                f"saved {save_result.files} file(s) as {save_result.snapshot.commit}"
                if save_result.created and save_result.snapshot is not None
                else "nothing changed; no snapshot written"
            )
        elif args.command == "autosave" and args.autosave_action == "list":
            listed = autosave.snapshots(coordinator, _actor(args.actor), limit=args.limit)
            payload = {
                "ok": True,
                "autosave": {
                    "ref": autosave.autosave_ref(_actor(args.actor)),
                    "snapshots": [item.payload() for item in listed],
                },
            }
            human = "\n".join(
                f"{item.commit} {item.saved_at:.3f} {', '.join(item.scope)}" for item in listed
            ) or "no snapshots"
        elif args.command == "autosave" and args.autosave_action == "status":
            rows = autosave.status(coordinator, _actor(args.actor))
            return_code = int(any(row.state != "saved" for row in rows))
            payload = {"ok": True, "autosave": {"paths": [row.payload() for row in rows]}}
            human = "\n".join(
                f"{row.state.upper()} {row.path}" for row in rows if row.state != "saved"
            ) or f"all {len(rows)} claimed path(s) saved"
        elif args.command == "autosave":
            restored = autosave.restore(
                coordinator,
                _actor(args.actor),
                args.path,
                source=args.source,
                snapshot=args.snapshot,
                write=not args.stdout,
                force=args.force,
            )
            if args.stdout and not json_output:
                sys.stdout.buffer.write(restored.content or b"")
                sys.stdout.flush()
                return 0
            payload = {"ok": True, "autosave": restored.payload()}
            state = "present" if restored.present else "absent"
            human = (
                f"{'restored' if restored.written else 'unchanged'} {restored.path} "
                f"({state} in {restored.snapshot})"
            )
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
        divergences: list[dict[str, Any]] = []
        if args.command == "wal" and args.recover and isinstance(error, WalDivergedError):
            pending_divergence = int(error.details.get("pending", 0))
            divergences = list(error.details.get("divergences", []))
            payload["recovery"] = {
                "consistent": False,
                "pending": pending_divergence,
                "repaired": error.details.get("repaired", 0),
                "refused": error.details.get("refused", 0),
                "locks": error.details.get("locks", []),
                "divergences": divergences,
            }
        if json_output:
            _emit(payload, True, "")
        else:
            suffix = (
                f"; pending={pending_divergence} landing(s) still in flight"
                if pending_divergence
                else ""
            )
            # The summary is one line whatever break the message carries: a GitError
            # carries git's stderr, two lines for an include git cannot expand, and git
            # passes non-ASCII breaks through.
            sys.stderr.write(f"onetree: {_one_line(str(error))}{suffix}\n")
            recording_error = error.details.get("recording_error")
            if recording_error is not None:
                sys.stderr.write(
                    f"  refusal recording failed ({_one_line(str(recording_error['type']))}): "
                    f"{_one_line(str(recording_error['message']))}\n"
                )
            # Each divergence names what the operator must reconcile, including a second
            # item beside the branch divergence (an unreadable referee setting). The
            # detail says "this commit"; the commit and operation it means are printed
            # with it, one line per item whatever the detail carries.
            for item in divergences:
                where = [
                    f"{name} {item[name]}"
                    for name in ("commit", "operation")
                    if item.get(name) is not None
                ]
                suffix_text = f" ({', '.join(where)})" if where else ""
                sys.stderr.write(
                    f"  {item['kind']} at {item['target']}: "
                    f"{_one_line(str(item['detail']))}{suffix_text}\n"
                )
            # A degraded gate names what it could not audit, here as well as in the WAL
            # row and the JSON payload.
            for row in error.details.get("degraded") or []:
                sys.stderr.write(f"  degraded: {_one_line(str(row))}\n")
        return error.exit_code

    _emit(payload, json_output, human)
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
