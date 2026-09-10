"""Exercise source admission against the explicitly supplied estate gate."""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import GitFixture, KillGit

from onetree import git as git_module
from onetree import referee
from onetree.core import Coordinator
from onetree.errors import LandingRefusedError

_SOURCE_PIN = git_module.PAYLOAD_GATE_SOURCE_SHA256
_SESSION = "11111111-1111-4111-8111-111111111111"
_OTHER_SESSION = "22222222-2222-4222-8222-222222222222"


def test_crash_instrument_forwards_calls_without_fault_configuration(
    git_repo: GitFixture,
    kill_git: KillGit,
) -> None:
    result = subprocess.run(
        [str(kill_git.executable), "-C", str(git_repo.root), "interpret-trailers", "--parse"],
        input="Subject\n\nCaller-Trailer: retained\n",
        capture_output=True,
        text=True,
        env={"LC_ALL": "C"},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "Caller-Trailer: retained\n"
    assert not kill_git.state.exists()


@pytest.mark.parametrize("name", [
    "ONETREE_TEST_KILL_REF", "ONETREE_TEST_KILL_STATE",
    "ONETREE_TEST_KILL_NTH", "ONETREE_TEST_KILL_MODE",
])
def test_crash_instrument_rejects_partial_fault_configuration(
    kill_git: KillGit, name: str,
) -> None:
    result = subprocess.run(
        [str(kill_git.executable), "--version"],
        capture_output=True,
        text=True,
        env={"LC_ALL": "C", name: "armed"},
        check=False,
    )
    assert result.returncode != 0
    assert "unbound variable" in result.stderr
    assert not kill_git.state.exists()


@pytest.fixture(autouse=True)
def clean_commit_payload_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    # Override the parent fixture: admission must exercise the production pin.
    for name in (
        "ONETREE_COMMIT_PAYLOAD",
        "CLAUDE_SESSION_ID",
        "CLAUDE_CODE_SESSION_ID",
        "CODEX_THREAD_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    assert git_module.PAYLOAD_GATE_SOURCE_SHA256 == _SOURCE_PIN


@pytest.fixture
def estate_gate(request: pytest.FixtureRequest) -> Path:
    source = os.environ.get("ONETREE_TEST_ESTATE_GATE")
    if source is None:
        pytest.skip("requires explicit ONETREE_TEST_ESTATE_GATE for source admission")
    gate = Path(source).resolve(strict=True)
    request.node.user_properties.extend(
        [
            ("implementation", str(git_module.__file__)),
            ("gate", str(gate)),
            ("gate_sha256", hashlib.sha256(gate.read_bytes()).hexdigest()),
        ]
    )
    return gate


@pytest.mark.parametrize(("vendor", "environment_name"), [
    ("claude", "CLAUDE_CODE_SESSION_ID"),
    ("claude", "CLAUDE_SESSION_ID"),
    ("codex", "CODEX_THREAD_ID"),
])
@pytest.mark.parametrize("case", ["valid", "missing_coauthor", "missing_session", "wrong_session"])
@pytest.mark.parametrize("suffix", ["", "\n\n# comment"], ids=["plain", "comment_tail"])
def test_real_gate_preserves_vendor_identity_and_refuses_bad_attribution(
    git_repo: GitFixture,
    estate_gate: Path,
    monkeypatch: pytest.MonkeyPatch,
    vendor: str,
    environment_name: str,
    case: str,
    suffix: str,
) -> None:
    trailer_name = "Claude-Session" if vendor == "claude" else "Codex-Session"
    monkeypatch.setenv(environment_name, _SESSION)
    coordinator = Coordinator(git_repo.root, commit_payload=estate_gate)
    session = coordinator.register_session("admission", pid=os.getpid(), ttl=300)
    coordinator.claim("admission", [str(git_repo.root / "a.txt")])
    git_repo.write("a.txt", f"{vendor} {case}\n")
    git_repo.stage(session.index_path, "a.txt")
    branch_before = git_repo.oid()
    wal_before = coordinator.wal.read().head
    trailers = []
    if case != "missing_coauthor":
        trailers.append(f"Co-Authored-By: {vendor.title()} <{vendor}@example.invalid>")
    if case != "missing_session":
        identity = _OTHER_SESSION if case == "wrong_session" else _SESSION
        trailers.append(f"{trailer_name}: {identity}")
    message = "Estate admission control\n\n" + "\n".join(trailers) + suffix

    if case == "valid":
        landed = coordinator.land("admission", message=message)
        assert git_repo.oid() == landed.commit
        assert coordinator.wal.read().head != wal_before
        body = git_repo.git("show", "-s", "--format=%B", landed.commit).stdout
        parsed = git_repo.git("interpret-trailers", "--parse", input_text=body).stdout
        assert f"{trailer_name}: {_SESSION}" in parsed
        assert "Co-Authored-By:" in parsed
        assert "Onetree-Operation:" in parsed
        assert suffix in body
    else:
        with pytest.raises(LandingRefusedError) as refused:
            coordinator.land("admission", message=message)
        assert refused.value.details["reason"] == "payload_gate_refused"
        expected = (
            "identity-other-session" if case == "wrong_session" else "identity-missing-trailer"
        )
        assert any(row["kind"] == expected for row in refused.value.details["findings"])
        assert git_repo.oid() == branch_before
        assert coordinator.wal.read().head != wal_before
        report = coordinator.inspect_wal()
        assert report.consistent
        assert [
            (event.kind, event.data.get("reason"), event.data.get("paths"))
            for event in report.events
            if event.kind in {"refused", "land_intent", "landed"}
        ] == [("refused", "payload_gate_refused", ["a.txt"])]


@pytest.mark.parametrize(("identity", "trailer", "refused"), [
    (
        {"CODEX_THREAD_ID": _SESSION, "CLAUDE_CODE_SESSION_ID": _OTHER_SESSION},
        f"Codex-Session: {_SESSION}", False,
    ),
    (
        {"CODEX_THREAD_ID": _SESSION, "CLAUDE_CODE_SESSION_ID": _OTHER_SESSION},
        f"Codex-Session: {_OTHER_SESSION}", True,
    ),
    (
        {"CLAUDE_CODE_SESSION_ID": _OTHER_SESSION, "CLAUDE_SESSION_ID": _SESSION},
        f"Claude-Session: {_SESSION}", False,
    ),
])
def test_real_gate_keeps_vendor_and_alias_identity_semantics(
    git_repo: GitFixture,
    estate_gate: Path,
    monkeypatch: pytest.MonkeyPatch,
    identity: dict[str, str],
    trailer: str,
    refused: bool,
) -> None:
    for name, value in identity.items():
        monkeypatch.setenv(name, value)
    coordinator = Coordinator(git_repo.root, commit_payload=estate_gate)
    session = coordinator.register_session("identity", pid=os.getpid(), ttl=300)
    coordinator.claim("identity", [str(git_repo.root / "a.txt")])
    git_repo.write("a.txt", "native identity control\n")
    git_repo.stage(session.index_path, "a.txt")
    branch_before = git_repo.oid()
    message = f"Identity control\n\nCo-Authored-By: Codex <codex@example.invalid>\n{trailer}\n"
    if refused:
        with pytest.raises(LandingRefusedError) as failure:
            coordinator.land("identity", message=message)
        assert any(
            row["kind"] == "identity-other-session" for row in failure.value.details["findings"]
        )
        assert git_repo.oid() == branch_before
    else:
        landed = coordinator.land("identity", message=message)
        assert git_repo.oid() == landed.commit


@pytest.mark.parametrize("channel", ["local", "global", "system", "environment"])
def test_landing_message_does_not_execute_configured_trailer_commands(
    git_repo: GitFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    channel: str,
) -> None:
    marker = tmp_path / "trailer-command-ran"
    script = f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
    key = "trailer.Onetree-Operation.cmd"
    if channel == "local":
        git_repo.git("config", key, command)
    elif channel == "environment":
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", key)
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", command)
    else:
        config = tmp_path / "injected.gitconfig"
        git_repo.git("config", "--file", str(config), key, command)
        monkeypatch.setenv(f"GIT_CONFIG_{channel.upper()}", str(config))
        monkeypatch.delenv("GIT_CONFIG_NOSYSTEM", raising=False)

    git_repo.git(
        "interpret-trailers", "--trailer", "Onetree-Operation: control",
        input_text="Control\n",
    )
    assert marker.read_text() == "ran"
    marker.unlink()
    repository = git_module.Repository(git_repo.root)
    message = repository.landing_message("Control", "a" * 32, "b" * 32, "admission")
    assert "Onetree-Operation:" in message
    assert f"Onetree-Operation: {'a' * 32}" in message
    assert not marker.exists()


def test_real_gate_refuses_a_per_operator_include_as_degraded(
    git_repo: GitFixture,
    estate_gate: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On the path the broker invokes -- the gate's `commit` mode -- the audit children
    whose reads decide the verdict run without HOME whatever the caller holds, so a `~`
    include in `.git/config` fails their config read; the gate reports DEGRADED naming the
    include and the broker refuses `payload_gate_degraded` with a WAL row. The gate's
    index-check mode is not this path: there a safe.directory probe that forwards HOME
    runs first, and without HOME its own failure is the DEGRADED (Round BF6 DUO). The stub
    gate of the ordinary suite dies on the same input (`PayloadGateError`, no row); the
    workspace-guard fixture asserts that shape, this one the shipped gate's (Round BF4
    DUO, task #164)."""
    home = tmp_path / "home"
    home.mkdir()
    git_repo.git("config", "--file", str(home / "referee.gitconfig"), "onetree.referee",
                 "/bin/true")
    git_repo.git("config", "include.path", "~/referee.gitconfig")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _SESSION)
    coordinator = Coordinator(git_repo.root, commit_payload=estate_gate)
    session = coordinator.register_session("admission", pid=os.getpid(), ttl=300)
    coordinator.claim("admission", [str(git_repo.root / "a.txt")])
    git_repo.write("a.txt", "per-operator include\n")
    git_repo.stage(session.index_path, "a.txt")
    message = (
        "Per-operator include\n\nCo-Authored-By: Claude <claude@example.invalid>\n"
        f"Claude-Session: {_SESSION}\n"
    )
    # Control: the broker's own read, which keeps HOME, sees the included referee.
    assert referee.configuration(coordinator.repo) == ("/bin/true", referee.DEFAULT_TIMEOUT)
    with pytest.raises(LandingRefusedError) as refused:
        coordinator.land("admission", message=message)
    assert refused.value.details["reason"] == "payload_gate_degraded"
    assert any("referee.gitconfig" in str(row) for row in refused.value.details["degraded"])
    assert git_repo.oid() == git_repo.base
    assert [
        (event.kind, event.data.get("reason"))
        for event in coordinator.wal.read().events
        if event.kind in {"refused", "land_intent", "landed"}
    ] == [("refused", "payload_gate_degraded")]
