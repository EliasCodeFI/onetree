"""Who is calling: the session id an agent sets, and the agent process a session belongs to."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

from onetree.errors import IdentityError

#: onetree's own variable. A person or a harness sets it on purpose, so it wins over the ids
#: that agents set for themselves.
OWN_SESSION_VARIABLE = "ONETREE_SESSION_ID"
#: Session ids coding agents set: Codex sets CODEX_THREAD_ID, Claude Code sets
#: CLAUDE_CODE_SESSION_ID. CLAUDE_SESSION_ID is still read for setups that export it by hand.
AGENT_SESSION_VARIABLES = ("CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID")
#: Claude Code sets CLAUDE_PID to its own process id for the commands and hooks it runs.
CLAUDE_PID_VARIABLE = "CLAUDE_PID"
_SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh", "mksh", "fish", "busybox"})
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def session_id(environ: Mapping[str, str] | None = None) -> str | None:
    """The session id from the environment, or None when no variable names one."""

    environ = os.environ if environ is None else environ
    own = environ.get(OWN_SESSION_VARIABLE)
    if own:
        return own
    found = {value for name in AGENT_SESSION_VARIABLES if (value := environ.get(name))}
    if len(found) > 1:
        raise IdentityError("session environment is ambiguous", sessions=sorted(found))
    return found.pop() if found else None


def claude_pid(environ: Mapping[str, str] | None = None) -> int | None:
    environ = os.environ if environ is None else environ
    raw = environ.get(CLAUDE_PID_VARIABLE, "")
    return int(raw) if raw.isdigit() and int(raw) > 0 else None


def default_session_pid(actor: str, environ: Mapping[str, str] | None = None) -> int | None:
    """The process a session named by Claude Code's own id should live as long as.

    Claude Code runs every shell command in a new shell, so the parent of `onetree session`
    exits right after it. When the session id is Claude Code's, the session belongs to the
    Claude Code process instead. Otherwise None: the caller's parent, as before.
    """

    environ = os.environ if environ is None else environ
    if actor and environ.get("CLAUDE_CODE_SESSION_ID") == actor:
        return claude_pid(environ)
    return None


def _process_name(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/comm").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def _parent_pid(pid: int) -> int | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        # The name in field 2 may hold spaces and parentheses; the fields after it do not.
        return int(stat.rsplit(")", 1)[1].split()[1])
    except (OSError, IndexError, ValueError):
        return None


def agent_pid(environ: Mapping[str, str] | None = None) -> int:
    """The coding agent's own process, seen from a hook or a helper the agent started.

    CLAUDE_PID when Claude Code set it. Otherwise the nearest ancestor that is not a shell,
    since agents start hook commands through a shell or directly.
    """

    found = claude_pid(environ)
    if found is not None:
        return found
    pid = os.getppid()
    for _ in range(8):
        if _process_name(pid) not in _SHELLS:
            return pid
        parent = _parent_pid(pid)
        if parent is None or parent <= 1:
            return pid
        pid = parent
    return pid


def subagent_actor(session: str, agent: str) -> str:
    """The id a subagent of ``session`` claims under: one per subagent, at most 128 characters."""

    suffix = _UNSAFE.sub("-", agent).strip("-.") or "agent"
    return f"{session[:63]}.{suffix[:64]}"
