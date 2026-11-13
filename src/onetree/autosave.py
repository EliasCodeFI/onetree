"""Recoverable snapshots of a session's claimed working bytes.

The workspace guard detects erasures that move a ref; path-form checkouts move
none, so they erase a peer's unstaged work undetected.
Autosave does not detect or prevent that. It makes the bytes a session saved
under its own claims recoverable afterwards, and nothing more: bytes written
after the last save are not held anywhere and are reported as such.

Each save is a commit on ``refs/onetree/autosave/<actor>`` whose tree holds the
present files of the saved scope at their working-tree bytes and modes, and
whose message records the scope, the deletions and the base. It writes no WAL
event and touches no index but a temporary one.
"""

from __future__ import annotations

import json
import os
import stat
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from onetree.core import Coordinator, _covers, _overlaps, _validate_actor
from onetree.errors import ClaimConflictError, GitError, IdentityError, OnetreeError
from onetree.git import Repository, TreeEntry

AUTOSAVE_REF_PREFIX = "refs/onetree/autosave/"
_SUBJECT = "onetree autosave"
_FORMAT = 1
_CAS_RETRIES = 8


class AutosaveRefusedError(OnetreeError):
    """An autosave save or restore was refused; nothing was written."""

    kind = "autosave_refused"
    exit_code = 1


@dataclass(frozen=True)
class Snapshot:
    commit: str
    tree: str
    parent: str | None
    actor: str
    base: str | None
    saved_at: float
    scope: tuple[str, ...]
    deleted: tuple[str, ...]

    def payload(self) -> dict[str, Any]:
        return {
            "commit": self.commit,
            "tree": self.tree,
            "parent": self.parent,
            "actor": self.actor,
            "base": self.base,
            "saved_at": self.saved_at,
            "scope": list(self.scope),
            "deleted": list(self.deleted),
        }


@dataclass(frozen=True)
class SaveResult:
    ref: str
    created: bool
    snapshot: Snapshot | None
    files: int

    def payload(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "created": self.created,
            "files": self.files,
            "snapshot": None if self.snapshot is None else self.snapshot.payload(),
        }


@dataclass(frozen=True)
class RestoreResult:
    path: str
    snapshot: str
    saved_at: float
    present: bool
    mode: str | None
    content: bytes | None
    written: bool

    def payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "path": self.path,
            "snapshot": self.snapshot,
            "saved_at": self.saved_at,
            "present": self.present,
            "mode": self.mode,
            "written": self.written,
        }
        if self.content is not None:
            try:
                payload["content_utf8"] = self.content.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                payload["content_hex"] = self.content.hex()
        return payload


def autosave_ref(actor: str) -> str:
    return AUTOSAVE_REF_PREFIX + _validate_actor(actor)


def _literal(path: str) -> str:
    return f":(literal){path}"


def _head(repo: Repository) -> str | None:
    result = repo.git("rev-parse", "--verify", "--quiet", "HEAD^{commit}", check=False)
    if result.returncode != 0:
        return None
    return repo.validate_oid(result.stdout.decode("ascii").strip(), label="HEAD")


def _owned_claims(coordinator: Coordinator, actor: str) -> tuple[str, ...]:
    now = time.time()
    state = coordinator._fold(coordinator.wal.read())
    if not state.session_active(actor, now):
        raise IdentityError("session is not active", session=actor)
    return tuple(claim.path for claim in state.active_claims(now) if claim.actor == actor)


def _candidates(repo: Repository, base: str | None, scope: Sequence[str]) -> list[str]:
    """Tracked-in-base and non-ignored untracked paths under ``scope``."""

    temporary = repo.new_temp_index()
    try:
        if base is not None:
            repo.git("read-tree", base, index=temporary)
        pathspecs = ["."] if "." in scope else [_literal(path) for path in scope]
        listed = repo.git(
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            *pathspecs,
            index=temporary,
        ).stdout
    finally:
        temporary.unlink(missing_ok=True)
    paths = {item.decode("utf-8", errors="surrogateescape") for item in listed.split(b"\0") if item}
    # An explicitly claimed file is saved even when an ignore rule matches it.
    for path in scope:
        if path != "." and ((repo.root / path).is_symlink() or (repo.root / path).is_file()):
            paths.add(path)
    return sorted(paths)


def _hash_paths(repo: Repository, paths: Sequence[str], *, write: bool = True) -> list[str]:
    if not paths:
        return []
    flags = ("-w", "--no-filters") if write else ("--no-filters",)
    # --stdin-paths reads one path per line and C-unquotes a leading double quote.
    if any("\n" in path or path.startswith('"') for path in paths):
        return [
            repo.validate_oid(repo.git_text("hash-object", *flags, "--", path)) for path in paths
        ]
    output = (
        repo.git(
            "hash-object",
            *flags,
            "--stdin-paths",
            input_bytes="".join(f"{path}\n" for path in paths).encode("utf-8", "surrogateescape"),
        )
        .stdout.decode("ascii")
        .split()
    )
    if len(output) != len(paths):
        raise GitError("hash-object returned a different number of objects", paths=len(paths))
    return [repo.validate_oid(oid, label="blob") for oid in output]


def _working_entries(
    repo: Repository, paths: Sequence[str], *, write: bool = True
) -> tuple[dict[str, TreeEntry], list[str]]:
    """Present entries at their working bytes and modes, and the absent paths."""

    regular: list[tuple[str, str]] = []
    entries: dict[str, TreeEntry] = {}
    absent: list[str] = []
    for path in paths:
        try:
            info = os.lstat(repo.root / path)
        except FileNotFoundError:
            absent.append(path)
            continue
        except NotADirectoryError:
            absent.append(path)
            continue
        if stat.S_ISLNK(info.st_mode):
            target = os.readlink(os.fsencode(repo.root / path))
            entries[path] = TreeEntry("120000", _hash_bytes(repo, target, write=write))
        elif stat.S_ISREG(info.st_mode):
            regular.append((path, "100755" if info.st_mode & stat.S_IXUSR else "100644"))
        # A directory (nested repository or submodule checkout) is not a file to save.
    for (path, mode), oid in zip(
        regular, _hash_paths(repo, [path for path, _ in regular], write=write), strict=True
    ):
        entries[path] = TreeEntry(mode, oid)
    return entries, absent


def _hash_bytes(repo: Repository, content: bytes, *, write: bool = True) -> str:
    if write:
        return repo.hash_blob(content)
    return repo.validate_oid(repo.git_text("hash-object", "--stdin", input_bytes=content))


def _write_tree(repo: Repository, entries: dict[str, TreeEntry]) -> str:
    temporary = repo.new_temp_index()
    try:
        rows = b"".join(
            f"{entry.mode} {entry.oid}\t".encode() + path.encode("utf-8", "surrogateescape") + b"\0"
            for path, entry in sorted(entries.items())
        )
        repo.git("update-index", "-z", "--index-info", index=temporary, input_bytes=rows)
        return repo.write_tree(temporary)
    finally:
        temporary.unlink(missing_ok=True)


def _parse(repo: Repository, commit: str, raw: bytes) -> Snapshot:
    header, _, message = raw.partition(b"\n\n")
    tree: str | None = None
    parents: list[str] = []
    for line in header.split(b"\n"):
        if line.startswith(b"tree "):
            tree = line[5:].decode("ascii")
        elif line.startswith(b"parent "):
            parents.append(line[7:].decode("ascii"))
    subject, _, body = message.decode("utf-8", errors="replace").partition("\n\n")
    try:
        meta = json.loads(body)
    except ValueError as error:
        raise GitError("autosave snapshot message is not JSON", commit=commit) from error
    if (
        tree is None
        or len(parents) > 1
        or subject.strip() != _SUBJECT
        or not isinstance(meta, dict)
        or meta.get("v") != _FORMAT
    ):
        raise GitError("autosave ref holds a commit that is not a snapshot", commit=commit)
    base = meta.get("base")
    try:
        return Snapshot(
            commit=commit,
            tree=repo.validate_oid(tree, label="tree"),
            parent=parents[0] if parents else None,
            actor=str(meta["actor"]),
            base=None if base is None else str(base),
            saved_at=float(meta["saved_at"]),
            scope=tuple(str(item) for item in meta["scope"]),
            deleted=tuple(str(item) for item in meta["deleted"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise GitError("autosave snapshot metadata is malformed", commit=commit) from error


def _read_snapshot(repo: Repository, commit: str) -> Snapshot:
    return _parse(repo, commit, repo.git("cat-file", "commit", commit).stdout)


def snapshots(
    coordinator: Coordinator, actor: str, *, limit: int | None = None
) -> tuple[Snapshot, ...]:
    """The actor's snapshots, newest first."""

    repo = coordinator.repo
    tip = repo.resolve_ref(autosave_ref(actor), missing_ok=True)
    if tip is None:
        return ()
    arguments = ["rev-list", "--first-parent"]
    if limit is not None:
        arguments.append(f"--max-count={limit}")
    commits = repo.git_text(*arguments, tip).split()
    found: list[Snapshot] = []
    for oid, object_type, body in repo.cat_file_batch(commits, reference=autosave_ref(actor)):
        if object_type != "commit":
            raise GitError("autosave chain object is missing", commit=oid)
        found.append(_parse(repo, oid, body))
    return tuple(found)


def save(coordinator: Coordinator, actor: str, paths: Sequence[str] = ()) -> SaveResult:
    """Snapshot the working bytes of the actor's claims (or the named claimed paths).

    A no-op when the saved scope, its files and its deletions equal the newest
    snapshot's. Refuses paths no claim of the actor covers.
    """

    actor = _validate_actor(actor)
    repo = coordinator.repo
    reference = autosave_ref(actor)
    owned = _owned_claims(coordinator, actor)
    if paths:
        scope = tuple(sorted({coordinator._canonical_path(p, for_release=True) for p in paths}))
        unclaimed = [path for path in scope if not any(_covers(c, path) for c in owned)]
        if unclaimed:
            raise AutosaveRefusedError(
                "autosave refused; the session does not claim every path",
                reason="unclaimed",
                paths=unclaimed,
            )
    else:
        scope = tuple(sorted(owned))
    if "." in scope:
        scope = (".",)
    if not scope:
        return SaveResult(reference, False, None, 0)

    base = _head(repo)
    entries, absent = _working_entries(repo, _candidates(repo, base, scope))
    tracked = set()
    if base is not None and absent:
        listed = repo.git("ls-tree", "-r", "-z", "--name-only", base, "--", *absent).stdout
        tracked = {item.decode("utf-8", "surrogateescape") for item in listed.split(b"\0") if item}
    deleted = tuple(path for path in absent if path in tracked)
    tree = _write_tree(repo, entries)

    for _ in range(_CAS_RETRIES):
        previous = repo.resolve_ref(reference, missing_ok=True)
        if previous is not None:
            prior = _read_snapshot(repo, previous)
            if prior.tree == tree and prior.scope == scope and prior.deleted == deleted:
                return SaveResult(reference, False, prior, len(entries))
        saved_at = time.time()
        meta = {
            "v": _FORMAT,
            "actor": actor,
            "base": base,
            "saved_at": saved_at,
            "scope": list(scope),
            "deleted": list(deleted),
        }
        message = f"{_SUBJECT}\n\n{json.dumps(meta, sort_keys=True)}\n"
        commit = repo.commit_tree(tree, message, () if previous is None else (previous,))
        if repo.update_ref(reference, commit, previous):
            snapshot = Snapshot(commit, tree, previous, actor, base, saved_at, scope, deleted)
            return SaveResult(reference, True, snapshot, len(entries))
    raise GitError("autosave CAS retry limit exceeded", reference=reference)


def _entry(repo: Repository, treeish: str, path: str) -> TreeEntry | None:
    output = repo.git("ls-tree", "-z", treeish, "--", path).stdout
    for record in output.split(b"\0"):
        metadata, separator, name = record.partition(b"\t")
        if separator and name.decode("utf-8", "surrogateescape") == path:
            mode, object_type, oid = metadata.decode("ascii").split()
            if object_type != "blob":
                raise AutosaveRefusedError(
                    "autosave restores files, not directories", reason="not_a_file", path=path
                )
            return TreeEntry(mode, oid)
    return None


def _current_oid(repo: Repository, path: str) -> str | None:
    target = repo.root / path
    try:
        info = os.lstat(target)
    except (FileNotFoundError, NotADirectoryError):
        return None
    if stat.S_ISLNK(info.st_mode):
        link = os.readlink(os.fsencode(target))
        return repo.git_text("hash-object", "--stdin", input_bytes=link)
    if stat.S_ISREG(info.st_mode):
        return repo.git_text("hash-object", "--no-filters", "--", path)
    raise AutosaveRefusedError(
        "autosave restores files; the working path is a directory", reason="not_a_file", path=path
    )


def _write(repo: Repository, path: str, entry: TreeEntry | None, content: bytes) -> None:
    target = repo.root / path
    if entry is None:
        target.unlink(missing_ok=True)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.onetree-autosave-{os.urandom(4).hex()}")
    try:
        if entry.mode == "120000":
            os.symlink(os.fsdecode(content), temporary)
        else:
            umask = os.umask(0)
            os.umask(umask)
            permissions = (0o777 if entry.mode == "100755" else 0o666) & ~umask
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, permissions)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
            os.chmod(temporary, permissions)
        os.replace(temporary, target)
    finally:
        if temporary.is_symlink() or temporary.exists():
            temporary.unlink()


def restore(
    coordinator: Coordinator,
    actor: str,
    path: str,
    *,
    source: str | None = None,
    snapshot: str | None = None,
    write: bool = True,
    force: bool = False,
) -> RestoreResult:
    """Return, and unless ``write`` is false write back, a path's snapshotted state.

    The snapshot is the newest one in ``source``'s chain (default: the actor's)
    whose scope covers the path, or the named ``snapshot``. Writing requires
    that no other live session claims the path and that the actor claims it,
    and refuses (unless ``force``) to overwrite working bytes that are neither
    the base's nor held by any snapshot in the chain.
    """

    actor = _validate_actor(actor)
    repo = coordinator.repo
    canonical = coordinator._canonical_path(path, for_release=True)
    chain = snapshots(coordinator, source or actor)
    if snapshot is not None:
        chosen = [item for item in chain if item.commit == snapshot]
        if not chosen or not any(_covers(scope, canonical) for scope in chosen[0].scope):
            raise AutosaveRefusedError(
                "the named snapshot does not hold this path",
                reason="not_saved",
                path=canonical,
                snapshot=snapshot,
            )
    else:
        chosen = [item for item in chain if any(_covers(s, canonical) for s in item.scope)][:1]
        if not chosen:
            raise AutosaveRefusedError(
                "no snapshot holds this path; its bytes were never saved",
                reason="not_saved",
                path=canonical,
            )
    picked = chosen[0]
    entry = _entry(repo, picked.tree, canonical)
    content = b"" if entry is None else repo.git("cat-file", "blob", entry.oid).stdout
    result = RestoreResult(
        canonical,
        picked.commit,
        picked.saved_at,
        entry is not None,
        None if entry is None else entry.mode,
        content if entry is not None else None,
        False,
    )
    if not write:
        return result

    now = time.time()
    state = coordinator._fold(coordinator.wal.read())
    if not state.session_active(actor, now):
        raise IdentityError("session is not active", session=actor)
    active = state.active_claims(now)
    holders = sorted({c.actor for c in active if c.actor != actor and _overlaps(c.path, canonical)})
    if holders:
        raise ClaimConflictError(
            f"restore refused; held by {', '.join(holders)}",
            paths=[canonical],
            holders=holders,
        )
    if not any(c.actor == actor and _covers(c.path, canonical) for c in active):
        raise AutosaveRefusedError(
            "restore refused; the session does not claim this path",
            reason="unclaimed",
            paths=[canonical],
        )
    current = _current_oid(repo, canonical)
    wanted = None if entry is None else entry.oid
    if current == wanted and (entry is None or _mode_matches(repo, canonical, entry)):
        return result
    if current is not None and not force:
        base = _head(repo)
        known = {
            item.oid
            for item in ([] if base is None else [_entry(repo, base, canonical)])
            if item is not None
        }
        known.update(
            item.oid
            for item in (_entry(repo, snap.tree, canonical) for snap in chain)
            if item is not None
        )
        if current not in known:
            raise AutosaveRefusedError(
                "restore refused; the working bytes are held by no snapshot and differ "
                "from the base, so writing would erase them (save first, or --force)",
                reason="unsaved_changes",
                path=canonical,
                snapshot=picked.commit,
            )
    _write(repo, canonical, entry, content)
    return RestoreResult(
        result.path,
        result.snapshot,
        result.saved_at,
        result.present,
        result.mode,
        result.content,
        True,
    )


def _mode_matches(repo: Repository, path: str, entry: TreeEntry) -> bool:
    info = os.lstat(repo.root / path)
    if entry.mode == "120000":
        return stat.S_ISLNK(info.st_mode)
    executable = bool(info.st_mode & stat.S_IXUSR)
    return stat.S_ISREG(info.st_mode) and executable == (entry.mode == "100755")


@dataclass(frozen=True)
class PathStatus:
    path: str
    state: str
    snapshot: str | None

    def payload(self) -> dict[str, Any]:
        return {"path": self.path, "state": self.state, "snapshot": self.snapshot}


def status(coordinator: Coordinator, actor: str) -> tuple[PathStatus, ...]:
    """Compare the actor's claimed working bytes with its snapshots and the base.

    ``saved``: the working state equals the newest snapshot holding the path.
    ``reverted_to_base``: it equals the base while that snapshot does not -- the
    shape a path-form erasure leaves, or a deliberate revert; restore applies.
    ``unsaved``: anything else; those bytes are held by no snapshot.
    Writes no objects.
    """

    actor = _validate_actor(actor)
    repo = coordinator.repo
    owned = _owned_claims(coordinator, actor)
    scope = (".",) if "." in owned else tuple(sorted(owned))
    if not scope:
        return ()
    base = _head(repo)
    chain = snapshots(coordinator, actor)
    current, _ = _working_entries(repo, _candidates(repo, base, scope), write=False)
    base_entries = {} if base is None else repo.tree_entries(base)
    trees: dict[str, dict[str, TreeEntry]] = {}

    def held(path: str) -> bool:
        return any(_covers(item, path) for item in scope)

    paths = set(current)
    for snap in chain:
        if snap.tree not in trees:
            trees[snap.tree] = repo.tree_entries(snap.tree)
        paths.update(path for path in trees[snap.tree] if held(path))
        paths.update(path for path in snap.deleted if held(path))
    rows: list[PathStatus] = []
    for path in sorted(paths):
        covering = next((s for s in chain if any(_covers(c, path) for c in s.scope)), None)
        working = current.get(path)
        if covering is None:
            # Never saved: at risk only if it differs from the base.
            if working is not None and working != base_entries.get(path):
                rows.append(PathStatus(path, "unsaved", None))
            continue
        saved = trees[covering.tree].get(path)
        if working == saved:
            state = "saved"
        elif working == base_entries.get(path):
            state = "reverted_to_base"
        else:
            state = "unsaved"
        rows.append(PathStatus(path, state, covering.commit))
    return tuple(rows)
