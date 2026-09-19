"""The three systems of ``docs/PREREGISTRATION.md`` behind one synthetic writer protocol.

Every system runs the same corpus. For each task a writer reads the task's files, waits the
task's seeded think time, appends its token line to each, and tries to get the result onto
the target branch through that system's coordination layer. What the layer is, and therefore
what counts as a refusal, is fixed here per system:

- onetree: the broker. A writer first waits, through the broker's read-only check, until no
  peer holds the task's paths. Each attempt is then a fresh session
  incarnation, because a live session is never rebased and a writer sees its peers' landings
  only by registering again: register, claim the task's paths, write in the shared tree, stage
  in the private index, land, release.
  A claim conflict or a landing refusal is a refusal, and the attempt's bytes are restored
  before it is recorded. The payload gate is the operator's (``--commit-payload``); nothing in
  this package replaces or re-pins it. The referee is the corpus referee this package ships,
  configured in the run's own repository, and it runs inside every landing.
- worktree: git itself. Each writer owns a linked worktree and branch created at setup, which is
  the layer's setup time and disk. An attempt resets the branch to the target, writes, commits,
  and merges into the target checkout under one integration lock, the merge queue an
  incumbent fleet runs. A conflicted merge is aborted and is a refusal.
- naive: nothing. Writers share one checkout and one index and run ``git add`` and
  ``git commit``. Git failing (a held index lock, nothing left to commit) is an error, never a
  refusal: no layer exists to issue a receipt.

The incumbent and the floor land without a referee, so the harness runs the same corpus
referee over each commit they landed, after the writers' window; those verdicts are
``referee`` events.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from onetree.bench import corpus as corpus_module
from onetree.bench.corpus import Corpus, Task
from onetree.bench.events import EventWriter, now
from onetree.core import Coordinator, SessionInfo
from onetree.errors import ClaimConflictError, LandingRefusedError, OnetreeError
from onetree.git import Repository

GIT = shutil.which("git") or "git"
TARGET = "refs/heads/main"
CONTROLLER = "bench-controller"
LANDED = "landed"
RETRY = "retry"
STOP = "stop"
WAIT_DEADLINE = "wait_deadline"
ABANDON_REASONS = {STOP: "nothing_to_commit", WAIT_DEADLINE: "claim_wait_deadline"}
LOCK_RETRIES = 50
_COMMIT_LINE = re.compile(rb"^\[[^\]\s]+(?: \(root-commit\))? ([0-9a-f]{7,64})\]", re.MULTILINE)

REFEREE_SOURCE = '''"""onetree-bench referee: corpus files keep header and token-line shape."""
import pathlib
import re
import sys

HEADER = "onetree-bench corpus file"
TOKEN = re.compile(r"^tok:w[0-9]+:t[0-9]+:[0-9a-f]{12}$")
problems = []
root = pathlib.Path.cwd() / "corpus"
files = sorted(root.glob("*.txt")) if root.is_dir() else []
if not files:
    problems.append("corpus/ is missing or empty")
for path in files:
    lines = path.read_text(encoding="utf-8").split("\\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    if not lines or lines[0] != HEADER:
        problems.append(path.name + ": header")
    for number, line in enumerate(lines[1:], 2):
        if not TOKEN.match(line):
            problems.append(path.name + ":" + str(number) + ": token line")
sys.stderr.write("".join(problem + "\\n" for problem in problems))
raise SystemExit(1 if problems else 0)
'''
"""Standalone on purpose: it runs in the broker's temporary checkout under a narrow environment,
and a stranger re-running the receipts needs only a Python interpreter to execute it."""


class HarnessError(RuntimeError):
    """The harness failed; nothing it produced may be read as a system outcome."""


@dataclass(frozen=True)
class Arm:
    """One system's run configuration; every writer process receives the same value."""

    system: str
    root: Path
    checkout: Path
    gate: Path | None
    message: str
    ttl: float
    max_attempts: int
    backoff_ms: int
    wait_s: float = 600.0
    poll_ms: int = 20

    def writer_checkout(self, writer: int) -> Path:
        if self.system == "worktree":
            return self.root / "wt" / f"w{writer}"
        return self.checkout

    @property
    def lock(self) -> Path:
        return self.root / "integrate.lock"


@dataclass(frozen=True)
class Edit:
    path: str
    write_id: str
    token: str
    before: str
    after: str


def git_environment() -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(
        GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0", LC_ALL="C"
    )
    return environment


def brief(completed: subprocess.CompletedProcess[bytes], limit: int = 300) -> str:
    """Git states the cause first; a tail would keep the advice and drop the cause."""

    text = (completed.stderr + completed.stdout).decode("utf-8", errors="replace").strip()
    return text[:limit]


def git(
    cwd: Path, *arguments: str, check: bool = True, input_bytes: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    completed = subprocess.run(  # noqa: S603 - argv-only git
        [GIT, "-C", str(cwd), *arguments],
        input=input_bytes,
        capture_output=True,
        env=git_environment(),
        check=False,
    )
    if check and completed.returncode != 0:
        raise HarnessError(
            f"git {' '.join(arguments)} exited {completed.returncode}: {brief(completed)}"
        )
    return completed


def git_text(cwd: Path, *arguments: str) -> str:
    return git(cwd, *arguments).stdout.decode("utf-8", errors="strict").strip()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def init_repository(checkout: Path, corpus: Corpus) -> str:
    """A new repository whose target branch holds every corpus file with only its header."""

    checkout.mkdir(parents=True)
    git(checkout, "init", "--quiet", "--initial-branch=main")
    git(checkout, "config", "user.name", "onetree-bench")
    git(checkout, "config", "user.email", "bench@example.invalid")
    git(checkout, "config", "core.logAllRefUpdates", "true")
    for name in corpus.files:
        path = checkout / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(corpus_module.HEADER, encoding="utf-8")
    git(checkout, "add", "--", *corpus.files)
    git(checkout, "commit", "--quiet", "-m", "onetree-bench baseline")
    return git_text(checkout, "rev-parse", "HEAD")


def setup(arm: Arm, writers: int, referee_script: Path, referee_timeout: float) -> dict[str, Any]:
    """The coordination layer's own setup; its duration is the protocol's setup overhead."""

    if arm.system == "onetree":
        coordinator = Coordinator(arm.checkout, commit_payload=arm.gate)
        command = shlex.join([sys.executable, str(referee_script)])
        coordinator.repo.git("config", "onetree.referee", command)
        coordinator.repo.git("config", "onetree.refereeTimeout", str(referee_timeout))
        coordinator.register_session(CONTROLLER, pid=os.getpid(), ttl=arm.ttl, target=TARGET)
        epoch = coordinator.protect_workspace(CONTROLLER)
        return {
            "referee": command,
            "referee_timeout": referee_timeout,
            "protected_tip": epoch["tip"],
        }
    if arm.system == "worktree":
        for writer in range(writers):
            git(
                arm.checkout,
                "worktree",
                "add",
                "--quiet",
                "-b",
                f"w{writer}",
                str(arm.writer_checkout(writer)),
                "main",
            )
        return {"worktrees": writers}
    return {}


def tree_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    seen: set[tuple[int, int]] = set()
    for current, directories, files in os.walk(path):
        for name in (*directories, *files):
            entry_stat = os.lstat(os.path.join(current, name))
            identity = (entry_stat.st_dev, entry_stat.st_ino)
            if identity not in seen:
                seen.add(identity)
                total += entry_stat.st_blocks * 512
    return total


def layer_only_object_bytes(checkout: Path, pattern: str) -> int:
    """On-disk bytes of objects reachable from the layer's refs and not from the target."""

    references = git_text(checkout, "for-each-ref", "--format=%(refname)", pattern).split()
    if not references:
        return 0
    listed = git(
        checkout, "rev-list", "--objects", "--no-object-names", *references, "--not", TARGET
    ).stdout
    if not listed.strip():
        return 0
    sized = git(checkout, "cat-file", "--batch-check=%(objectsize:disk)", input_bytes=listed).stdout
    return sum(int(value) for value in sized.split())


def layer_disk(arm: Arm) -> dict[str, int]:
    """Disk the layer itself owns: its directories plus objects only its refs reach."""

    git_dir = arm.checkout / ".git"
    if arm.system == "onetree":
        return {
            ".git/onetree": tree_bytes(git_dir / "onetree"),
            "objects only on refs/onetree": layer_only_object_bytes(arm.checkout, "refs/onetree"),
        }
    if arm.system == "worktree":
        return {
            "wt": tree_bytes(arm.root / "wt"),
            ".git/worktrees": tree_bytes(git_dir / "worktrees"),
            "objects only on writer branches": layer_only_object_bytes(
                arm.checkout, "refs/heads/w*"
            ),
        }
    return {}


def read_edits(checkout: Path, task: Task, attempt: int) -> list[Edit]:
    edits: list[Edit] = []
    for position, path in enumerate(task.paths):
        before = (checkout / path).read_text(encoding="utf-8")
        edits.append(
            Edit(
                path=path,
                write_id=f"w{task.writer}-t{task.index}-a{attempt}-p{position}",
                token=task.token(path, attempt),
                before=before,
                after=corpus_module.rewrite(before, task, path, attempt),
            )
        )
    return edits


def think(task: Task) -> None:
    if task.think_ms:
        time.sleep(task.think_ms / 1000)


def onetree_actor(task: Task, attempt: int) -> str:
    return f"bench-w{task.writer}-t{task.index}-a{attempt}"


def checkout_label(system: str, writer: int | None) -> str:
    return f"wt/w{writer}" if system == "worktree" and writer is not None else "main"


def index_label(system: str, task: Task, attempt: int) -> str:
    if system == "onetree":
        return onetree_actor(task, attempt)
    return checkout_label(system, task.writer)


def staged_tokens(
    checkout: Path, paths: Sequence[str], index_file: Path | None = None
) -> dict[str, list[str] | None]:
    environment = git_environment()
    if index_file is not None:
        environment["GIT_INDEX_FILE"] = str(index_file)
    base = [GIT, "-C", str(checkout), "--literal-pathspecs"]
    listed = subprocess.run(  # noqa: S603 - argv-only git
        [*base, "ls-files", "--stage", "-z", "--", *paths],
        capture_output=True,
        env=environment,
        check=False,
    )
    if listed.returncode != 0:
        raise HarnessError(f"git ls-files --stage exited {listed.returncode}: {brief(listed)}")
    blobs: dict[str, str] = {}
    for record in listed.stdout.split(b"\0"):
        if record:
            meta, _, name = record.partition(b"\t")
            _, blob, stage = meta.decode("ascii").split(" ")
            path = name.decode("utf-8")
            if stage != "0":
                raise HarnessError(f"{path} is unmerged in the index")
            blobs[path] = blob
    unique = sorted(set(blobs.values()))
    contents: dict[str, bytes] = {}
    if unique:
        batch = subprocess.run(  # noqa: S603 - argv-only git
            [*base, "cat-file", "--batch"],
            input="".join(f"{blob}\n" for blob in unique).encode("ascii"),
            capture_output=True,
            env=environment,
            check=False,
        )
        position = 0
        for blob in unique:
            end = batch.stdout.find(b"\n", position)
            header = batch.stdout[position:end].decode("ascii", errors="replace").split(" ")
            if batch.returncode != 0 or end < 0 or header[:2] != [blob, "blob"]:
                raise HarnessError(f"git cat-file --batch did not return blob {blob}")
            size = int(header[2])
            contents[blob] = batch.stdout[end + 1 : end + 1 + size]
            position = end + 1 + size + 1
    return {
        path: (
            corpus_module.tokens_in(contents[blobs[path]].decode("utf-8", errors="replace"))
            if path in blobs
            else None
        )
        for path in paths
    }


def read_back(
    checkout: Path,
    paths: Sequence[str],
    *,
    file: bool = True,
    index: bool = False,
    index_file: Path | None = None,
) -> dict[str, dict[str, Any]]:
    observed: dict[str, dict[str, Any]] = {path: {} for path in paths}
    for path, entry in observed.items() if file else ():
        try:
            text = (checkout / path).read_text(encoding="utf-8")
        except FileNotFoundError:
            entry["file"] = None
        except (OSError, UnicodeDecodeError):
            entry.update(file=[], unreadable=True)
        else:
            entry["file"] = corpus_module.tokens_in(text)
            if not corpus_module.well_formed(text):
                entry["unreadable"] = True
    if index:
        for path, tokens in staged_tokens(checkout, paths, index_file).items():
            observed[path]["index"] = tokens
    return observed


def observe(
    checkout: Path,
    paths: Sequence[str],
    events: EventWriter,
    after: str,
    task: Task,
    attempt: int,
    *,
    index_file: Path | None = None,
) -> None:
    index = events.system != "onetree" or index_file is not None
    events.emit(
        "observed",
        task=task.index,
        attempt=attempt,
        checkout=checkout_label(events.system, task.writer),
        index=index_label(events.system, task, attempt) if index else None,
        after=after,
        paths=read_back(checkout, paths, index=index, index_file=index_file),
    )


def observe_run_end(
    arm: Arm,
    writers: Sequence[int],
    files: Sequence[str],
    sessions: Sequence[str],
    harness: EventWriter,
    recorded: Iterable[str] = (),
) -> list[str]:
    if arm.system == "worktree":
        for writer in writers:
            label = checkout_label(arm.system, writer)
            harness.emit(
                "observed",
                checkout=label,
                index=label,
                after="run_end",
                paths=read_back(arm.writer_checkout(writer), files, index=True),
            )
    else:
        naive = arm.system == "naive"
        harness.emit(
            "observed",
            checkout=checkout_label(arm.system, None),
            index=checkout_label(arm.system, None) if naive else None,
            after="run_end",
            paths=read_back(arm.checkout, files, index=naive),
        )
    if arm.system == "onetree":
        repository = Repository(arm.checkout)
        for actor in sessions:
            harness.emit(
                "observed",
                checkout=None,
                index=actor,
                after="run_end",
                paths=read_back(
                    arm.checkout,
                    files,
                    file=False,
                    index=True,
                    index_file=repository.index_path(actor),
                ),
            )
    return census(arm.checkout, recorded, harness)


def write_edits(
    checkout: Path, edits: Sequence[Edit], task: Task, attempt: int, events: EventWriter
) -> None:
    for edit in edits:
        (checkout / edit.path).write_text(edit.after, encoding="utf-8")
        events.emit(
            "write",
            task=task.index,
            attempt=attempt,
            write_id=edit.write_id,
            path=edit.path,
            token=edit.token,
            content_sha256=sha256_text(edit.after),
            checkout=checkout_label(events.system, task.writer),
            index=index_label(events.system, task, attempt),
        )
    observe(checkout, [edit.path for edit in edits], events, "write", task, attempt)


def restore(checkout: Path, edits: Sequence[Edit]) -> None:
    for edit in edits:
        (checkout / edit.path).write_text(edit.before, encoding="utf-8")


def write_ids(edits: Sequence[Edit]) -> list[str]:
    return [edit.write_id for edit in edits]


def record_commit(
    checkout: Path,
    commit: str,
    scope: str,
    t: float,
    task: Task,
    attempt: int,
    edits: Sequence[Edit],
    events: EventWriter,
    payload: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    payload = landed_payload(checkout, commit) if payload is None else payload
    events.emit(
        "commit",
        t=t,
        task=task.index,
        attempt=attempt,
        write_ids=write_ids(edits),
        commit=commit,
        scope=scope,
        payload=payload,
    )
    return payload


def object_names(checkout: Path) -> frozenset[str]:
    listed = git(checkout, "cat-file", "--batch-all-objects", "--batch-check=%(objectname)")
    return frozenset(listed.stdout.decode("ascii").split())


def corpus_commits(checkout: Path, names: Iterable[str]) -> list[str]:
    ordered = sorted(names)
    queries = "".join(f"{name}^{{commit}}:corpus\n" for name in ordered).encode("ascii")
    answers = git(checkout, "cat-file", "--batch-check=%(objectname)", input_bytes=queries)
    return [
        name
        for name, answer in zip(ordered, answers.stdout.decode("ascii").splitlines(), strict=True)
        if answer != f"{name}^{{commit}}:corpus missing"
    ]


def token_commits(
    checkout: Path, names: Iterable[str]
) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    for commit in corpus_commits(checkout, names):
        payload = landed_payload(checkout, commit)
        if any(entry["tokens"] for entry in payload):
            yield commit, payload


def record_found(
    checkout: Path,
    before: frozenset[str],
    task: Task,
    attempt: int,
    edits: Sequence[Edit],
    events: EventWriter,
    reported: Iterable[str] = (),
) -> None:
    appeared = object_names(checkout) - before - set(reported)
    found_at = now()
    for commit, payload in token_commits(checkout, appeared):
        record_commit(checkout, commit, "found", found_at, task, attempt, edits, events, payload)


def census(checkout: Path, recorded: Iterable[str], harness: EventWriter) -> list[str]:
    unrecorded = object_names(checkout) - set(recorded)
    seen_at = now()
    emitted: list[str] = []
    for commit, payload in token_commits(checkout, unrecorded):
        harness.emit("commit", t=seen_at, commit=commit, scope="run_end", payload=payload)
        emitted.append(commit)
    return emitted


def refused_candidates(details: dict[str, Any]) -> list[str]:
    named: set[str] = set()
    gate = details.get("gate_receipt")
    report = gate.get("report") if isinstance(gate, dict) else None
    referee_receipt = details.get("referee_receipt")
    for receipt in (report, referee_receipt):
        if isinstance(receipt, dict) and isinstance(receipt.get("candidate"), str):
            named.add(receipt["candidate"])
    return sorted(named)


def landed_payload(checkout: Path, commit: str) -> list[dict[str, Any]]:
    """What the commit actually changed against its first parent, blob by blob."""

    compared = git(checkout, "diff", "--name-only", "-z", f"{commit}^1", commit, check=False)
    if compared.returncode != 0:
        root = len(git_text(checkout, "rev-list", "--no-walk", "--parents", commit).split()) == 1
        empty = git(checkout, "hash-object", "-t", "tree", "--stdin", input_bytes=b"")
        base = empty.stdout.decode("ascii").strip() if root else f"{commit}^1"
        compared = git(checkout, "diff", "--name-only", "-z", base, commit)
    names = compared.stdout.split(b"\0")
    entries: list[dict[str, Any]] = []
    for raw in names:
        if not raw:
            continue
        path = raw.decode("utf-8")
        if git(checkout, "cat-file", "-e", f"{commit}:{path}", check=False).returncode != 0:
            entries.append({"path": path, "blob": None, "content_sha256": None, "tokens": []})
            continue
        blob = git_text(checkout, "rev-parse", f"{commit}:{path}")
        content = git(checkout, "cat-file", "blob", blob).stdout
        entries.append(
            {
                "path": path,
                "blob": blob,
                "content_sha256": hashlib.sha256(content).hexdigest(),
                "tokens": corpus_module.tokens_in(content.decode("utf-8", errors="replace")),
            }
        )
    return entries


def _release(coordinator: Coordinator, actor: str) -> None:
    with contextlib.suppress(OnetreeError):
        coordinator.release(actor)


def onetree_begin(
    arm: Arm, coordinator: Coordinator, task: Task, attempt: int, events: EventWriter
) -> tuple[str, SessionInfo | None]:
    """Register a fresh incarnation and claim the task's paths; a refused claim is recorded."""

    actor = onetree_actor(task, attempt)
    try:
        session = coordinator.register_session(actor, pid=os.getpid(), ttl=arm.ttl, target=TARGET)
        coordinator.claim(actor, [str(arm.checkout / path) for path in task.paths], ttl=arm.ttl)
    except ClaimConflictError as error:
        events.emit(
            "refusal",
            task=task.index,
            attempt=attempt,
            write_ids=[],
            reason=str(error.details.get("reason") or error.kind),
            detail=str(error)[:300],
        )
        _release(coordinator, actor)
        return actor, None
    except OnetreeError as error:
        events.emit(
            "error",
            task=task.index,
            attempt=attempt,
            write_ids=[],
            detail=f"{error.kind}: {error}"[:300],
        )
        _release(coordinator, actor)
        return actor, None
    return actor, session


def onetree_submit(
    arm: Arm,
    coordinator: Coordinator,
    actor: str,
    session: SessionInfo,
    task: Task,
    attempt: int,
    edits: Sequence[Edit],
    events: EventWriter,
) -> str:
    """Stage the written paths privately and land; a refusal restores the bytes first."""

    before = object_names(arm.checkout)
    try:
        coordinator.repo.git("add", "--", *task.paths, index=session.index_path)
        result = coordinator.land(actor, message=arm.message)
    except LandingRefusedError as error:
        refused_at = now()
        named = refused_candidates(error.details)
        for candidate in named:
            record_commit(
                arm.checkout, candidate, "candidate", refused_at, task, attempt, edits, events
            )
        record_found(arm.checkout, before, task, attempt, edits, events, named)
        restore(arm.checkout, edits)
        observe(
            arm.checkout, task.paths, events, "restore", task, attempt,
            index_file=session.index_path,
        )
        events.emit(
            "refusal",
            task=task.index,
            attempt=attempt,
            write_ids=write_ids(edits),
            reason=str(error.details.get("reason") or error.kind),
            detail=str(error)[:300],
        )
        _release(coordinator, actor)
        return RETRY
    except OnetreeError as error:
        record_found(arm.checkout, before, task, attempt, edits, events)
        restore(arm.checkout, edits)
        observe(
            arm.checkout, task.paths, events, "restore", task, attempt,
            index_file=session.index_path,
        )
        events.emit(
            "error",
            task=task.index,
            attempt=attempt,
            write_ids=write_ids(edits),
            detail=f"{error.kind}: {error}"[:300],
        )
        _release(coordinator, actor)
        return RETRY
    landed_at = now()
    payload = record_commit(
        arm.checkout, result.commit, "target", landed_at, task, attempt, edits, events
    )
    receipt = dict(result.referee_receipt or {})
    events.emit(
        "landed",
        task=task.index,
        attempt=attempt,
        write_ids=write_ids(edits),
        commit=result.commit,
        payload=payload,
        green=receipt.get("result") == "pass",
        referee=receipt.get("result"),
    )
    _release(coordinator, actor)
    return LANDED


def onetree_wait(arm: Arm, coordinator: Coordinator, watcher: str, task: Task) -> bool:
    """Poll the read-only check until no peer holds the task's paths, within the wait budget.

    A client that claimed blindly would turn every poll into a refusal, and the refusal count
    would measure the polling rate instead of the coordination layer. A check is a query, not
    a rejection, so waiting through it records nothing; a claim that still loses the race
    after a clear check is a refusal.
    """

    paths = [str(arm.checkout / path) for path in task.paths]
    deadline = time.monotonic() + arm.wait_s
    while any(coordinator.check(watcher, path).blocked for path in paths):
        if time.monotonic() >= deadline:
            return False
        time.sleep(arm.poll_ms / 1000)
    return True


def onetree_attempt(
    arm: Arm,
    coordinator: Coordinator,
    watcher: str,
    task: Task,
    attempt: int,
    events: EventWriter,
) -> str:
    try:
        clear = onetree_wait(arm, coordinator, watcher, task)
    except OnetreeError as error:
        events.emit(
            "error",
            task=task.index,
            attempt=attempt,
            write_ids=[],
            detail=f"{error.kind}: {error}"[:300],
        )
        return RETRY
    if not clear:
        return WAIT_DEADLINE
    actor, session = onetree_begin(arm, coordinator, task, attempt, events)
    if session is None:
        return RETRY
    edits = read_edits(arm.checkout, task, attempt)
    think(task)
    write_edits(arm.checkout, edits, task, attempt, events)
    return onetree_submit(arm, coordinator, actor, session, task, attempt, edits, events)


@contextlib.contextmanager
def integration_lock(path: Path) -> Iterator[None]:
    with path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def worktree_commit(
    arm: Arm, task: Task, attempt: int, edits: Sequence[Edit], events: EventWriter
) -> bool:
    checkout = arm.writer_checkout(task.writer)
    git(checkout, "add", "--", *task.paths)
    before = object_names(checkout)
    committed = git(
        checkout,
        "commit",
        "--quiet",
        "-m",
        f"bench w{task.writer} t{task.index} a{attempt}",
        check=False,
    )
    if committed.returncode != 0:
        record_found(checkout, before, task, attempt, edits, events)
        events.emit(
            "error",
            task=task.index,
            attempt=attempt,
            write_ids=write_ids(edits),
            detail="git commit: " + brief(committed),
        )
        return False
    committed_at = now()
    local = git_text(checkout, "rev-parse", "--verify", "HEAD^{commit}")
    record_commit(checkout, local, "local", committed_at, task, attempt, edits, events)
    return True


def worktree_integrate(
    arm: Arm, task: Task, attempt: int, edits: Sequence[Edit], events: EventWriter
) -> str:
    """Merge the writer's branch into the target checkout under the integration lock."""

    with integration_lock(arm.lock):
        before = object_names(arm.checkout)
        merged = git(
            arm.checkout, "merge", "--no-ff", "--no-edit", "--quiet", f"w{task.writer}", check=False
        )
        if merged.returncode != 0:
            unmerged = git(arm.checkout, "ls-files", "--unmerged").stdout
            git(arm.checkout, "merge", "--abort", check=False)
            record_found(arm.checkout, before, task, attempt, edits, events)
            if unmerged:
                events.emit(
                    "refusal",
                    task=task.index,
                    attempt=attempt,
                    write_ids=write_ids(edits),
                    reason="merge_conflict",
                    detail=brief(merged),
                )
            else:
                events.emit(
                    "error",
                    task=task.index,
                    attempt=attempt,
                    write_ids=write_ids(edits),
                    detail="git merge: " + brief(merged),
                )
            return RETRY
        merged_at = now()
        head = git_text(arm.checkout, "rev-parse", "HEAD")
        payload = record_commit(
            arm.checkout, head, "target", merged_at, task, attempt, edits, events
        )
        events.emit(
            "landed",
            task=task.index,
            attempt=attempt,
            write_ids=write_ids(edits),
            commit=head,
            payload=payload,
            green=None,
        )
    return LANDED


def worktree_attempt(arm: Arm, task: Task, attempt: int, events: EventWriter) -> str:
    checkout = arm.writer_checkout(task.writer)
    git(checkout, "reset", "--quiet", "--hard", "main")
    files = [
        name.decode("utf-8")
        for name in git(checkout, "ls-files", "-z", "--", "corpus").stdout.split(b"\0")
        if name
    ]
    observe(checkout, files, events, "reset", task, attempt)
    edits = read_edits(checkout, task, attempt)
    think(task)
    write_edits(checkout, edits, task, attempt, events)
    if not worktree_commit(arm, task, attempt, edits, events):
        return RETRY
    return worktree_integrate(arm, task, attempt, edits, events)


def _lock_retried(arm: Arm, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    completed = git(arm.checkout, *arguments, check=False)
    for step in range(LOCK_RETRIES):
        if completed.returncode == 0 or b"index.lock" not in completed.stderr:
            break
        time.sleep(arm.backoff_ms * (1 + step % 4) / 1000)
        completed = git(arm.checkout, *arguments, check=False)
    return completed


def naive_submit(
    arm: Arm, task: Task, attempt: int, edits: Sequence[Edit], events: EventWriter
) -> str:
    """Stage and commit through the one shared index, whatever else it holds."""

    added = _lock_retried(arm, "add", "--", *task.paths)
    if added.returncode != 0:
        events.emit(
            "error",
            task=task.index,
            attempt=attempt,
            write_ids=write_ids(edits),
            detail="git add: " + brief(added),
        )
        return RETRY
    observe(arm.checkout, task.paths, events, "add", task, attempt)
    before = object_names(arm.checkout)
    committed = _lock_retried(
        arm,
        "-c",
        "core.abbrev=no",
        "commit",
        "-m",
        f"bench w{task.writer} t{task.index} a{attempt}",
    )
    committed_at = now()
    if committed.returncode != 0:
        output = committed.stdout + committed.stderr
        swept = b"nothing to commit" in output or b"no changes added to commit" in output
        record_found(arm.checkout, before, task, attempt, edits, events)
        events.emit(
            "error",
            task=task.index,
            attempt=attempt,
            write_ids=write_ids(edits),
            detail="git commit: " + brief(committed),
        )
        return STOP if swept else RETRY
    match = _COMMIT_LINE.search(committed.stdout)
    if match is None:
        raise HarnessError(f"git commit printed no commit id: {brief(committed)}")
    commit = git_text(arm.checkout, "rev-parse", "--verify", match.group(1).decode() + "^{commit}")
    payload = record_commit(
        arm.checkout, commit, "target", committed_at, task, attempt, edits, events
    )
    events.emit(
        "landed",
        task=task.index,
        attempt=attempt,
        write_ids=write_ids(edits),
        commit=commit,
        payload=payload,
        green=None,
    )
    return LANDED


def naive_attempt(arm: Arm, task: Task, attempt: int, events: EventWriter) -> str:
    edits = read_edits(arm.checkout, task, attempt)
    think(task)
    write_edits(arm.checkout, edits, task, attempt, events)
    return naive_submit(arm, task, attempt, edits, events)


def run_writer(arm: Arm, tasks: Sequence[Task], events: EventWriter) -> dict[str, int]:
    """One writer's whole intent, attempt by attempt, until it lands or its bound is spent."""

    summary = {"landed": 0, "abandoned": 0, "attempts": 0}
    if not tasks:
        return summary
    coordinator: Coordinator | None = None
    watcher = f"bench-w{tasks[0].writer}-watch"
    if arm.system == "onetree":
        coordinator = Coordinator(arm.checkout, commit_payload=arm.gate)
    for task in tasks:
        outcome = RETRY
        attempt = 0
        while outcome == RETRY and attempt < arm.max_attempts:
            attempt += 1
            summary["attempts"] += 1
            if coordinator is not None:
                outcome = onetree_attempt(arm, coordinator, watcher, task, attempt, events)
            elif arm.system == "worktree":
                outcome = worktree_attempt(arm, task, attempt, events)
            else:
                outcome = naive_attempt(arm, task, attempt, events)
            if outcome == RETRY:
                time.sleep(arm.backoff_ms * min(attempt, 8) / 1000)
        if outcome == LANDED:
            summary["landed"] += 1
        else:
            summary["abandoned"] += 1
            events.emit(
                "abandon",
                task=task.index,
                attempts=attempt,
                reason=ABANDON_REASONS.get(outcome, "attempt_bound"),
            )
    return summary


def referee_commit(
    checkout: Path, commit: str, script: Path, scratch: Path, timeout: float
) -> dict[str, Any]:
    """The corpus referee over one landed commit's tree, outside any coordination layer."""

    listing = git(checkout, "ls-tree", "-r", "-z", "--name-only", commit, "--", "corpus").stdout
    with tempfile.TemporaryDirectory(prefix="referee-", dir=scratch) as raw:
        target = Path(raw)
        for entry in listing.split(b"\0"):
            if not entry:
                continue
            name = entry.decode("utf-8")
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(git(checkout, "cat-file", "blob", f"{commit}:{name}").stdout)
        try:
            completed = subprocess.run(  # noqa: S603 - harness interpreter and shipped script
                [sys.executable, str(script)],
                cwd=target,
                capture_output=True,
                env={"PATH": os.defpath, "LC_ALL": "C"},
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {"result": "timeout", "returncode": None, "detail": ""}
    detail = completed.stderr.decode("utf-8", errors="replace")[-300:]
    return {
        "result": "pass" if completed.returncode == 0 else "red",
        "returncode": completed.returncode,
        "detail": detail,
    }


def final_tokens(checkout: Path, files: Sequence[str]) -> dict[str, list[str]]:
    return {
        name: corpus_module.tokens_in(
            git(checkout, "show", f"{TARGET}:{name}").stdout.decode("utf-8", errors="replace")
        )
        for name in files
    }


__all__ = [
    "CONTROLLER",
    "GIT",
    "LANDED",
    "REFEREE_SOURCE",
    "RETRY",
    "STOP",
    "TARGET",
    "Arm",
    "Edit",
    "HarnessError",
    "final_tokens",
    "git",
    "git_text",
    "init_repository",
    "landed_payload",
    "layer_disk",
    "naive_attempt",
    "naive_submit",
    "onetree_attempt",
    "onetree_begin",
    "onetree_submit",
    "read_edits",
    "referee_commit",
    "restore",
    "run_writer",
    "setup",
    "think",
    "worktree_attempt",
    "worktree_commit",
    "worktree_integrate",
    "write_edits",
]
