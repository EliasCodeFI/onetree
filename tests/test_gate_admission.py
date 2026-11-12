"""Per-repository payload gate admission: the operator admits a gate by path and SHA-256."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from conftest import _CLEAN_PAYLOAD_GATE_SOURCE, GitFixture

from onetree import git as git_module
from onetree.cli import main
from onetree.core import Coordinator
from onetree.errors import PayloadGateError

_KEYS = ("onetree.payloadGate", "onetree.payloadGateSha256")


def _json_call(
    capsys: pytest.CaptureFixture[str], argv: Sequence[str]
) -> tuple[int, dict[str, Any]]:
    code = main([*argv, "--json"])
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.count("\n") == 1
    payload = json.loads(captured.out)
    assert isinstance(payload, dict)
    return code, payload


def _unadmit(git_repo: GitFixture) -> None:
    for key in _KEYS:
        git_repo.git("config", "--local", "--unset-all", key)


def _local(git_repo: GitFixture) -> dict[str, str]:
    listed = git_repo.git("config", "--local", "--list").stdout.splitlines()
    return dict(line.split("=", 1) for line in listed if line.startswith("onetree.payloadgate"))


def _gate(tmp_path: Path, name: str = "operator_gate.py") -> Path:
    gate = tmp_path / name
    gate.write_text(_CLEAN_PAYLOAD_GATE_SOURCE, encoding="utf-8")
    return gate


def _stage(git_repo: GitFixture, actor: str, relative: str, content: str) -> Coordinator:
    coordinator = Coordinator(git_repo.root)
    session = coordinator.register_session(actor, pid=os.getpid(), ttl=300)
    coordinator.claim(actor, [str(git_repo.root / relative)])
    git_repo.write(relative, content)
    git_repo.stage(session.index_path, relative)
    return coordinator


def _source(
    path: str | None,
    origin: str | None,
    resolved: str | None,
    digest: str | None,
    error: str | None = None,
) -> dict[str, str | None]:
    return {"path": path, "origin": origin, "resolved": resolved, "sha256": digest, "error": error}


def test_an_unadmitted_repository_reports_no_gate(
    git_repo: GitFixture, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONETREE_COMMIT_PAYLOAD", raising=False)
    _unadmit(git_repo)
    code, payload = _json_call(capsys, ["gate", "--repo", str(git_repo.root)])
    assert code == 1
    assert payload == {
        "ok": True,
        "gate": {
            "admitted": False,
            "path": None,
            "sha256": None,
            "source": _source(None, None, None, None),
            "source_sha256": None,
            "matches": False,
            "config": {"path": _KEYS[0], "sha256": _KEYS[1]},
        },
    }
    assert main(["gate", "--repo", str(git_repo.root)]) == 1
    assert capsys.readouterr().out == "no payload gate admitted; landing refuses\n"


def test_admission_through_the_cli_is_visible_as_path_and_digest(
    git_repo: GitFixture,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ONETREE_COMMIT_PAYLOAD", raising=False)
    _unadmit(git_repo)
    gate = _gate(tmp_path)
    digest = hashlib.sha256(gate.read_bytes()).hexdigest()
    code, payload = _json_call(
        capsys,
        ["gate", "--repo", str(git_repo.root), "--admit", str(gate), "--sha256", digest],
    )
    assert code == 0
    assert payload["gate"] == {
        "admitted": True,
        "path": str(gate),
        "sha256": digest,
        "source": _source(str(gate), "admission", str(gate.resolve()), digest),
        "source_sha256": digest,
        "matches": True,
        "config": {"path": _KEYS[0], "sha256": _KEYS[1]},
    }
    assert _local(git_repo) == {
        "onetree.payloadgate": str(gate),
        "onetree.payloadgatesha256": digest,
    }
    code, shown = _json_call(capsys, ["gate", "--repo", str(git_repo.root)])
    assert (code, shown) == (0, payload)
    assert main(["gate", "--repo", str(git_repo.root)]) == 0
    assert capsys.readouterr().out == (
        f"payload gate {gate} admitted at sha256 {digest}; source matches\n"
    )


def test_a_changed_or_missing_admitted_source_is_reported(
    git_repo: GitFixture,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ONETREE_COMMIT_PAYLOAD", raising=False)
    gate = _gate(tmp_path)
    admitted = git_module.Repository(git_repo.root).admit_payload_gate(gate).sha256
    gate.write_bytes(gate.read_bytes() + b"#")
    changed = hashlib.sha256(gate.read_bytes()).hexdigest()
    code, payload = _json_call(capsys, ["gate", "--repo", str(git_repo.root)])
    assert code == 1
    assert (payload["gate"]["sha256"], payload["gate"]["source_sha256"]) == (admitted, changed)
    assert payload["gate"]["matches"] is False
    assert main(["gate", "--repo", str(git_repo.root)]) == 1
    assert capsys.readouterr().out.endswith(
        f"; SOURCE DIFFERS (sha256 {changed}), landing refuses\n"
    )

    gate.unlink()
    code, payload = _json_call(capsys, ["gate", "--repo", str(git_repo.root)])
    assert code == 1
    assert (payload["gate"]["source_sha256"], payload["gate"]["matches"]) == (None, False)
    assert "cannot be resolved" in payload["gate"]["source"]["error"]
    assert main(["gate", "--repo", str(git_repo.root)]) == 1
    assert "; SOURCE UNREADABLE (commit payload gate cannot be resolved" in (
        capsys.readouterr().out
    )


@pytest.mark.parametrize(
    ("named", "message"),
    [
        ("0" * 64, "does not match the digest named for admission"),
        ("A" * 64, "not a lowercase hex SHA-256"),
        ("abc", "not a lowercase hex SHA-256"),
    ],
)
def test_admission_refuses_bytes_other_than_the_named_digest(
    git_repo: GitFixture,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    named: str,
    message: str,
) -> None:
    before = _local(git_repo)
    gate = _gate(tmp_path, "reviewed_gate.py")
    code, payload = _json_call(
        capsys, ["gate", "--repo", str(git_repo.root), "--admit", str(gate), "--sha256", named]
    )
    assert code == 2
    assert payload["ok"] is False
    assert payload["error"]["type"] == "payload_gate_error"
    assert message in payload["error"]["message"]
    assert _local(git_repo) == before


def test_admission_refuses_a_path_that_is_not_a_regular_file(
    git_repo: GitFixture, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = _local(git_repo)
    for source, message in (
        (tmp_path / "absent.py", "cannot be resolved"),
        (tmp_path, "not a regular file"),
    ):
        code, payload = _json_call(
            capsys, ["gate", "--repo", str(git_repo.root), "--admit", str(source)]
        )
        assert code == 2
        assert payload["error"]["type"] == "payload_gate_error"
        assert message in payload["error"]["message"]
    assert _local(git_repo) == before


def test_sha256_without_admit_is_a_usage_error(
    git_repo: GitFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["gate", "--repo", str(git_repo.root), "--sha256", "0" * 64])
    assert exit_info.value.code == 2
    assert "--sha256 requires --admit" in capsys.readouterr().err


def test_cli_landing_refuses_a_changed_gate_with_a_machine_readable_error(
    git_repo: GitFixture,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ONETREE_COMMIT_PAYLOAD", raising=False)
    gate = _gate(tmp_path)
    code, _ = _json_call(capsys, ["gate", "--repo", str(git_repo.root), "--admit", str(gate)])
    assert code == 0
    coordinator = _stage(git_repo, "alpha", "a.txt", "through the cli\n")
    land = [
        "land",
        "--repo",
        str(git_repo.root),
        "--session",
        "alpha",
        "--commit-payload",
        str(gate),
    ]
    code, landed = _json_call(capsys, land)
    assert code == 0
    assert landed["land"]["commit"] == git_repo.oid()

    gate.write_bytes(gate.read_bytes().replace(b"CLEAN", b"CLEAN", 1) + b" ")
    _stage(git_repo, "beta", "b.txt", "after one byte\n")
    branch_before = git_repo.oid()
    wal_before = coordinator.wal.read().head
    code, refused = _json_call(capsys, [*land[:4], "beta", *land[5:]])
    assert code == 2
    assert refused["ok"] is False
    assert refused["error"]["type"] == "payload_gate_error"
    assert refused["error"]["message"] == (
        "commit payload gate source does not match admitted SHA-256"
    )
    assert (
        refused["error"]["expected_sha256"] == landed["land"]["gate_receipt"]["gate_source_sha256"]
    )
    assert refused["error"]["observed_sha256"] == hashlib.sha256(gate.read_bytes()).hexdigest()
    assert (git_repo.oid(), coordinator.wal.read().head) == (branch_before, wal_before)

    code, _ = _json_call(capsys, ["gate", "--repo", str(git_repo.root), "--admit", str(gate)])
    assert code == 0
    code, relanded = _json_call(capsys, [*land[:4], "beta", *land[5:]])
    assert code == 0
    assert relanded["land"]["commit"] == git_repo.oid()


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ({_KEYS[0]: "PATH"}, "admission is incomplete"),
        ({_KEYS[1]: "SHA"}, "admission is incomplete"),
        ({_KEYS[0]: " ", _KEYS[1]: "SHA"}, "admission is incomplete"),
        ({_KEYS[0]: "PATH", _KEYS[1]: "0" * 63}, "not a lowercase hex SHA-256"),
        ({_KEYS[0]: "PATH", _KEYS[1]: "UPPER"}, "not a lowercase hex SHA-256"),
    ],
)
def test_a_malformed_admission_refuses_landing(
    git_repo: GitFixture, tmp_path: Path, settings: dict[str, str], message: str
) -> None:
    gate = _gate(tmp_path)
    digest = hashlib.sha256(gate.read_bytes()).hexdigest()
    _unadmit(git_repo)
    for key, value in settings.items():
        value = (
            value.replace("PATH", str(gate)).replace("UPPER", digest.upper()).replace("SHA", digest)
        )
        git_repo.git("config", "--local", key, value)
    coordinator = _stage(git_repo, "alpha", "a.txt", "malformed admission\n")
    branch_before = git_repo.oid()
    with pytest.raises(PayloadGateError, match=message):
        coordinator.land("alpha")
    assert git_repo.oid() == branch_before


def test_two_admitted_values_refuse_landing_and_admission(
    git_repo: GitFixture, tmp_path: Path
) -> None:
    gate = _gate(tmp_path)
    digest = hashlib.sha256(gate.read_bytes()).hexdigest()
    git_repo.git("config", "--local", "--add", _KEYS[1], digest)
    coordinator = _stage(git_repo, "alpha", "a.txt", "two digests\n")
    with pytest.raises(PayloadGateError, match="must have one value"):
        coordinator.land("alpha")

    included = tmp_path / "admission.gitconfig"
    git_repo.git("config", "--file", str(included), _KEYS[1], digest)
    _unadmit(git_repo)
    git_repo.git("config", "--local", "include.path", str(included))
    with pytest.raises(PayloadGateError, match="must have one value"):
        coordinator.repo.admit_payload_gate(gate)


def test_an_operator_global_admission_is_not_the_repositorys(
    git_repo: GitFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = _gate(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    global_config = home / ".gitconfig"
    git_repo.git("config", "--file", str(global_config), _KEYS[0], str(gate))
    git_repo.git(
        "config",
        "--file",
        str(global_config),
        _KEYS[1],
        hashlib.sha256(gate.read_bytes()).hexdigest(),
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    # The referee runs the suite with GIT_CONFIG_GLOBAL=/dev/null; the broker's git drops
    # that name and reads HOME, and so must this fixture's.
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    _unadmit(git_repo)
    assert git_repo.git("config", "--get", _KEYS[1]).stdout.strip()
    repository = git_module.Repository(git_repo.root)
    assert repository.payload_gate_admission() is None
    _stage(git_repo, "alpha", "a.txt", "global admission\n")
    branch_before = git_repo.oid()
    with pytest.raises(PayloadGateError, match="not admitted for this repository"):
        Coordinator(git_repo.root, commit_payload=gate).land("alpha")
    assert git_repo.oid() == branch_before


@pytest.mark.parametrize(
    ("appended", "message"),
    [
        (b"\n[onetree\n", "cannot read payload gate admission"),
        (b"\n[onetree]\n\tpayloadGate = \xff\n", "payload gate admission is not UTF-8"),
    ],
)
def test_unreadable_admission_configuration_is_a_gate_error(
    git_repo: GitFixture, appended: bytes, message: str
) -> None:
    repository = git_module.Repository(git_repo.root)
    config = repository.git_dir / "config"
    config.write_bytes(config.read_bytes() + appended)
    with pytest.raises(PayloadGateError, match=message):
        repository.payload_gate_admission()

