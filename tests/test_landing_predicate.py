"""Differential and end-to-end contract for broker landing identity."""

from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from conftest import GitFixture

import onetree.git as git_module
from onetree.core import Coordinator
from onetree.git import Repository, landing_ids

_GIT_EXECUTABLE = shutil.which("git") or "git"
_FIELD_SEPARATOR = "\x1f"
_A = "a" * 32
_B = "b" * 32


@dataclass(frozen=True)
class PredicateCase:
    name: str
    message: bytes
    expected: frozenset[str]
    allowed_extra: frozenset[str] = frozenset()
    generated: bool = False


def _cases() -> tuple[PredicateCase, ...]:
    scissors = b"# ------------------------ >8 ------------------------"
    cases = [
        PredicateCase("clean", f"subject\n\nOnetree-Operation: {_A}\n".encode(), frozenset({_A})),
        PredicateCase(
            "divider-in-body",
            f"subject\n\n---\nreview notes\n\nOnetree-Operation: {_A}\n".encode(),
            frozenset({_A}),
        ),
        PredicateCase(
            "diff-hunk-in-body",
            (
                "subject\n\n--- a/pkg.py\n+++ b/pkg.py\n@@ -1 +1 @@\n-old\n+new\n\n"
                f"Onetree-Operation: {_A}\n"
            ).encode(),
            frozenset({_A}),
        ),
        PredicateCase(
            "comment-tail",
            f"subject\n\nOnetree-Operation: {_A}\n\n# editor comment\n".encode(),
            frozenset({_A}),
        ),
        PredicateCase(
            "conflicts-tail",
            f"subject\n\nOnetree-Operation: {_A}\n\nConflicts:\n\tfile.txt\n".encode(),
            frozenset({_A}),
        ),
        PredicateCase(
            "scissors-tail",
            f"subject\n\nOnetree-Operation: {_A}\n".encode()
            + scissors
            + b"\nignored editor text\n",
            frozenset({_A}),
        ),
        PredicateCase(
            "folded-squash-behind-prose",
            (
                f"squashed\n\nOnetree-Operation: {_A}\n\nsecond\n\n"
                f"Onetree-Operation: {_B}\n\nrelease prose\n"
            ).encode(),
            frozenset({_A, _B}),
            frozenset({_A}),
        ),
        PredicateCase(
            "prose-line",
            f"subject\n\nOnetree-Operation: {_A}\nordinary prose\n".encode(),
            frozenset({_A}),
        ),
        PredicateCase(
            "text-after-trailer-block",
            f"subject\n\nOnetree-Operation: {_A}\n\nreviewer notes\n".encode(),
            frozenset({_A}),
        ),
        PredicateCase(
            "title-marker",
            f"Onetree-Operation: {_A}\n".encode(),
            frozenset({_A}),
            frozenset({_A}),
        ),
        PredicateCase(
            "configured-equals-separator",
            f"subject\n\nOnetree-Operation={_A}\n".encode(),
            frozenset({_A}),
        ),
        PredicateCase(
            "folded-space",
            f"subject\n\nOnetree-Operation: {_A}\n  continued\n".encode(),
            frozenset({_A, f"{_A} continued"}),
        ),
        PredicateCase(
            "folded-tab",
            f"subject\n\nOnetree-Operation: {_A}\n\tcontinued\n".encode(),
            frozenset({_A, f"{_A} continued"}),
            frozenset({_A}),
        ),
        PredicateCase(
            "repeated-values",
            f"subject\n\nOnetree-Operation: {_A}\nOnetree-Operation: {_B}\n".encode(),
            frozenset({_A, _B}),
        ),
        PredicateCase(
            "blank-runs",
            f"subject\n\n\nbody\n\n\nOnetree-Operation: {_A}\n\n\n".encode(),
            frozenset({_A}),
        ),
        PredicateCase(
            "crlf",
            f"subject\r\n\r\nbody\r\n\r\nOnetree-Operation: {_A}\r\n".encode(),
            frozenset({_A}),
        ),
        PredicateCase(
            "crlf-folded",
            f"subject\r\n\r\nOnetree-Operation: {_A}\r\n\tcontinued\r\n".encode(),
            frozenset({_A, f"{_A} continued", f"{_A}\r continued"}),
            frozenset({_A, f"{_A} continued"}),
        ),
        PredicateCase(
            "non-ascii",
            "subject\n\nOnetree-Operation: café\n".encode(),
            frozenset({"café"}),
        ),
        PredicateCase(
            "unicode-nbsp-preserved",
            b"subject\n\nOnetree-Operation: \xc2\xa0abc\xc2\xa0\n",
            frozenset({"\N{NO-BREAK SPACE}abc\N{NO-BREAK SPACE}"}),
        ),
        PredicateCase(
            "unicode-nbsp-is-a-value",
            b"subject\n\nOnetree-Operation: \xc2\xa0\n",
            frozenset({"\N{NO-BREAK SPACE}"}),
        ),
        PredicateCase(
            "indented-negative",
            f"subject\n\n Onetree-Operation: {_A}\n".encode(),
            frozenset(),
        ),
        PredicateCase(
            "short-key-negative",
            f"subject\n\nOnetree-Operatio: {_A}\n".encode(),
            frozenset(),
        ),
        PredicateCase(
            "separator-inside-token-negative",
            f"subject\n\nOnetree-Operatione: {_A}\n".encode(),
            frozenset(),
        ),
        PredicateCase(
            "suffix-as-reachable-separator",
            f"subject\n\nOnetree-Operations: {_A}\n".encode(),
            frozenset({f": {_A}"}),
        ),
        PredicateCase("empty-negative", b"subject\n\nOnetree-Operation:\n", frozenset()),
    ]
    divider_templates = {
        "divider-first": f"---\nsubject\n\nOnetree-Operation: {_A}\n",
        "divider-before-block": f"subject\n\n---\n\nOnetree-Operation: {_A}\n",
        "divider-inside-block": f"subject\n\n---\nOnetree-Operation: {_A}\n",
        "divider-after-block": f"subject\n\nOnetree-Operation: {_A}\n---\n",
        "divider-final-paragraph": f"subject\n\nOnetree-Operation: {_A}\n\n---\n",
    }
    for name, message in divider_templates.items():
        cases.append(
            PredicateCase(
                name,
                message.encode(),
                frozenset({_A}),
            )
        )
    return tuple(cases)


# This is the vocabulary from the 1,502-shape facts-pass harness. The fixed
# cases above keep every known defect readable; the seeded grammar below makes
# the differential test range beyond those defects without a Hypothesis/runtime
# dependency.
_GENERATED_VOCAB = (
    b"subject",
    b"body text",
    b"",
    b"Onetree-Operation: op-1",
    b"Onetree-Operation: op-2",
    b"Onetree-Operation:op-3",
    b"Onetree-Operation : op-4",
    b"onetree-operation: op-5",
    b"ONETREE-OPERATION: op-6",
    b"Onetree-Operation% op-7",
    b"Onetree-Operation=op-8",
    b"Onetree-Operation op-9",
    b"Onetree-Operations: op-10",
    b" Onetree-Operation: op-11",
    b"\tOnetree-Operation: op-12",
    b"Onetree-Operation:",
    b"Onetree-Operation: caf\xc3\xa9",
    b"Onetree-Operation: \xc2\xa0",
    b"Signed-off-by: A <a@example.invalid>",
    b"Acked-by: B <b@example.invalid>",
    b"(cherry picked from commit c559b185a314a60f7f8e77c4cf9700f604fe8cd7)",
    b"prose",
    b"prose2",
    b"plain text line",
    b"# c1",
    b"# c2",
    b"; c1",
    b"// c1",
    b"Conflicts:",
    b"\tfile.txt",
    b"    file.txt",
    b"# Conflicts:",
    b"#\tfile.txt",
    b"Conflicts: x",
    b"conflicts:",
    b"---",
    b"--- ",
    b"----",
    b"--- 8< ---",
    b"-- -",
    b"# ------------------------ >8 ------------------------",
    b"; ------------------------ >8 ------------------------",
    b"------------------------ >8 ------------------------",
    b"# ---- >8 ----",
    b"  cont",
    b"\tcontinued",
    b"(paren line)",
    b"Mark: 2",
)


def _predicate_model(raw_message: bytes) -> frozenset[str]:
    """Independent executable statement of INV8's byte-level line union."""

    token = b"onetree-operation"
    horizontal = b" \t"
    raw_lines = raw_message.split(b"\n")
    normalized_lines = [
        line[:-1] if line.endswith(b"\r") else line
        for line in raw_lines
    ]
    found: set[str] = set()
    for lines, raw_crlf in ((normalized_lines, False), (raw_lines, True)):
        for index, line in enumerate(lines):
            if line[:1] in (b" ", b"\t") or line[: len(token)].lower() != token:
                continue
            if len(line) <= len(token):
                continue
            positions = [len(token)]
            next_nonblank = len(token)
            while (
                next_nonblank < len(line)
                and line[next_nonblank : next_nonblank + 1] in (b" ", b"\t")
            ):
                next_nonblank += 1
            if next_nonblank < len(line) and next_nonblank != len(token):
                positions.append(next_nonblank)
            continuations = [b""]
            accumulated = b""
            following = index + 1
            while following < len(lines) and lines[following][:1] in (b" ", b"\t"):
                accumulated += b" " + lines[following].strip(horizontal)
                continuations.append(accumulated)
                following += 1
            token_bytes = set(line[: len(token)])
            for position in positions:
                if line[position] in token_bytes:
                    continue
                for continuation in continuations:
                    raw_value = (line[position + 1 :] + continuation).strip(horizontal)
                    if raw_crlf and raw_value.endswith(b"\r"):
                        raw_value = raw_value[:-1]
                    if not raw_value:
                        continue
                    try:
                        found.add(raw_value.decode("utf-8", errors="strict"))
                    except UnicodeDecodeError:
                        found.add(raw_value.decode("utf-8", errors="surrogateescape"))
    return frozenset(found)


def _generated_cases() -> tuple[PredicateCase, ...]:
    """Fill the fixed corpus to 1,502 deterministic, independently scored shapes."""

    anchors = _cases()
    seen = {case.message for case in anchors}
    generated: list[PredicateCase] = []
    generator = random.Random(20260820)  # noqa: S311 - deterministic test grammar
    while len(anchors) + len(generated) < 1502:
        line_count = generator.randint(1, 8)
        lines = [generator.choice(_GENERATED_VOCAB) for _ in range(line_count)]
        if generator.random() < 0.75:
            lines = [b"subject", *([b""] * generator.randint(1, 3)), *lines]
        line_ending = b"\r\n" if generator.random() < 0.2 else b"\n"
        message = line_ending.join(lines)
        if generator.random() < 0.8:
            message += line_ending
        if message in seen:
            continue
        seen.add(message)
        generated.append(
            PredicateCase(
                f"generated-{len(generated):04d}",
                message,
                _predicate_model(message),
                generated=True,
            )
        )
    return tuple(generated)


def _all_cases() -> tuple[PredicateCase, ...]:
    cases = (*_cases(), *_generated_cases())
    assert len(cases) == 1502
    assert len({case.name for case in cases}) == len(cases)
    assert len({case.message for case in cases}) == len(cases)
    return cases


@pytest.mark.parametrize("case", _cases(), ids=lambda case: case.name)
def test_landing_ids_have_an_exact_shape_contract(case: PredicateCase) -> None:
    assert landing_ids(case.message) == case.expected


def test_landing_ids_never_raise_on_non_utf8_values() -> None:
    raw_value = b"\xffoperation"
    expected = raw_value.decode("utf-8", errors="surrogateescape")
    assert landing_ids(b"subject\n\nOnetree-Operation: " + raw_value + b"\n") == frozenset(
        {expected}
    )


def test_generated_landing_shapes_match_the_independent_inv8_model() -> None:
    generated = _generated_cases()
    assert len(generated) == 1502 - len(_cases())
    for case in generated:
        assert landing_ids(case.message) == case.expected, case.name


def test_landing_ids_preserve_every_folded_prefix() -> None:
    operation = "a" * 32
    raw_message = (
        f"subject\n\nOnetree-Operation: {operation}\n"
        "  first\n"
        "\tsecond\n"
        " third\n"
    ).encode()

    assert landing_ids(raw_message) == frozenset(
        {
            operation,
            f"{operation} first",
            f"{operation} first second",
            f"{operation} first second third",
        }
    )


def test_landing_ids_preserve_raw_semantics_when_trimming_exposes_a_cr() -> None:
    operation = "a" * 32
    raw_message = f"Onetree-Operation: {operation}\r ".encode()

    assert landing_ids(raw_message) == frozenset({operation, f"{operation}\r"})
    assert landing_ids(raw_message) == _predicate_model(raw_message)


def test_audit_identity_reduction_preserves_any_match_and_witness_order() -> None:
    raw_message = f"Onetree-Operation: {_B}\nOnetree-Operation: {_A}\n".encode()
    exact = landing_ids(raw_message)
    assert exact == frozenset({_A, _B})

    assert git_module._audit_landing_ids(
        raw_message,
        recorded=set(),
        pending=set(),
    ) == frozenset({_A})
    assert git_module._audit_landing_ids(
        raw_message,
        recorded=set(),
        pending={_A},
    ) == frozenset({_B})
    assert git_module._audit_landing_ids(
        raw_message,
        recorded=set(),
        pending=exact,
    ) == frozenset({_A})
    assert git_module._audit_landing_ids(
        raw_message,
        recorded={_B},
        pending=set(),
    ) == frozenset({_B})


def test_audit_identity_reduction_matches_the_exact_contract_corpus() -> None:
    for case in _all_cases():
        expected = case.expected
        witness = frozenset() if not expected else frozenset({min(expected)})
        assert git_module._audit_landing_ids(
            case.message,
            recorded=set(),
            pending=set(),
        ) == witness, case.name
        if not expected:
            continue
        for recorded in expected:
            assert git_module._audit_landing_ids(
                case.message,
                recorded={recorded},
                pending=set(),
            ) == frozenset({recorded}), (case.name, case.message, expected)
        recorded_witness = git_module._audit_landing_ids(
            case.message,
            recorded=expected,
            pending=set(),
        )
        assert len(recorded_witness) == 1
        assert recorded_witness <= expected
        for pending in expected:
            remaining = expected.difference({pending})
            pending_witness = min(remaining) if remaining else pending
            assert git_module._audit_landing_ids(
                case.message,
                recorded=set(),
                pending={pending},
            ) == frozenset({pending_witness}), case.name

        longest = max(expected, key=len)
        missing_recorded = f"not-recorded-{len(longest):08d}"
        assert missing_recorded not in expected
        assert git_module._audit_landing_ids(
            case.message,
            recorded={missing_recorded},
            pending=set(),
        ) == witness, case.name


def test_audit_identity_reduction_does_not_consume_the_exact_prefix_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation = "a" * 32
    raw_message = (
        f"Onetree-Operation : {operation}\n".encode()
        + b"  cont\r\n" * 1_000
    )
    original_values = git_module._landing_values
    original_decode = git_module._decode_landing_slice
    exact_prefix_characters = 0
    decoded_characters = 0

    def measured_values(message: bytes) -> Iterator[str]:
        nonlocal exact_prefix_characters
        for value in original_values(message):
            exact_prefix_characters += len(value)
            yield value

    def measured_decode(candidate: bytearray, start: int, end: int) -> str:
        nonlocal decoded_characters
        decoded_characters += end - start
        return original_decode(candidate, start, end)

    monkeypatch.setattr(git_module, "_landing_values", measured_values)
    monkeypatch.setattr(git_module, "_decode_landing_slice", measured_decode)

    assert git_module._audit_landing_ids(
        raw_message,
        recorded=set(),
        pending=set(),
    ) == frozenset({f": {operation}"})
    assert exact_prefix_characters == 0
    assert decoded_characters <= len(raw_message) * 4


def _bounded_cli(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    source_root = Path(__file__).resolve().parents[1] / "src"
    environment = os.environ.copy()
    inherited_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        f"{source_root}{os.pathsep}{inherited_path}" if inherited_path else str(source_root)
    )
    child = (
        "import resource\n"
        "import sys\n"
        f"limit = {1536 * 1024 * 1024}\n"
        "resource.setrlimit(resource.RLIMIT_AS, (limit, limit))\n"
        "from onetree.cli import main\n"
        "raise SystemExit(main(sys.argv[1:]))\n"
    )
    return subprocess.run(
        [
            sys.executable,
            "-c",
            child,
            *arguments,
            "--repo",
            str(repository),
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=60,
    )


@pytest.mark.parametrize(
    ("marker", "continuation"),
    [
        pytest.param(b"Onetree-Operation: ", b"  cont\n", id="direct-lf"),
        pytest.param(b"Onetree-Operation: ", b"  cont\r\n", id="direct-crlf"),
        pytest.param(b"Onetree-Operation : ", b"  cont\n", id="spaced-lf"),
        pytest.param(b"Onetree-Operation : ", b"  cont\r\n", id="spaced-crlf"),
    ],
)
def test_folded_prefix_audit_and_adoption_fit_the_bounded_process(
    git_repo: GitFixture,
    marker: bytes,
    continuation: bytes,
) -> None:
    repository = Repository(git_repo.root)
    tree = repository.git_text("show", "-s", "--format=%T", git_repo.base)
    operation = "a" * 32
    raw_message = b"subject\n\n" + marker + operation.encode() + b"\n"
    raw_message += continuation * 16_000
    assert len(raw_message) < 128 * 1024
    commit = repository.git_text(
        "commit-tree",
        tree,
        "-p",
        git_repo.base,
        input_bytes=raw_message,
    )
    assert repository.update_ref("refs/heads/main", commit, git_repo.base)

    inspection = _bounded_cli(git_repo.root, "wal")
    assert inspection.returncode == 1, inspection.stderr
    assert inspection.stdout, inspection.stderr
    inspected = json.loads(inspection.stdout)
    assert inspected["wal"]["consistent"] is False
    assert [item["commit"] for item in inspected["wal"]["divergences"]] == [commit]

    adoption = _bounded_cli(git_repo.root, "wal", "--adopt")
    assert adoption.returncode == 0, adoption.stderr
    adopted = json.loads(adoption.stdout)
    assert adopted["adoption"]["commits"] == [commit]
    assert adopted["adoption"]["consistent"] is True
    expected_operation = operation if marker == b"Onetree-Operation: " else f": {operation}"
    events = Coordinator(git_repo.root).wal.read().events
    assert [
        event.data["operation"]
        for event in events
        if event.kind == "adopted" and event.data.get("commit") == commit
    ] == [expected_operation]


OracleConfig = tuple[tuple[str, str], ...]


def _oracle_environment(*, config: OracleConfig | None) -> dict[str, str]:
    environment = os.environ.copy()
    for name in tuple(environment):
        if name in {
            "GIT_CONFIG_COUNT",
            "GIT_CONFIG_PARAMETERS",
            "GIT_CONFIG_GLOBAL",
            "GIT_CONFIG_SYSTEM",
            "GIT_CONFIG_NOSYSTEM",
        } or name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            environment.pop(name, None)
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    environment["GIT_CONFIG_SYSTEM"] = os.devnull
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    if config is not None:
        pairs = (
            ("i18n.logOutputEncoding", "UTF-8"),
            ("log.showSignature", "false"),
            *config,
        )
        environment["GIT_CONFIG_COUNT"] = str(len(pairs))
        for index, (key, value) in enumerate(pairs):
            environment[f"GIT_CONFIG_KEY_{index}"] = key
            environment[f"GIT_CONFIG_VALUE_{index}"] = value
    return environment


def _git_oracle(
    root: os.PathLike[str], *, config: OracleConfig | None
) -> dict[str, frozenset[str]]:
    format_argument = (
        "--format=%(refname)%00%(trailers:key=Onetree-Operation,valueonly,unfold,"
        f"separator=%x{ord(_FIELD_SEPARATOR):02x})%00"
    )
    completed = subprocess.run(
        [
            _GIT_EXECUTABLE,
            "-C",
            os.fspath(root),
            "for-each-ref",
            "--sort=refname",
            format_argument,
            "refs/predicate/",
        ],
        check=False,
        capture_output=True,
        env=_oracle_environment(config=config),
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    found: dict[str, frozenset[str]] = {}
    remaining = completed.stdout
    while remaining:
        raw_reference, reference_separator, tail = remaining.partition(b"\x00")
        raw_values, record_separator, remaining = tail.partition(b"\x00\n")
        assert reference_separator, tail[:120]
        assert record_separator, tail[:120]
        reference = raw_reference.decode("ascii", errors="strict")
        value_text = raw_values.decode("utf-8", errors="surrogateescape")
        values = frozenset(value for value in value_text.split(_FIELD_SEPARATOR) if value)
        assert reference not in found
        found[reference] = values
    return found


def _oracle_configs(cases: tuple[PredicateCase, ...]) -> tuple[OracleConfig, ...]:
    separators = (":", "%", "%:", "=", "=:", " ", " :", ":s", ":%= \ts")
    configurations: list[OracleConfig] = [
        (("trailer.separators", separator), ("core.commentChar", "#"))
        for separator in separators
    ]
    comment_prefixes: set[str] = set()
    for case in cases:
        for line in case.message.splitlines():
            if not line or any(byte < 32 or byte > 126 for byte in line):
                continue
            text = line.decode("ascii")
            for length in (1, 2, 3, len(text)):
                comment_prefixes.add(text[:length])
    for prefix in sorted(comment_prefixes):
        configurations.append(
            (("trailer.separators", ":"), ("core.commentString", prefix))
        )
        configurations.append(
            (("trailer.separators", ":%= \ts"), ("core.commentString", prefix))
        )
    # The oracle is a union over this matrix, so a matrix that quietly shrank
    # would weaken `git_values <= parsed` without failing it -- fewer configs
    # means fewer values Git ever emits, and a subset assertion only gets
    # easier. The count is pinned here rather than at the call site because it
    # is a property of this derivation alone: nothing below reads Git or the
    # repository, so the value moves only when the corpus or this function does.
    assert len(configurations) == 303
    return tuple(configurations)


def _write_oracle_case(
    git_repo: GitFixture,
    message: bytes,
    *,
    reference: str = "refs/predicate/0000",
) -> None:
    repository = Repository(git_repo.root)
    tree = repository.git_text("show", "-s", "--format=%T", git_repo.base)
    commit = repository.git_text("commit-tree", tree, input_bytes=message)
    assert repository.update_ref(reference, commit, None)


def test_git_oracle_environment_pin_overrides_repository_config(
    git_repo: GitFixture,
) -> None:
    _write_oracle_case(git_repo, _cases()[0].message)
    git_repo.git("config", "trailer.separators", "=")

    assert _git_oracle(git_repo.root, config=None)["refs/predicate/0000"] == frozenset()
    pinned = _git_oracle(
        git_repo.root,
        config=(("trailer.separators", ":"), ("core.commentChar", "#")),
    )
    assert pinned["refs/predicate/0000"] == frozenset({_A})


def test_git_oracle_preserves_unicode_value_bytes(git_repo: GitFixture) -> None:
    case = next(case for case in _cases() if case.name == "unicode-nbsp-is-a-value")
    _write_oracle_case(git_repo, case.message)

    observed = _git_oracle(
        git_repo.root,
        config=(("trailer.separators", ":"), ("core.commentChar", "#")),
    )

    assert observed["refs/predicate/0000"] == case.expected


def test_landing_ids_are_a_declared_superset_of_config_pinned_git(
    git_repo: GitFixture,
) -> None:
    repository = Repository(git_repo.root)
    tree = repository.git_text("show", "-s", "--format=%T", git_repo.base)
    cases = _all_cases()
    # Each commit-tree needs its own message on stdin, so those stay one apiece.
    # The ref creations do not: one `update-ref --stdin -z` transaction replaces
    # 1,502 processes, and it is the stricter setup as well -- the batch either
    # creates every ref or none, where a per-ref loop can leave a partial corpus
    # behind. `update_ref` is the production CAS path, with lock probing and a
    # retry window; none of that is under test here.
    commits = [
        repository.git_text("commit-tree", tree, input_bytes=case.message) for case in cases
    ]
    records = "".join(
        f"create refs/predicate/{index:04d}\0{commit}\0"
        for index, commit in enumerate(commits)
    )
    repository.git("update-ref", "--stdin", "-z", input_bytes=records.encode("ascii"))

    git_repo.git("config", "trailer.separators", "=")
    unpinned = _git_oracle(git_repo.root, config=None)
    assert unpinned["refs/predicate/0000"] == frozenset()
    pinned_control = _git_oracle(
        git_repo.root,
        config=(("trailer.separators", ":"), ("core.commentChar", "#")),
    )
    assert pinned_control["refs/predicate/0000"] == frozenset({_A})

    union: dict[str, set[str]] = {
        f"refs/predicate/{index:04d}": set() for index in range(len(cases))
    }
    for config in _oracle_configs(cases):
        observed = _git_oracle(git_repo.root, config=config)
        assert len(observed) == len(cases)
        for reference, values in observed.items():
            union[reference].update(values)
    for index, case in enumerate(cases):
        parsed = landing_ids(case.message)
        git_values = frozenset(union[f"refs/predicate/{index:04d}"])
        assert parsed == case.expected, case.name
        assert git_values <= parsed, case.name
        if not case.generated:
            # Generated shapes declare no allow-list; their exactness is the
            # equality against the independent model above.
            assert parsed - git_values == case.allowed_extra, case.name


@pytest.mark.parametrize(
    "message",
    [
        f"Onetree-Operation: {_A}\n".encode(),
        f"subject\n\nOnetree-Operation: {_A}\nordinary prose\n".encode(),
        f"subject\n\nOnetree-Operation: {_A}\n\nreviewer notes\n".encode(),
        f"subject\n\nOnetree-Operation={_A}\n".encode(),
        f"subject\n\nOnetree-Operation: {_A}\n\n---\nreview notes\n".encode(),
        f"subject\n\nOnetree-Operation: {_A}\n\n# editor comment\n".encode(),
        f"subject\n\nOnetree-Operation: {_A}\n\nConflicts:\n\tfile.txt\n".encode(),
        (
            f"subject\n\nOnetree-Operation: {_A}\n"
            "# ------------------------ >8 ------------------------\n"
            "ignored editor text\n"
        ).encode(),
        f"subject\n\nOnetree-Operation: {_A}\n\tcontinued\n".encode(),
        f"subject\r\n\r\nOnetree-Operation: {_A}\r\n\tcontinued\r\n".encode(),
        f"subject\n\nOnetree-Operations: {_A}\n".encode(),
        (
            f"squashed\n\nOnetree-Operation: {_A}\n\nsecond\n\n"
            f"Onetree-Operation: {_B}\n\nrelease prose\n"
        ).encode(),
    ],
    ids=(
        "title",
        "prose",
        "later-paragraph",
        "configured-separator",
        "divider-tail",
        "comment-tail",
        "conflicts-tail",
        "scissors-tail",
        "folded-prefix",
        "folded-crlf-prefix",
        "suffix-separator",
        "folded-squash",
    ),
)
def test_every_deliberate_over_detection_has_an_adoption_escape(
    git_repo: GitFixture,
    message: bytes,
) -> None:
    coordinator = Coordinator(git_repo.root)
    tree = coordinator.repo.git_text("show", "-s", "--format=%T", git_repo.base)
    commit = coordinator.repo.git_text(
        "commit-tree", tree, "-p", git_repo.base, input_bytes=message
    )
    assert coordinator.repo.update_ref("refs/heads/main", commit, git_repo.base)

    report = coordinator.inspect_wal()
    assert not report.consistent
    assert [(item.kind, item.commit) for item in report.divergences] == [
        ("unrecorded_landing", commit)
    ]
    adoption = coordinator.adopt_landings()
    assert [event.data["commit"] for event in adoption.adopted] == [commit]
    assert adoption.report.consistent
    assert coordinator.register_session("after-adoption", pid=os.getpid(), ttl=300).actor == (
        "after-adoption"
    )


def test_a_recorded_landing_uses_any_operation_pair(git_repo: GitFixture) -> None:
    coordinator = Coordinator(git_repo.root)
    session = coordinator.register_session("alpha", pid=os.getpid(), ttl=300)
    coordinator.claim("alpha", [str(git_repo.root / "a.txt")])
    git_repo.write("a.txt", "quoted operation\n")
    git_repo.stage(session.index_path, "a.txt")
    quoted = "0" * 32
    result = coordinator.land(
        "alpha",
        message=(
            "The next marker is quoted operator prose.\n\n"
            f"Onetree-Operation: {quoted}\n\n"
            "The real broker marker follows at the end."
        ),
    )
    raw_object = coordinator.repo.git("cat-file", "commit", result.commit).stdout
    raw_message = coordinator.repo._commit_message(raw_object, commit=result.commit)
    assert landing_ids(raw_message) == frozenset({quoted, result.operation})
    assert coordinator.inspect_wal().consistent
