"""Differential and end-to-end contract for broker landing identity."""

from __future__ import annotations

import os
import random
import shutil
import subprocess
from dataclasses import dataclass

import pytest
from conftest import GitFixture

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
    for index, case in enumerate(cases):
        commit = repository.git_text("commit-tree", tree, input_bytes=case.message)
        assert repository.update_ref(f"refs/predicate/{index:04d}", commit, None)

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
    configurations = _oracle_configs(cases)
    assert len(configurations) == 303
    for config in configurations:
        observed = _git_oracle(git_repo.root, config=config)
        assert len(observed) == len(cases)
        for reference, values in observed.items():
            union[reference].update(values)
    over_cases = 0
    over_values = 0
    for index, case in enumerate(cases):
        parsed = landing_ids(case.message)
        git_values = frozenset(union[f"refs/predicate/{index:04d}"])
        assert parsed == case.expected, case.name
        assert git_values <= parsed, case.name
        over_cases += bool(parsed - git_values)
        over_values += len(parsed - git_values)
        if case.generated:
            # The independent model is the exact declaration for generated
            # shapes: subset-only would let a parser broaden unnoticed.
            assert parsed - git_values == case.expected - git_values, case.name
        else:
            assert parsed - git_values == case.allowed_extra, case.name
    assert (over_cases, over_values) == (595, 1063)


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
