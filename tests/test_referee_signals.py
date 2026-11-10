from __future__ import annotations

import signal
import subprocess
from pathlib import Path
from typing import Any

import pytest
from conftest import GitFixture
from test_referee import configure, staged


def test_referee_command_starts_with_default_sigint(
    git_repo: GitFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Known bad: base 0adeed777bc67580ad3b560cad5c966564118b4d inherits ignored SIGINT."""
    disposition = tmp_path / "referee-sigint"
    configure(
        git_repo,
        "import signal; from pathlib import Path; "
        f"Path({str(disposition)!r}).write_text("
        "'default' if signal.getsignal(signal.SIGINT) is signal.default_int_handler "
        "else 'not-default')",
    )
    coordinator = staged(git_repo)
    popen = subprocess.Popen
    supervisor_launches: list[list[str]] = []

    def launch_with_ignored_sigint(*args: Any, **kwargs: Any) -> Any:
        command = args[0] if args else kwargs.get("args", ())
        if isinstance(command, (list, tuple)) and any(
            str(item).endswith("_referee_supervisor.py") for item in command
        ):
            assert kwargs.get("preexec_fn") is None
            kwargs["preexec_fn"] = lambda: signal.signal(signal.SIGINT, signal.SIG_IGN)
            supervisor_launches.append(list(command))
        return popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", launch_with_ignored_sigint)
    receipt = coordinator.land("alpha").referee_receipt
    assert len(supervisor_launches) == 1
    assert receipt["result"] == "pass"
    assert disposition.read_text(encoding="utf-8") == "default"
