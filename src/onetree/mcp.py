"""`onetree mcp`: claim, release, claims and land as tools over the Model Context Protocol.

The server reads one JSON-RPC message per line on stdin and answers on stdout. Claude Code starts
it once per session and runs it outside the command sandbox, so the agent's own process stays
visible to it: claims and landings work from a session whose shell commands are sandboxed.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import IO, Any

from onetree import __version__, agents
from onetree.core import Coordinator
from onetree.errors import IdentityError, OnetreeError

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
_PATHS = {
    "type": "array",
    "items": {"type": "string"},
    "description": "Absolute paths, or paths relative to the repository's top folder.",
}
TOOLS: list[dict[str, Any]] = [
    {
        "name": "claim",
        "description": "Claim files for this session before writing them. Files that do not "
        "exist yet can be claimed too. Refused when another agent holds one; the answer names it.",
        "inputSchema": {"type": "object", "properties": {"paths": _PATHS}, "required": ["paths"]},
    },
    {
        "name": "release",
        "description": "Give up claims this session holds (all of them when no paths are given).",
        "inputSchema": {"type": "object", "properties": {"paths": _PATHS}},
    },
    {
        "name": "claims",
        "description": "Who holds which files in this shared working tree.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "land",
        "description": "Land this session's work: stage every file it holds, check the commit, run "
        "the repository's tests and move the branch. Takes as long as the tests take.",
        "inputSchema": {
            "type": "object",
            "properties": {"message": {"type": "string", "description": "The commit message."}},
            "required": ["message"],
        },
    },
]


class Server:
    def __init__(self, repo: str, actor: str | None, environ: Mapping[str, str]) -> None:
        self.repo = repo
        self.actor = actor
        self.environ = environ
        self._coordinator: Coordinator | None = None

    def coordinator(self) -> Coordinator:
        if self._coordinator is None:
            self._coordinator = Coordinator(self.repo)
        return self._coordinator

    def session(self) -> str:
        if not self.actor:
            raise IdentityError("no session id: start the server with --session or from an agent")
        coordinator = self.coordinator()
        try:
            coordinator.register_session(self.actor, pid=agents.agent_pid(self.environ))
        except IdentityError as error:
            if "already registered" not in str(error):
                raise
        return self.actor

    def _absolute(self, paths: Any) -> list[str]:
        if not isinstance(paths, list) or not all(isinstance(item, str) for item in paths):
            raise ValueError("paths must be a list of strings")
        root = self.coordinator().repo.root
        return [str(Path(item) if os.path.isabs(item) else root / item) for item in paths]

    def call(self, name: str, arguments: Mapping[str, Any]) -> tuple[str, bool]:
        """Run one tool; the text the agent reads and whether it reports an error."""

        try:
            if name == "claims":
                held = self.coordinator().claims()
                if not held:
                    return "nobody holds a claim", False
                mine = "  (this session)"
                lines = [
                    f"{claim.path}  {claim.actor}{mine if claim.actor == self.actor else ''}"
                    for claim in held
                ]
                return "\n".join(lines), False
            actor = self.session()
            coordinator = self.coordinator()
            if name == "claim":
                result = coordinator.claim(actor, self._absolute(arguments.get("paths")))
                return "claimed " + ", ".join(claim.path for claim in result.claims), False
            if name == "release":
                released = coordinator.release(actor, self._absolute(arguments.get("paths", [])))
                return "released " + (", ".join(released.released) or "nothing"), False
            if name == "land":
                message = arguments.get("message")
                if not isinstance(message, str) or not message.strip():
                    raise ValueError("message must be a non-empty string")
                coordinator.renew(actor)
                staged = coordinator.stage_claimed(actor)
                if not staged:
                    return "nothing to land: the files this session holds are unchanged", True
                landed = coordinator.land(actor, message=message)
                return (
                    f"landed {landed.commit} on {landed.target}: {', '.join(landed.payload)}",
                    False,
                )
            return f"unknown tool {name}", True
        except OnetreeError as error:
            return f"refused: {error} {json.dumps(error.payload(), sort_keys=True)}", True
        except ValueError as error:
            return f"invalid arguments: {error}", True

    def handle(self, message: Mapping[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        identifier = message.get("id")
        if identifier is None:
            return None  # a notification: nothing to answer
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        if method == "initialize":
            requested = params.get("protocolVersion")
            version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            result: dict[str, Any] = {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "onetree", "version": __version__},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments")
            if not isinstance(name, str) or name not in {tool["name"] for tool in TOOLS}:
                return _error(identifier, -32602, f"unknown tool: {name}")
            text, failed = self.call(name, arguments if isinstance(arguments, dict) else {})
            result = {"content": [{"type": "text", "text": text}], "isError": failed}
        else:
            return _error(identifier, -32601, f"method not found: {method}")
        return {"jsonrpc": "2.0", "id": identifier, "result": result}


def _error(identifier: Any, code: int, text: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": identifier, "error": {"code": code, "message": text}}


def serve(
    repo: str,
    actor: str | None,
    stdin: IO[str],
    stdout: IO[str],
    environ: Mapping[str, str] | None = None,
) -> int:
    server = Server(repo, actor, os.environ if environ is None else environ)

    def write(answer: Mapping[str, Any]) -> None:
        stdout.write(json.dumps(answer, ensure_ascii=True) + "\n")
        stdout.flush()

    for line in stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            write(_error(None, -32700, "parse error"))
            continue
        if not isinstance(message, dict):
            write(_error(None, -32600, "invalid request"))
            continue
        answer = server.handle(message)
        if answer is not None:
            write(answer)
    return 0
