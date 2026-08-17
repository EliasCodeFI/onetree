"""Process-liveness primitives shared by the Git layer and the coordinator.

Liveness is (pid, birth-tick) rather than bare pid: a recycled pid must not
revive a dead session. Kernels that do not expose a birth tick degrade to bare
pid existence, and callers are expected to surface that degradation.
"""

from __future__ import annotations

import os
from pathlib import Path

_STAT_START_FIELD = 19


def process_start(pid: int) -> int | None:
    """Return Linux's process-birth tick, or None where the kernel hides it."""

    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        closing = text.rfind(")")
        if closing < 0:
            return None
        fields = text[closing + 2 :].split()
        return int(fields[_STAT_START_FIELD])
    except (FileNotFoundError, IndexError, OSError, UnicodeError, ValueError):
        return None


def process_alive(pid: int, expected_start: int | None) -> bool:
    """Report whether ``pid`` is the same incarnation that recorded the tick."""

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    current_start = process_start(pid)
    return expected_start is None or current_start == expected_start


def process_tag(pid: int | None = None) -> str:
    """Return a filename-safe ``pid-start`` owner tag for temporary state."""

    owner = os.getpid() if pid is None else pid
    return f"{owner}-{process_start(owner) or 0}"
