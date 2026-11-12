"""The repository's layout: the benchmark harness lives in a repository of its own, and coding
agents read their instructions from AGENTS.md alone."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_the_package_ships_no_benchmark_harness() -> None:
    assert importlib.util.find_spec("onetree.bench") is None
    assert not (ROOT / "src" / "onetree" / "bench").exists()


def test_agents_read_agents_md_and_no_claude_md_hides_it() -> None:
    # Claude Code reads AGENTS.md only when no CLAUDE.md exists in the directory or above it.
    assert (ROOT / "AGENTS.md").is_file()
    assert not (ROOT / "CLAUDE.md").exists()


def test_agent_sandbox_placeholders_stay_out_of_the_repository() -> None:
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for placeholder in (".claude/", ".codex/", ".mcp.json"):
        assert placeholder in ignored, placeholder
