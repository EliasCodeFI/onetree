from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest
from coverage import Coverage, CoverageData
from test_demo import inputs as inputs
from test_demo import run_demo

from onetree import demo


def _line(marker: str) -> int:
    source, start = inspect.getsourcelines(demo._worker)
    matches = [start + offset for offset, text in enumerate(source) if marker in text]
    assert len(matches) == 1, marker
    return matches[0]


def _recorded(paths: set[Path], pid: int, measured: Path) -> set[int]:
    lines: set[int] = set()
    for path in paths:
        if f".pid{pid}." not in path.name:
            continue
        data = CoverageData(basename=str(path))
        data.read()
        for name in data.measured_files():
            if Path(name).resolve() == measured:
                lines.update(data.lines(name) or ())
    return lines


def test_worker_execution_is_present_in_collected_data(
    tmp_path: Path, inputs: tuple[Path, Path],
) -> None:
    collector = Coverage.current()
    if collector is None:
        pytest.skip("collector verification requires a coverage-enabled test run")
    data_file = Path(collector.get_option("run:data_file")).absolute()
    pattern = data_file.name + ".*"
    before = set(data_file.parent.glob(pattern))
    output = tmp_path / "sample"
    report = run_demo(output, inputs, "--workers", "2")
    assert report["ok"], report
    new = set(data_file.parent.glob(pattern)) - before

    measured = Path(demo.__file__).resolve()
    entered = _line('actor = f"demo-worker-{number}"')
    landed = _line('"phase": "landed"')
    refused = _line("refusal = error.payload()")
    controller = json.loads((output / "demo.json").read_text())["controller_pid"]
    assert entered not in _recorded(new, controller, measured)
    for record in report["workers"]:
        lines = _recorded(new, record["pid"], measured)
        assert {entered, landed} <= lines, (
            f"worker {record['number']} (pid {record['pid']}) landed but the collector "
            f"recorded {sorted(lines)} of its body"
        )
        assert (refused in lines) == bool(record["number"]), sorted(lines)
