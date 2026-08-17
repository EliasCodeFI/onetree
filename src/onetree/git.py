"""Small typed wrappers around the Git plumbing used by Round A."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from onetree.errors import GitError

_GIT_EXECUTABLE = shutil.which("git")


@dataclass(frozen=True, order=True)
class TreeEntry:
    """One recursive tree entry relevant to a payload audit."""

    mode: str
    oid: str

    def payload(self) -> dict[str, str]:
        return {"mode": self.mode, "oid": self.oid}


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

    def resolve_ref(self, reference: str, *, missing_ok: bool = False) -> str | None:
        result = self.git(
            "rev-parse",
            "--verify",
            "--quiet",
            "--end-of-options",
            f"{reference}^{{commit}}",
            check=False,
        )
        if result.returncode == 1 and missing_ok:
            return None
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise GitError(f"cannot resolve ref {reference}: {detail}", reference=reference)
        try:
            oid = result.stdout.decode("ascii", errors="strict").strip().lower()
        except UnicodeDecodeError as error:
            raise GitError("resolved object ID is not ASCII", reference=reference) from error
        return self.validate_oid(oid, label="resolved")

    def head_ref(self) -> str:
        result = self.git("symbolic-ref", "--quiet", "HEAD", check=False)
        if result.returncode != 0:
            raise GitError("HEAD is detached; pass a branch ref", operation="symbolic-ref")
        reference = result.stdout.decode("ascii", errors="strict").strip()
        self.validate_branch_ref(reference)
        return reference

    def validate_branch_ref(self, reference: str) -> str:
        if not reference.startswith("refs/heads/"):
            raise GitError("landing target must be a refs/heads ref", reference=reference)
        result = self.git("check-ref-format", reference, check=False)
        if result.returncode != 0:
            raise GitError("invalid landing target ref", reference=reference)
        return reference

    def index_path(self, actor: str) -> Path:
        return self.git_dir / "onetree" / "sessions" / actor / "index"

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
        temporary = path.with_name(f"index.new-{os.getpid()}-{os.urandom(6).hex()}")
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
        temporary_dir = self.git_dir / "onetree" / "tmp"
        temporary_dir.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix="index-", dir=temporary_dir)
        os.close(descriptor)
        temporary = Path(temporary_name)
        temporary.unlink()
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

    def update_ref(self, reference: str, new: str, old: str | None) -> bool:
        expected = self.zero_oid if old is None else old
        result = self.git("update-ref", "--create-reflog", reference, new, expected, check=False)
        if result.returncode == 0:
            return True
        current = self.resolve_ref(reference, missing_ok=True)
        if current != old:
            return False
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise GitError(f"cannot update ref {reference}: {detail}", reference=reference)

    def update_refs_atomically(
        self,
        updates: Sequence[tuple[str, str, str | None]],
    ) -> bool:
        lines = ["start"]
        for reference, new, old in updates:
            expected = self.zero_oid if old is None else old
            lines.append(f"update {reference} {new} {expected}")
        lines.extend(("prepare", "commit", ""))
        result = self.git(
            "update-ref",
            "--create-reflog",
            "--stdin",
            input_bytes="\n".join(lines).encode("ascii"),
            check=False,
        )
        if result.returncode == 0:
            return True
        moved = any(
            self.resolve_ref(reference, missing_ok=True) != old for reference, _, old in updates
        )
        if moved:
            return False
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise GitError(f"atomic ref transaction failed: {detail}", operation="update-ref")

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        result = self.git("merge-base", "--is-ancestor", ancestor, descendant, check=False)
        if result.returncode in {0, 1}:
            return result.returncode == 0
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise GitError(f"cannot test ancestry: {detail}", operation="merge-base")
