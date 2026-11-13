"""`onetree hook` claims a file before an agent edits it and refuses git commands that erase work.

Two Claude Code sessions in one tree, their subagents, and a Codex session, driven through the
hook input Claude Code and Codex document. Known bad: main 0215e36, which has no hook command,
so nothing stops an agent from writing a file another agent is editing.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from conftest import GitFixture

from onetree.cli import main
from onetree.core import Coordinator


def hook(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    agent: str,
    event: str,
    payload: dict[str, Any],
) -> tuple[int, str, str]:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    try:
        code = main(["hook", agent, event])
    except SystemExit as exit_:  # the parser refuses a command it does not know
        code = int(exit_.code or 0)
    out = capsys.readouterr()
    return code, out.out, out.err


def denied(stdout: str) -> str | None:
    if not stdout.strip():
        return None
    decision = json.loads(stdout)["hookSpecificOutput"]
    assert decision["hookEventName"] == "PreToolUse"
    assert decision["permissionDecision"] == "deny"
    return str(decision["permissionDecisionReason"])


@pytest.fixture(autouse=True)
def claude_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_PID", str(os.getpid()))


def edit(root: Path, session: str, name: str, tool: str = "Edit", **extra: Any) -> dict[str, Any]:
    return {
        "session_id": session,
        "cwd": str(root),
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": {"file_path": str(root / name)},
        **extra,
    }


def test_two_sessions_the_second_edit_is_refused_with_the_holders_name(
    git_repo: GitFixture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = git_repo.root
    code, out, _ = hook(
        monkeypatch,
        capsys,
        "claude",
        "session-start",
        {"session_id": "s1", "cwd": str(root), "source": "startup"},
    )
    assert code == 0
    assert "This repository uses onetree" in out

    assert hook(monkeypatch, capsys, "claude", "pre-tool-use", edit(root, "s1", "a.txt"))[:2] == (
        0,
        "",
    )
    code, out, _ = hook(monkeypatch, capsys, "claude", "pre-tool-use", edit(root, "s2", "a.txt"))
    assert code == 0
    reason = denied(out)
    assert reason is not None
    assert "a.txt" in reason
    assert "(s1)" in reason
    new_file = edit(root, "s2", "new.py", tool="Write")
    assert denied(hook(monkeypatch, capsys, "claude", "pre-tool-use", new_file)[1]) is None
    holders = {claim.path: claim.actor for claim in Coordinator(root).claims()}
    assert holders == {"a.txt": "s1", "new.py": "s2"}

    git_repo.write("a.txt", "a1\n")
    payload = {**edit(root, "s1", "a.txt"), "hook_event_name": "PostToolUse", "tool_response": {}}
    assert hook(monkeypatch, capsys, "claude", "post-tool-use", payload)[:2] == (0, "")
    assert (
        git_repo.git("show-ref", "--verify", "refs/onetree/autosave/s1", check=False).returncode
        == 0
    )


def test_a_subagent_claims_for_itself_and_hands_its_files_to_its_session(
    git_repo: GitFixture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = git_repo.root
    sub = edit(root, "s1", "b.txt", agent_id="agent-7", agent_type="Explore")
    assert denied(hook(monkeypatch, capsys, "claude", "pre-tool-use", sub)[1]) is None
    reason = denied(
        hook(monkeypatch, capsys, "claude", "pre-tool-use", edit(root, "s1", "b.txt"))[1]
    )
    assert reason is not None
    assert "s1.agent-7" in reason

    stop = {
        "session_id": "s1",
        "cwd": str(root),
        "hook_event_name": "SubagentStop",
        "agent_id": "agent-7",
        "agent_type": "Explore",
        "stop_hook_active": False,
    }
    code, _, err = hook(monkeypatch, capsys, "claude", "subagent-stop", stop)
    assert code == 0
    assert "1 file(s) passed" in err
    assert (
        denied(hook(monkeypatch, capsys, "claude", "pre-tool-use", edit(root, "s1", "b.txt"))[1])
        is None
    )
    assert [(claim.path, claim.actor) for claim in Coordinator(root).claims()] == [("b.txt", "s1")]
    assert hook(monkeypatch, capsys, "claude", "subagent-stop", stop)[1:] == ("", "")


@pytest.mark.parametrize(
    ("command", "refused"),
    [
        ("git checkout -- a.txt", "checkout"),
        ("cd sub && git -C . reset --hard", "reset"),
        ("GIT_PAGER=cat git -c core.x=1 stash", "stash"),
        ("timeout 30 git commit -m 'fix (bug)'", "commit"),
        ("ls; git clean -fdx", "clean"),
        ("echo $(git switch main)", "switch"),
        ("git stash list", None),
        ("git clean -n", None),
        ("git status && git diff", None),
        ("echo 'git checkout is refused here'", None),
    ],
)
def test_git_commands_that_erase_work_or_move_the_branch_are_refused(
    git_repo: GitFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    refused: str | None,
) -> None:
    payload = {
        "session_id": "s1",
        "cwd": str(git_repo.root),
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }
    code, out, _ = hook(monkeypatch, capsys, "claude", "pre-tool-use", payload)
    assert code == 0
    reason = denied(out)
    if refused is None:
        assert reason is None
    else:
        assert reason is not None
        assert f"git {refused}" in reason


def test_outside_an_onetree_repository_the_hooks_do_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    plain = tmp_path / "plain"
    subprocess.run([shutil.which("git") or "git", "init", "--quiet", str(plain)], check=True)
    (plain / "a.txt").write_text("a\n", encoding="utf-8")
    for event, payload in (
        ("session-start", {"session_id": "s1", "cwd": str(plain)}),
        ("pre-tool-use", edit(plain, "s1", "a.txt")),
        (
            "pre-tool-use",
            {
                "session_id": "s1",
                "cwd": str(plain),
                "tool_name": "Bash",
                "tool_input": {"command": "git reset --hard"},
            },
        ),
        ("post-tool-use", edit(plain, "s1", "a.txt")),
        ("subagent-stop", {"session_id": "s1", "cwd": str(plain), "agent_id": "a"}),
    ):
        assert hook(monkeypatch, capsys, "claude", event, payload) == (0, "", "")
    assert hook(monkeypatch, capsys, "claude", "pre-tool-use", {"tool_name": "Read"}) == (0, "", "")
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert main(["hook", "claude", "pre-tool-use"]) == 0
    assert "not JSON" in capsys.readouterr().err


def test_codex_patches_are_claimed_path_by_path(
    git_repo: GitFixture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = git_repo.root
    patch = (
        "*** Begin Patch\n*** Update File: a.txt\n@@\n-a0\n+a1\n"
        "*** Add File: docs/new.md\n+new\n*** End Patch\n"
    )
    payload = {
        "session_id": "thr_1",
        "cwd": str(root),
        "hook_event_name": "PreToolUse",
        "tool_name": "apply_patch",
        "tool_input": {"command": patch},
    }
    assert (
        hook(
            monkeypatch, capsys, "codex", "session-start", {"session_id": "thr_1", "cwd": str(root)}
        )[0]
        == 0
    )
    assert denied(hook(monkeypatch, capsys, "codex", "pre-tool-use", payload)[1]) is None
    assert sorted(claim.path for claim in Coordinator(root).claims()) == ["a.txt", "docs/new.md"]

    other = {**payload, "session_id": "thr_2"}
    reason = denied(hook(monkeypatch, capsys, "codex", "pre-tool-use", other)[1])
    assert reason is not None
    assert "thr_1" in reason
    shell = {
        "session_id": "thr_2",
        "cwd": str(root),
        "tool_name": "Bash",
        "tool_input": {"command": "git restore a.txt"},
    }
    assert "git restore" in (
        denied(hook(monkeypatch, capsys, "codex", "pre-tool-use", shell)[1]) or ""
    )


def test_the_plugin_files_wire_these_hooks_and_the_mcp_server() -> None:
    root = Path(__file__).resolve().parents[1]
    marketplace = root / ".claude-plugin" / "marketplace.json"
    assert marketplace.is_file()
    listing = json.loads(marketplace.read_text(encoding="utf-8"))
    assert listing["name"] == "onetree"
    entry = listing["plugins"][0]
    assert entry["name"] == "onetree"
    assert entry["source"] == "./plugins/onetree"
    manifest = json.loads(
        (root / "plugins" / "onetree" / ".claude-plugin" / "plugin.json").read_text()
    )
    assert manifest["name"] == entry["name"]
    commands = {
        handler["command"]
        for groups in manifest["hooks"].values()
        for group in groups
        for handler in group["hooks"]
    }
    assert commands == {
        "onetree hook claude session-start",
        "onetree hook claude pre-tool-use",
        "onetree hook claude post-tool-use",
        "onetree hook claude subagent-stop",
    }
    assert set(manifest["hooks"]) == {"SessionStart", "PreToolUse", "PostToolUse", "SubagentStop"}
    assert manifest["mcpServers"] == {"onetree": {"command": "onetree", "args": ["mcp"]}}
