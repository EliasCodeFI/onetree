"""Small typed wrappers around the Git plumbing used by Round A."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from onetree.errors import GitError, RefCorruptError, RefLockedError, UnsupportedWorktreeError
from onetree.proc import process_tag

_GIT_EXECUTABLE = shutil.which("git")
_GIT_COMMON_OPTIONS = (
    "-c",
    "advice.graftFileDeprecated=false",
    "-c",
    "core.commitGraph=false",
    "-c",
    "core.multiPackIndex=false",
    "-c",
    "log.showSignature=false",
)

#: Operation id carried by every broker-built candidate commit. It is what
#: makes a landing self-identifying in ordinary Git history, so divergence
#: detection can tell a broker commit apart from a permitted hand commit.
LANDING_TRAILER = "Onetree-Operation"
_LANDING_TRAILER_BYTES = LANDING_TRAILER.lower().encode("ascii")
_LANDING_TRAILER_WHITESPACE = b" \t"
_BROKEN_AUDIT_REF_WARNING = b"warning: ignoring broken ref "
_TEMP_INDEX_PREFIX = "index-"
_TEMP_INDEX_NAME = re.compile(r"index-(?P<pid>\d+)-(?P<start>\d+)-[0-9a-f]{12}\Z")
_LOCK_ATTEMPTS = 4
_LOCK_BACKOFF_SECONDS = 0.02


def _landing_marker_positions(line: bytes) -> tuple[int, ...]:
    """Separator positions one column-zero marker exposes to the parser."""

    if line[:1] in (b" ", b"\t"):
        return ()
    token_length = len(_LANDING_TRAILER_BYTES)
    token = line[:token_length]
    if token.lower() != _LANDING_TRAILER_BYTES or len(line) <= token_length:
        return ()

    positions = [token_length]
    separator = token_length
    while (
        separator < len(line)
        and line[separator : separator + 1] in (b" ", b"\t")
    ):
        separator += 1
    if separator < len(line) and separator != token_length:
        positions.append(separator)

    token_bytes = frozenset(token)
    return tuple(position for position in positions if line[position] not in token_bytes)


def _landing_candidate_slices(
    lines: Sequence[bytes],
    index: int,
    position: int,
    *,
    raw_crlf: bool,
) -> Iterator[tuple[bytearray, int, int]]:
    """Byte-buffer slices one marker-position family exposes."""

    line = lines[index]
    horizontal = frozenset(_LANDING_TRAILER_WHITESPACE)
    candidate = bytearray(line[position + 1 :])
    start = 0
    while start < len(candidate) and candidate[start] in horizontal:
        start += 1
    end = len(candidate)
    while end > start and candidate[end - 1] in horizontal:
        end -= 1
    following = index + 1
    while True:
        value_end = end
        if raw_crlf and value_end > start and candidate[value_end - 1] == 0x0D:
            value_end -= 1
        if value_end > start:
            yield candidate, start, value_end
        if following >= len(lines) or lines[following][:1] not in (b" ", b"\t"):
            break
        continuation = lines[following].strip(_LANDING_TRAILER_WHITESPACE)
        candidate.extend(b" ")
        candidate.extend(continuation)
        if continuation:
            end = len(candidate)
            while start < end and candidate[start] in horizontal:
                start += 1
        following += 1


def _decode_landing_slice(candidate: bytearray, start: int, end: int) -> str:
    """Decode one candidate slice with the exact non-UTF-8 identity policy."""

    view = memoryview(candidate)[start:end]
    try:
        try:
            return str(view, "utf-8", errors="strict")
        except UnicodeDecodeError:
            return str(view, "utf-8", errors="surrogateescape")
    finally:
        view.release()


def _landing_candidate_values(
    lines: Sequence[bytes],
    index: int,
    *,
    raw_crlf: bool,
) -> Iterator[str]:
    """Values one column-zero marker can expose under pinned parsing settings."""

    for position in _landing_marker_positions(lines[index]):
        for candidate, start, end in _landing_candidate_slices(
            lines, index, position, raw_crlf=raw_crlf
        ):
            yield _decode_landing_slice(candidate, start, end)


def _landing_line_views(
    raw_message: bytes,
) -> tuple[tuple[Sequence[bytes], bool], ...]:
    """Normalized and raw line views required by the exact identity union."""

    raw_lines = tuple(raw_message.split(b"\n"))
    if b"\r" in raw_message:
        normalized_lines = tuple(
            line[:-1] if line.endswith(b"\r") else line
            for line in raw_lines
        )
        return ((normalized_lines, False), (raw_lines, True))
    return ((raw_lines, False),)


def _landing_values(raw_message: bytes) -> Iterator[str]:
    """Yield the exact landing-identity union without retaining its prefixes."""

    for lines, raw_crlf in _landing_line_views(raw_message):
        for index in range(len(lines)):
            yield from _landing_candidate_values(lines, index, raw_crlf=raw_crlf)


def landing_ids(raw_message: bytes) -> frozenset[str]:
    """Return every landing identity carried by raw commit-message bytes.

    This is a union over the Git commit-object parser settings that can steer
    literal ``Onetree-Operation`` lines. It deliberately reads the whole
    message: patch-stream dividers, paragraph position, comments, scissors and
    generated-trailer ratios do not gate landing identity. Invalid UTF-8 is
    represented with surrogate escapes so parsing never turns a marker into an
    operational error.
    """

    return frozenset(_landing_values(raw_message))


@dataclass(frozen=True)
class _LandingQueries:
    """Finite identity queries indexed by their round-trippable byte lengths."""

    values: frozenset[str]
    byte_lengths: frozenset[int]
    max_byte_length: int


_EMPTY_LANDING_QUERIES = _LandingQueries(frozenset(), frozenset(), -1)


def _landing_queries(values: Collection[str]) -> _LandingQueries:
    unique = frozenset(values)
    lengths: set[int] = set()
    for value in unique:
        try:
            encoded = value.encode("utf-8", errors="surrogateescape")
        except UnicodeEncodeError:
            continue
        lengths.add(len(encoded))
    byte_lengths = frozenset(lengths)
    return _LandingQueries(
        unique,
        byte_lengths,
        max(byte_lengths, default=-1),
    )


def _reduce_landing_ids(
    raw_message: bytes,
    *,
    recorded: _LandingQueries,
    pending: _LandingQueries,
) -> frozenset[str]:
    """Reduce identities without decoding every cumulative folded prefix."""

    smallest: str | None = None
    smallest_not_pending: str | None = None
    for lines, raw_crlf in _landing_line_views(raw_message):
        for index in range(len(lines)):
            for position in _landing_marker_positions(lines[index]):
                previous: tuple[int, int] | None = None
                family_smallest: str | None = None
                family_not_pending: str | None = None
                for candidate, start, end in _landing_candidate_slices(
                    lines, index, position, raw_crlf=raw_crlf
                ):
                    state = (start, end)
                    if state == previous:
                        continue
                    previous = state
                    length = end - start
                    operation: str | None = None
                    if (
                        family_smallest is None
                        or family_not_pending is None
                        or length in recorded.byte_lengths
                    ):
                        operation = _decode_landing_slice(candidate, start, end)
                    if length in recorded.byte_lengths:
                        if operation is None:  # pragma: no cover - condition decodes
                            raise AssertionError(
                                "recorded-length candidate was not decoded"
                            )
                        if operation in recorded.values:
                            return frozenset({operation})
                    if family_smallest is None:
                        if operation is None:  # pragma: no cover - first candidate decodes
                            raise AssertionError("first candidate was not decoded")
                        family_smallest = operation
                    if family_not_pending is None:
                        if operation is None:  # pragma: no cover - pending candidate decodes
                            raise AssertionError("pending candidate was not decoded")
                        if operation not in pending.values:
                            family_not_pending = operation
                    if (
                        family_not_pending is not None
                        and length >= recorded.max_byte_length
                    ):
                        break
                if family_smallest is not None and (
                    smallest is None or family_smallest < smallest
                ):
                    smallest = family_smallest
                if family_not_pending is not None and (
                    smallest_not_pending is None
                    or family_not_pending < smallest_not_pending
                ):
                    smallest_not_pending = family_not_pending
    witness = smallest_not_pending if smallest_not_pending is not None else smallest
    return frozenset() if witness is None else frozenset({witness})


def _audit_landing_ids(
    raw_message: bytes,
    *,
    recorded: Collection[str],
    pending: Collection[str],
) -> frozenset[str]:
    """Reduce an exact identity stream to the one witness repository audit needs."""

    return _reduce_landing_ids(
        raw_message,
        recorded=_landing_queries(recorded),
        pending=_landing_queries(pending),
    )


@dataclass(frozen=True, order=True)
class TreeEntry:
    """One recursive tree entry relevant to a payload audit."""

    mode: str
    oid: str

    def payload(self) -> dict[str, str]:
        return {"mode": self.mode, "oid": self.oid}


@dataclass(frozen=True)
class TempIndex:
    """One temporary index file below ``.git/onetree/tmp`` and its owner."""

    path: Path
    pid: int | None
    start: int | None

    def payload(self) -> dict[str, object]:
        return {"path": str(self.path), "pid": self.pid, "start": self.start}


@dataclass(frozen=True)
class MarkedCommit:
    """One walked commit and the landing identities its raw message carries."""

    commit: str
    operations: frozenset[str]


@dataclass(frozen=True)
class GitResult:
    """Captured output from one non-raising Git call."""

    returncode: int
    stdout: bytes
    stderr: bytes


class Repository:
    """A discovered work-tree repository and its private plumbing helpers."""

    def __init__(self, path: Path | str) -> None:
        requested = Path(path).expanduser().resolve()
        discovery = self._invoke(
            requested,
            ("rev-parse", "--show-toplevel"),
            check=True,
        )
        try:
            root_text = discovery.stdout.decode("utf-8", errors="strict").strip()
        except UnicodeDecodeError as error:
            raise GitError("repository root is not UTF-8", operation="discover") from error
        self.root = Path(root_text).resolve()
        git_dir_text = self.git_text("rev-parse", "--absolute-git-dir")
        self.git_dir = Path(git_dir_text).resolve()
        common_dir_text = self.git_text("rev-parse", "--git-common-dir")
        common_candidate = Path(common_dir_text)
        self.common_git_dir = (
            common_candidate if common_candidate.is_absolute() else self.root / common_candidate
        ).resolve()
        if self.git_dir != self.common_git_dir:
            raise UnsupportedWorktreeError(
                "linked Git worktrees are unsupported in Gate 1; use the primary working tree",
                git_dir=str(self.git_dir),
                common_git_dir=str(self.common_git_dir),
            )
        object_format = self.git_text("rev-parse", "--show-object-format")
        if object_format not in {"sha1", "sha256"}:
            raise GitError("unsupported Git object format", object_format=object_format)
        self.object_format = object_format
        self.oid_length = 40 if object_format == "sha1" else 64
        self.zero_oid = "0" * self.oid_length
        ref_format = self.git_text("rev-parse", "--show-ref-format")
        if ref_format not in {"files", "reftable"}:
            raise GitError("unsupported Git ref format", ref_format=ref_format)
        self.ref_format = ref_format
        self._git_paths: dict[str, Path] = {}
        self._valid_branch_refs: set[str] = set()

    @staticmethod
    def _environment(index: Path | None = None) -> dict[str, str]:
        environment = os.environ.copy()
        for name in (
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_COMMON_DIR",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_SHALLOW_FILE",
            "GIT_TEST_COMMIT_GRAPH",
            "GIT_TEST_MULTI_PACK_INDEX",
        ):
            environment.pop(name, None)
        environment.setdefault("GIT_AUTHOR_NAME", "onetree session")
        environment.setdefault("GIT_AUTHOR_EMAIL", "onetree@localhost")
        environment.setdefault("GIT_COMMITTER_NAME", "onetree broker")
        environment.setdefault("GIT_COMMITTER_EMAIL", "onetree@localhost")
        environment["GIT_OPTIONAL_LOCKS"] = "0"
        environment["GIT_NO_REPLACE_OBJECTS"] = "1"
        environment["GIT_GRAFT_FILE"] = os.devnull
        # audit_heads classifies one Git 2.53 diagnostic by its exact plumbing
        # wording. Pin every invocation so an ambient locale cannot reframe it.
        environment["LC_ALL"] = "C"
        if index is not None:
            environment["GIT_INDEX_FILE"] = str(index)
        return environment

    @classmethod
    def _invoke(
        cls,
        cwd: Path,
        arguments: Sequence[str],
        *,
        index: Path | None = None,
        input_bytes: bytes | None = None,
        check: bool,
    ) -> GitResult:
        try:
            if _GIT_EXECUTABLE is None:
                raise OSError("git executable was not found on PATH")
            completed = subprocess.run(  # noqa: S603 - argv-only Git plumbing
                [_GIT_EXECUTABLE, *_GIT_COMMON_OPTIONS, "-C", str(cwd), *arguments],
                check=False,
                capture_output=True,
                input=input_bytes,
                env=cls._environment(index),
            )
        except OSError as error:
            raise GitError(f"could not execute git: {error}", operation=arguments[0]) from error
        result = GitResult(completed.returncode, completed.stdout, completed.stderr)
        if check and result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise GitError(
                f"git {arguments[0]} failed with exit {result.returncode}: {detail}",
                operation=arguments[0],
                returncode=result.returncode,
            )
        return result

    def git(
        self,
        *arguments: str,
        index: Path | None = None,
        input_bytes: bytes | None = None,
        check: bool = True,
    ) -> GitResult:
        return self._invoke(
            self.root,
            arguments,
            index=index,
            input_bytes=input_bytes,
            check=check,
        )

    def git_text(
        self,
        *arguments: str,
        index: Path | None = None,
        input_bytes: bytes | None = None,
    ) -> str:
        output = self.git(*arguments, index=index, input_bytes=input_bytes).stdout
        try:
            return output.decode("utf-8", errors="strict").strip()
        except UnicodeDecodeError as error:
            raise GitError(
                f"git {arguments[0]} output is not UTF-8", operation=arguments[0]
            ) from error

    def validate_oid(self, oid: str, *, label: str = "object") -> str:
        if re.fullmatch(rf"[0-9a-f]{{{self.oid_length}}}", oid) is None:
            raise GitError(f"malformed {label} object ID", value=oid)
        return oid

    def _rev_parse(self, revision: str) -> GitResult:
        return self.git(
            "rev-parse",
            "--verify",
            "--quiet",
            "--end-of-options",
            revision,
            check=False,
        )

    def object_type(self, oid: str) -> str | None:
        result = self.git("cat-file", "-t", "--end-of-options", oid, check=False)
        if result.returncode != 0:
            return None
        return result.stdout.decode("ascii", errors="replace").strip() or None

    def git_path(self, name: str) -> Path:
        """Absolute on-disk path Git would use for ``name`` inside this repository."""

        cached = self._git_paths.get(name)
        if cached is None:
            text = self.git_text("rev-parse", "--git-path", name)
            candidate = Path(text)
            cached = candidate if candidate.is_absolute() else self.root / candidate
            self._git_paths[name] = cached
        return cached

    @staticmethod
    def _packed_refs_names(content: bytes) -> set[bytes]:
        names: set[bytes] = set()
        for line in content.splitlines():
            if not line or line.startswith((b"#", b"^")):
                continue
            _, separator, name = line.partition(b" ")
            if separator and name.strip():
                names.add(name.strip())
        return names

    def ref_storage_state(self, reference: str) -> str | None:
        """Why Git could not read ``reference``, or None when it is truly absent.

        Git reports "this ref does not exist" and "this ref exists and I cannot
        read it" through the same exit status with, on some shapes, the same
        empty stderr: a ref file that is ``chmod 000`` and a ref path that is a
        directory both come back indistinguishable from absent (measured on Git
        2.53.0). Matching the message text therefore cannot carry the
        distinction, so it is taken from the active ref backend directly. On
        reftable, only a stable empty ``tables.list`` carries a separate
        decision: nonempty table contents are Git's responsibility, and a
        concurrent replacement is ordinary ref traffic rather than corruption.
        """

        if self.ref_format == "reftable":
            existence = self.git("show-ref", "--exists", reference, check=False)
            if existence.returncode == 2:
                # A zero-byte tables.list produces this status while the ref's
                # bytes sit in orphaned *.ref tables beside it. That stable
                # sentinel is checked below; concurrent list replacement is
                # retried by resolve_ref rather than treated as corruption.
                return self._reftable_storage_state()
            if existence.returncode == 0:
                return "reftable names the ref but Git cannot resolve it"
            storage_error = self._reftable_storage_state()
            if storage_error is not None:
                return storage_error
            detail = existence.stderr.decode("utf-8", errors="replace").strip()
            return f"reftable cannot determine whether the ref exists: {detail or 'unknown error'}"

        loose = self.git_path(reference)
        try:
            status: os.stat_result | None = loose.lstat()
        except FileNotFoundError:
            status = None
        except OSError as error:
            return f"the ref path cannot be examined: {error.strerror}"
        if status is not None:
            if stat.S_ISDIR(status.st_mode):
                return "the ref path is a directory"
            try:
                with loose.open("rb") as handle:
                    handle.read(1)
            except OSError as error:
                return f"the ref file cannot be opened: {error.strerror}"
            return "the ref file is present but Git cannot resolve it"
        packed = self.git_path("packed-refs")
        try:
            content = packed.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as error:
            return f"packed-refs cannot be read: {error.strerror}"
        if reference.encode("utf-8", errors="replace") in self._packed_refs_names(content):
            return "packed-refs names the ref but Git cannot resolve it"
        return None

    def _reftable_storage_state(self) -> str | None:
        """Return the one storage fact reftable absence needs from us.

        Git decides whether a nonempty table set names a ref. The broker only
        adds the stable no-table-named sentinel: Git 2.53.0 writes a list that
        names at least one table at init and replaces it atomically thereafter. Parsing
        names, opening listed tables, or comparing two valid list snapshots
        would turn compaction and ordinary writes into false corruption.
        """

        table_list = self.git_path("reftable") / "tables.list"
        try:
            contents = table_list.read_bytes()
        except OSError as error:
            return f"reftable tables.list cannot be read: {error.strerror}"
        if not contents.split():
            return "reftable tables.list is empty"
        return None

    def resolve_ref(self, reference: str, *, missing_ok: bool = False) -> str | None:
        """Resolve to a commit, distinguishing absent from present-but-corrupt.

        ``rev-parse --verify --quiet <ref>^{commit}`` exits 1 both when the ref
        is absent and when it exists but names a blob, a tree, or an object the
        store does not have. Reading exit 1 as "absent" turns a corrupt ref into
        an empty healthy one, so the corrupt shapes are separated here and
        raised as :class:`RefCorruptError` regardless of ``missing_ok``.
        """

        result = self._rev_parse(f"{reference}^{{commit}}")
        if result.returncode == 0:
            try:
                oid = result.stdout.decode("ascii", errors="strict").strip().lower()
            except UnicodeDecodeError as error:
                raise GitError("resolved object ID is not ASCII", reference=reference) from error
            return self.validate_oid(oid, label="resolved")
        if result.returncode != 1:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise GitError(f"cannot resolve ref {reference}: {detail}", reference=reference)
        probe = self._rev_parse(reference)
        if probe.returncode == 0:
            stored = probe.stdout.decode("ascii", errors="replace").strip().lower()
            raise RefCorruptError(
                f"{reference} exists but does not resolve to a commit",
                reference=reference,
                oid=stored,
                object_type=self.object_type(stored),
            )
        storage_error = self.ref_storage_state(reference)
        if storage_error is not None or self.ref_format == "reftable":
            # Storage was observed after both Git probes. A peer may have
            # created or deleted the ref in that window, so classify only after
            # one fresh pair of probes and one fresh storage observation. The
            # reftable absent arm always takes this retry: a nonempty
            # tables.list can be atomically replaced by a healthy peer between
            # the first probes and this observation.
            retry = self._rev_parse(f"{reference}^{{commit}}")
            if retry.returncode == 0:
                try:
                    oid = retry.stdout.decode("ascii", errors="strict").strip().lower()
                except UnicodeDecodeError as error:
                    raise GitError(
                        "resolved object ID is not ASCII", reference=reference
                    ) from error
                return self.validate_oid(oid, label="resolved")
            if retry.returncode != 1:
                detail = retry.stderr.decode("utf-8", errors="replace").strip()
                raise GitError(f"cannot resolve ref {reference}: {detail}", reference=reference)
            retry_probe = self._rev_parse(reference)
            if retry_probe.returncode == 0:
                stored = retry_probe.stdout.decode("ascii", errors="replace").strip().lower()
                raise RefCorruptError(
                    f"{reference} exists but does not resolve to a commit",
                    reference=reference,
                    oid=stored,
                    object_type=self.object_type(stored),
                )
            storage_error = self.ref_storage_state(reference)
            if storage_error is not None:
                raise RefCorruptError(
                    f"{reference} cannot be read: {storage_error}",
                    reference=reference,
                    oid=None,
                    object_type=None,
                    reason=storage_error,
                )
        if missing_ok:
            return None
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise GitError(f"cannot resolve ref {reference}: {detail}", reference=reference)

    def require_commit(self, reference: str) -> str:
        """Resolve ``reference`` or raise; never returns None."""

        resolved = self.resolve_ref(reference)
        if resolved is None:  # pragma: no cover - missing_ok is not set
            raise AssertionError("resolve_ref returned None without missing_ok")
        return resolved

    def head_ref(self) -> str:
        result = self.git("symbolic-ref", "--quiet", "HEAD", check=False)
        if result.returncode != 0:
            raise GitError("HEAD is detached; pass a branch ref", operation="symbolic-ref")
        reference = result.stdout.decode("ascii", errors="strict").strip()
        self.validate_branch_ref(reference)
        return reference

    def validate_branch_ref(self, reference: str) -> str:
        if reference in self._valid_branch_refs:
            return reference
        if not reference.startswith("refs/heads/"):
            raise GitError("landing target must be a refs/heads ref", reference=reference)
        result = self.git("check-ref-format", reference, check=False)
        if result.returncode != 0:
            raise GitError("invalid landing target ref", reference=reference)
        # Validity is a property of the name alone, and replay validates the
        # same handful of names once per event; the answer is cached so folding
        # a long WAL does not spawn one subprocess per landing record.
        self._valid_branch_refs.add(reference)
        return reference

    def _loose_ref_path_is_exact_storage(self, path: Path, reference: str) -> bool:
        """Whether a loose pathname stores an exact ref rather than a namespace."""

        try:
            status = path.lstat()
        except FileNotFoundError:
            return False
        except OSError as error:
            raise GitError(
                f"cannot inspect loose branch ref: {error.strerror or error}",
                operation="audit-ref-storage",
                reference=reference,
            ) from error
        if stat.S_ISDIR(status.st_mode):
            return False
        if not stat.S_ISLNK(status.st_mode):
            return True
        try:
            target_status = path.stat()
        except FileNotFoundError:
            return False
        except OSError as error:
            raise GitError(
                f"cannot inspect loose branch symlink: {error.strerror or error}",
                operation="audit-ref-storage",
                reference=reference,
            ) from error
        return not stat.S_ISDIR(target_status.st_mode)

    def _file_ref_is_exactly_stored(
        self,
        reference: str,
        packed_names: Collection[bytes],
    ) -> bool:
        """Whether the files backend stores this exact name in loose or packed form."""

        return self._loose_ref_path_is_exact_storage(
            self.git_path(reference), reference
        ) or os.fsencode(reference) in packed_names

    def _loose_branch_alias_is_dangling(
        self,
        path: Path,
        reference: str,
        packed_names: Collection[bytes],
    ) -> bool:
        """Return whether a loose branch alias has no stored terminal target."""

        if os.fsencode(reference) in packed_names:
            # A broken loose alias must not hide an exact packed branch with the
            # same name. Git decides whether that conflicting storage resolves.
            return False
        if not self._loose_ref_path_is_exact_storage(path, reference):
            return True

        symbolic = self.git("symbolic-ref", "--quiet", reference, check=False)
        if symbolic.returncode != 0:
            return False
        raw_target = symbolic.stdout.removesuffix(b"\n")
        if not raw_target or b"\n" in raw_target:
            return False
        target = os.fsdecode(raw_target)
        target_probe = self._rev_parse(target)
        if target_probe.returncode != 1:
            return False
        return not self._file_ref_is_exactly_stored(target, packed_names)

    def _stored_file_branch_refs(self) -> frozenset[str]:
        """Return valid branch names present in the files ref backend.

        Git's ref-filter can omit a dangling symbolic ref without a diagnostic,
        so stderr cannot prove that the listing is complete. This inventory
        reads names only; Git still resolves and validates every emitted value.
        Invalid loose filenames are deliberately excluded by check-ref-format.
        """

        if self.ref_format != "files":
            return frozenset()

        loose_candidates: dict[str, Path] = {}
        loose_root = self.git_path("refs/heads")
        try:
            root_status = loose_root.lstat()
        except FileNotFoundError:
            root_status = None
        except OSError as error:
            raise GitError(
                f"cannot inspect loose branch refs: {error.strerror or error}",
                operation="audit-ref-storage",
            ) from error
        if root_status is not None:
            if not stat.S_ISDIR(root_status.st_mode):
                raise GitError(
                    "cannot inspect loose branch refs: refs/heads is not a directory",
                    operation="audit-ref-storage",
                )
            walk_errors: list[OSError] = []
            directory_ancestry: dict[Path, frozenset[tuple[int, int]]] = {
                loose_root: frozenset({(root_status.st_dev, root_status.st_ino)})
            }
            for directory, directories, filenames in os.walk(
                loose_root,
                topdown=True,
                onerror=walk_errors.append,
                followlinks=True,
            ):
                directory_path = Path(directory)
                ancestors = directory_ancestry.get(directory_path)
                if ancestors is None:
                    raise GitError(
                        "cannot inspect loose branch refs: unbound namespace traversal",
                        operation="audit-ref-storage",
                    )
                leaves = list(filenames)
                for name in tuple(directories):
                    child = directory_path / name
                    try:
                        child_status = child.lstat()
                    except OSError as error:
                        raise GitError(
                            f"cannot inspect loose branch refs: {error.strerror or error}",
                            operation="audit-ref-storage",
                        ) from error
                    if stat.S_ISLNK(child_status.st_mode):
                        try:
                            child_status = child.stat()
                        except FileNotFoundError:
                            directories.remove(name)
                            leaves.append(name)
                            continue
                        except OSError as error:
                            raise GitError(
                                "cannot inspect loose branch namespace symlink: "
                                f"{error.strerror or error}",
                                operation="audit-ref-storage",
                            ) from error
                    if not stat.S_ISDIR(child_status.st_mode):
                        directories.remove(name)
                        leaves.append(name)
                        continue
                    identity = (child_status.st_dev, child_status.st_ino)
                    if identity in ancestors:
                        relative = child.relative_to(loose_root)
                        raise GitError(
                            "cannot inspect loose branch refs: namespace symlink cycle",
                            operation="audit-ref-storage",
                            reference=f"refs/heads/{relative.as_posix()}",
                        )
                    directory_ancestry[child] = ancestors.union({identity})
                for name in leaves:
                    path = directory_path / name
                    relative = path.relative_to(loose_root)
                    loose_candidates[f"refs/heads/{relative.as_posix()}"] = path
            if walk_errors:
                walk_error = walk_errors[0]
                raise GitError(
                    "cannot inspect loose branch refs: "
                    f"{walk_error.strerror or walk_error}",
                    operation="audit-ref-storage",
                )

        packed = self.git_path("packed-refs")
        try:
            packed_content = packed.read_bytes()
        except FileNotFoundError:
            packed_content = b""
        except OSError as error:
            raise GitError(
                f"cannot inspect packed branch refs: {error.strerror or error}",
                operation="audit-ref-storage",
            ) from error
        packed_names = self._packed_refs_names(packed_content)
        packed_candidates = {
            os.fsdecode(raw_reference)
            for raw_reference in packed_names
            if raw_reference.startswith(b"refs/heads/")
        }

        valid: set[str] = set()
        for reference in set(loose_candidates).union(packed_candidates):
            check_name = self.git("check-ref-format", reference, check=False)
            if check_name.returncode == 0:
                loose = loose_candidates.get(reference)
                if (
                    loose is not None
                    and self._loose_branch_alias_is_dangling(
                        loose, reference, packed_names
                    )
                ):
                    continue
                valid.add(reference)
            elif check_name.returncode != 1:
                raise GitError(
                    "cannot validate a stored branch name",
                    operation="check-ref-format",
                    returncode=check_name.returncode,
                )
        return frozenset(valid)

    def audit_heads(self) -> dict[str, str]:
        """Resolve every local branch plus HEAD in one parseable ref listing."""

        stored_before = self._stored_file_branch_refs()
        result = self.git(
            "for-each-ref",
            "--include-root-refs",
            "--sort=refname",
            "--format=%(objectname)%00%(refname)",
            "--",
            "HEAD",
            "refs/heads",
            check=False,
        )
        stored_after = self._stored_file_branch_refs()
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise GitError(
                f"cannot list audit refs: {detail or 'unknown error'}",
                operation="for-each-ref",
                returncode=result.returncode,
            )
        if result.stderr and self.ref_format != "files":
            # Successful Git commands may write unrelated warnings or traces.
            # The files backend gets its completeness decision from the stable
            # storage-name snapshots, not from diagnostic prose. Reftable has no
            # equivalent inventory, so retain only Git's explicit broken-branch
            # diagnostic as a fail-closed signal there.
            for raw_warning in result.stderr.split(b"\n"):
                if not raw_warning.startswith(_BROKEN_AUDIT_REF_WARNING):
                    continue
                raw_reference = raw_warning[len(_BROKEN_AUDIT_REF_WARNING) :]
                try:
                    broken_reference = raw_reference.decode("utf-8", errors="strict")
                except UnicodeDecodeError as error:
                    raise GitError(
                        "cannot list audit refs: broken-ref diagnostic is not UTF-8",
                        operation="for-each-ref",
                        returncode=result.returncode,
                    ) from error
                if broken_reference.startswith("refs/heads/"):
                    raise GitError(
                        "cannot list audit refs: Git ignored a broken branch",
                        operation="for-each-ref",
                        returncode=result.returncode,
                        reference=broken_reference,
                    )
        heads: dict[str, str] = {}
        for raw_line in result.stdout.splitlines():
            raw_oid, separator, raw_reference = raw_line.partition(b"\x00")
            if not separator or not raw_oid or not raw_reference:
                raise GitError("malformed audit ref listing", operation="for-each-ref")
            try:
                reference = raw_reference.decode("utf-8", errors="strict")
                oid = raw_oid.decode("ascii", errors="strict").lower()
            except UnicodeDecodeError as error:
                raise GitError(
                    "audit ref listing is not UTF-8/ASCII", operation="for-each-ref"
                ) from error
            if reference != "HEAD":
                self.validate_branch_ref(reference)
            if reference in heads:
                raise GitError(
                    "audit ref listing repeats a reference",
                    operation="for-each-ref",
                    reference=reference,
                )
            commit = self.validate_oid(oid, label="audit ref")
            object_type = self.object_type(commit)
            if object_type != "commit":
                raise RefCorruptError(
                    f"{reference} does not resolve to a commit",
                    reference=reference,
                    oid=commit,
                    object_type=object_type,
                )
            heads[reference] = commit
        omitted = sorted((stored_before & stored_after).difference(heads))
        if omitted:
            raise GitError(
                "cannot list audit refs: Git omitted a stored local branch",
                operation="for-each-ref",
                returncode=result.returncode,
                references=omitted,
            )
        if self.ref_format == "files" and "HEAD" not in heads:
            symbolic = self.git("symbolic-ref", "--quiet", "HEAD", check=False)
            if symbolic.returncode == 0:
                try:
                    target = symbolic.stdout.decode("utf-8", errors="strict").strip()
                except UnicodeDecodeError as error:
                    raise GitError(
                        "HEAD symbolic ref is not UTF-8", operation="symbolic-ref"
                    ) from error
                self.validate_branch_ref(target)
                if self.resolve_ref(target, missing_ok=True) is not None:
                    raise GitError(
                        "audit ref listing omitted a resolvable HEAD",
                        operation="for-each-ref",
                        reference="HEAD",
                    )
            else:
                # A valid detached HEAD is emitted by --include-root-refs. If it
                # was omitted, distinguish corruption before refusing the list.
                self.require_commit("HEAD")
                raise GitError(
                    "audit ref listing omitted detached HEAD",
                    operation="for-each-ref",
                    reference="HEAD",
                )
        return heads

    def default_branch_ref(self) -> str:
        """Return HEAD's branch, or its unique same-tip branch while detached."""

        symbolic = self.symbolic_head()
        if symbolic is not None:
            return self.validate_branch_ref(symbolic)
        head = self.require_commit("HEAD")
        matches = sorted(
            reference
            for reference, commit in self.audit_heads().items()
            if reference != "HEAD" and commit == head
        )
        if len(matches) == 1:
            return matches[0]
        raise GitError(
            "HEAD is detached; pass a branch ref",
            operation="symbolic-ref",
            matching_branches=matches,
        )

    def index_path(self, actor: str) -> Path:
        return self.git_dir / "onetree" / "sessions" / actor / "index"

    def temp_index_dir(self) -> Path:
        return self.git_dir / "onetree" / "tmp"

    def new_temp_index(self) -> Path:
        """Reserve an owner-tagged temporary index path.

        The owner tag is part of the name because a SIGKILL leaves the file
        behind: a later sweep can only decide whether the residue is abandoned
        if the file itself says which process incarnation created it.
        """

        directory = self.temp_index_dir()
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{_TEMP_INDEX_PREFIX}{process_tag()}-{os.urandom(6).hex()}"

    def temp_indexes(self) -> tuple[TempIndex, ...]:
        """List temporary index residue with the owner encoded in each name."""

        directory = self.temp_index_dir()
        if not directory.is_dir():
            return ()
        residue: list[TempIndex] = []
        for entry in sorted(directory.iterdir()):
            if not entry.name.startswith(_TEMP_INDEX_PREFIX):
                continue
            match = _TEMP_INDEX_NAME.fullmatch(entry.name)
            if match is None:
                residue.append(TempIndex(entry, None, None))
                continue
            start = int(match.group("start"))
            residue.append(TempIndex(entry, int(match.group("pid")), start or None))
        return tuple(residue)

    def ensure_index(self, actor: str, base: str, *, reset: bool = False) -> Path:
        sessions = self.git_dir / "onetree" / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        actor_directory = sessions / actor
        actor_directory.mkdir(exist_ok=True)
        if actor_directory.is_symlink() or actor_directory.resolve() != actor_directory.absolute():
            raise GitError(
                "session index directory is not private Git metadata",
                directory=str(actor_directory),
            )
        path = actor_directory / "index"
        if path.exists() and not reset:
            if not path.is_file() or path.is_symlink() or path.stat().st_nlink != 1:
                raise GitError("session index is not a regular file", index=str(path))
            return path
        temporary = self.new_temp_index()
        try:
            self.git("read-tree", base, index=temporary)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return path

    def write_tree(self, index: Path) -> str:
        return self.validate_oid(self.git_text("write-tree", index=index), label="tree")

    def tree_entries(self, treeish: str) -> dict[str, TreeEntry]:
        output = self.git("ls-tree", "-r", "-z", treeish).stdout
        entries: dict[str, TreeEntry] = {}
        for record in output.split(b"\0"):
            if not record:
                continue
            metadata, separator, path_bytes = record.partition(b"\t")
            if not separator:
                raise GitError("malformed ls-tree record", treeish=treeish)
            try:
                mode, object_type, oid = metadata.decode("ascii", errors="strict").split()
                path = path_bytes.decode("utf-8", errors="strict")
            except (UnicodeDecodeError, ValueError) as error:
                raise GitError("malformed ls-tree output", treeish=treeish) from error
            if object_type not in {"blob", "commit"}:
                raise GitError("unexpected recursive tree object", object_type=object_type)
            if path in entries:
                raise GitError("duplicate path in tree", path=path)
            entries[path] = TreeEntry(mode, self.validate_oid(oid, label="tree entry"))
        return entries

    def overlay_tree(
        self,
        parent: str,
        replacements: Mapping[str, TreeEntry | None],
    ) -> str:
        temporary = self.new_temp_index()
        try:
            self.git("read-tree", parent, index=temporary)
            for path, entry in sorted(replacements.items()):
                if entry is None:
                    self.git("update-index", "--force-remove", "--", path, index=temporary)
                else:
                    self.git(
                        "update-index",
                        "--add",
                        "--cacheinfo",
                        entry.mode,
                        entry.oid,
                        path,
                        index=temporary,
                    )
            return self.write_tree(temporary)
        finally:
            temporary.unlink(missing_ok=True)

    def hash_blob(self, content: bytes) -> str:
        oid = self.git_text("hash-object", "-w", "--stdin", input_bytes=content)
        return self.validate_oid(oid, label="blob")

    def make_single_blob_tree(self, name: str, blob: str) -> str:
        if name != "event.json":
            raise GitError("WAL tree filename must be event.json", name=name)
        row = f"100644 blob {blob}\t{name}\n".encode()
        oid = self.git_text("mktree", input_bytes=row)
        return self.validate_oid(oid, label="tree")

    def commit_tree(self, tree: str, message: str, parents: Sequence[str] = ()) -> str:
        arguments = ["commit-tree", tree]
        for parent in parents:
            arguments.extend(("-p", parent))
        oid = self.git_text(*arguments, input_bytes=(message.rstrip("\n") + "\n").encode())
        return self.validate_oid(oid, label="commit")

    def commit_parents(self, commit: str) -> tuple[str, ...]:
        content = self.git("cat-file", "commit", commit).stdout
        header, separator, _ = content.partition(b"\n\n")
        if not separator:
            raise GitError("commit object has no header terminator", commit=commit)
        parents: list[str] = []
        for line in header.splitlines():
            if not line.startswith(b"parent "):
                continue
            try:
                parent = line.removeprefix(b"parent ").decode("ascii", errors="strict")
            except UnicodeDecodeError as error:
                raise GitError("commit parent is not ASCII", commit=commit) from error
            parents.append(self.validate_oid(parent, label="commit parent"))
        return tuple(parents)

    def ref_lock_path(self, reference: str) -> Path:
        if self.ref_format == "reftable":
            return self.git_path("reftable/tables.list.lock")
        return self.git_path(f"{reference}.lock")

    def symbolic_head(self) -> str | None:
        """The ref HEAD points at, or None when HEAD is detached."""

        result = self.git("symbolic-ref", "--quiet", "HEAD", check=False)
        if result.returncode == 0:
            return result.stdout.decode("utf-8", errors="replace").strip() or None
        if result.returncode == 1:
            return None
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise GitError(f"cannot read HEAD: {detail}", operation="symbolic-ref")

    def is_shallow(self) -> bool:
        """Whether Git's history walk is cut at any non-root shallow boundary."""

        reported = self.git_text("rev-parse", "--is-shallow-repository")
        if reported == "false":
            return False
        if reported != "true":
            raise GitError(
                "git returned an invalid shallow-repository status",
                status=reported,
            )

        marker = self.git_dir / "shallow"
        try:
            content = marker.read_bytes()
        except FileNotFoundError:
            # Git observed the marker first. Its disappearance is a concurrent
            # state change, not evidence that the guarded walk was complete.
            return True
        except OSError as error:
            raise GitError(
                f"cannot read the shallow marker: {error}",
                shallow_file=str(marker),
            ) from error

        for raw_commit in content.splitlines():
            try:
                commit = raw_commit.decode("ascii", errors="strict")
            except UnicodeDecodeError as error:
                raise GitError(
                    "shallow marker contains a non-ASCII object ID",
                    shallow_file=str(marker),
                ) from error
            commit = self.validate_oid(commit, label="shallow boundary")
            if self.object_type(commit) != "commit":
                return True
            if self.commit_parents(commit):
                return True
        return False

    def nonempty_graft_file(self) -> Path | None:
        """Return the legacy graft boundary when it can alter Git's object graph."""

        graft_file = self.git_dir / "info" / "grafts"
        try:
            content = graft_file.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise GitError(
                f"cannot inspect the legacy graft file: {error}",
                graft_file=str(graft_file),
            ) from error
        for line in content.splitlines():
            if line.strip() and not line.startswith(b"#"):
                return graft_file
        return None

    def ref_lock_paths(self, reference: str) -> tuple[Path, ...]:
        """Every lock file an update to ``reference`` can leave behind.

        Updating the branch HEAD points at also writes the HEAD reflog, so Git
        takes ``HEAD.lock`` alongside the ref's own lock. A crash between the two
        can leave either one. Reporting only the ref's own lock sends an operator
        to remove the lock that is not there, and makes the retry that follows
        fail on the one nobody named. HEAD is re-read on every call because a
        checkout moves the association mid-session.
        """

        paths = [self.ref_lock_path(reference)]
        if reference.startswith("refs/heads/") and self.symbolic_head() == reference:
            paths.append(self.ref_lock_path("HEAD"))
        return tuple(dict.fromkeys(paths))

    def _reapplied(self, old: str | None, new: str, observed: str | None) -> bool:
        """Did this invocation's own update land before its exit status was lost?

        ``git update-ref`` commits a single-ref update with one rename and then
        still has work to do before it exits. A death in that window -- an OOM
        kill, a SIGKILL, or merely a lost status after Git completed -- returns
        a non-zero status over a rename that already applied. A peer may append
        ordinary commits before this process re-reads the ref, so the candidate
        need not still be its tip.

        That is not a race any peer can win. A broker candidate carries this
        operation's and this intent's UUIDs in its commit trailer, and a WAL
        append carries a fresh event id, so no other process can produce the OID
        being written here. Observing it on the ref therefore means this
        invocation succeeded and only its report was lost. A no-op CAS is not
        a reapplied write even when its old value remains an ancestor of the
        moved target. Every other value is somebody else's write and remains a
        genuine CAS loss.
        """

        if observed is None or new == old:
            return False
        return observed == new or self.is_ancestor(new, observed)

    def update_ref(self, reference: str, new: str, old: str | None) -> bool:
        """Compare-and-swap one ref; True on win, False only on a genuine CAS loss.

        A failing ``update-ref`` is four different things: the update applied and
        the process died before saying so (a win whose report was lost), the
        expected value moved (a lost race, retryable by re-auditing), another
        process holds the ref lock (retryable in place), or Git failed for its
        own reasons (not retryable at all). Collapsing the last two into "lost
        race" turns one bad invocation into a retry storm that ends in an
        unrelated error, and collapsing the first into it refuses a landing that
        is already on the branch, so each is separated here.

        The lock probe runs after the failed invocation, so a peer that releases
        its lock in between leaves contention looking like a hard Git failure.
        Every failure that did not move the ref is therefore retried within the
        bounded window; only the last observation decides which error names the
        exhausted attempt, and a genuine failure is unaffected by being asked
        again -- a refused update has no effect to repeat.
        """

        expected = self.zero_oid if old is None else old
        candidates = self.ref_lock_paths(reference)
        detail = ""
        held: tuple[Path, ...] = ()
        contended = False
        for attempt in range(_LOCK_ATTEMPTS):
            result = self.git(
                "update-ref", "--create-reflog", reference, new, expected, check=False
            )
            if result.returncode == 0:
                return True
            # Asked in this order on purpose: a no-op CAS (new == old, which is
            # how a lock is probed) must stay on the retry path rather than
            # reporting itself reapplied.
            observed = self.resolve_ref(reference, missing_ok=True)
            if observed != old:
                return self._reapplied(old, new, observed)
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            held = tuple(lock for lock in candidates if lock.exists())
            contended = contended or bool(held)
            if attempt + 1 < _LOCK_ATTEMPTS:
                time.sleep(_LOCK_BACKOFF_SECONDS * (attempt + 1))
        if held:
            raise RefLockedError(
                f"cannot update ref {reference}: it is locked by another process",
                reference=reference,
                lock=str(held[0]),
                locks=[str(lock) for lock in held],
                detail=detail,
            )
        raise GitError(
            f"cannot update ref {reference}: {detail}",
            reference=reference,
            attempts=_LOCK_ATTEMPTS,
            contended=contended,
        )

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        result = self.git("merge-base", "--is-ancestor", ancestor, descendant, check=False)
        if result.returncode in {0, 1}:
            return result.returncode == 0
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise GitError(f"cannot test ancestry: {detail}", operation="merge-base")

    @staticmethod
    def landing_message(body: str, operation: str, intent: str, actor: str) -> str:
        """Append the trailer block that makes a broker landing self-identifying."""

        return (
            f"{body.rstrip(chr(10))}\n\n"
            f"{LANDING_TRAILER}: {operation}\n"
            f"Onetree-Intent: {intent}\n"
            f"Onetree-Actor: {actor}\n"
        )

    def marked_commits(
        self,
        target: str,
        excludes: Sequence[str] = (),
    ) -> Iterator[MarkedCommit]:
        """Return every walked commit with its exact landing-identity set."""

        commits = self._walked_commit_oids(target, excludes)
        return iter(self._marked_commits_from_oids(commits, reference=target))

    def audit_marked_commits(
        self,
        target: str,
        excludes: Sequence[str],
        *,
        recorded: Collection[tuple[str, str]],
        pending: Collection[str],
    ) -> Iterator[MarkedCommit]:
        """Return compact identity witnesses sufficient for repository audit."""

        commits = self._walked_commit_oids(target, excludes)
        return iter(
            self._marked_commits_from_oids(
                commits,
                reference=target,
                recorded=recorded,
                pending=pending,
            )
        )

    def _walked_commit_oids(
        self,
        target: str,
        excludes: Sequence[str],
    ) -> list[str]:
        """Read one complete linear Git walk before exposing any object IDs.

        The former ``--skip`` pagination restarted ``git log`` from ``target``
        for every page, making an otherwise linear audit quadratic in branch
        depth. This retains the object IDs until Git exits so a caller can
        neither observe a partial failed walk nor retain the child process
        descriptors by abandoning an iterator.
        """

        arguments = (
            "log",
            "-z",
            "--encoding=UTF-8",
            "--no-show-signature",
            "--format=%H%x00",
            target,
            *(f"^{oid}" for oid in excludes),
            "--",
        )
        # stderr goes to a file rather than a pipe. Git can therefore fill it
        # without blocking before it closes stdout.
        found: list[str] = []
        with tempfile.TemporaryFile() as stderr_file:
            try:
                if _GIT_EXECUTABLE is None:
                    raise OSError("git executable was not found on PATH")
                process = subprocess.Popen(  # noqa: S603 - argv-only Git plumbing
                    [
                        _GIT_EXECUTABLE,
                        *_GIT_COMMON_OPTIONS,
                        "-C",
                        str(self.root),
                        *arguments,
                    ],
                    stdout=subprocess.PIPE,
                    stderr=stderr_file,
                    env=self._environment(),
                )
            except OSError as error:
                raise GitError("could not execute git: " + str(error), operation="log") from error

            if process.stdout is None:  # pragma: no cover - PIPE is explicit
                process.kill()
                process.wait()
                raise GitError("could not capture git log output", operation="log")

            buffer = b""
            pending_oid: bytes | None = None
            returncode: int | None = None
            malformed_stream = False
            try:
                while True:
                    chunk = process.stdout.read(64 * 1024)
                    if not chunk:
                        break
                    buffer += chunk
                    while b"\x00" in buffer:
                        field, _, buffer = buffer.partition(b"\x00")
                        if pending_oid is None:
                            pending_oid = field
                        else:
                            found.append(self._parse_walked_commit(pending_oid, field, target))
                            pending_oid = None
                malformed_stream = bool(buffer or pending_oid is not None)
                returncode = process.wait()
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                process.stdout.close()
            if returncode != 0:
                stderr_file.seek(0)
                stderr = stderr_file.read(64 * 1024 + 1)
                truncated = len(stderr) > 64 * 1024
                detail = stderr[: 64 * 1024].decode("utf-8", errors="replace").strip()
                if truncated:
                    detail += " [stderr truncated]"
                raise GitError(
                    f"git log failed with exit {returncode}: {detail}",
                    operation="log",
                    returncode=returncode,
                )
            if malformed_stream:
                raise GitError("malformed commit listing stream", reference=target)
        return found

    def _parse_walked_commit(
        self,
        raw_oid: bytes,
        raw_padding: bytes,
        target: str,
    ) -> str:
        try:
            oid = raw_oid.strip().decode("ascii", errors="strict")
        except UnicodeDecodeError as error:
            raise GitError("commit listing object ID is not ASCII", reference=target) from error
        commit = self.validate_oid(oid, label="target")
        if raw_padding:
            raise GitError("malformed commit listing stream", reference=target)
        return commit

    def _marked_commits_from_oids(
        self,
        commits: Sequence[str],
        *,
        reference: str,
        recorded: Collection[tuple[str, str]] | None = None,
        pending: Collection[str] = (),
    ) -> list[MarkedCommit]:
        """Read raw commits through Git's size-framed batch protocol."""

        if not commits:
            return []
        recorded_queries: dict[str, _LandingQueries] | None = None
        pending_queries = _EMPTY_LANDING_QUERIES
        if recorded is not None:
            recorded_values: dict[str, set[str]] = {}
            for recorded_commit, operation in recorded:
                recorded_values.setdefault(recorded_commit, set()).add(operation)
            recorded_queries = {
                recorded_commit: _landing_queries(operations)
                for recorded_commit, operations in recorded_values.items()
            }
            pending_queries = _landing_queries(pending)
        requests = b"".join(commit.encode("ascii") + b"\n" for commit in commits)
        with tempfile.TemporaryFile() as output_file, tempfile.TemporaryFile() as stderr_file:
            try:
                if _GIT_EXECUTABLE is None:
                    raise OSError("git executable was not found on PATH")
                completed = subprocess.run(  # noqa: S603 - argv-only Git plumbing
                    [
                        _GIT_EXECUTABLE,
                        *_GIT_COMMON_OPTIONS,
                        "-C",
                        str(self.root),
                        "cat-file",
                        "--batch",
                    ],
                    check=False,
                    input=requests,
                    stdout=output_file,
                    stderr=stderr_file,
                    env=self._environment(),
                )
            except OSError as error:
                raise GitError(
                    "could not execute git: " + str(error),
                    operation="cat-file",
                ) from error
            if completed.returncode != 0:
                stderr_file.seek(0)
                stderr = stderr_file.read(64 * 1024 + 1)
                truncated = len(stderr) > 64 * 1024
                detail = stderr[: 64 * 1024].decode("utf-8", errors="replace").strip()
                if truncated:
                    detail += " [stderr truncated]"
                raise GitError(
                    f"git cat-file failed with exit {completed.returncode}: {detail}",
                    operation="cat-file",
                    returncode=completed.returncode,
                )

            output_file.seek(0)
            found: list[MarkedCommit] = []
            for commit in commits:
                header = output_file.readline().removesuffix(b"\n")
                fields = header.split(b" ")
                expected_oid = commit.encode("ascii")
                if (
                    len(fields) != 3
                    or fields[0] != expected_oid
                    or fields[1] != b"commit"
                    or not fields[2].isdigit()
                ):
                    raise GitError(
                        "malformed cat-file batch header",
                        reference=reference,
                        commit=commit,
                    )
                size = int(fields[2])
                raw_object = output_file.read(size)
                if len(raw_object) != size or output_file.read(1) != b"\n":
                    raise GitError(
                        "truncated cat-file batch object",
                        reference=reference,
                        commit=commit,
                )
                raw_message = self._commit_message(raw_object, commit=commit)
                operations = (
                    landing_ids(raw_message)
                    if recorded_queries is None
                    else _reduce_landing_ids(
                        raw_message,
                        recorded=recorded_queries.get(
                            commit, _EMPTY_LANDING_QUERIES
                        ),
                        pending=pending_queries,
                    )
                )
                found.append(MarkedCommit(commit, operations))
            if output_file.read(1):
                raise GitError("unexpected cat-file batch output", reference=reference)
        return found

    @staticmethod
    def _commit_message(raw_object: bytes, *, commit: str) -> bytes:
        _, separator, raw_message = raw_object.partition(b"\n\n")
        if not separator:
            raise GitError("commit object has no header terminator", commit=commit)
        return raw_message

    def landing_operations(self, commit: str) -> frozenset[str]:
        """Read every landing identity from exactly one raw commit object."""

        raw_object = self.git("cat-file", "commit", commit).stdout
        raw_message = self._commit_message(raw_object, commit=commit)
        return landing_ids(raw_message)

    def commit_has_landing_operation(self, commit: str, operation: str) -> bool:
        """Test exact identity membership without retaining unrelated prefixes."""

        raw_object = self.git("cat-file", "commit", commit).stdout
        raw_message = self._commit_message(raw_object, commit=commit)
        return _reduce_landing_ids(
            raw_message,
            recorded=_landing_queries({operation}),
            pending=_EMPTY_LANDING_QUERIES,
        ) == frozenset({operation})
