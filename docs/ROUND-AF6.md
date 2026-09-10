# Gate 1 Round AF6: predicates on repository properties

AF6 builds on `978d595` on branch `af5`; it does not merge or move `main`.
Every local result below was produced in `/home/anvil/onetree` after asserting
that `onetree.core.__file__` resolves below `/home/anvil/onetree/src`. The local
interpreter is Python 3.14.4 and Git is 2.53.0. CI's Python 3.11 and 3.13 result
is an external post-push gate and is not claimed by this pre-push record.

## Disposition

| Item | Disposition | Executable evidence |
| --- | --- | --- |
| 1. Missing WAL | Closed. Mutation is refused by typed `unrecorded_landing` exactly when a target branch contains broker-trailered landings absent from the local WAL. `check` and `wal` remain read-only. `wal --adopt` writes local `adopted` events; it neither fetches nor reconciles automatically. | The ordinary-push clone matrix covers default `origin`, `--origin upstream`, a renamed remote, detached HEAD with explicit target, and a non-HEAD target. A first WAL append on another branch does not disable the guard. The crash-left matrix has a configured `origin`, spans 0/1/2/3/5 hand commits on both backends, refuses mutation, and adopts explicitly. |
| 2. Trailer predicate | Closed by deleting `first_marked_commit`. Detection and adoption share `marked_commits`' real-trailer predicate. | Positive broker-landing fixtures plus `test_an_ordinary_message_mention_is_not_a_landing_trailer`; replacing the trailer format with the whole commit message makes the negative control fail. |
| 3. Hot-path walk | Closed by deleting `first_marked_commit`. Read-only `check` no longer walks history. Mutation deliberately pays the complete audit required by item 1. | On the preserved 300,000-commit repository with no WAL: `check` was 0.004/0.004/0.003 s; `register_session` was 2.594 s. AF5 measured 1.70 s for `check` because its separate grep walk was incorrectly on `_read_wal`. |
| 4. False-green guards | Closed. Nonzero log exit, malformed object IDs, and complete chunk-seam parsing each have a direct failing counterfactual. | M3, M10, and M14 each fail the named new fixture. The seam fixture emits more than 64 KiB and compares every parsed commit ID with an independent `git log`. |
| 5. Reftable sentinel | Closed. `not contents.split()` sizes the refusal to “no table is named”; files-backend absence is unchanged. | `tables.list` contents `b""`, `b"\n"`, and `b"\n\n"` all raise `RefCorruptError`; changing the predicate back to `not contents` makes the newline case fail. |
| 6. stderr and descriptors | Closed. stderr is drained through a temporary file, diagnostics are bounded to 64 KiB, and the child and both streams close before an iterator is returned. | A shim writes 200,000 stderr bytes and completes within the 5 s guard. Thirty partially consumed walks retain zero descriptors; the pre-fix measurement was 60 extra descriptors. No real `git log` invocation producing more than 64 KiB on stderr was found by either review seat, so field realizability remains **unknown**. |
| 7. Shallow history | Closed conservatively. Inspection reports the visible result plus `shallow_history`; every mutation and adoption refuses until the repository is deepened. `_scan_floor` now promises only complete Git-visible history. | Depth 4 exposes and reports the landing four commits below the tip; depth 3 does not expose it. Both depths report the shallow boundary and refuse registration. |
| 8. Record accuracy | Closed here and corrected below. | AF5's red-first selection was 9 failed, 2 passed, **76** deselected, not 74. AF5 coverage fell from 92.26% to 91.78% (0.48 percentage points); that movement was not disclosed there. |

## Semantics and boundaries

The deciding predicate has one carrier: `_unrecorded_landings(state, targets)`
compares the operations from `marked_commits` with the WAL's pending, landed,
adopted, and refused operation sets. It does not inspect remote names or
existence, HEAD position, WAL emptiness, or commit-message substrings. The same
predicate supplies both mutation refusal and read-only divergence reporting.
This deliberately does not distinguish a crash before the first WAL write from
a copy that never received a WAL; the local states are identical.

Adoption requires a branch ref. On a detached checkout the operator supplies
`--target refs/heads/<name>`. Each adopted event records the operation, target,
first parent, commit, and tree measured from the local objects. Folding validates
the shape and object IDs; inspection re-verifies containment, tree identity,
and the commit's real `Onetree-Operation` trailer. Ordinary `git push` publishes
only the branch in the clone fixtures, and the bare remote is asserted to contain
no `refs/onetree/wal`.

Shallow history is a visibility boundary, not evidence of absence. AF6 does not
claim to audit objects the repository does not have. Read-only commands report
that limitation; mutating commands fail closed with typed `shallow_history`.

`marked_commits` still makes one linear Git walk and parses stdout in 64 KiB
chunks. It retains parsed results until the child exits so a failed walk cannot
yield a partial healthy audit and an abandoned iterator cannot retain process
descriptors. stderr is never a pipe. A corrupt loose object in the middle of
history makes real Git exit nonzero and `inspect_wal` raise `GitError`.

## Red-first and mutation evidence

Before the product changes, the initial AF6 selection against `978d595` was
**23 failed, 3 passed, 74 deselected**. It covered the clone shapes, first-WAL
append, origin-bearing crash-left fixtures, shallow boundaries, whitespace-only
reftable state, and the 200 KiB stderr shim. The descriptor fixture was then
observed red separately at **72 open descriptors versus a 12-descriptor
baseline** before the eager cleanup change.

The required single mutations were measured against the AF6 candidate and then
restored:

| Mutation | Observed result |
| --- | --- |
| Delete the shared unrecorded-landing refusal | plain ordinary-push clone fixture: 1 failed (`DID NOT RAISE`) |
| M3: ignore nonzero `git log` exit | corrupt-mid-history fixture: 1 failed (`DID NOT RAISE GitError`) |
| M9: use the whole commit message | ordinary-message negative control: 1 failed with `UnrecordedLandingError` |
| M10: remove object-ID validation | malformed-record fixture: 1 failed (`DID NOT RAISE GitError`) |
| M14: discard the record straddling each 64 KiB seam | large-log fixture: 1 failed; two commit IDs were lost |
| Restore the one-byte reftable sentinel | newline-only fixture: 1 failed (`DID NOT RAISE RefCorruptError`) |
| AF5 M-R: bypass `_reapplied` for the WAL ref | 1 failed; two intent operation IDs were recorded |
| Disable linked-worktree refusal | linked-worktree fixture: 1 failed; the allowed-shape control passed |

The inherited AF4 battery remains discriminating: M-1 failed the healthy birth
tick, M-4 failed four warning-crosscheck cases, M-4b failed three of four cases,
M-4c failed the two accepted cases, and M-7 failed the human warning suffix.
Restoring the retired scan floor (M-3b) produced **14 failed, 2 passed, 92
deselected** across the crash-left and page-boundary selection.

No test function was removed: AF5 contains 97 test functions and AF6 contains
106. The expanded parametrization and new guards collect 202 tests, versus
AF5's 179.

## Current-tree measurements

The preserved fast-import repositories contain one honest branch of the named
depth. The post-cleanup cold measurements are:

| History depth | `inspect_wal()` |
| ---: | ---: |
| 1,000 | 0.043 s |
| 20,000 | 0.199 s |
| 100,000 | 0.835 s |
| 300,000 | 2.686 s |

This is the required linear curve over the measured range, not a claim beyond
it. `_reapplied` containment passed at depths 0/1/2/3/5/10/40 on files and
reftable. The absent-WAL race produced zero spurious `RefCorruptError`s in
2,500 attempts on each backend. All seven preserved reftable damage shapes
raised `WalCorruptError`. The worktree control constructed a primary checkout,
`--separate-git-dir`, symlinked `.git`, a repository reached through a symlink,
and a submodule; only the linked worktree was refused.

## Gates

| Gate | Measured result |
| --- | --- |
| Module path | `/home/anvil/onetree/src/onetree/core.py` |
| `ruff check src tests` | `All checks passed!` |
| `mypy --strict` | `Success: no issues found in 7 source files` |
| `pytest` | **202 passed in 99.32 s**; total branch-aware coverage **91.65%** (floor 90). |
| CI Python 3.11 and 3.13 | Must be observed on PR #1 at the exact pushed head; not assumed locally. |

The final local gate receipt is `/tmp/af6-final-gates.jsonl`; it stores argv,
return code, duration, output hash, and a chained row hash. Coverage is 0.13
percentage points below AF5's 91.78%; that movement is disclosed rather than
hidden.
