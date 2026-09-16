from __future__ import annotations

from pathlib import Path

import pytest
from conftest import GitFixture
from coverage import Coverage, CoverageData
from test_referee import staged

from onetree import referee


def test_supervisor_execution_is_present_in_collected_data(git_repo: GitFixture) -> None:
    collector = Coverage.current()
    if collector is None:
        pytest.skip("collector verification requires a coverage-enabled test run")
    data_file = Path(collector.get_option("run:data_file")).absolute()
    pattern = data_file.name + ".*"
    before = set(data_file.parent.glob(pattern))
    git_repo.git("config", "onetree.referee", "/bin/true")
    landed = staged(git_repo).land("alpha")
    assert landed.referee_receipt["result"] == "pass"

    supervisor = str(Path(referee.__file__).with_name("_referee_supervisor.py"))
    executions = []
    for path in set(data_file.parent.glob(pattern)) - before:
        data = CoverageData(basename=str(path))
        data.read()
        if lines := data.lines(supervisor):
            executions.append(lines)
    assert executions, "supervisor ran successfully but collector recorded none of its execution"
