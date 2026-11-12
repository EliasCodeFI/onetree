"""Regression tests for the repository's CI and source-provenance contract."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import ci_matrix
import pytest

import onetree

_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOW = _ROOT / ".github" / "workflows" / "ci.yml"
_HELPER = _ROOT / "tests" / "ci_matrix.py"


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


def _declared_minors() -> list[str]:
    lines = [
        line.strip() for line in _WORKFLOW.read_text().splitlines()
        if line.strip().startswith("python-version: [")
    ]
    assert len(lines) == 1
    minors = json.loads(lines[0].split(": ", 1)[1])
    assert isinstance(minors, list)
    return [str(minor) for minor in minors]


def _workflow_run_line(step_name: str) -> str:
    lines = [line.strip() for line in _WORKFLOW.read_text().splitlines()]
    start = lines.index(f"- name: {step_name}")
    run = next(line for line in lines[start + 1:] if line.startswith("run: "))
    return run.removeprefix("run: ") + "\n"


def _fake_resolver(minor: str) -> ci_matrix.Interpreter:
    return ci_matrix.Interpreter(minor, f"/managed/cpython-{minor}.99/bin/python{minor}",
                                 f"{minor}.99")


def _fake_uv(tmp_path: Path) -> Path:
    uv = tmp_path / "bin" / "uv"
    uv.parent.mkdir()
    version = ci_matrix.parse_workflow(_WORKFLOW.read_text()).uv_version
    uv.write_text(f"#!{sys.executable}\nprint({f'uv {version} (contract fixture)'!r})\n")
    uv.chmod(0o755)
    return uv


def _plan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], *extra: str,
    resolve: ci_matrix.Resolver = _fake_resolver,
) -> tuple[int, str, Path, Path]:
    run_root = tmp_path / "run"
    provenance = tmp_path / "provenance.json"
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    code = ci_matrix.main(
        ["plan", "--run-root", str(run_root), "--provenance", str(provenance),
         "--home", str(home), "--uv", str(_fake_uv(tmp_path)), "--source", str(_ROOT), *extra],
        resolve=resolve,
    )
    return code, capsys.readouterr().out, run_root, provenance


def _argument(command: list[str], flag: str) -> str:
    return command[command.index(flag) + 1]


def test_local_ci_plan_derives_the_declared_set_and_gate_commands_from_the_workflow(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """The emitted plan, not a copied constant, must carry CI's minors and gate commands."""

    code, output, run_root, provenance_path = _plan(tmp_path, capsys)
    assert code == 0
    plan = json.loads(output)
    assert isinstance(plan, list)
    assert all(set(item) == {"name", "cmd"} and isinstance(item["name"], str)
               and all(isinstance(part, str) for part in item["cmd"]) for item in plan)
    minors = _declared_minors()
    gates = {name: _workflow_run_line(name) for name in ("Install", "Lint", "Types",
                                                         "Tests + coverage gate")}
    expected_names = ["actions/checkout", "astral-sh/setup-uv",
                      "Require HOME and an absolute PATH on the runner",
                      "Prepare standalone Python", *gates]
    assert [item["name"] for item in plan] == [
        f"{minor}:{number:02d}:{name}"
        for minor in minors for number, name in enumerate(expected_names, start=1)
    ]
    for item in plan:
        command = item["cmd"]
        label, number, name = item["name"].split(":", 2)
        action = command[command.index("--") + 1:]
        workspace = run_root / label / "workspace"
        assert _argument(command, "--cwd") == str(workspace)
        assert [int(part) for index, part in enumerate(command)
                if index and command[index - 1] == "--requires"] == list(range(1, int(number)))
        search_path = _argument(command, "--path").split(":")
        after_prepare = int(number) > expected_names.index("Prepare standalone Python") + 1
        assert (search_path[0] == str(workspace / ".venv" / "bin")) is after_prepare
        assert not any("ONETREE_TEST" in part for part in command)
        if name in gates:
            assert action == ["/usr/bin/bash", "-e", "-c", gates[name]]
        if name == "Prepare standalone Python":
            script = action[3]
            assert "--no-python-downloads" in script
            assert f"--python /managed/cpython-{label}.99/bin/python{label}" in script
            assert script.endswith("env -i PATH=/usr/bin:/bin .venv/bin/python -I -S --version\n")
    provenance = json.loads(provenance_path.read_text())
    assert provenance["workflow"]["declared"] == minors
    assert provenance["workflow"]["sha256"] == hashlib.sha256(_WORKFLOW.read_bytes()).hexdigest()
    assert [leg["label"] for leg in provenance["legs"]] == minors
    assert {leg["kind"] for leg in provenance["legs"]} == {"declared"}
    assert provenance["extras"] == []
    assert (run_root / "run-id").read_text().strip() == provenance["run_id"]


def test_local_ci_extras_are_labelled_and_never_declared(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    code, output, _, provenance_path = _plan(tmp_path, capsys, "--extra", "3.99")
    assert code == 0
    labels = {item["name"].split(":", 1)[0] for item in json.loads(output)}
    assert labels == {*_declared_minors(), "extra-3.99"}
    provenance = json.loads(provenance_path.read_text())
    assert provenance["workflow"]["declared"] == _declared_minors()
    assert [leg["kind"] for leg in provenance["legs"] if leg["label"] == "extra-3.99"] == ["extra"]

    again = tmp_path / "again"
    again.mkdir()
    code, output, run_root, _ = _plan(again, capsys, "--extra", _declared_minors()[0])
    assert code == 2
    assert output == ""
    assert not run_root.exists()


def test_local_ci_plan_refuses_a_missing_interpreter_without_emitting_anything(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    missing = _declared_minors()[-1]

    def resolve(minor: str) -> ci_matrix.Interpreter:
        if minor == missing:
            raise ci_matrix.ResolutionError(f"Python {minor} is not installed")
        return _fake_resolver(minor)

    code, output, run_root, provenance_path = _plan(tmp_path, capsys, resolve=resolve)
    assert code == 2
    assert output == ""
    assert not run_root.exists()
    assert not provenance_path.exists()


_LINT = "run: ruff check src tests"
_PR_GUARD = (
    "if: github.event_name == 'pull_request' && "
    "github.event.pull_request.head.repo.full_name != github.repository"
)
# task198: the Owner's review input, `&&` binding tighter than `||`, so true on every event.
_ALWAYS_TRUE_TYPES = (
    "        if: github.event_name == 'pull_request' && false || true\n        run: exit 73\n"
)
_CHECKOUT_STEP = "      - uses: actions/checkout@v7\n"
# The first step the workflow carried until publication (main a768f9a): it failed every pull
# request from a fork. The workflow no longer has it; the derivation still recognizes it.
_FORK_GUARD_STEP = (
    "      - name: Require repository-owned pull request\n"
    f"        {_PR_GUARD}\n"
    "        run: exit 1\n"
)


def _with_fork_guard(text: str) -> str:
    assert text.count(_CHECKOUT_STEP) == 1
    return text.replace(_CHECKOUT_STEP, _FORK_GUARD_STEP + _CHECKOUT_STEP)


@pytest.mark.parametrize(("edit", "pattern", "replacement"), [
    ("missing declaration", r"(?m)^\s*python-version: .*\n", ""),
    ("duplicate declaration", r"(?m)^(\s*)(python-version: .*)$", r"\1\2\n\1\2"),
    ("non-JSON declaration", r"(?m)^(\s*python-version: )\[.*\]$", r"\1[3.11, 3.12]"),
    ("empty declaration", r"(?m)^(\s*python-version: )\[.*\]$", r"\1[]"),
    ("matrix include", r"(?m)^(\s*)(python-version: .*)$", r"\1\2\n\1include: []"),
    ("fail-fast enabled", r"fail-fast: false", "fail-fast: true"),
    ("job environment", r"(?m)^(    )(steps:)$", r"\1env:\n\1  A: b\n\1\2"),
    ("unsupported step key", re.escape(_LINT), f"shell: sh\\n        {_LINT}"),
    ("unsupported condition", re.escape(_LINT), f"if: always()\\n        {_LINT}"),
    ("unknown action", r"actions/checkout@v7", "actions/setup-python@v5"),
    ("changed preparation", r"--seed \.venv", "--seed venv"),
    ("runner variable in a gate", re.escape(_LINT), f"{_LINT} $RUNNER_TEMP"),
    ("second job", r"(?m)^(  )(gates:)$", r"\1other:\n\1  runs-on: x\n\1\2"),
    ("condition true on every event", r"(?m)^        run: mypy\n", _ALWAYS_TRUE_TYPES),
    ("pull-request guard widened with or", re.escape(_PR_GUARD),
     _PR_GUARD + " || github.event_name == 'push'"),
    ("pull-request guard with an unfamiliar conjunct", re.escape(_PR_GUARD),
     _PR_GUARD + " && github.actor != 'bot'"),
    ("expression-wrapped pull-request guard", re.escape(_PR_GUARD),
     "if: ${{ " + _PR_GUARD.removeprefix("if: ") + " }}"),
    ("pull-request condition on an action step", r"(?m)^      - uses: actions/checkout@v7$",
     "      - " + _PR_GUARD + "\n        uses: actions/checkout@v7"),
    ("no push trigger", r"(?m)^  push:\n    branches: \[main\]\n", ""),
    ("serialized legs", r"(?m)^( +)(fail-fast: false)$", r"\1\2\n\1max-parallel: 1"),
])
def test_workflow_shapes_the_derivation_does_not_recognize_are_refused(
    edit: str, pattern: str, replacement: str,
) -> None:
    text = _WORKFLOW.read_text()
    ci_matrix.parse_workflow(text)  # control: the unedited workflow is recognized
    if pattern == re.escape(_PR_GUARD):
        # The workflow no longer carries the fork guard: these cases edit it where it is put back.
        text = _with_fork_guard(text)
        guarded = ci_matrix.parse_workflow(text)  # control: the exact guard is recognized
        assert [step.name for step in guarded.steps if step.kind == "not-applicable"] == [
            "Require repository-owned pull request"
        ]
    edited = re.sub(pattern, replacement, text, count=1)
    assert edited != text, edit
    with pytest.raises(ci_matrix.WorkflowShapeError):
        ci_matrix.parse_workflow(edited)


def test_the_plan_command_refuses_a_condition_that_is_true_on_every_event(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """task198: a required gate behind an unfamiliar condition must not vanish from the plan."""

    text = _WORKFLOW.read_text()
    workflow = ci_matrix.parse_workflow(text)
    assert [(step.name, step.kind) for step in workflow.steps if step.name == "Types"] == [
        ("Types", "run")
    ]
    assert [step.name for step in workflow.steps if step.kind == "not-applicable"] == []
    edited = text.replace("        run: mypy\n", _ALWAYS_TRUE_TYPES)
    assert edited != text
    bad = tmp_path / "ci.yml"
    bad.write_text(edited)
    code, output, run_root, provenance = _plan(tmp_path, capsys, "--workflow", str(bad))
    assert code == 2
    assert output == ""
    assert not run_root.exists()
    assert not provenance.exists()


def _step(run_root: Path, number: int, *action: str, requires: tuple[int, ...] = (),
          run_id: str = "contract") -> subprocess.CompletedProcess[bytes]:
    command = [sys.executable, "-I", "-B", str(_HELPER), "step", "--run-root", str(run_root),
               "--run-id", run_id, "--leg", "leg", "--number", str(number),
               "--cwd", str(run_root), "--home", str(run_root), "--path", "/usr/bin:/bin"]
    for required in requires:
        command += ["--requires", str(required)]
    return subprocess.run([*command, "--", *action], capture_output=True, check=False)


def test_a_step_after_a_failed_prerequisite_is_not_run_and_logs_are_complete(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    run_root.mkdir()
    (run_root / "run-id").write_text("contract\n")
    noisy = "printf 'x%.0s' $(seq 1 3000); exit 3"
    failed = _step(run_root, 1, "/usr/bin/bash", "-c", noisy)
    assert failed.returncode == 3
    assert (run_root / "leg" / "logs" / "1.log").read_bytes() == b"x" * 3000
    assert not (run_root / "leg" / "stamps" / "1.pass").exists()

    # Control: the same dependent command succeeds when nothing gates it.
    assert _step(run_root, 9, "/bin/true").returncode == 0
    dependent = _step(run_root, 2, "/bin/true", requires=(1,))
    assert dependent.returncode == ci_matrix.NOT_RUN
    assert b"NOT RUN: prerequisite step 1" in dependent.stdout
    assert not (run_root / "leg" / "stamps" / "2.pass").exists()
    assert not (run_root / "leg" / "logs" / "2.log").exists()

    passed = _step(run_root, 3, "/usr/bin/bash", "-c", "echo ok")
    stamp = json.loads((run_root / "leg" / "stamps" / "3.pass").read_text())
    assert passed.returncode == 0
    assert stamp["log_sha256"] == hashlib.sha256(b"ok\n").hexdigest()
    assert _step(run_root, 4, "/bin/true", requires=(3,)).returncode == 0
    assert _step(run_root, 3, "/bin/true").returncode == ci_matrix.NOT_RUN
    assert _step(run_root, 5, "/bin/true", run_id="other").returncode == ci_matrix.NOT_RUN

    unstartable = _step(run_root, 6, str(tmp_path / "missing-executable"))
    assert unstartable.returncode == 127
    assert b"could not start" in (run_root / "leg" / "logs" / "6.log").read_bytes()
    assert not (run_root / "leg" / "stamps" / "6.pass").exists()


def test_the_checkout_step_copies_the_tree_without_local_artifacts(tmp_path: Path) -> None:
    source = tmp_path / "source"
    for relative in ("src/pkg/module.py", "tests/test_x.py", ".git/HEAD", ".venv/bin/python",
                     "src/pkg.egg-info/PKG-INFO", "src/pkg/__pycache__/module.pyc",
                     ".coverage.host.1"):
        (source / relative).parent.mkdir(parents=True, exist_ok=True)
        (source / relative).write_text(relative)
    run_root = tmp_path / "run"
    run_root.mkdir()
    (run_root / "run-id").write_text("contract\n")
    workspace = run_root / "leg" / "workspace"
    assert _step(run_root, 1, "checkout", str(source), str(workspace)).returncode == 0
    copied = sorted(str(path.relative_to(workspace)) for path in workspace.rglob("*")
                    if path.is_file())
    assert copied == ["src/pkg/module.py", "tests/test_x.py"]


def test_local_ci_helper_selftest_passes() -> None:
    completed = subprocess.run(
        [sys.executable, "-I", "-B", str(_HELPER), "--selftest"],
        capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert re.search(r"selftest: (\d+)/\1 passed", completed.stdout)
