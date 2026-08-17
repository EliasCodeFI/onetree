"""CLI contract tests: exit codes and the --json single-object guarantee."""

from __future__ import annotations

import json

import pytest

from onetree import __version__
from onetree.cli import main


def test_status_human_output(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["status"]) == 0
    out = capsys.readouterr()
    assert __version__ in out.out
    assert out.err == ""


def test_status_json_is_single_parseable_object(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["status", "--json"]) == 0
    out = capsys.readouterr()
    payload = json.loads(out.out)
    assert payload == {"name": "onetree", "version": __version__, "phase": "gate-0"}
    assert out.err == ""


def test_unknown_command_is_refused() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["no-such-command"])
    assert exc_info.value.code == 2
