"""A Claude Code session is recognised by CLAUDE_CODE_SESSION_ID and lives as long as Claude Code.

Known bad: main 0215e36, which reads CLAUDE_SESSION_ID (a name Claude Code does not set), so
`onetree session` inside Claude Code asks for a session id, and ties a session to the shell that
ran it, which exits at once.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Sequence
from typing import Any

import pytest
from conftest import GitFixture

from onetree.cli import main


def run(argv: Sequence[str]) -> int:
    try:
        return main(list(argv))
    except SystemExit as exit_:  # the parser refuses a command or option it does not know
        return int(exit_.code or 0)


def as_json(capsys: pytest.CaptureFixture[str], argv: Sequence[str]) -> tuple[int, dict[str, Any]]:
    code = run([*argv, "--json"])
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else {}


def test_a_claude_code_session_registers_for_the_claude_code_process(
    git_repo: GitFixture, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude-1")
    monkeypatch.setenv("CLAUDE_PID", str(os.getpid()))
    code, payload = as_json(capsys, ["session", "--repo", str(git_repo.root)])
    assert code == 0, payload
    assert payload["session"]["actor"] == "claude-1"
    assert payload["session"]["pid"] == os.getpid()

    code, payload = as_json(
        capsys, ["claim", str(git_repo.root / "a.txt"), "--repo", str(git_repo.root)]
    )
    assert code == 0, payload
    assert [claim["actor"] for claim in payload["claim"]["claims"]] == ["claude-1"]


def test_an_explicit_pid_and_other_session_ids_keep_the_parent_rule(
    git_repo: GitFixture, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude-2")
    monkeypatch.setenv("CLAUDE_PID", "999999999")
    code, payload = as_json(
        capsys, ["session", "--repo", str(git_repo.root), "--pid", str(os.getpid())]
    )
    assert code == 0, payload
    assert payload["session"]["pid"] == os.getpid()
    # A session named another way is not Claude Code's own, so CLAUDE_PID does not apply to it.
    code, payload = as_json(
        capsys,
        ["session", "--repo", str(git_repo.root), "--session", "other", "--pid", str(os.getpid())],
    )
    assert code == 0, payload
    assert payload["session"]["actor"] == "other"


def test_a_hidden_claude_code_process_gets_a_plain_answer(
    git_repo: GitFixture, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    finished = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        capture_output=True,
        text=True,
        check=True,
    )
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude-3")
    monkeypatch.setenv("CLAUDE_PID", finished.stdout.strip())
    code, payload = as_json(capsys, ["session", "--repo", str(git_repo.root)])
    assert code == 2, payload
    assert payload["error"]["type"] == "invalid_identity"
    assert "not visible from this shell" in payload["error"]["message"]
