"""Candidate-commit payload-gate invariants."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from conftest import _CLEAN_PAYLOAD_GATE_SOURCE, GATE_GIT_LITERAL, GitFixture, admit_gate

import onetree
from onetree import git as git_module
from onetree.cli import main
from onetree.core import Coordinator
from onetree.errors import LandingRefusedError, PayloadGateError

# The gates below take their log path as a baked-in source literal, substituted for
# ``__LOG_PATH__`` at the call site, rather than reading it from the environment. A gate
# that learns where to write from an inherited variable cannot be used to test whether
# inheritance was closed: once the child's environment is admitted by name, the variable is
# gone and the test fails for the reason it is meant to prove absent.
_OBSERVING_GATE_SOURCE = """\
import hashlib
import json
import os
import subprocess
import sys

candidate = sys.argv[2]
repository = sys.argv[4]
completed = subprocess.run(
    [_GIT_, "-C", repository, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", candidate],
    check=True,
    capture_output=True,
)
paths = sorted(
    path.decode("utf-8", errors="strict")
    for path in completed.stdout.split(b"\\0")
    if path
)
executed = os.path.realpath(__file__)
with open(executed, "rb") as stream:
    executed_bytes = stream.read()
observed = {
    "isolated": sys.flags.isolated,
    "no_site": sys.flags.no_site,
    "no_user_site": sys.flags.no_user_site,
    "coverage_names": sorted(n for n in os.environ if n.startswith("COVERAGE_")),
    "cwd_is_repository": os.path.samefile(os.getcwd(), repository),
    "executed_path": executed,
    "executed_mode": os.stat(executed).st_mode & 0o777,
    "executed_sha256": hashlib.sha256(executed_bytes).hexdigest(),
}
with open(__LOG_PATH__, "w", encoding="utf-8") as stream:
    json.dump(observed, stream)
print(
    json.dumps(
        {
            "candidate": candidate,
            "repository": repository,
            "staged_files": paths,
            "findings": [],
            "degraded": [],
            "verdict": "CLEAN",
        }
    )
)
""".replace("_GIT_", GATE_GIT_LITERAL)

_TAMPERING_GATE_SOURCE = """\
import json
import sys

with open(__GATE_PATH__, "w", encoding="utf-8") as stream:
    stream.write("# replaced while the gate was running\\n")
print(
    json.dumps(
        {
            "candidate": sys.argv[2],
            "repository": sys.argv[4],
            "staged_files": ["a.txt"],
            "findings": [],
            "degraded": [],
            "verdict": "CLEAN",
        }
    )
)
"""

_CLEAN_GATE_WITH_MARKER = (
    "import pathlib\n"
    "pathlib.Path(__MARKER__).write_text('ran', encoding='utf-8')\n"
    + _CLEAN_PAYLOAD_GATE_SOURCE
)
"""The fixture's exact-output CLEAN gate, leaving a marker file that proves it ran."""

_DUPLICATE_KEY_GATE_SOURCE = """\
import json
import sys

candidate = json.dumps(sys.argv[2])
repository = json.dumps(sys.argv[4])
print(
    '{"verdict": "REFUSE", "findings": ["credential-shaped payload"],'
    ' "candidate": ' + candidate + ', "repository": ' + repository + ','
    ' "staged_files": ["a.txt"], "degraded": [],'
    ' "verdict": "CLEAN", "findings": []}'
)
"""


def _register_and_stage(
    git_repo: GitFixture,
    actor: str,
    content: str,
    *,
    commit_payload: Path | str | None = None,
    admit_source: bool = True,
    relative: str = "a.txt",
) -> Coordinator:
    if admit_source and commit_payload is not None:
        gate = Path(commit_payload)
        if gate.is_file():
            admit_gate(git_repo.root, gate)
    coordinator = Coordinator(git_repo.root, commit_payload=commit_payload)
    session = coordinator.register_session(actor, pid=os.getpid(), ttl=300)
    coordinator.claim(actor, [str(git_repo.root / relative)])
    git_repo.write(relative, content)
    git_repo.stage(session.index_path, relative)
    return coordinator


def _write_gate(path: Path, payload: dict[str, Any], returncode: int) -> Path:
    path.write_text(
        "import json\n"
        f"payload = {payload!r}\n"
        "payload.setdefault('candidate', __import__('sys').argv[2])\n"
        "print(json.dumps(payload))\n"
        f"raise SystemExit({returncode})\n",
        encoding="utf-8",
    )
    return path


def _assert_unchanged(
    git_repo: GitFixture,
    coordinator: Coordinator,
    branch_before: str,
    wal_before: str | None,
) -> None:
    assert git_repo.oid() == branch_before
    assert coordinator.wal.read().head == wal_before


def _assert_refusal_recorded(
    git_repo: GitFixture,
    coordinator: Coordinator,
    branch_before: str,
    wal_before: str | None,
    reason: str,
) -> None:
    """The branch stands still and the WAL says why -- both, or the refusal is invisible.

    A gate verdict against the candidate is a landing refusal, and every other landing
    refusal is written to the WAL before it is raised. The exception raised into the
    caller is not a substitute: it reaches one process, while recovery and every peer
    read the WAL.
    """

    assert git_repo.oid() == branch_before
    assert coordinator.wal.read().head != wal_before
    report = coordinator.inspect_wal()
    assert [
        (event.data.get("reason"), event.data.get("paths"))
        for event in report.events
        if event.kind == "refused"
    ] == [(reason, ["a.txt"])]
    assert report.consistent


def test_configured_refusing_payload_gate_is_not_ignored(
    git_repo: GitFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = tmp_path / "refuse_gate.py"
    gate.write_text(
        "import json\n"
        "print(json.dumps({\"repository\": __import__('sys').argv[4], "
        "\"candidate\": __import__('sys').argv[2], "
        '"staged_files": ["a.txt"], "findings": ["known bad"], '
        '"degraded": [], "verdict": "REFUSE"}))\n'
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    admit_gate(git_repo.root, gate)
    monkeypatch.setenv("ONETREE_COMMIT_PAYLOAD", str(gate))
    coordinator = _register_and_stage(git_repo, "alpha", "credential-shaped payload\n")
    branch_before = git_repo.oid()
    wal_before = coordinator.wal.read().head

    with pytest.raises(LandingRefusedError, match="refused the candidate") as refused:
        coordinator.land("alpha")

    assert refused.value.details["reason"] == "payload_gate_refused"
    _assert_refusal_recorded(
        git_repo, coordinator, branch_before, wal_before, "payload_gate_refused"
    )


def test_clean_gate_audits_exact_candidate_before_intent(
    git_repo: GitFixture,
    tmp_path: Path,
) -> None:
    log = tmp_path / "gate-log.json"
    gate = tmp_path / "recording_gate.py"
    gate.write_text(
        (
            "import json\n"
            "import subprocess\n"
            "import sys\n"
            "candidate = sys.argv[2]\n"
            "repository = sys.argv[4]\n"
            'wal = subprocess.run([_GIT_, "-C", repository, "rev-parse", '
            '"--verify", "refs/onetree/wal"], check=True, capture_output=True, '
            "text=True).stdout.strip()\n"
            'ancestry = subprocess.run([_GIT_, "-C", repository, "merge-base", '
            '"--is-ancestor", candidate, "refs/heads/main"], check=False, '
            "capture_output=True).returncode\n"
            "if ancestry not in (0, 1):\n"
            '    raise SystemExit(f"merge-base --is-ancestor exited {ancestry}")\n'
            "on_branch = ancestry == 0\n"
            'paths = subprocess.run([_GIT_, "-C", repository, "diff-tree", '
            '"--no-commit-id", "--name-only", "-r", "-z", candidate], '
            'check=True, capture_output=True).stdout.split(b"\\0")\n'
            "paths = sorted(item.decode() for item in paths if item)\n"
            'open(__LOG_PATH__, "w", encoding="utf-8").write('
            'json.dumps({"candidate": candidate, "wal": wal, '
            '"on_branch": on_branch, "paths": paths}))\n'
            'print(json.dumps({"candidate": candidate, "repository": repository, '
            '"staged_files": paths, '
            '"findings": [], "degraded": [], "verdict": "CLEAN"}))\n'
        )
        .replace("_GIT_", GATE_GIT_LITERAL)
        .replace("__LOG_PATH__", repr(str(log))),
        encoding="utf-8",
    )
    coordinator = _register_and_stage(
        git_repo,
        "alpha",
        "clean candidate\n",
        commit_payload=gate,
    )
    wal_before = coordinator.wal.read().head

    landed = coordinator.land("alpha")

    observed = json.loads(log.read_text(encoding="utf-8"))
    assert observed == {
        "candidate": landed.commit,
        "wal": wal_before,
        "on_branch": False,
        "paths": ["a.txt"],
    }
    assert git_repo.oid() == landed.commit


def test_gate_must_echo_the_exact_candidate_identity(
    git_repo: GitFixture,
    tmp_path: Path,
) -> None:
    gate = _write_gate(
        tmp_path / "constant_clean.py",
        {
            "candidate": "0" * 40,
            "repository": str(git_repo.root),
            "staged_files": ["a.txt"],
            "findings": [],
            "degraded": [],
            "verdict": "CLEAN",
        },
        0,
    )
    coordinator = _register_and_stage(
        git_repo,
        "alpha",
        "candidate identity must be coupled\n",
        commit_payload=gate,
    )
    branch_before = git_repo.oid()
    wal_before = coordinator.wal.read().head

    with pytest.raises(PayloadGateError, match="different candidate"):
        coordinator.land("alpha")

    _assert_unchanged(git_repo, coordinator, branch_before, wal_before)


def test_unadmitted_gate_source_is_rejected_before_execution(
    git_repo: GitFixture,
    tmp_path: Path,
) -> None:
    marker = tmp_path / "executed"
    gate = tmp_path / "unadmitted_echo.py"
    gate.write_text(
        "import json\n"
        "import pathlib\n"
        "import sys\n"
        f"pathlib.Path({str(marker)!r}).write_text('ran', encoding='utf-8')\n"
        'print(json.dumps({"candidate": sys.argv[2], '
        '"repository": sys.argv[4], "staged_files": ["a.txt"], '
        '"findings": [], "degraded": [], "verdict": "CLEAN"}))\n',
        encoding="utf-8",
    )
    coordinator = _register_and_stage(
        git_repo,
        "alpha",
        "gate source identity must be trusted\n",
        commit_payload=gate,
        admit_source=False,
    )
    branch_before = git_repo.oid()
    wal_before = coordinator.wal.read().head

    with pytest.raises(PayloadGateError, match="source does not match admitted"):
        coordinator.land("alpha")

    assert not marker.exists()
    _assert_unchanged(git_repo, coordinator, branch_before, wal_before)


def test_missing_payload_gate_fails_closed_before_wal_or_branch_mutation(
    git_repo: GitFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ONETREE_COMMIT_PAYLOAD", raising=False)
    _unadmit(git_repo)
    coordinator = _register_and_stage(git_repo, "alpha", "missing gate\n")
    branch_before = git_repo.oid()
    wal_before = coordinator.wal.read().head

    with pytest.raises(PayloadGateError, match="not configured"):
        coordinator.land("alpha")

    _assert_unchanged(git_repo, coordinator, branch_before, wal_before)


def _unadmit(git_repo: GitFixture) -> None:
    for key in ("onetree.payloadGate", "onetree.payloadGateSha256"):
        git_repo.git("config", "--local", "--unset-all", key)


def test_a_named_gate_without_repository_admission_fails_closed(
    git_repo: GitFixture,
    tmp_path: Path,
) -> None:
    """A repository whose operator admitted no gate refuses every landing, even one whose
    caller names a gate that would report CLEAN."""

    marker = tmp_path / "executed"
    gate = tmp_path / "unadmitted_clean.py"
    gate.write_text(
        _CLEAN_GATE_WITH_MARKER.replace("__MARKER__", repr(str(marker))), encoding="utf-8"
    )
    _unadmit(git_repo)
    coordinator = _register_and_stage(
        git_repo, "alpha", "no admission\n", commit_payload=gate, admit_source=False
    )
    branch_before = git_repo.oid()
    wal_before = coordinator.wal.read().head

    with pytest.raises(PayloadGateError, match="not admitted for this repository") as failure:
        coordinator.land("alpha")

    assert failure.value.kind == "payload_gate_error"
    assert failure.value.details["observed_sha256"] == hashlib.sha256(gate.read_bytes()).hexdigest()
    assert not marker.exists()
    _assert_unchanged(git_repo, coordinator, branch_before, wal_before)


def test_the_admitted_path_is_the_gate_when_none_is_named(
    git_repo: GitFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "executed"
    gate = tmp_path / "admitted_only.py"
    gate.write_text(
        _CLEAN_GATE_WITH_MARKER.replace("__MARKER__", repr(str(marker))), encoding="utf-8"
    )
    admitted = admit_gate(git_repo.root, gate)
    monkeypatch.delenv("ONETREE_COMMIT_PAYLOAD", raising=False)
    coordinator = _register_and_stage(git_repo, "alpha", "admitted path\n", admit_source=False)

    landed = coordinator.land("alpha")

    assert git_repo.oid() == landed.commit
    assert marker.read_text(encoding="utf-8") == "ran"
    assert landed.gate_receipt is not None
    assert landed.gate_receipt["gate_source_sha256"] == admitted


def test_one_changed_byte_refuses_and_readmission_restores_landing(
    git_repo: GitFixture,
    tmp_path: Path,
) -> None:
    """Done item 2: admit, land; change one byte, refuse; re-admit, land again."""

    marker = tmp_path / "executed"
    gate = tmp_path / "operator_gate.py"
    gate.write_text(
        _CLEAN_GATE_WITH_MARKER.replace("__MARKER__", repr(str(marker))), encoding="utf-8"
    )
    first_digest = git_module.Repository(git_repo.root).admit_payload_gate(gate).sha256
    coordinator = _register_and_stage(
        git_repo, "alpha", "first landing\n", commit_payload=gate, admit_source=False
    )
    first = coordinator.land("alpha")
    assert git_repo.oid() == first.commit
    assert first.gate_receipt is not None
    assert first.gate_receipt["gate_source_sha256"] == first_digest

    gate.write_bytes(gate.read_bytes() + b"\n")
    marker.unlink()
    changed_digest = hashlib.sha256(gate.read_bytes()).hexdigest()
    assert changed_digest != first_digest
    coordinator = _register_and_stage(
        git_repo, "beta", "second landing\n", commit_payload=gate, admit_source=False,
        relative="b.txt",
    )
    branch_before = git_repo.oid()
    wal_before = coordinator.wal.read().head
    with pytest.raises(PayloadGateError, match="does not match admitted") as failure:
        coordinator.land("beta")
    assert failure.value.payload()["type"] == "payload_gate_error"
    assert failure.value.details["expected_sha256"] == first_digest
    assert failure.value.details["observed_sha256"] == changed_digest
    assert not marker.exists()
    _assert_unchanged(git_repo, coordinator, branch_before, wal_before)

    readmitted = git_module.Repository(git_repo.root).admit_payload_gate(
        gate, expected_sha256=changed_digest
    )
    assert readmitted.sha256 == changed_digest
    second = coordinator.land("beta")
    assert git_repo.oid() == second.commit
    assert second.gate_receipt is not None
    assert second.gate_receipt["gate_source_sha256"] == changed_digest
    assert marker.read_text(encoding="utf-8") == "ran"


@pytest.mark.parametrize(
    ("name", "payload", "returncode", "message", "reported"),
    [
        ("empty-object", {}, 0, "malformed required fields", {}),
        (
            "wrong-path",
            {
                "repository": "REPOSITORY",
                "staged_files": ["b.txt"],
                "findings": [],
                "degraded": [],
                "verdict": "CLEAN",
            },
            0,
            "audited a different payload",
            {"observed_paths": ["b.txt"]},
        ),
        (
            "false-clean",
            {
                "repository": "REPOSITORY",
                "staged_files": ["a.txt"],
                "findings": ["hidden refusal"],
                "degraded": [],
                "verdict": "CLEAN",
            },
            0,
            "called a non-empty report CLEAN",
            {"findings": ["hidden refusal"]},
        ),
        (
            # The sibling "false-clean" case carries findings and no degraded rows, so it
            # reaches the refusal through the first operand of ``findings or degraded`` and
            # leaves the second unreached. A CLEAN report that admits it could not complete
            # its own audit is the input that reaches the second, and it is not a variant of
            # the first: a gate that degraded and still said CLEAN is claiming a census it
            # says it does not have.
            "false-clean-degraded",
            {
                "repository": "REPOSITORY",
                "staged_files": ["a.txt"],
                "findings": [],
                "degraded": ["audit unavailable"],
                "verdict": "CLEAN",
            },
            0,
            "called a non-empty report CLEAN",
            {"degraded": ["audit unavailable"]},
        ),
        (
            "duplicate-path",
            {
                "repository": "REPOSITORY",
                "staged_files": ["a.txt", "a.txt"],
                "findings": [],
                "degraded": [],
                "verdict": "CLEAN",
            },
            0,
            "audited a different payload",
            {"observed_paths": ["a.txt", "a.txt"]},
        ),
        (
            "status-disagrees",
            {
                "repository": "REPOSITORY",
                "staged_files": ["a.txt"],
                "findings": [],
                "degraded": [],
                "verdict": "CLEAN",
            },
            1,
            "verdict and exit status disagree",
            {"verdict": "CLEAN", "returncode": 1},
        ),
    ],
)
def test_malformed_or_mismatched_gate_output_fails_closed(
    git_repo: GitFixture,
    tmp_path: Path,
    name: str,
    payload: dict[str, Any],
    returncode: int,
    message: str,
    reported: dict[str, Any],
) -> None:
    """Each named report must be refused for the reason that names it.

    Five reports fail here for five different reasons, and a bare ``PayloadGateError``
    assertion cannot tell them apart: any one of the five checks could be deleted and the
    case would still pass on whichever check happened to fire next. ``message`` pins which
    check fired, and ``reported`` pins that the refusal names the offending value back --
    the only part a caller or an operator can act on.
    """

    if payload.get("repository") == "REPOSITORY":
        payload = {**payload, "repository": str(git_repo.root)}
    gate = _write_gate(tmp_path / f"{name}.py", payload, returncode)
    coordinator = _register_and_stage(
        git_repo,
        "alpha",
        f"{name}\n",
        commit_payload=gate,
    )
    branch_before = git_repo.oid()
    wal_before = coordinator.wal.read().head

    with pytest.raises(PayloadGateError, match=message) as failure:
        coordinator.land("alpha")

    assert {key: failure.value.details[key] for key in reported} == reported
    _assert_unchanged(git_repo, coordinator, branch_before, wal_before)


@pytest.mark.parametrize(
    ("kind", "message"),
    [
        ("empty-python-source", "did not emit one UTF-8 JSON value"),
        ("system-true-binary", "wrote to stderr"),
    ],
)
def test_noop_executable_cannot_paint_the_payload_gate_green(
    git_repo: GitFixture,
    tmp_path: Path,
    kind: str,
    message: str,
) -> None:
    """A gate that does nothing must not read as CLEAN, even with its digest admitted.

    Admitting the digest is the point of the case, not an oversight: it takes the
    source-identity check out of the picture so that what remains is the question this
    case exists to answer -- silence is not a verdict. Left unadmitted, the landing would
    fail before the gate ever ran, and the case would prove only what
    ``test_unadmitted_gate_source_is_rejected_before_execution`` already proves.

    The two kinds are refused on different grounds and both are pinned. An empty Python
    source genuinely does nothing: exit 0, no output, no diagnostics. A system binary
    handed to the interpreter cannot be parsed at all, so it is refused on the diagnostics
    it emits rather than on the verdict it never wrote.
    """

    gate: Path | str
    if kind == "empty-python-source":
        gate = tmp_path / "noop.py"
        gate.write_text("", encoding="utf-8")
    else:
        gate = shutil.which("true") or "/bin/true"
    coordinator = _register_and_stage(
        git_repo,
        "alpha",
        f"noop gate: {kind}\n",
        commit_payload=gate,
    )
    branch_before = git_repo.oid()
    wal_before = coordinator.wal.read().head

    with pytest.raises(PayloadGateError, match=message):
        coordinator.land("alpha")

    _assert_unchanged(git_repo, coordinator, branch_before, wal_before)


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("not-json\n", "did not emit one UTF-8 JSON value"),
        ("[]\n", "JSON is not an object"),
    ],
)
def test_non_object_or_non_json_gate_output_fails_closed(
    git_repo: GitFixture,
    tmp_path: Path,
    source: str,
    message: str,
) -> None:
    """Unparseable output and well-formed non-object output are distinct refusals.

    ``[]`` parses, so the parse check cannot see it and only the object check can; text
    that is not JSON never reaches the object check. Pinning one message per case keeps
    either check from standing in for the other after the other is removed.
    """

    gate = tmp_path / "bad_output.py"
    gate.write_text(f"print({source!r})\n", encoding="utf-8")
    coordinator = _register_and_stage(
        git_repo,
        "alpha",
        "bad gate output\n",
        commit_payload=gate,
    )
    branch_before = git_repo.oid()
    wal_before = coordinator.wal.read().head

    with pytest.raises(PayloadGateError, match=message):
        coordinator.land("alpha")

    _assert_unchanged(git_repo, coordinator, branch_before, wal_before)


@pytest.mark.parametrize(
    ("gate_name", "message"),
    [
        ("absent.py", "cannot be resolved"),
        ("gate-directory", "is not a regular file"),
    ],
)
def test_unresolvable_or_non_file_gate_source_fails_closed(
    git_repo: GitFixture,
    tmp_path: Path,
    gate_name: str,
    message: str,
) -> None:
    """A gate that resolves to nothing and one that resolves to a directory differ.

    Only the absent name fails resolution; a directory resolves perfectly well and is
    stopped one check later. Without a per-case message the directory case passes on the
    resolution check alone, and deleting the regular-file check leaves it green.
    """

    gate = tmp_path / gate_name
    if gate_name == "gate-directory":
        gate.mkdir()
    coordinator = _register_and_stage(
        git_repo,
        "alpha",
        "bad gate source\n",
        commit_payload=gate,
    )
    branch_before = git_repo.oid()
    wal_before = coordinator.wal.read().head

    with pytest.raises(PayloadGateError, match=message):
        coordinator.land("alpha")

    _assert_unchanged(git_repo, coordinator, branch_before, wal_before)


def test_gate_stderr_invalidates_an_otherwise_clean_report(
    git_repo: GitFixture,
    tmp_path: Path,
) -> None:
    gate = tmp_path / "noisy.py"
    gate.write_text(
        "import json\n"
        "import sys\n"
        "print('unexpected noise', file=sys.stderr)\n"
        'print(json.dumps({"candidate": sys.argv[2], "repository": sys.argv[4], '
        '"staged_files": ["a.txt"], "findings": [], '
        '"degraded": [], "verdict": "CLEAN"}))\n',
        encoding="utf-8",
    )
    coordinator = _register_and_stage(
        git_repo,
        "alpha",
        "noisy gate\n",
        commit_payload=gate,
    )
    branch_before = git_repo.oid()
    wal_before = coordinator.wal.read().head

    with pytest.raises(PayloadGateError, match="stderr"):
        coordinator.land("alpha")

    _assert_unchanged(git_repo, coordinator, branch_before, wal_before)


def test_degraded_payload_gate_without_a_payload_census_is_a_typed_refusal(
    git_repo: GitFixture,
    tmp_path: Path,
) -> None:
    gate = _write_gate(
        tmp_path / "degraded.py",
        {
            "repository": str(git_repo.root),
            "staged_files": [],
            "findings": [],
            "degraded": ["audit unavailable"],
            "verdict": "DEGRADED",
        },
        2,
    )
    coordinator = _register_and_stage(
        git_repo,
        "alpha",
        "degraded gate\n",
        commit_payload=gate,
    )
    branch_before = git_repo.oid()
    wal_before = coordinator.wal.read().head

    with pytest.raises(LandingRefusedError, match="degraded the candidate") as refused:
        coordinator.land("alpha")

    assert refused.value.details["reason"] == "payload_gate_degraded"
    _assert_refusal_recorded(
        git_repo, coordinator, branch_before, wal_before, "payload_gate_degraded"
    )


def test_cli_human_mode_names_what_a_degraded_gate_could_not_audit(
    git_repo: GitFixture,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A degraded gate's rows name what it could not audit. They were in the WAL row and
    the JSON payload only; human mode said `commit payload gate degraded the candidate`
    and nothing more. Each row is one stderr line under the summary
    whatever break it carries, and a row's own spacing stays: a path with two spaces is
    that path (an earlier rendering collapsed every whitespace run). The gate
    is scripted; the first row is the shipped gate's text for a per-operator include, with
    the newline git's diagnostic carries."""
    row = (
        "commit audit: rev-parse exited 128: error: could not expand include path "
        "'~/referee.gitconfig'\nfatal: bad config line 10 in file .git/config"
    )
    spaced = "census unavailable for 'a  b.txt'"
    gate = _write_gate(
        tmp_path / "degraded.py",
        {
            "repository": str(git_repo.root),
            "staged_files": [],
            "findings": [],
            "degraded": [row, spaced],
            "verdict": "DEGRADED",
        },
        2,
    )
    _register_and_stage(git_repo, "alpha", "degraded gate\n", commit_payload=gate)
    argv = [
        "land", "--repo", str(git_repo.root), "--session", "alpha",
        "--commit-payload", str(gate),
    ]
    assert main(argv) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    summary, *items = captured.err.splitlines()
    assert summary == "onetree: commit payload gate degraded the candidate"
    assert items == [
        (
            "  degraded: commit audit: rev-parse exited 128: error: could not expand "
            "include path '~/referee.gitconfig' fatal: bad config line 10 in file .git/config"
        ),
        f"  degraded: {spaced}",
    ]
    # Control: JSON carries the rows as the gate wrote them.
    assert main([*argv, "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["error"]["reason"] == "payload_gate_degraded"
    assert payload["error"]["degraded"] == [row, spaced]


def test_degraded_payload_gate_for_a_different_repository_fails_closed(
    git_repo: GitFixture,
    tmp_path: Path,
) -> None:
    gate = _write_gate(
        tmp_path / "wrong-degraded-repository.py",
        {
            "repository": str(tmp_path / "other-repository"),
            "staged_files": [],
            "findings": [],
            "degraded": ["audit unavailable"],
            "verdict": "DEGRADED",
        },
        2,
    )
    coordinator = _register_and_stage(
        git_repo,
        "alpha",
        "wrong degraded repository\n",
        commit_payload=gate,
    )
    branch_before = git_repo.oid()
    wal_before = coordinator.wal.read().head

    with pytest.raises(PayloadGateError, match="different payload"):
        coordinator.land("alpha")

    _assert_unchanged(git_repo, coordinator, branch_before, wal_before)


def test_exact_path_set_does_not_depend_on_gate_output_order(
    git_repo: GitFixture,
    tmp_path: Path,
) -> None:
    gate = _write_gate(
        tmp_path / "reverse_paths.py",
        {
            "repository": str(git_repo.root),
            "staged_files": ["b.txt", "a.txt"],
            "findings": [],
            "degraded": [],
            "verdict": "CLEAN",
        },
        0,
    )
    admit_gate(git_repo.root, gate)
    coordinator = Coordinator(git_repo.root, commit_payload=gate)
    session = coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    coordinator.claim(
        "alpha",
        [str(git_repo.root / "a.txt"), str(git_repo.root / "b.txt")],
    )
    git_repo.write("a.txt", "first path\n")
    git_repo.write("b.txt", "second path\n")
    git_repo.stage(session.index_path, "a.txt", "b.txt")

    landed = coordinator.land("alpha")

    assert git_repo.changed_paths(landed.commit) == {"a.txt", "b.txt"}


def test_payload_gate_timeout_fails_closed(
    git_repo: GitFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = tmp_path / "slow.py"
    gate.write_text("import time\ntime.sleep(1)\n", encoding="utf-8")
    monkeypatch.setattr(git_module, "PAYLOAD_GATE_TIMEOUT_SECONDS", 0.01)
    coordinator = _register_and_stage(
        git_repo,
        "alpha",
        "slow gate\n",
        commit_payload=gate,
    )
    branch_before = git_repo.oid()
    wal_before = coordinator.wal.read().head

    with pytest.raises(PayloadGateError, match="timed out"):
        coordinator.land("alpha")

    _assert_unchanged(git_repo, coordinator, branch_before, wal_before)


def test_cli_explicit_gate_overrides_the_environment_default(
    git_repo: GitFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clean_gate = Path(os.environ["ONETREE_COMMIT_PAYLOAD"])
    refusing_gate = _write_gate(
        tmp_path / "ambient_refusal.py",
        {
            "repository": str(git_repo.root),
            "staged_files": ["a.txt"],
            "findings": ["ambient gate must not win"],
            "degraded": [],
            "verdict": "REFUSE",
        },
        1,
    )
    monkeypatch.setenv("ONETREE_COMMIT_PAYLOAD", str(refusing_gate))
    coordinator = _register_and_stage(
        git_repo,
        "alpha",
        "explicit CLI gate\n",
        commit_payload=clean_gate,
    )

    code = main(
        [
            "land",
            "--repo",
            str(git_repo.root),
            "--session",
            "alpha",
            "--commit-payload",
            str(clean_gate),
            "--json",
        ]
    )

    captured = capsys.readouterr()
    assert captured.err == ""
    assert code == 0
    assert json.loads(captured.out)["land"]["commit"] == git_repo.oid()
    assert coordinator.wal.read().head is not None


def test_gate_source_digest_is_compared_against_the_repository_admission(
    git_repo: GitFixture,
    tmp_path: Path,
) -> None:
    """The comparison reads the digest this repository admitted, not the caller's gate.

    A gate whose bytes are honest and whose report would be CLEAN is still refused while the
    repository admits other bytes, and the refusal names the admitted digest and path.
    """

    gate = _write_gate(
        tmp_path / "honest_but_unadmitted.py",
        {
            "repository": str(git_repo.root),
            "staged_files": ["a.txt"],
            "findings": [],
            "degraded": [],
            "verdict": "CLEAN",
        },
        0,
    )
    admitted = tmp_path / "clean_commit_payload.py"
    coordinator = _register_and_stage(
        git_repo,
        "alpha",
        "repository admission governs\n",
        commit_payload=gate,
        admit_source=False,
    )
    branch_before = git_repo.oid()
    wal_before = coordinator.wal.read().head

    with pytest.raises(PayloadGateError, match="does not match admitted") as failure:
        coordinator.land("alpha")

    assert failure.value.details["expected_sha256"] == (
        hashlib.sha256(admitted.read_bytes()).hexdigest()
    )
    assert failure.value.details["admitted_gate"] == str(admitted.resolve())
    assert failure.value.details["observed_sha256"] == hashlib.sha256(gate.read_bytes()).hexdigest()
    _assert_unchanged(git_repo, coordinator, branch_before, wal_before)


def _land_with_observing_gate(
    git_repo: GitFixture,
    tmp_path: Path,
    content: str,
) -> tuple[Path, dict[str, Any]]:
    """Land once through a gate that records how its own process was started."""

    log = tmp_path / "observed.json"
    gate = tmp_path / "observing_gate.py"
    gate.write_text(
        _OBSERVING_GATE_SOURCE.replace("__LOG_PATH__", repr(str(log))), encoding="utf-8"
    )
    coordinator = _register_and_stage(git_repo, "alpha", content, commit_payload=gate)

    landed = coordinator.land("alpha")

    assert git_repo.oid() == landed.commit
    observed: dict[str, Any] = json.loads(log.read_text(encoding="utf-8"))
    return gate, observed


def test_payload_gate_child_runs_under_isolation_flags_with_coverage_stripped(
    git_repo: GitFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate child runs under ``-I`` and ``-S`` with no ``COVERAGE_*`` name in scope.

    Observed from inside the running child, because ``sys.flags`` and the child's own
    environment are not visible from the calling side. ``-I`` sets ``isolated`` and
    ``no_user_site``; ``-S`` sets ``no_site``, without which a ``.pth`` in the
    interpreter's own site-packages would execute at startup and read ``COVERAGE_*``.

    This does NOT establish the wider property that no ambient variable reaches the child
    at all. That property is carried by
    ``test_payload_gate_child_environment_carries_only_admitted_names`` (the child's
    environment equals the admitted set, so nothing else can be present) and by
    ``test_payload_gate_child_is_not_reachable_through_the_dynamic_loader`` (``LD_PRELOAD``
    and ``LD_AUDIT`` do not reach it). Naming this test for that wider property would claim
    an assurance its four assertions do not make.
    """

    monkeypatch.setenv("COVERAGE_PROCESS_START", str(tmp_path / "planted.coveragerc"))

    _, observed = _land_with_observing_gate(git_repo, tmp_path, "isolation is observable\n")

    keys = ("isolated", "no_site", "no_user_site", "coverage_names")
    assert {key: observed[key] for key in keys} == {
        "isolated": 1,
        "no_site": 1,
        "no_user_site": 1,
        "coverage_names": [],
    }


def test_payload_gate_executes_a_private_copy_inside_the_repository(
    git_repo: GitFixture,
    tmp_path: Path,
) -> None:
    """The child runs the admitted bytes from somewhere its author cannot reach.

    Between the digest check and the run, the configured path stays writable by whoever
    configured it. Executing it directly would leave that whole window open, so the broker
    runs a copy it made itself: a different path, read-only, byte-identical to what was
    admitted. Each of those three is asserted, because the copy without the digest proves
    nothing and the digest without the copy proves nothing.

    The working directory is asserted with it. It is the one remaining piece of the child's
    starting state the broker chooses, and a gate that resolves a relative path resolves it
    somewhere.

    The location is asserted too, and it is the reason this test carries the name it does.
    A name that states a property no assertion checks is a green row over unexamined work:
    the copy previously landed wherever ``tempfile`` chose, which is to say wherever the
    broker's own ``TMPDIR`` pointed, on a filesystem nobody in this repository selected.
    """

    gate, observed = _land_with_observing_gate(git_repo, tmp_path, "private copy is observable\n")

    git_dir = Path(git_repo.git("rev-parse", "--absolute-git-dir").stdout.strip()).resolve()
    assert Path(observed["executed_path"]).is_relative_to(git_dir)
    assert observed["executed_path"] != str(gate)
    assert observed["executed_mode"] == 0o400
    assert observed["executed_sha256"] == hashlib.sha256(gate.read_bytes()).hexdigest()
    assert observed["cwd_is_repository"] is True


def test_gate_source_replaced_while_executing_is_refused(
    git_repo: GitFixture,
    tmp_path: Path,
) -> None:
    """The bytes that were admitted must still be on disk when the run ends.

    The private copy makes the running child immune to a swap, but the configured path is
    what the next run reads and what an operator inspects. A gate that rewrites it
    mid-flight leaves an admitted digest naming bytes that no longer exist, so the run that
    produced the verdict and the file that is supposed to justify it no longer agree.

    Nothing in the suite reached this check before: it is the only refusal in the broker
    that requires the source to change inside the window it guards, and a check no input
    has ever made fire is a claim, not a control.
    """

    gate = tmp_path / "self_replacing.py"
    gate.write_text(
        _TAMPERING_GATE_SOURCE.replace("__GATE_PATH__", repr(str(gate))), encoding="utf-8"
    )
    admitted = hashlib.sha256(gate.read_bytes()).hexdigest()
    coordinator = _register_and_stage(
        git_repo,
        "alpha",
        "gate rewrites its own source\n",
        commit_payload=gate,
    )
    branch_before = git_repo.oid()
    wal_before = coordinator.wal.read().head

    with pytest.raises(PayloadGateError, match="changed while executing") as failure:
        coordinator.land("alpha")

    replaced = hashlib.sha256(gate.read_bytes()).hexdigest()
    assert failure.value.details["before_sha256"] == admitted
    assert failure.value.details["after_sha256"] == replaced
    assert replaced != admitted
    _assert_unchanged(git_repo, coordinator, branch_before, wal_before)


def test_duplicate_verdict_keys_are_refused_rather_than_resolved_last_wins(
    git_repo: GitFixture,
    tmp_path: Path,
) -> None:
    """A repeated name voids the document; it must not silently pick one of its values.

    ``json.loads`` resolves a repeated name last-wins and reports nothing, so a report
    that states ``REFUSE`` with findings and then repeats ``verdict`` and ``findings``
    parses cleanly into a document saying the opposite of its honest half. Every check
    downstream runs on the survivor, so none of them can tell the two apart.

    The gate here exits 0 to match the forged ``CLEAN``: without the refusal this
    candidate lands, credential finding and all.
    """

    gate = tmp_path / "duplicate_keys.py"
    gate.write_text(_DUPLICATE_KEY_GATE_SOURCE, encoding="utf-8")
    coordinator = _register_and_stage(
        git_repo,
        "alpha",
        "duplicate verdict keys\n",
        commit_payload=gate,
    )
    branch_before = git_repo.oid()
    wal_before = coordinator.wal.read().head

    with pytest.raises(PayloadGateError, match="did not emit one UTF-8 JSON value"):
        coordinator.land("alpha")

    _assert_unchanged(git_repo, coordinator, branch_before, wal_before)


# A gate that reports the environment its own process received. The log path is baked into
# the source as a literal rather than read from the environment: a gate that learns where to
# write from an inherited variable cannot be used to test whether inheritance was closed.
_ENVIRONMENT_GATE_TEMPLATE = """\
import json
import os
import subprocess
import sys

LOG = __LOG_PATH__

candidate = sys.argv[2]
repository = sys.argv[4]
completed = subprocess.run(
    [_GIT_, "-C", repository, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", candidate],
    check=True,
    capture_output=True,
)
paths = sorted(
    path.decode("utf-8", errors="strict")
    for path in completed.stdout.split(b"\\0")
    if path
)
with open(LOG, "w", encoding="utf-8") as stream:
    json.dump(dict(os.environ), stream)
print(
    json.dumps(
        {
            "candidate": candidate,
            "repository": repository,
            "staged_files": paths,
            "findings": [],
            "degraded": [],
            "verdict": "CLEAN",
        }
    )
)
""".replace("_GIT_", GATE_GIT_LITERAL)

# The names the gate child is admitted to receive, stated here as a literal.
#
# This restates the set that `Repository._payload_gate_environment` builds rather than
# importing it, and the duplication is deliberate: a test that imports the allowlist asserts
# only that the code equals itself, so widening the allowlist would silently widen its own
# guard. Two deliberate edits are the price of the name set being a contract.
#
# These execution controls are chosen by the broker. Optional caller identity metadata
# is declared separately so the gate can compare attribution without inheriting controls.
_ADMITTED_GATE_ENVIRONMENT_NAMES = frozenset(
    {
        "PATH",
        "LC_ALL",
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        "GIT_OPTIONAL_LOCKS",
        "GIT_NO_REPLACE_OBJECTS",
        "GIT_GRAFT_FILE",
    }
)

_NATIVE_GATE_IDENTITY_NAMES = (
    "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_THREAD_ID",
)

# The values the four Git identity names are admitted WITH, stated here as literals for the
# same reason the name set is: importing them would assert only that the code equals itself.
# The name set above cannot see this distinction at all -- an inherited value arrives under an
# admitted name, so a set comparison passes on it.
_PINNED_GATE_IDENTITY = {
    "GIT_AUTHOR_NAME": "onetree session",
    "GIT_AUTHOR_EMAIL": "onetree@localhost",
    "GIT_COMMITTER_NAME": "onetree broker",
    "GIT_COMMITTER_EMAIL": "onetree@localhost",
}


def _land_with_environment_gate(
    git_repo: GitFixture,
    tmp_path: Path,
    content: str,
    *,
    actor: str = "alpha",
    relative: str = "a.txt",
) -> dict[str, str]:
    """Land once, returning the environment the gate child actually received.

    The whole mapping is returned, not just its names: a name admitted with an inherited
    value is a different failure from a name admitted at all, and only the values separate
    them.

    `actor` and `relative` exist so one body can land twice, which is the only way to observe
    what a second landing in the same process receives. Re-landing as the same actor on the
    same path is refused -- `target changed on payload paths` -- so a second landing supplies
    its own session and its own file. The log and gate files are named per actor, or the
    second landing would read the first one's record and agree with it.
    """

    log = tmp_path / f"gate_environment_{actor}.json"
    gate = tmp_path / f"environment_gate_{actor}.py"
    gate.write_text(
        _ENVIRONMENT_GATE_TEMPLATE.replace("__LOG_PATH__", repr(str(log))),
        encoding="utf-8",
    )
    coordinator = _register_and_stage(
        git_repo, actor, content, commit_payload=gate, relative=relative
    )

    landed = coordinator.land(actor)

    assert git_repo.oid() == landed.commit
    environment: dict[str, str] = json.loads(log.read_text(encoding="utf-8"))
    return environment


@pytest.mark.parametrize("native_identity", [
    {},
    {"CODEX_THREAD_ID": "11111111-1111-4111-8111-111111111111"},
    {"CLAUDE_CODE_SESSION_ID": "22222222-2222-4222-8222-222222222222"},
    {"CLAUDE_SESSION_ID": "33333333-3333-4333-8333-333333333333"},
    {
        "CODEX_THREAD_ID": " 11111111-1111-4111-8111-111111111111 ",
        "CLAUDE_CODE_SESSION_ID": "22222222-2222-4222-8222-222222222222",
        "CLAUDE_SESSION_ID": "33333333-3333-4333-8333-333333333333",
    },
    {"CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": " ", "CLAUDE_SESSION_ID": ""},
])
def test_payload_gate_child_environment_carries_only_admitted_names(
    git_repo: GitFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    native_identity: dict[str, str],
) -> None:
    """Only chosen execution controls and caller identity may reach the gate child.

    Anything reaching the child's environment reaches code that runs before the gate's first
    line, so the environment is part of the gate's trusted computing base. Subtracting
    known-dangerous names cannot establish that: the set of names able to carry code grows
    with every release of Git, CPython and the loader, and a name nobody has thought of yet
    is admitted by default under subtraction and refused by default under admission.

    The parent here carries names from every channel the audit relies on being closed --
    loader, interpreter, Git, coverage, and the temporary-file root -- and the child is
    required to see none of them. ``TMPDIR`` is in that list because a gate handed it
    chooses where its own scratch work lands, on a filesystem it was told about rather than
    one the broker picked.
    """

    for name in _NATIVE_GATE_IDENTITY_NAMES:
        monkeypatch.delenv(name, raising=False)
    for name, value in native_identity.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("SESSION_ID", "unrelated identity must not pass")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "attacker_modules"))
    monkeypatch.setenv("PYTHONSTARTUP", str(tmp_path / "attacker_startup.py"))
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", str(tmp_path / "attacker_diff"))
    monkeypatch.setenv("GIT_SSH_COMMAND", str(tmp_path / "attacker_ssh"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.pager")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(tmp_path / "attacker_pager"))
    monkeypatch.setenv("COVERAGE_PROCESS_START", str(tmp_path / "attacker_coverage"))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "attacker_tmp"))
    monkeypatch.setenv("ONETREE_TEST_AMBIENT_MARKER", "inherited")

    received = _land_with_environment_gate(git_repo, tmp_path, "admitted names only\n")

    assert set(received) == _ADMITTED_GATE_ENVIRONMENT_NAMES | native_identity.keys()
    assert {name: received[name] for name in native_identity} == native_identity


def test_payload_gate_child_identity_is_chosen_rather_than_inherited(
    git_repo: GitFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git execution identity must remain independent of ambient author settings.

    The name set is only half the property. `_environment` applies the four Git identity
    names with `setdefault`, so a parent that already carries one keeps its own -- and a
    forwarded value arrives under a name the allowlist admits, which is exactly the shape a
    set comparison cannot see. What reaches the child then decides who a commit the gate
    makes is attributed to, chosen by whoever set the parent's environment.

    The parent here carries all four, each with a value no part of the broker would pick.
    """

    for name in _PINNED_GATE_IDENTITY:
        monkeypatch.setenv(name, f"ambient {name}")

    received = _land_with_environment_gate(git_repo, tmp_path, "identity is pinned\n")

    assert {name: received[name] for name in _PINNED_GATE_IDENTITY} == _PINNED_GATE_IDENTITY


def _default_search_path() -> str:
    """`os.defpath` with its empty components dropped: the `PATH` the gate child is promised.

    Re-deriving the expected value is sound only where the default is the host's, as it is for
    the one test below that calls this. A test supplying its own default must spell the answer
    out instead: re-derivation there would compare the code against itself, and a defect
    applied to the broker and to this function alike would be invisible to both.
    """
    return os.pathsep.join(entry for entry in os.defpath.split(os.pathsep) if entry)


def test_payload_gate_child_path_is_the_default_search_path(
    git_repo: GitFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`PATH` is chosen too: the child searches the platform default, whatever the parent had.

    The name test cannot see this, since `PATH` is admitted, and the value test above pins
    only the four identity names. The reason is the admission contract itself, not a defence
    the gate needs: the shipped gate resolves `git` through its own default search path and
    never through this value (measured 2026-09-02, CLEAN with a fake `git` first on `PATH`
    and with no `git` on `PATH` at all), so a forwarded `PATH` would be the one admitted
    value that arrived ambient. The parent here leads its `PATH` with a directory nobody
    vetted. The equality with the platform default is the check; the assertion before it
    only names the directory a forwarded value would have carried, so a failure reads as one.
    """

    attacker_bin = tmp_path / "attacker_bin"
    attacker_bin.mkdir()
    inherited = os.environ.get("PATH")
    monkeypatch.setenv(
        "PATH", os.pathsep.join([str(attacker_bin), *([inherited] if inherited else [])])
    )

    received = _land_with_environment_gate(git_repo, tmp_path, "path is pinned\n")

    assert str(attacker_bin) not in received["PATH"].split(os.pathsep)
    assert received["PATH"] == _default_search_path()


def test_payload_gate_child_path_carries_no_empty_component(
    git_repo: GitFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty `PATH` component means the working directory, which is the repository under audit.

    The platform default carries none here, so a fixture that reused it would pass any broker
    that gets the drop wrong. Empty components sit in the leading, middle and trailing
    positions, so stripping only the leading separator leaves two behind; two of them are
    adjacent, so collapsing a doubled separator -- a non-overlapping string replacement --
    leaves one behind. The entries are names no platform ships, so a broker that hard-codes a
    literal or captures the default at import hands over a value without them. Their order is
    neither ascending nor descending, so a broker that drops the empties and then sorts what
    is left has reordered a search path whichever way it sorts. Two entries cannot carry that
    last property, because any pair is already sorted one way or the other: the revision
    before this one used a descending pair and so passed a descending-sorting broker outright,
    having been written to catch an ascending one.

    Each property is load-bearing, measured by ablation: removing it from both defaults turns
    the broker it answers green. Sorted entries pass a sorting broker in the matching
    direction; entries equal to the host's own default pass every broker that never reads
    `os.defpath`; no adjacent pair passes a collapse-then-strip broker; a leading-only empty
    passes a leading-strip broker. The ablation has to substitute the host's real default, not
    merely plausible names: a fixture that differs in content fails those brokers anyway, and
    reports nothing about the property under test.

    It lands twice because the defect need not live inside `_payload_gate_environment`. A
    broker memoising anywhere between that method and the child's `environ` -- at the
    `subprocess` launch frame, say -- hands back what the first landing of the process saw,
    and calling the method directly never walks that segment. The first landing is asserted
    rather than discarded, so the cold path is sampled instead of merely consumed.

    Both expected values are spelled out rather than re-derived from `os.defpath`. An oracle
    that recomputes the production expression asserts only that the code equals itself, which
    is the rule this file already states for the admitted name set.
    """

    monkeypatch.setattr(
        os, "defpath", os.pathsep.join(["/opt/warm-b", "", "/opt/warm-c", "/opt/warm-a"])
    )

    first = _land_with_environment_gate(git_repo, tmp_path, "first landing\n")

    assert first["PATH"] == os.pathsep.join(["/opt/warm-b", "/opt/warm-c", "/opt/warm-a"])

    monkeypatch.setattr(
        os,
        "defpath",
        os.pathsep.join(["", "/opt/vetted-b", "", "", "/opt/vetted-c", "/opt/vetted-a", ""]),
    )

    second = _land_with_environment_gate(
        git_repo, tmp_path, "empty component dropped\n", actor="beta", relative="b.txt"
    )

    assert "" not in second["PATH"].split(os.pathsep)
    assert second["PATH"] == os.pathsep.join(["/opt/vetted-b", "/opt/vetted-c", "/opt/vetted-a"])


def test_payload_gate_child_is_not_reachable_through_the_dynamic_loader(
    git_repo: GitFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ambient loader variable cannot reach the gate child, and cannot break it either.

    `-I` and `-S` are interpreter flags: ld.so resolves LD_PRELOAD and LD_AUDIT before
    CPython's main() runs, so a library named there executes ahead of every control the
    broker has, and the SHA-256 pin does not help -- it verifies the bytes on disk, which are
    exactly the bytes an injected constructor lets the interpreter go on to read.

    The observable is the loader's own diagnostic for an unresolvable entry. The broker
    treats any stderr from the gate as fatal, so while the variable is inherited the landing
    cannot succeed at all; once it is dropped, the same landing is clean. A landing that
    SUCCEEDS is therefore the evidence, and no forged verdict can counterfeit it.
    """

    monkeypatch.setenv("LD_PRELOAD", str(tmp_path / "not-a-library.so"))
    monkeypatch.setenv("LD_AUDIT", str(tmp_path / "not-an-auditor.so"))

    received = _land_with_environment_gate(git_repo, tmp_path, "loader channel is closed\n")

    assert "LD_PRELOAD" not in received
    assert "LD_AUDIT" not in received


def test_the_gate_onetree_ships_admits_a_benign_landing(git_repo: GitFixture) -> None:
    """onetree's own standard gate, admitted in this repository, admits a benign landing.

    Every other test in this module runs a purpose-built gate, so the child environment the
    broker builds is never tested against a real gate: an environment that starves a real
    gate would pass every stub in this file.
    """

    gate = Path(onetree.__file__).with_name("standard_gate.py")
    assert gate.is_file(), gate
    coordinator = _register_and_stage(
        git_repo, "alpha", "standard gate integration\n", commit_payload=gate
    )
    landed = coordinator.land("alpha", message="Standard gate integration\n")

    assert git_repo.oid() == landed.commit
    assert landed.gate_receipt is not None
    assert landed.gate_receipt["report"]["verdict"] == "CLEAN"
