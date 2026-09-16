# Gate 1 Round AF5: predicates sized to the defect, not to the fixture

AF5 builds on `dc924a6` (`origin/af4`) on branch `af5`; it does not merge AF4
or alter `main`. The executable measurements in this record were made with
`PYTHONPATH` and `MYPYPATH` pinned to `/home/anvil/onetree/src`, after asserting
that `onetree.core.__file__` and `onetree.git.__file__` resolve there. The local
interpreter is Python 3.14.4. CI's Python 3.11 and 3.13 matrix is deliberately
listed as unobserved until the pull request runs it.

## Disposition

| Item | Disposition | Executable guard or measurement |
| --- | --- | --- |
| F1 | Closed. A failed ref CAS is reapplied when the observed tip is the candidate **or contains it**. A no-op CAS remains a genuine retry. | `test_a_status_lost_branch_cas_stays_a_win_above_any_hand_commits` spans 0, 1, and 3 hand commits on files and reftable. |
| F2 / F3 | Closed by collapse. Reftable absence now carries only the stable, zero-byte `tables.list` sentinel; valid-list changes and listed-table compaction are not corruption. | Independent replacement and table-open fixtures; 2,000 concurrent absent-ref reads on each backend, zero `RefCorruptError`s. |
| B-1 | Closed by quarantine and repair. A default clone that has a configured `origin`, broker history, and no WAL reports `wal_history_missing` and cannot append. | `test_a_plain_clone_missing_the_wal_is_quarantined_before_it_can_append` drives `onetree wal --repair`, then registration and recovery. |
| B-2 | Closed. The audit is one streamed `git log`, not repeated `--skip` walks. | `test_the_branch_audit_uses_one_log_without_skip_pagination` plus the four-point timing curve below. |
| B-4 | Closed by guard. The identical status-loss predicate protects the intent append to `refs/onetree/wal`. | `test_a_status_lost_wal_intent_append_is_not_retried_under_a_new_operation`; the WAL-only disable mutation fails there. |
| F5 / B-3 | Settled and enforced. Gate 1 refuses a linked worktree at `Repository` construction. | Decision `9051eef65b37`; `test_linked_worktrees_are_refused_before_they_can_split_shared_wal_state`. |
| Record accuracy | Corrected. This record does not reuse AF4's 163-test mutation denominators as AF5 figures. | All current-run figures are labeled below; AF4's historical table remains unamended. |

## Mechanisms and boundaries

`Repository._reapplied(old, new, observed)` now returns false for an absent
observation or a no-op CAS, then accepts `observed == new` **or**
`is_ancestor(new, observed)`. A candidate is on a target when the target
contains it; an ordinary hand commit between the successful rename and the
post-failure read does not turn the winner into a refused operation.

The reftable decision now lives in two places only:

- Git's `show-ref` decides whether a nonempty table set names the ref.
- onetree refuses a stable zero-byte `reftable/tables.list`, observed never
  healthy on Git 2.53.0 (an implementation observation, not a Git guarantee).

The old name parsing, per-table open, directory-shape, and two-snapshot byte
comparison were removed. In particular, a compaction unlink cannot return
early before a second ref probe because onetree no longer opens listed tables.
`resolve_ref` re-runs its Git probes and storage observation for the reftable
absent arm, so a peer creating a ref while it is examined is resolved rather
than classified as corruption. The existing zero-byte refusal and D1
`test_a_ref_created_after_both_probes_is_retried_not_called_corrupt` remain
green.

The clone preflight is intentionally narrower than "any marker with no WAL":
a local crash before the first WAL write is AF4's crash-left landing and must
remain discoverable after the first registration. A default clone is
observable as the missing WAL shape with a configured `origin`. That shape is
quarantined before a session, claim, check, landing, recovery, release, or GC
can append. `inspect_wal()` reports one `wal_history_missing` divergence; the
only repair is explicit `onetree wal --repair`, which runs
`git fetch --no-tags --no-write-fetch-head origin
refs/onetree/wal:refs/onetree/wal` and re-inspects the result.

The branch-side audit remains scoped to `_divergences`, reached by
`inspect_wal()` and `recover()`. It is not part of normal `check`, `claim`, or
`land` calls. `marked_commits` starts one Git process and parses its existing
record delimiter incrementally; it does not use `--skip`, `--max-count`,
`TrailerScan`, or `DIVERGENCE_SCAN_LIMIT`.

[AF6](ROUND-AF6.md#disposition) supersedes both the origin-dependent clone
preflight and this audit-scope description. It records a repository-property
check independent of remote names, with the complete audit on mutation paths
and read-only `check` remaining free of that walk. AF6 also records that AF5's
separate marker walk did run on `check`; this paragraph was not evidence that
the entire AF5 read path avoided history work.

Gate 1's one-working-tree scope is now an executable precondition:
`Repository.__init__` compares `--absolute-git-dir` with `--git-common-dir`
and raises the typed `unsupported_worktree` error when they differ. That stops
the shared-ref/per-worktree-session split before any onetree metadata is
written.

## Red-first and mutation evidence

Before the product changes, the new AF5 selection run against `dc924a6`
produced **9 failed, 2 passed, 74 deselected**. The four failing F1 cases were
the 1- and 3-hand-commit depths across both backends; depth 0 stayed green,
which is exactly the fixture-sized hole. The other failures were the two
reftable transient shapes, `--skip` pagination, clone silence, and linked
worktree silence.

[AF6's record-accuracy correction](ROUND-AF6.md#disposition) reports **76
deselected**, not 74, and discloses a coverage decline from 92.26% to 91.78%
that AF5 omitted. The original selection figure above is retained as the
claim being corrected, not as a second valid result.

The WAL-ref fixture is a guard for already-correct AF4 behavior, so its
red-first evidence is the known-bad WAL-only mutation rather than base behavior:
skipping `_reapplied` only when `reference == refs/onetree/wal` left two intent
operations and failed `test_a_status_lost_wal_intent_append_is_not_retried_under_a_new_operation`
at **1 failed, 86 deselected**. Replacing `_reapplied` with false produced
**6 failed, 81 deselected** across the branch-depth guard; unconditional true
and removal of the `new == old` guard each made the existing lost-race control
fail at **1 failed, 33 deselected**.

Two AF4 fixture constraints are deliberately preserved:

- `test_a_crash_left_landing_is_found_under_any_number_of_hand_commits` keeps
  `[0, 1, 2, 3, 5]`. At the AF4 base, only depths 2, 3, and 5 were red; trimming
  it to 0 and 1 would remove the item-3 guard.
- The AF4 zero-byte reftable fixture still holds item 5 and item 6 through the
  same three mutations, all with `Failed: DID NOT RAISE RefCorruptError`. It is
  not misrepresented as distinguishing those old arms; AF5 adds separate
  fixtures for valid-list replacement and table-open compaction instead.

`self_after` is also described precisely in `tests/conftest.py`: it lets real
Git finish, then kills the Git shim so the broker observes a lost status. It
does not reproduce a death during rename; the broker-visible observations are
the same.

## Current-head measurements

All values in this table were collected from the AF5 candidate's pinned source
tree. `inspect_wal()` measurements use the supplied fast-import probe with one
registered session and an otherwise honest branch history.

| Measure | Result |
| --- | --- |
| Git implementation premise | `git version 2.53.0` |
| files absent-WAL race | 2,000 attempts, 0 `RefCorruptError`s |
| reftable absent-WAL race | 2,000 attempts, 0 `RefCorruptError`s |
| 1,000 commits | 0.031 s cold, 0.027 s warm |
| 20,000 commits | 0.171 s cold, 0.173 s warm |
| 100,000 commits | 0.765 s cold, 0.846 s warm |
| 300,000 commits | 2.473 s cold, 2.576 s warm |

The curve is linear in the measured range: 300,000 commits take 2.473 seconds.
AF4's repeated-skip result is historical context, not presented as an AF5 gate
measurement.

## Gates

| Gate | Measured result |
| --- | --- |
| `ruff check src tests` | `All checks passed!` |
| `mypy --strict src/onetree` | `Success: no issues found in 7 source files` |
| `pytest` | **179 passed in 77.56 s**; total coverage **91.78%** (floor 90) |
| CI Python 3.11 and 3.13 | Unobserved locally; the final PR is the required evidence source. |

The local receipt is `/tmp/af5-gates.jsonl`; it records the pinned commands,
exit status, duration, and output hash for all three gates.
