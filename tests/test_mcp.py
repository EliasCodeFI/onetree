"""`onetree mcp` serves claim, release, claims and land over MCP on stdio.

Claude Code runs MCP servers outside the command sandbox, so this is how a session whose shell is
sandboxed still lands. Known bad: main 0215e36, which has no such server.
"""

from __future__ import annotations

import io
import json
import os
import sys
from typing import Any

import pytest
from conftest import GitFixture

from onetree.cli import main


def serve(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    messages: list[Any],
) -> tuple[int, dict[Any, dict[str, Any]]]:
    lines = [message if isinstance(message, str) else json.dumps(message) for message in messages]
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n".join(lines) + "\n\n"))
    try:
        code = main(["mcp", *argv])
    except SystemExit as exit_:
        code = int(exit_.code or 0)
    answers = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    return code, {answer.get("id"): answer for answer in answers}


def call(identifier: int, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    return {"jsonrpc": "2.0", "id": identifier, "method": "tools/call", "params": params}


def text(answer: dict[str, Any]) -> tuple[str, bool]:
    result = answer["result"]
    return result["content"][0]["text"], result["isError"]


def test_a_session_claims_and_lands_through_the_server(
    git_repo: GitFixture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLAUDE_PID", str(os.getpid()))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(git_repo.root))
    git_repo.write("a.txt", "a1\n")
    git_repo.write("b.txt", "someone else's edit\n")
    code, answers = serve(
        monkeypatch,
        capsys,
        [],
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "t"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            call(3, "claim", {"paths": ["a.txt", str(git_repo.root / "new.txt")]}),
            call(4, "claims"),
            call(5, "land", {"message": "s1 lands a.txt"}),
            call(6, "release", {"paths": ["new.txt"]}),
            {"jsonrpc": "2.0", "id": 7, "method": "ping"},
        ],
    )
    assert code == 0
    assert answers[1]["result"]["protocolVersion"] == "2025-06-18"
    assert answers[1]["result"]["serverInfo"]["name"] == "onetree"
    assert [tool["name"] for tool in answers[2]["result"]["tools"]] == [
        "claim",
        "release",
        "claims",
        "land",
    ]
    assert text(answers[3]) == ("claimed a.txt, new.txt", False)
    assert text(answers[4]) == ("a.txt  s1  (this session)\nnew.txt  s1  (this session)", False)
    landed, failed = text(answers[5])
    assert not failed
    assert landed.startswith("landed ")
    assert landed.endswith(": a.txt")
    assert git_repo.git("show", "main:a.txt").stdout == "a1\n"
    assert git_repo.git("show", "main:b.txt").stdout == "b0\n"
    assert text(answers[6]) == ("released new.txt", False)
    assert answers[7]["result"] == {}


def test_refusals_and_bad_requests_come_back_as_answers(
    git_repo: GitFixture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLAUDE_PID", str(os.getpid()))
    repo = ["--repo", str(git_repo.root)]
    serve(monkeypatch, capsys, [*repo, "--session", "s1"], [call(1, "claim", {"paths": ["a.txt"]})])
    code, answers = serve(
        monkeypatch,
        capsys,
        [*repo, "--session", "s2"],
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "1999-01-01"},
            },
            call(2, "claim", {"paths": ["a.txt"]}),
            call(3, "land", {"message": "nothing"}),
            call(4, "land", {}),
            call(5, "claim", {"paths": "a.txt"}),
            call(6, "nope"),
            {"jsonrpc": "2.0", "id": 7, "method": "resources/list"},
            "this is not json",
            "[1, 2]",
        ],
    )
    assert code == 0
    assert answers[1]["result"]["protocolVersion"] == "2025-06-18"
    refusal, failed = text(answers[2])
    assert failed
    assert "held by s1" in refusal
    assert text(answers[3]) == ("nothing to land: the files this session holds are unchanged", True)
    assert text(answers[4])[0].startswith("invalid arguments")
    assert text(answers[5])[0].startswith("invalid arguments")
    assert answers[6]["error"]["code"] == -32602
    assert answers[7]["error"]["code"] == -32601
    assert answers[None]["error"]["code"] in (-32700, -32600)

    code, answers = serve(monkeypatch, capsys, repo, [call(1, "claim", {"paths": ["a.txt"]})])
    assert text(answers[1])[0].startswith("refused: no session id")
