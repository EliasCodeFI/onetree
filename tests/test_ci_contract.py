"""Regression tests for the repository's CI and source-provenance contract."""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import onetree

_ROOT = Path(__file__).resolve().parents[1]


def test_tests_import_onetree_from_the_checkout_under_test() -> None:
    """A shared editable environment must fail loudly instead of testing another tree."""

    imported_package = Path(onetree.__file__).resolve().parent
    expected_package = (_ROOT / "src" / "onetree").resolve()

    assert imported_package == expected_package, (
        f"tests collected from {_ROOT} but imported onetree from {imported_package}"
    )


def test_ci_covers_every_supported_python_minor_and_uses_node24_actions() -> None:
    """The workflow must exercise 3.11-3.13 without deprecated Node 20 actions."""

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
