"""Hook commands for Claude Code and Codex.

    onetree hook claude session-start | pre-tool-use | post-tool-use | subagent-stop
    onetree hook codex session-start | pre-tool-use

Each reads the hook's JSON from stdin. In a repository where onetree is set up (an admitted gate
or a log), the agent's session is registered, the file an edit tool is about to write is claimed
for the session first, and git commands that would erase other agents' work or move the shared
branch are refused. Anywhere else the hooks do nothing.

A refusal is a PreToolUse "deny" decision whose reason the agent reads. A problem of onetree's own
never blocks the agent: the hook reports it on stderr and lets the tool run; a landing still
refuses anything the session does not hold.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from onetree import agents, autosave
from onetree.core import Coordinator
from onetree.errors import ClaimConflictError, IdentityError, OnetreeError, PayloadGateError

EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
PATCH_TOOLS = frozenset({"apply_patch"})
SHELL_TOOLS = frozenset({"Bash"})
#: git subcommands that rewrite the shared working tree or move the shared branch.
REFUSED_GIT = frozenset(
    {
        "checkout", "switch", "restore", "reset", "stash", "clean", "commit",
        "rebase", "merge", "pull", "cherry-pick", "revert", "am",
    }
)
_HARMLESS = {"stash": frozenset({"list", "show"})}
_GIT_VALUE_OPTIONS = frozenset(
    {"-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}
)
_WRAPPERS = frozenset({"sudo", "command", "exec", "time", "nohup", "env", "builtin", "xargs",
                       "nice", "timeout", "stdbuf"})
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")
_SEPARATORS = re.compile(r"&&|\|\||[;|&\n()`]|\$\(")
_PATCH_FILE = re.compile(r"^\*\*\* (?:(?:Add|Update|Delete) File|Move to): (.+?)\s*$", re.MULTILINE)

SESSION_CONTEXT = (
    "This repository uses onetree: several agents share this one working tree. Before each "
    "edit, the file is claimed for this session; if another agent holds it, the edit is refused "
    "and the reason names the holder, so work on something else meanwhile. Do not run git "
    "commit, checkout, switch, restore, reset, stash, clean, merge, rebase or pull here. To land "
    "finished work, use the onetree land tool, or run `onetree land -a -m \"<message>\"`: it "
    "takes the files this session holds, checks them, runs the tests and moves the branch."
)


@dataclass(frozen=True)
class Outcome:
    code: int = 0
    stdout: str = ""
    stderr: str = ""


def _deny(reason: str) -> Outcome:
    decision = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
    return Outcome(stdout=json.dumps(decision) + "\n")


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _coordinator(near: Path) -> Coordinator | None:
    """The coordinator of the repository around ``near``, when onetree is set up there."""

    probe = near
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if probe.is_file():
        probe = probe.parent
    try:
        coordinator = Coordinator(probe)
        if coordinator.wal.head() is not None:
            return coordinator
        try:
            return None if coordinator.repo.payload_gate_admission() is None else coordinator
        except PayloadGateError:
            return coordinator
    except (OnetreeError, OSError):
        return None


def _actor(agent: str, payload: Mapping[str, Any], environ: Mapping[str, str]) -> str | None:
    if agent == "codex":
        return environ.get("CODEX_THREAD_ID") or _text(payload.get("session_id"))
    session = _text(payload.get("session_id")) or environ.get("CLAUDE_CODE_SESSION_ID")
    subagent = _text(payload.get("agent_id"))
    if session and subagent:
        return agents.subagent_actor(session, subagent)
    return session


def _ensure_session(coordinator: Coordinator, actor: str, environ: Mapping[str, str]) -> None:
    try:
        coordinator.register_session(actor, pid=agents.agent_pid(environ))
    except IdentityError as error:
        if "already registered" not in str(error):
            raise


def edit_paths(tool: str, data: Mapping[str, Any], cwd: Path) -> list[Path]:
    """The files an edit or patch tool call is about to write, as absolute paths."""

    raw: list[str] = []
    if tool in EDIT_TOOLS:
        for key in ("file_path", "notebook_path", "path"):
            value = _text(data.get(key))
            if value:
                raw.append(value)
                break
    elif tool in PATCH_TOOLS:
        patch = data.get("command") or data.get("patch") or data.get("input")
        if isinstance(patch, list):
            patch = "\n".join(item for item in patch if isinstance(item, str))
        if isinstance(patch, str):
            raw.extend(match.group(1) for match in _PATCH_FILE.finditer(patch))
    return [Path(os.path.normpath(cwd / path)) for path in raw]


def refused_git(command: str) -> list[tuple[str | None, str]]:
    """Git calls in a shell command that the shared tree must not run: (-C dir, subcommand)."""

    found: list[tuple[str | None, str]] = []
    for segment in _SEPARATORS.split(command):
        try:
            words = shlex.split(segment, comments=True)
        except ValueError:
            words = segment.split()
        index = 0
        while index < len(words) and (
            _ASSIGNMENT.fullmatch(words[index])
            or words[index] in _WRAPPERS
            or (index > 0 and words[index - 1] in _WRAPPERS and words[index][:1] in "-0123456789")
        ):
            index += 1
        if index >= len(words) or os.path.basename(words[index]) != "git":
            continue
        index += 1
        directory: str | None = None
        while index < len(words) and words[index].startswith("-"):
            option = words[index]
            if option == "-C" and index + 1 < len(words):
                directory = words[index + 1]
                index += 2
            elif option in _GIT_VALUE_OPTIONS and index + 1 < len(words):
                index += 2
            else:
                index += 1
        if index >= len(words) or words[index] not in REFUSED_GIT:
            continue
        subcommand, rest = words[index], words[index + 1 :]
        if rest and rest[0] in _HARMLESS.get(subcommand, frozenset()):
            continue
        if subcommand == "clean" and any(word in ("-n", "--dry-run") for word in rest):
            continue
        found.append((directory, subcommand))
    return found


def _git_refusal(subcommand: str) -> str:
    if subcommand == "commit":
        return (
            "onetree: `git commit` is not used in this shared working tree; commits go through "
            "onetree, which checks them and runs the tests first. Land with the onetree land tool "
            "or `onetree land -a -m \"<message>\"`."
        )
    return (
        f"onetree: `git {subcommand}` is not allowed in this shared working tree: it can erase "
        "other agents' unsaved edits or move the branch they all work on. Edit the files you "
        "need (each is claimed for you), then land with the onetree land tool or "
        "`onetree land -a -m \"<message>\"`."
    )


def _claim_refusal(error: ClaimConflictError) -> str:
    paths = ", ".join(str(path) for path in error.details.get("paths", []))
    holders = ", ".join(str(holder) for holder in error.details.get("holders", []))
    return (
        f"onetree: {paths} is being edited by another agent ({holders}) in this shared working "
        "tree. Leave it alone for now and work on something else; it frees up when that agent "
        "lands or releases it. `onetree claims` shows who holds what."
    )


def _pre_tool_use(agent: str, payload: Mapping[str, Any], environ: Mapping[str, str]) -> Outcome:
    tool = _text(payload.get("tool_name")) or ""
    data = payload.get("tool_input")
    data = data if isinstance(data, dict) else {}
    cwd = Path(_text(payload.get("cwd")) or os.getcwd())
    if tool in SHELL_TOOLS:
        command = _text(data.get("command"))
        for directory, subcommand in refused_git(command or ""):
            place = cwd if directory is None else Path(os.path.normpath(cwd / directory))
            if _coordinator(place) is not None:
                return _deny(_git_refusal(subcommand))
        return Outcome()
    paths = edit_paths(tool, data, cwd)
    if not paths:
        return Outcome()
    actor = _actor(agent, payload, environ)
    groups: dict[Path, tuple[Coordinator, list[str]]] = {}
    for path in paths:
        coordinator = _coordinator(path.parent)
        if coordinator is not None:
            groups.setdefault(coordinator.repo.root, (coordinator, []))[1].append(str(path))
    if not groups or actor is None:
        return Outcome()
    for coordinator, claimed in groups.values():
        try:
            try:
                coordinator.claim(actor, claimed)
            except IdentityError:
                _ensure_session(coordinator, actor, environ)
                coordinator.claim(actor, claimed)
        except ClaimConflictError as error:
            return _deny(_claim_refusal(error))
        except OnetreeError as error:
            if error.kind == "invalid_path":
                continue  # Git's own metadata or outside the repository: not a claimable file
            return Outcome(stderr=f"onetree: could not claim {', '.join(claimed)}: {error}\n")
    return Outcome()


def _post_tool_use(agent: str, payload: Mapping[str, Any], environ: Mapping[str, str]) -> Outcome:
    data = payload.get("tool_input")
    data = data if isinstance(data, dict) else {}
    cwd = Path(_text(payload.get("cwd")) or os.getcwd())
    actor = _actor(agent, payload, environ)
    if actor is None:
        return Outcome()
    for path in edit_paths(_text(payload.get("tool_name")) or "", data, cwd):
        coordinator = _coordinator(path.parent)
        if coordinator is None:
            continue
        try:
            autosave.save(coordinator, actor, [str(path)])
        except OnetreeError as error:
            return Outcome(stderr=f"onetree: no snapshot of {path}: {error}\n")
    return Outcome()


def _session_start(agent: str, payload: Mapping[str, Any], environ: Mapping[str, str]) -> Outcome:
    coordinator = _coordinator(Path(_text(payload.get("cwd")) or os.getcwd()))
    actor = _actor(agent, payload, environ)
    if coordinator is None or actor is None:
        return Outcome()
    try:
        _ensure_session(coordinator, actor, environ)
    except OnetreeError as error:
        return Outcome(stderr=f"onetree: the session was not registered: {error}\n")
    return Outcome(stdout=SESSION_CONTEXT + "\n")


def _subagent_stop(agent: str, payload: Mapping[str, Any], environ: Mapping[str, str]) -> Outcome:
    session = _text(payload.get("session_id")) or environ.get("CLAUDE_CODE_SESSION_ID")
    subagent = _text(payload.get("agent_id"))
    coordinator = _coordinator(Path(_text(payload.get("cwd")) or os.getcwd()))
    if coordinator is None or not session or not subagent:
        return Outcome()
    try:
        _ensure_session(coordinator, session, environ)
        moved = coordinator.hand_over(agents.subagent_actor(session, subagent), session)
    except OnetreeError as error:
        return Outcome(stderr=f"onetree: the subagent's files stay with it: {error}\n")
    if moved:
        return Outcome(stderr=f"onetree: {len(moved)} file(s) passed from the subagent\n")
    return Outcome()


HANDLERS = {
    "session-start": _session_start,
    "pre-tool-use": _pre_tool_use,
    "post-tool-use": _post_tool_use,
    "subagent-stop": _subagent_stop,
}
AGENT_EVENTS = {
    "claude": ("session-start", "pre-tool-use", "post-tool-use", "subagent-stop"),
    "codex": ("session-start", "pre-tool-use", "post-tool-use"),
}


def run(agent: str, event: str, text: str, environ: Mapping[str, str] | None = None) -> Outcome:
    environ = os.environ if environ is None else environ
    try:
        payload = json.loads(text) if text.strip() else {}
    except ValueError:
        return Outcome(stderr="onetree: the hook input is not JSON; nothing done\n")
    if not isinstance(payload, dict):
        return Outcome(stderr="onetree: the hook input is not a JSON object; nothing done\n")
    try:
        return HANDLERS[event](agent, payload, environ)
    except (OnetreeError, OSError) as error:
        return Outcome(stderr=f"onetree: {error}\n")
