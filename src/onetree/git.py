"""Small typed wrappers around the Git plumbing used by Round A."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from onetree.errors import GitError, RefCorruptError, RefLockedError
from onetree.proc import process_tag

_GIT_EXECUTABLE = shutil.which("git")

#: Operation id carried by every broker-built candidate commit. It is what
#: makes a landing self-identifying in ordinary Git history, so divergence
#: detection can tell a broker commit apart from a permitted hand commit.
LANDING_TRAILER = "Onetree-Operation"
_TEMP_INDEX_PREFIX = "index-"
_TEMP_INDEX_NAME = re.compile(r"index-(?P<pid>\d+)-(?P<start>\d+)-[0-9a-f]{12}\Z")
_LOCK_ATTEMPTS = 4
_LOCK_BACKOFF_SECONDS = 0.02
#: Upper bound on one page of the history walk that looks for unrecorded broker
#: commits. A complete audit may consume more than one page; this bounds each
#: Git result, not the amount of honest history the repository may accumulate.
DIVERGENCE_SCAN_LIMIT = 4096


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
    """A commit on a target ref that carries the broker's landing trailer."""

    commit: str
    operation: str | None


@dataclass(frozen=True)
class TrailerScan:
    """The result of one branch-side trailer walk, and whether it was complete.

    ``truncated`` is the difference between "no unrecorded landing is on this
    branch" and "the walk stopped before it could tell", which is the whole
    value of scanning at all.
    """

    commits: tuple[MarkedCommit, ...]
    truncated: bool
    limit: int


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
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE"):
            environment.pop(name, None)
        environment.setdefault("GIT_AUTHOR_NAME", "onetree session")
        environment.setdefault("GIT_AUTHOR_EMAIL", "onetree@localhost")
        environment.setdefault("GIT_COMMITTER_NAME", "onetree broker")
        environment.setdefault("GIT_COMMITTER_EMAIL", "onetree@localhost")
        environment["GIT_OPTIONAL_LOCKS"] = "0"
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
                [_GIT_EXECUTABLE, "-C", str(cwd), *arguments],
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
        distinction, so it is taken from the active ref backend directly. A
        backend-wide table must be readable before reftable can answer that one
        name is absent.
        """

        if self.ref_format == "reftable":
            existence = self.git("show-ref", "--exists", reference, check=False)
            if existence.returncode == 2:
                return None
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
        """Why reftable cannot decide a ref, or None when its tables are readable."""

        directory = self.git_path("reftable")
        try:
            status = directory.lstat()
        except OSError as error:
            return f"the reftable directory cannot be examined: {error.strerror}"
        if not stat.S_ISDIR(status.st_mode):
            return "the reftable path is not a directory"
        table_list = directory / "tables.list"
        try:
            first = table_list.read_bytes()
        except OSError as error:
            return f"reftable tables.list cannot be read: {error.strerror}"
        try:
            names = first.decode("utf-8", errors="strict").splitlines()
        except UnicodeDecodeError:
            return "reftable tables.list is not UTF-8"
        for name in names:
            candidate = Path(name)
            if not name or candidate.name != name or not name.endswith(".ref"):
                return "reftable tables.list names an invalid table"
            try:
                with (directory / candidate).open("rb") as handle:
                    handle.read(1)
            except OSError as error:
                return f"reftable table {name} cannot be read: {error.strerror}"
        try:
            second = table_list.read_bytes()
        except OSError as error:
            return f"reftable tables.list cannot be re-read: {error.strerror}"
        if second != first:
            return "reftable tables.list changed while it was examined"
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
        unreadable = self.ref_storage_state(reference)
        if unreadable is not None:
            # Storage was observed after both Git probes. A peer may have
            # created or deleted the ref in that window, so classify only after
            # one fresh pair of probes and one fresh storage observation.
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
            unreadable = self.ref_storage_state(reference)
            if unreadable is not None:
                raise RefCorruptError(
                    f"{reference} cannot be read: {unreadable}",
                    reference=reference,
                    oid=None,
                    object_type=None,
                    reason=unreadable,
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

    def existing_commits(self, oids: Iterable[str]) -> tuple[str, ...]:
        """Filter ``oids`` down to the ones this object store holds as commits."""

        wanted = sorted({oid for oid in oids if oid})
        if not wanted:
            return ()
        request = "".join(f"{oid}\n" for oid in wanted).encode("ascii")
        output = self.git("cat-file", "--batch-check", input_bytes=request).stdout
        present: list[str] = []
        for line in output.decode("ascii", errors="replace").splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[1] == "commit":
                present.append(fields[0].lower())
        return tuple(present)

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

    def update_ref(self, reference: str, new: str, old: str | None) -> bool:
        """Compare-and-swap one ref; True on win, False only on a genuine CAS loss.

        A failing ``update-ref`` is three different things: the expected value
        moved (a lost race, retryable by re-auditing), another process holds the
        ref lock (retryable in place), or Git failed for its own reasons (not
        retryable at all). Collapsing the last two into "lost race" turns one bad
        invocation into a retry storm that ends in an unrelated error, so each is
        separated here and only the first returns False.

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
            if self.resolve_ref(reference, missing_ok=True) != old:
                return False
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
        *,
        limit: int | None = None,
        skip: int = 0,
    ) -> TrailerScan:
        """List commits on ``target`` above ``excludes`` and their landing trailer.

        One record more than the cap is requested so a walk that reached the cap
        is distinguishable from one that ran out of history: the caller has to
        be able to say "the audit was cut short" instead of "the branch is
        clean". ``limit`` is read from the module constant at call time so a
        fixture can shrink the window without rebuilding thousands of commits.
        """

        bound = DIVERGENCE_SCAN_LIMIT if limit is None else limit
        arguments = [
            "log",
            f"--max-count={bound + 1}",
            f"--skip={skip}",
            "--format=%H%x1f%(trailers:key=" + LANDING_TRAILER + ",valueonly,separator=%x2C)%x1e",
            target,
            *(f"^{oid}" for oid in excludes),
            "--",
        ]
        output = self.git(*arguments).stdout
        commits: list[MarkedCommit] = []
        for raw_record in output.split(b"\x1e"):
            record = raw_record.strip()
            if not record:
                continue
            try:
                text = record.decode("utf-8", errors="strict")
            except UnicodeDecodeError as error:
                raise GitError("commit listing is not UTF-8", reference=target) from error
            oid, separator, trailer = text.partition("\x1f")
            if not separator:
                raise GitError("malformed commit listing record", reference=target)
            operation = trailer.strip() or None
            commits.append(MarkedCommit(self.validate_oid(oid.strip(), label="target"), operation))
        return TrailerScan(tuple(commits[:bound]), len(commits) > bound, bound)
