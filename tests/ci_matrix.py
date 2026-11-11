"""Derive a local run of this repository's CI workflow as a gate_receipt plan.

Contract (OneTree task97, Owner disposition 82f69cd68e5c):

- The declared interpreter set and the gate commands come from the workflow file.
  Only the workflow shape recognized here is accepted. Any other shape fails
  instead of being guessed at. A step condition is recognized only when it is
  exactly a supported pull-request-only condition (task198); every other
  condition is refused, never evaluated, so no required step is silently omitted.
- Every declared minor resolves to an existing user-managed interpreter with
  downloads disabled; a missing minor is an error, never a silent download.
  Extra minors are labelled ``extra-<minor>`` and are never part of the declared set.
- The installed uv release must match the workflow pin at planning and setup.
  A missing, unreadable or different version is an error; this helper never installs
  a replacement or changes the workflow pin to fit the local machine (task212).
- ``plan`` writes exactly the JSON array ``gate_receipt.py run --plan`` accepts to
  stdout. Interpreter and workflow provenance goes only to the ``--provenance`` file.
- Each leg runs in a fresh private copy with a fresh virtual environment under a
  run root that must not exist beforehand. Step environments are explicit: HOME
  and PATH only, so ``ONETREE_TEST_ESTATE_GATE`` and ``ONETREE_TEST_SHIPPED_GATE``
  are absent.
- A step whose prerequisite has no pass record in the same run exits non-zero
  without running, so no dependent step is recorded as passing after a failure.
  Every step's complete output is kept in a log beside the run.

A local result is a local result: it is not evidence of a hosted job.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
BASH = "/usr/bin/bash"
BASE_PATH = ("/usr/local/bin", "/usr/bin", "/bin")
REMOVED_ENVIRONMENT = ("ONETREE_TEST_ESTATE_GATE", "ONETREE_TEST_SHIPPED_GATE")
INVENTORY_MODULES = ("pip", "pytest", "pytest_cov", "coverage", "mypy", "ruff")
NOT_RUN = 125
COPY_EXCLUDES = (
    ".git", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "htmlcov", "build", "dist",
)
_COPY_EXCLUDE_PATTERNS = ("*.egg-info", ".coverage", ".coverage.*", ".venv*")
_MINOR = re.compile(r"3\.(0|[1-9][0-9]*)")
_TOP_KEYS = frozenset({"name", "on", "permissions", "jobs"})
_JOB_KEYS = frozenset({"runs-on", "strategy", "steps"})
_STRATEGY_KEYS = frozenset({"fail-fast", "matrix"})
_STEP_KEYS = frozenset({"name", "if", "uses", "with", "env", "run"})
# Step conditions recognized as pull-request-only, compared exactly. No expression is
# evaluated: any other condition, including one extending this text, is refused.
_PR_ONLY_CONDITIONS = frozenset({
    (
        "github.event_name == 'pull_request' && "
        "github.event.pull_request.head.repo.full_name != github.repository"
    ),
})
_RUNNER_VARIABLE = re.compile(r"\b(GITHUB|RUNNER|ACTIONS)_[A-Z_]+|\$\{?CI\b")
_PREPARE_TEMPLATE = (
    "uv venv --managed-python --python '${{ matrix.python-version }}' --seed .venv",
    'echo "$GITHUB_WORKSPACE/.venv/bin" >> "$GITHUB_PATH"',
    "env -i PATH=/usr/bin:/bin .venv/bin/python -I -S --version",
)
_PREPARE_ENV = {"UV_PYTHON_INSTALL_DIR": "${{ runner.tool_cache }}/uv-python"}


class WorkflowShapeError(ValueError):
    """The workflow uses a shape this derivation does not recognize."""


class ResolutionError(RuntimeError):
    """A requested interpreter minor could not be resolved without downloading."""


@dataclass(frozen=True)
class Step:
    name: str
    kind: str  # not-applicable | checkout | setup-uv | prepare | run
    run: str = ""
    uses: str = ""
    with_: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Workflow:
    minors: tuple[str, ...]
    steps: tuple[Step, ...]
    uv_version: str


@dataclass(frozen=True)
class Interpreter:
    minor: str
    path: str
    version: str


Resolver = Callable[[str], Interpreter]


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_ignorable(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def _scalar(raw: str, where: str) -> str:
    value = raw.strip()
    if value.startswith('"'):
        try:
            decoded, end = json.JSONDecoder().raw_decode(value)
        except json.JSONDecodeError as error:
            raise WorkflowShapeError(f"{where}: unsupported double-quoted scalar") from error
        rest = value[end:].strip()
        if not isinstance(decoded, str) or (rest and not rest.startswith("#")):
            raise WorkflowShapeError(f"{where}: unsupported double-quoted scalar")
        return decoded
    if value.startswith("'"):
        match = re.fullmatch(r"'((?:[^']|'')*)'\s*(#.*)?", value)
        if match is None:
            raise WorkflowShapeError(f"{where}: unsupported single-quoted scalar")
        return match.group(1).replace("''", "'")
    if value[:1] in {"[", "{", "&", "*", "!", ">", "|", "%", "@", "`"}:
        raise WorkflowShapeError(f"{where}: unsupported scalar form {value[:1]!r}")
    return value.split(" #", 1)[0].rstrip()


def _key(line: str, where: str) -> tuple[str, str]:
    match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_-]*):(?: (.*))?", line.strip())
    if match is None:
        raise WorkflowShapeError(f"{where}: expected a 'key: value' line, got {line.strip()!r}")
    return match.group(1), match.group(2) or ""


def _mapping(lines: Sequence[str], indices: list[int], where: str) -> dict[str, list[int]]:
    if not indices:
        raise WorkflowShapeError(f"{where}: empty mapping")
    level = _indent(lines[indices[0]])
    groups: dict[str, list[int]] = {}
    current: str | None = None
    for index in indices:
        line = lines[index]
        if _indent(line) < level:
            raise WorkflowShapeError(f"{where}: inconsistent indentation")
        if _indent(line) == level:
            key, _ = _key(line, where)
            if key in groups:
                raise WorkflowShapeError(f"{where}: duplicate key {key!r}")
            groups[key] = [index]
            current = key
        elif current is None:
            raise WorkflowShapeError(f"{where}: nested line without a key")
        else:
            groups[current].append(index)
    return groups


def _flat(lines: Sequence[str], indices: list[int], where: str) -> dict[str, str]:
    groups = _mapping(lines, indices, where)
    values: dict[str, str] = {}
    for key, group in groups.items():
        if len(group) != 1:
            raise WorkflowShapeError(f"{where}.{key}: nested values are not supported")
        _, raw = _key(lines[group[0]], where)
        values[key] = _scalar(raw, f"{where}.{key}")
    return values


def _block(lines: Sequence[str], key_index: int, where: str) -> str:
    key_indent = _indent(lines[key_index])
    content: list[str] = []
    index = key_index + 1
    while index < len(lines):
        line = lines[index]
        if line.strip() and _indent(line) <= key_indent:
            break
        content.append(line)
        index += 1
    first = next((line for line in content if line.strip()), None)
    if first is None:
        raise WorkflowShapeError(f"{where}: empty literal block")
    depth = _indent(first)
    body = []
    for line in content:
        if line.strip() and _indent(line) < depth:
            raise WorkflowShapeError(f"{where}: literal block is under-indented")
        body.append(line[depth:] if line.strip() else "")
    while body and not body[-1]:
        body.pop()
    return "\n".join(body) + "\n"


def _parse_step(lines: Sequence[str], indices: list[int], number: int) -> Step:
    where = f"steps[{number}]"
    first = lines[indices[0]]
    item_indent = _indent(first)
    # Rewrite the "- " marker as indentation so the item is an ordinary mapping.
    rewritten = list(lines)
    rewritten[indices[0]] = " " * (item_indent + 2) + first.strip()[2:]
    groups = _mapping(rewritten, indices, where)
    unknown = set(groups) - _STEP_KEYS
    if unknown:
        raise WorkflowShapeError(f"{where}: unsupported step keys {sorted(unknown)}")
    scalars: dict[str, str] = {}
    for key in ("name", "if", "uses"):
        if key in groups:
            if len(groups[key]) != 1:
                raise WorkflowShapeError(f"{where}.{key}: expected one line")
            scalars[key] = _scalar(_key(rewritten[groups[key][0]], where)[1], f"{where}.{key}")
    mappings: dict[str, dict[str, str]] = {}
    for key in ("with", "env"):
        if key in groups:
            if _key(rewritten[groups[key][0]], where)[1].strip():
                raise WorkflowShapeError(f"{where}.{key}: only a block mapping is supported")
            mappings[key] = _flat(rewritten, groups[key][1:], f"{where}.{key}")
    with_, env = mappings.get("with", {}), mappings.get("env", {})
    run = ""
    if "run" in groups:
        raw = _key(rewritten[groups["run"][0]], where)[1].strip()
        if raw == "|":
            run = _block(rewritten, groups["run"][0], f"{where}.run")
        elif len(groups["run"]) == 1 and raw:
            run = _scalar(raw, f"{where}.run") + "\n"
        else:
            raise WorkflowShapeError(f"{where}.run: unsupported run form {raw!r}")
    if ("uses" in scalars) == bool(run):
        raise WorkflowShapeError(f"{where}: a step needs exactly one of uses or run")
    name = scalars.get("name") or scalars.get("uses", "").split("@", 1)[0]
    if "if" in scalars:
        if scalars["if"] not in _PR_ONLY_CONDITIONS or "uses" in scalars:
            raise WorkflowShapeError(f"{where}: unsupported condition {scalars['if']!r}")
        return Step(name, "not-applicable", run=run)
    if "uses" in scalars:
        action = scalars["uses"].split("@", 1)[0]
        if env:
            raise WorkflowShapeError(f"{where}: env on an action step is not supported")
        if action == "actions/checkout" and not with_:
            return Step(name, "checkout", uses=scalars["uses"])
        if action == "astral-sh/setup-uv" and set(with_) == {"version", "enable-cache"} and (
            with_["enable-cache"] == "false"
        ):
            return Step(name, "setup-uv", uses=scalars["uses"], with_=with_)
        raise WorkflowShapeError(f"{where}: unsupported action {scalars['uses']!r}")
    if "${{" in run or "${{" in "".join(env.values()):
        if tuple(run.splitlines()) != _PREPARE_TEMPLATE or env != _PREPARE_ENV:
            raise WorkflowShapeError(f"{where}: unrecognized expression step {name!r}")
        return Step(name, "prepare", run=run)
    if env:
        raise WorkflowShapeError(f"{where}: env on a run step is not supported")
    if _RUNNER_VARIABLE.search(run):
        raise WorkflowShapeError(f"{where}: run step reads a runner-provided variable")
    return Step(name, "run", run=run)


def parse_workflow(text: str) -> Workflow:
    """Recognize the workflow or raise ``WorkflowShapeError``; never guess."""

    lines = text.splitlines()
    if any("\t" in line for line in lines):
        raise WorkflowShapeError("tabs are not supported")
    top = _mapping(lines, [i for i, line in enumerate(lines) if not _is_ignorable(line)], "top")
    if set(top) != _TOP_KEYS:
        raise WorkflowShapeError(f"top-level keys must be {sorted(_TOP_KEYS)}, got {sorted(top)}")
    # A pull-request-only step is omitted because the local run models a push event,
    # which is sound only while the workflow is triggered by push.
    if "push" not in _mapping(lines, top["on"][1:], "on"):
        raise WorkflowShapeError("the workflow has no push trigger for the local run to model")
    jobs = _mapping(lines, top["jobs"][1:], "jobs")
    if len(jobs) != 1:
        raise WorkflowShapeError(f"exactly one job is supported, got {sorted(jobs)}")
    job_name, job_lines = next(iter(jobs.items()))
    job = _mapping(lines, job_lines[1:], f"jobs.{job_name}")
    if set(job) != _JOB_KEYS:
        raise WorkflowShapeError(f"job keys must be {sorted(_JOB_KEYS)}, got {sorted(job)}")
    strategy = _mapping(lines, job["strategy"][1:], "strategy")
    if set(strategy) != _STRATEGY_KEYS:
        raise WorkflowShapeError(f"strategy keys must be {sorted(_STRATEGY_KEYS)}")
    # Local legs always run to completion independently, which is CI's behaviour only
    # while fail-fast is off.
    if _scalar(_key(lines[strategy["fail-fast"][0]], "strategy")[1], "fail-fast") != "false":
        raise WorkflowShapeError("strategy.fail-fast must be false")
    matrix = _mapping(lines, strategy["matrix"][1:], "matrix")
    if set(matrix) != {"python-version"} or len(matrix["python-version"]) != 1:
        raise WorkflowShapeError(f"matrix must hold one python-version line: {sorted(matrix)}")
    declarations = [line for line in lines if line.strip().startswith("python-version:")]
    if len(declarations) != 1:
        raise WorkflowShapeError(f"found {len(declarations)} python-version declarations, not one")
    raw = _key(lines[matrix["python-version"][0]], "matrix")[1]
    try:
        minors = json.loads(raw)
    except json.JSONDecodeError as error:
        raise WorkflowShapeError("python-version must be a JSON list of strings") from error
    if (not isinstance(minors, list) or not minors
            or not all(isinstance(m, str) and _MINOR.fullmatch(m) for m in minors)
            or len(set(minors)) != len(minors)):
        raise WorkflowShapeError(f"python-version must list distinct 3.N strings: {raw!r}")
    step_lines = job["steps"][1:]
    if not step_lines:
        raise WorkflowShapeError("no steps")
    item_indent = _indent(lines[step_lines[0]])
    items: list[list[int]] = []
    for index in step_lines:
        line = lines[index]
        if _indent(line) == item_indent:
            if not line.strip().startswith("- "):
                raise WorkflowShapeError(f"line {index + 1}: expected a step item")
            items.append([index])
        elif _indent(line) < item_indent or not items:
            raise WorkflowShapeError(f"line {index + 1}: inconsistent step indentation")
        else:
            items[-1].append(index)
    steps = tuple(_parse_step(lines, item, number) for number, item in enumerate(items))
    kinds = [step.kind for step in steps]
    for kind in ("checkout", "setup-uv", "prepare"):
        if kinds.count(kind) != 1:
            raise WorkflowShapeError(f"expected exactly one {kind} step, found {kinds.count(kind)}")
    if not kinds.index("checkout") < kinds.index("setup-uv") < kinds.index("prepare"):
        raise WorkflowShapeError("checkout, setup-uv and prepare steps are out of order")
    if "run" not in kinds[kinds.index("prepare"):]:
        raise WorkflowShapeError("no gate step follows the interpreter preparation")
    setup = steps[kinds.index("setup-uv")]
    return Workflow(tuple(minors), steps, setup.with_["version"])


def _probe(path: str) -> str:
    completed = subprocess.run(
        [path, "-I", "-S", "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"],
        capture_output=True, text=True, check=False, env={"PATH": "/usr/bin:/bin"}, timeout=60,
    )
    if completed.returncode != 0:
        raise ResolutionError(f"{path} did not run: {completed.stderr.strip()}")
    return completed.stdout.strip()


def uv_resolver(uv: str, home: str) -> Resolver:
    """Resolve managed interpreters from a neutral directory without downloads."""

    def resolve(minor: str) -> Interpreter:
        with tempfile.TemporaryDirectory(prefix="ci-matrix-resolve-") as neutral:
            completed = subprocess.run(
                [uv, "--offline", "--no-cache", "--no-python-downloads", "--managed-python",
                 "python", "find", "--system", minor],
                capture_output=True, text=True, check=False, cwd=neutral, timeout=60,
                env={"HOME": home, "PATH": "/usr/bin:/bin"},
            )
        if completed.returncode != 0 or not completed.stdout.strip():
            raise ResolutionError(
                f"Python {minor} is not installed as a managed interpreter "
                f"(uv exit {completed.returncode}): {completed.stderr.strip()}"
            )
        path = completed.stdout.strip()
        version = _probe(path)
        if not version.startswith(minor + "."):
            raise ResolutionError(f"{minor} resolved to {path}, which runs Python {version}")
        return Interpreter(minor, path, version)

    return resolve


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _require_uv_version(output: str, expected: str) -> None:
    pattern = rf"uv {re.escape(expected)}(?: \([^\r\n]*\))?"
    if re.fullmatch(pattern, output.strip()) is None:
        raise ResolutionError(
            f"uv version must match workflow pin {expected!r}; got {output.strip()!r}"
        )


def build_plan(
    workflow: Workflow,
    *,
    source: Path,
    run_root: Path,
    run_id: str,
    home: str,
    uv: str,
    extras: Sequence[str],
    resolve: Resolver,
    inventory_tool: str | None = None,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    for extra in extras:
        if not _MINOR.fullmatch(extra):
            raise WorkflowShapeError(f"extra minor {extra!r} is not a 3.N string")
        if extra in workflow.minors:
            raise WorkflowShapeError(f"extra minor {extra} is already declared by the workflow")
    if len(set(extras)) != len(extras):
        raise WorkflowShapeError("extra minors repeat")
    script = str(Path(__file__).resolve())
    python = sys.executable
    legs = [(minor, minor, "declared") for minor in workflow.minors]
    legs += [(f"extra-{minor}", minor, "extra") for minor in extras]
    resolved = [(label, kind, resolve(minor)) for label, minor, kind in legs]
    plan: list[dict[str, object]] = []
    provenance: list[dict[str, object]] = []
    for label, kind, interpreter in resolved:
        workspace = run_root / label / "workspace"
        venv_bin = workspace / ".venv" / "bin"
        exact = os.path.realpath(interpreter.path)
        provenance.append({
            "label": label, "kind": kind, "minor": interpreter.minor,
            "interpreter": interpreter.path, "interpreter_realpath": exact,
            "interpreter_version": interpreter.version, "workspace": str(workspace),
        })
        prepared = False
        for step in workflow.steps:
            if step.kind == "not-applicable":
                continue
            if step.kind == "checkout":
                action = ["checkout", str(source), str(workspace)]
            elif step.kind == "setup-uv":
                action = ["verify-uv", uv, workflow.uv_version]
            elif step.kind == "prepare":
                action = [BASH, "-e", "-c", _prepare_script(step, uv, exact, interpreter.version)]
            else:
                action = [BASH, "-e", "-c", step.run]
            path = _search_path(venv_bin if prepared else None, uv)
            plan.append(_gate(python, script, run_root, run_id, label, _leg_size(plan, label),
                              step.name, workspace, home, path, action))
            prepared = prepared or step.kind == "prepare"
        if inventory_tool is not None:
            action = [
                "/usr/bin/python3", "-I", "-B", inventory_tool, "scan",
                "--venv", f"{label}={venv_bin / 'python'}", "--json",
                *(f"--module={module}" for module in INVENTORY_MODULES),
            ]
            plan.append(_gate(python, script, run_root, run_id, label, _leg_size(plan, label),
                              "local-inventory", workspace, home,
                              _search_path(venv_bin, uv), action))
    return plan, provenance


def _leg_size(plan: Sequence[dict[str, object]], label: str) -> int:
    return sum(1 for item in plan if str(item["name"]).startswith(f"{label}:"))


def _search_path(venv_bin: Path | None, uv: str) -> str:
    entries = [*([str(venv_bin)] if venv_bin is not None else []), str(Path(uv).parent), *BASE_PATH]
    return ":".join(dict.fromkeys(entries))


def _prepare_script(step: Step, uv: str, interpreter: str, version: str) -> str:
    # Owner disposition: the exact resolved interpreter with downloads disabled. The
    # workflow's PATH export becomes the explicit PATH of later steps; its probe runs as is.
    probe = step.run.splitlines()[2]
    check = "import sys; print('%d.%d.%d' % sys.version_info[:3])"
    return (
        f"{shlex.quote(uv)} venv --managed-python --no-python-downloads "
        f"--python {shlex.quote(interpreter)} --seed .venv\n"
        f"test \"$(env -i PATH=/usr/bin:/bin .venv/bin/python -I -S -c {shlex.quote(check)})\""
        f" = {shlex.quote(version)}\n"
        f"{probe}\n"
    )


def _gate(
    python: str, script: str, run_root: Path, run_id: str, label: str, previous: int,
    name: str, workspace: Path, home: str, path: str, action: Sequence[str],
) -> dict[str, object]:
    number = previous + 1
    command = [
        python, "-I", "-B", script, "step", "--run-root", str(run_root), "--run-id", run_id,
        "--leg", label, "--number", str(number), "--cwd", str(workspace), "--home", home,
        "--path", path,
    ]
    for required in range(1, number):
        command += ["--requires", str(required)]
    return {"name": f"{label}:{number:02d}:{name}", "cmd": [*command, "--", *action]}


def _ignore(directory: str, names: list[str]) -> set[str]:
    del directory
    return {
        name for name in names
        if name in COPY_EXCLUDES
        or any(fnmatch(name, pattern) for pattern in _COPY_EXCLUDE_PATTERNS)
    }


def run_step(arguments: argparse.Namespace) -> int:
    """Run one plan step with an explicit environment and a complete log."""

    run_root = Path(arguments.run_root)
    if (run_root / "run-id").read_text(encoding="ascii").strip() != arguments.run_id:
        sys.stderr.write("NOT RUN: run id does not match this run root\n")
        return NOT_RUN
    leg_root = run_root / arguments.leg
    stamps = leg_root / "stamps"
    logs = leg_root / "logs"
    stamps.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    for previous in arguments.requires:
        if not (stamps / f"{previous}.pass").is_file():
            sys.stdout.write(
                f"NOT RUN: prerequisite step {previous} of leg {arguments.leg} "
                "has no pass record in this run\n"
            )
            return NOT_RUN
    stamp = stamps / f"{arguments.number}.pass"
    log = logs / f"{arguments.number}.log"
    if stamp.exists() or log.exists():
        sys.stdout.write("NOT RUN: this step already ran in this run root\n")
        return NOT_RUN
    action = list(arguments.action)
    try:
        if action[:1] == ["checkout"]:
            source, destination = Path(action[1]), Path(action[2])
            shutil.copytree(source, destination, symlinks=True, ignore=_ignore)
            output = f"copied {source} to {destination}\n".encode()
            returncode = 0
        else:
            Path(arguments.cwd).mkdir(parents=True, exist_ok=True)
            if action[:1] == ["verify-uv"] and len(action) != 3:
                raise OSError("verify-uv requires executable and workflow version")
            expected_uv = action[2] if action[:1] == ["verify-uv"] else None
            command = [action[1], "--version"] if expected_uv is not None else action
            completed = subprocess.run(
                command, cwd=arguments.cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                check=False, env={"HOME": arguments.home, "PATH": arguments.path},
            )
            output, returncode = completed.stdout, completed.returncode
            if expected_uv is not None and returncode == 0:
                try:
                    _require_uv_version(output.decode("utf-8", errors="replace"), expected_uv)
                except ResolutionError as error:
                    output += f"\nci_matrix: {error}\n".encode()
                    returncode = 2
    except OSError as error:
        output, returncode = f"step could not start: {error}\n".encode(), 127
    log.write_bytes(output)
    sys.stdout.buffer.write(output)
    sys.stdout.flush()
    if returncode == 0:
        stamp.write_text(json.dumps({
            "log": str(log), "log_sha256": hashlib.sha256(output).hexdigest(),
            "finished": datetime.now(UTC).isoformat(),
        }) + "\n", encoding="utf-8")
    return returncode


def _plan_command(arguments: argparse.Namespace, resolve: Resolver | None) -> int:
    workflow_path = Path(arguments.workflow).resolve()
    workflow = parse_workflow(workflow_path.read_text(encoding="utf-8"))
    run_root = Path(arguments.run_root).absolute()
    home = arguments.home or os.environ.get("HOME", "")
    if not home or not Path(home).is_absolute() or not Path(home).is_dir():
        raise ResolutionError(f"HOME must be an existing absolute directory, got {home!r}")
    uv = arguments.uv or shutil.which("uv")
    if uv is None or not Path(uv).is_absolute():
        raise ResolutionError("uv was not found; pass --uv with an absolute path")
    if run_root.exists():
        raise ResolutionError(f"run root {run_root} already exists; every run needs a new one")
    source = Path(arguments.source).resolve()
    if not source.is_dir():
        raise ResolutionError(f"source tree {source} is not a directory")
    uv_version = subprocess.run(
        [uv, "--version"], capture_output=True, text=True, check=False, timeout=60,
        env={"HOME": home, "PATH": "/usr/bin:/bin"},
    )
    if uv_version.returncode != 0:
        raise ResolutionError(f"{uv} --version failed: {uv_version.stderr.strip()}")
    _require_uv_version(uv_version.stdout, workflow.uv_version)
    run_id = secrets.token_hex(16)
    plan, legs = build_plan(
        workflow, source=source, run_root=run_root, run_id=run_id, home=home, uv=uv,
        extras=arguments.extra, resolve=resolve or uv_resolver(uv, home),
        inventory_tool=arguments.inventory_tool,
    )
    run_root.mkdir(parents=True, exist_ok=False)
    (run_root / "run-id").write_text(run_id + "\n", encoding="ascii")
    provenance = {
        "schema": 1, "generated": datetime.now(UTC).isoformat(), "run_root": str(run_root),
        "run_id": run_id, "source": str(source),
        "helper": {"path": str(Path(__file__).resolve()),
                   "sha256": _sha256(Path(__file__).resolve()),
                   "python": sys.executable, "python_version": sys.version.split()[0]},
        "workflow": {"path": str(workflow_path), "sha256": _sha256(workflow_path),
                     "declared": list(workflow.minors), "setup_uv_version": workflow.uv_version,
                     "steps": [{"name": s.name, "kind": s.kind} for s in workflow.steps]},
        "extras": list(arguments.extra),
        "uv": {"path": uv, "version": uv_version.stdout.strip()},
        "environment": {"HOME": home, "base_path": list(BASE_PATH),
                        "absent": list(REMOVED_ENVIRONMENT), "shell": f"{BASH} -e -c"},
        "copy_excludes": [*COPY_EXCLUDES, *_COPY_EXCLUDE_PATTERNS],
        "inventory_tool": arguments.inventory_tool,
        "legs": legs,
    }
    Path(arguments.provenance).write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    sys.stdout.write(json.dumps(plan, indent=2) + "\n")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--selftest", action="store_true", help="run the built-in checks")
    commands = parser.add_subparsers(dest="command")
    plan = commands.add_parser("plan", help="print a gate_receipt plan for the declared set")
    plan.add_argument("--run-root", required=True, help="new directory for this run")
    plan.add_argument("--provenance", required=True, help="file for interpreter provenance")
    plan.add_argument("--workflow", default=str(WORKFLOW))
    plan.add_argument("--source", default=str(WORKFLOW.parents[2]), help="tree to copy per leg")
    plan.add_argument("--extra", action="append", default=[], help="extra minor, labelled extra")
    plan.add_argument("--home", help="HOME for every step (default: this process's HOME)")
    plan.add_argument("--uv", help="absolute uv path (default: uv on PATH)")
    plan.add_argument("--inventory-tool", help="interp_matrix.py path for a local inventory step")
    step = commands.add_parser("step", help="run one plan step (used by the plan)")
    step.add_argument("--run-root", required=True)
    step.add_argument("--run-id", required=True)
    step.add_argument("--leg", required=True)
    step.add_argument("--number", required=True, type=int)
    step.add_argument("--requires", action="append", default=[], type=int)
    step.add_argument("--cwd", required=True)
    step.add_argument("--home", required=True)
    step.add_argument("--path", required=True)
    step.add_argument("action", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None, *, resolve: Resolver | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.selftest:
        return selftest()
    try:
        if arguments.command == "plan":
            return _plan_command(arguments, resolve)
        if arguments.command == "step":
            if arguments.action[:1] == ["--"]:
                arguments.action = arguments.action[1:]
            if not arguments.action:
                sys.stderr.write("step needs an action after --\n")
                return 2
            return run_step(arguments)
    except (WorkflowShapeError, ResolutionError, OSError) as error:
        sys.stderr.write(f"ci_matrix: {error}\n")
        return 2
    _parser().print_help(sys.stderr)
    return 2


def selftest() -> int:
    """Check recognition, refusal, planning and step dependencies without installing."""

    checks: list[tuple[str, Callable[[], None]]] = []

    def check(name: str) -> Callable[[Callable[[], None]], Callable[[], None]]:
        def register(function: Callable[[], None]) -> Callable[[], None]:
            checks.append((name, function))
            return function
        return register

    text = WORKFLOW.read_text(encoding="utf-8")

    def refuses(mutated: str) -> None:
        if mutated == text:
            raise AssertionError("fixture edit did not change the workflow")
        try:
            parse_workflow(mutated)
        except WorkflowShapeError:
            return
        raise AssertionError("known-bad workflow was accepted")

    @check("real workflow is recognized")
    def _real() -> None:
        workflow = parse_workflow(text)
        if not workflow.minors or not any(step.kind == "run" for step in workflow.steps):
            raise AssertionError("no minors or gates derived")

    @check("uv version pin admits exact release with optional build metadata")
    def _uv_matching() -> None:
        _require_uv_version("uv 0.12.3\n", "0.12.3")
        _require_uv_version("uv 0.12.3 (fixture build)\n", "0.12.3")

    @check("uv version pin refuses wrong, malformed and ambiguous output")
    def _uv_wrong() -> None:
        for output in ("uv 0.12.13", "uv 0.12.30", "bad output", "uv 0.12.3\nuv 0.12.13"):
            try:
                _require_uv_version(output, "0.12.3")
            except ResolutionError:
                continue
            raise AssertionError(f"accepted wrong uv version {output!r}")

    @check("missing declaration refused")
    def _missing() -> None:
        refuses(re.sub(r"(?m)^\s*python-version: .*\n", "", text))

    @check("duplicate declaration refused")
    def _duplicate() -> None:
        refuses(re.sub(r"(?m)^(\s*)(python-version: .*)$", r"\1\2\n\1\2", text, count=1))

    @check("non-JSON declaration refused")
    def _non_json() -> None:
        refuses(re.sub(r"(?m)^(\s*python-version: )\[.*\]$", r"\1[3.11, 3.12]", text))

    @check("unsupported step key refused")
    def _shell() -> None:
        lint = "run: ruff check src tests"
        refuses(text.replace(lint, f"shell: sh\n        {lint}"))

    @check("changed preparation refused")
    def _prepare() -> None:
        refuses(text.replace("--seed .venv", "--seed venv"))

    @check("condition true on every event refused (task198)")
    def _always_true() -> None:
        refuses(text.replace(
            "        run: mypy\n",
            "        if: github.event_name == 'pull_request' && false || true\n"
            "        run: exit 73\n",
        ))

    def fake(minor: str) -> Interpreter:
        return Interpreter(minor, f"/fake/python{minor}", f"{minor}.99")

    @check("plan legs, labels and dependencies")
    def _plan() -> None:
        workflow = parse_workflow(text)
        plan, legs = build_plan(
            workflow, source=Path("/src"), run_root=Path("/run"), run_id="id", home="/home/x",
            uv="/opt/uv/uv", extras=["3.99"], resolve=fake,
        )
        labels = [leg["label"] for leg in legs]
        if labels != [*workflow.minors, "extra-3.99"]:
            raise AssertionError(labels)
        for item in plan:
            command = [str(part) for part in item["cmd"]]  # type: ignore[attr-defined]
            number = int(command[command.index("--number") + 1])
            required = [
                int(command[i + 1]) for i, part in enumerate(command) if part == "--requires"
            ]
            if required != list(range(1, number)):
                raise AssertionError(f"{item['name']} requires {required}")

    @check("dependent step is not run after a failure")
    def _dependency() -> None:
        with tempfile.TemporaryDirectory(prefix="ci-matrix-selftest-") as temporary:
            root = Path(temporary) / "run"
            root.mkdir()
            (root / "run-id").write_text("abc\n", encoding="ascii")
            base = ["step", "--run-root", str(root), "--run-id", "abc", "--leg", "leg",
                    "--cwd", temporary, "--home", temporary, "--path", "/usr/bin:/bin"]
            failed = main([*base, "--number", "1", "--", "/bin/false"])
            dependent = main([*base, "--number", "2", "--requires", "1", "--", "/bin/true"])
            if failed == 0 or dependent != NOT_RUN or (root / "leg/stamps/2.pass").exists():
                raise AssertionError((failed, dependent))

    failures = 0
    for name, function in checks:
        try:
            function()
        except Exception as error:  # noqa: BLE001 - every failure is reported, not raised
            failures += 1
            sys.stdout.write(f"FAIL {name}: {error!r}\n")
        else:
            sys.stdout.write(f"ok   {name}\n")
    sys.stdout.write(f"selftest: {len(checks) - failures}/{len(checks)} passed\n")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
