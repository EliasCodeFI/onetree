"""Regression tests for the repository's CI and source-provenance contract."""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import onetree

_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOW = _ROOT / ".github" / "workflows" / "ci.yml"


def test_tests_import_onetree_from_the_checkout_under_test() -> None:
    """A shared editable environment must fail loudly instead of testing another tree."""

    imported_package = Path(onetree.__file__).resolve().parent
    expected_package = (_ROOT / "src" / "onetree").resolve()

    assert imported_package == expected_package, (
        f"tests collected from {_ROOT} but imported onetree from {imported_package}"
    )


def test_ci_covers_every_supported_python_minor_and_pins_the_admitted_action_revisions() -> None:
    """The workflow must exercise 3.11-3.13 and run only the admitted action revisions.

    Whether a revision runs on Node 24 is measured when it is admitted, not here: this
    test cannot see an action's `action.yml`, so a sha bump to a Node 20 revision passes
    it. The setup-uv sha below was measured `using: "node24"` on 2026-09-10.
    """

    workflow_lines = {
        line.strip()
        for line in (_ROOT / ".github" / "workflows" / "ci.yml").read_text().splitlines()
    }
    python_matrices = {line for line in workflow_lines if line.startswith("python-version: [")}
    checkout_versions = {
        line for line in workflow_lines if line.startswith("- uses: actions/checkout@")
    }
    setup_uv_versions = {
        line.split(" #", 1)[0]
        for line in workflow_lines
        if line.startswith("- uses: astral-sh/setup-uv@")
    }

    assert python_matrices == {'python-version: ["3.11", "3.12", "3.13"]'}
    assert checkout_versions == {"- uses: actions/checkout@v7"}
    assert setup_uv_versions == {
        "- uses: astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d"
    }


def test_ci_gates_run_on_a_fresh_github_hosted_runner_and_admit_pull_requests_from_forks() -> None:
    """A fork's pull request gets the gates, and never on a machine that outlives the job.

    Known-bad: main a768f9a, where the job ran on one self-hosted runner, one leg at a time,
    and its first step failed every pull request whose head was another repository. Outside
    pull requests are accepted since publication, so their code runs only on a GitHub-hosted
    runner, on the `pull_request` event (a read-only token and no secrets for a fork), never
    on `pull_request_target`.
    """

    text = _WORKFLOW.read_text()
    lines = [line.strip() for line in text.splitlines()]

    assert [line for line in lines if line.startswith("runs-on:")] == ["runs-on: ubuntu-24.04"]
    assert not [line for line in lines if line.startswith("max-parallel:")]
    assert "github.event.pull_request.head.repo" not in text
    assert "pull_request:" in lines
    assert "pull_request_target" not in text
    assert text.count("permissions:") == 1
    assert "permissions:\n  contents: read\n" in text


def test_notice_names_the_copyright_holder_beside_the_licence() -> None:
    """Known-bad: main a768f9a, which has a LICENSE and no NOTICE."""

    notice = _ROOT / "NOTICE"
    assert (_ROOT / "LICENSE").is_file()
    assert notice.is_file()
    assert notice.read_text().splitlines()[:2] == ["onetree", "Copyright 2026 Fastnet Oy"]


def test_distributions_carry_the_notice_as_a_licence_file() -> None:
    """Known-bad: main a768f9a, whose wheel carried LICENSE alone."""

    configuration = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    assert configuration["project"]["license-files"] == ["LICENSE", "NOTICE"]


def test_coverage_configuration_measures_python_subprocesses() -> None:
    """pytest-cov 7 delegates child-process startup to coverage.py's patch."""

    configuration = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    dev_dependencies = configuration["project"]["optional-dependencies"]["dev"]
    pytest_cov_dependencies = [
        dependency for dependency in dev_dependencies if dependency.startswith("pytest-cov")
    ]

    assert pytest_cov_dependencies == ["pytest-cov>=7.0"]
    assert configuration["tool"]["coverage"]["run"]["patch"] == ["subprocess"]


def test_mypy_resolves_packages_from_this_checkout() -> None:
    """Test and type gates must not follow another checkout's editable-install path."""

    configuration = tomllib.loads((_ROOT / "pyproject.toml").read_text())

    assert configuration["tool"]["pytest"]["ini_options"]["pythonpath"] == ["src"]
    assert configuration["tool"]["mypy"]["mypy_path"] == "$MYPY_CONFIG_FILE_DIR/src"


def test_subprocess_coverage_probe_executes_onetree_code_only_in_the_child() -> None:
    """Give the coverage gate a child-only package line whose measurement is observable."""

    environment = os.environ.copy()
    source = str(_ROOT / "src")
    inherited_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source if inherited_path is None else os.pathsep.join((source, inherited_path))
    )
    subprocess.run(
        [
            sys.executable,
            "-c",
            "from onetree.proc import process_tag; assert '-' in process_tag()",
        ],
        cwd=_ROOT,
        env=environment,
        check=True,
    )
