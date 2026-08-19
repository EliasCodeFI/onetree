# Gate 1 Round AF8B, closed by AF8C: Git history inputs

AF8B started from `fc4ad1e746782c7130d1322183618f6a862b2ecc` on
branch `af5`. AF8C resumed AF8B's uncommitted four-file working-tree diff
without resetting, stashing, or reconstructing it. Before the closure edit,
that diff was measured as 333 insertions and 19 deletions in
`src/onetree/core.py`, `src/onetree/errors.py`, `src/onetree/git.py`,
and `tests/test_recovery_semantics.py`. The imported module resolved to
`/home/anvil/onetree/src/onetree/core.py`.

The local measurement surface was Git 2.53.0 and Python 3.14.4. CI's Python
3.11 and 3.13 jobs are external post-push observations, not inferred from the
local interpreter.

## Shallow-history correction

`Repository.is_shallow()` now first asks Git
`rev-parse --is-shallow-repository`. When Git reports `true`, onetree reads
the repository's own marker and treats every valid listed commit with a raw
parent as a cut. Parent-object presence is not evidence that the edge is
walkable: in
`test_a_non_root_shallow_marker_refuses_with_its_parent_object_present`, the
parent object exists and is named in the raw commit, but
`git rev-parse <boundary>^` exits 128 and an ordinary log omits the hidden
landing. Reverting `is_shallow()` to AF8's parent-object-presence algorithm
made that test fail in an isolated mutation run.

A marker naming a genuine root remains harmless because there is no parent
edge to suppress. Missing and non-commit marker entries fail closed. If Git
reports `true` and the marker then disappears, onetree also returns `True`
because the observation raced with repository state; AF8B did not add a
deterministic deletion control for that narrow time-of-check/time-of-use
fallback, so it is not counted among the mutation-pinned claims below.

An ambient `GIT_SHALLOW_FILE` is removed in `Repository._environment()` so
the status query, the marker inspection, and the history walk describe one
repository. Removing that pop made
`test_an_ambient_shallow_file_cannot_make_the_guard_and_walk_disagree` fail.

## Required deletion audit

Every retained check that AF8B inherited as suite-deletable was removed alone
inside an isolated copy by the shelf's `mutation_pin.py`. Each control was
green before mutation and after byte-identical restoration.

| Check from the AF8B brief | Disposition | Observed input without the check |
| --- | --- | --- |
| Pop ambient `GIT_REPLACE_REF_BASE` | Removed. `GIT_NO_REPLACE_OBJECTS=1` subsumes it for the history traversals at issue; the direct measurement is below. | No deletion mutation exists because the check no longer exists. A three-commit custom-namespace probe supplied the negative and positive controls. |
| `self._refuse_legacy_grafts(targets)` in `adopt_landings()` | Retained. | Deleting only this call made `test_a_nonempty_legacy_graft_file_is_an_explicit_mutation_boundary` report 1 failed test. |
| Final malformed-stream refusal in `marked_commits()` | Retained after child-status handling. | Deleting only the final two-line refusal made `test_an_incomplete_stream_defers_to_the_child_exit_status[successful-child]` report 1 failed test. |
| Non-commit shallow-boundary return in `is_shallow()` | Retained. | Deleting only the type check and `return True` made both `blob` and `missing` cases of `test_a_non_commit_shallow_boundary_fails_closed` fail. |

The mutation-side graft refusal is independently observed too. After correcting
the parametrized expected kind to `legacy_grafts`, deleting only
`_read_wal()`'s graft refusal made
`test_hidden_history_cannot_launder_an_unrecorded_landing[grafts-legacy_grafts]`
report 1 failed test. The replace arm in the same parametrization still expects
`unrecorded_landing`; changing the graft error kind did not weaken either
hidden-history control.

## Minor defects

| Defect | Fix | Observed counterfactual |
| --- | --- | --- |
| The empty `GIT_GRAFT_FILE` override leaked Git's deprecation advice into stderr and therefore into error detail. | Every `subprocess.run` and streamed `Popen` Git command receives `-c advice.graftFileDeprecated=false`. | Removing that option made `test_the_empty_graft_override_does_not_add_deprecation_advice` report 1 failed test on Git 2.53.0. |
| `st_size > 0` classified blank and comment-only graft files as effective history boundaries. | `nonempty_graft_file()` now parses lines, ignoring blank lines and lines beginning with `#`. | Reverting to the size test made the `blank`, `whitespace`, and `comments` cases of `test_a_graft_file_without_effective_entries_is_not_a_boundary` all fail. |
| Graft mutation refusal used the generic `git_error` kind. | `LegacyGraftError` carries stable kind `legacy_grafts`. | Reverting the raise to `GitError` made `test_a_nonempty_legacy_graft_file_is_an_explicit_mutation_boundary` report 1 failed test. The missed hidden-history parametrization was also corrected from `git_error` to `legacy_grafts`. |
| An incomplete output pair was refused before `process.wait()`, masking a real child failure. | The parser records `malformed_stream`, waits, reports a non-zero child status first, and only then refuses malformed output from a successful child. | Reverting to the pre-`wait()` raise made `test_an_incomplete_stream_defers_to_the_child_exit_status[failed-child]` fail instead of preserving exit 23 and `deliberate log failure`. |

## Why `GIT_REPLACE_REF_BASE` was removed

The removal was measured against Git 2.53.0 rather than inferred from the name
of `GIT_NO_REPLACE_OBJECTS`. A scratch repository contained the chain
`tip -> hidden -> root`. A replacement for `tip`, stored under the custom
`refs/onetree-probe/` namespace selected by `GIT_REPLACE_REF_BASE`, changed
the direct `git log` from three commits to `tip -> root`; this proved that
the custom namespace was active. Adding `GIT_NO_REPLACE_OBJECTS=1` restored
all three commits while leaving the custom base set.

The same ambient custom base was then left set while exercising both onetree
process paths. `Repository.git_text("log", ...)` (the
`subprocess.run` path) and `Repository.marked_commits()` (the streamed
`Popen` path) each returned `tip`, `hidden`, and `root`.
`Repository._environment()` sets `GIT_NO_REPLACE_OBJECTS=1` in both paths.
Therefore, for replacement-object effects on these history traversals,
popping `GIT_REPLACE_REF_BASE` was redundant and its removal did not replace
an untested guard with an assumption. This result is scoped to Git 2.53.0 and
to object replacement during traversal; it is not a claim that the variable
cannot select where an explicit `git replace` command writes a ref.

## Commit-graph answer

Yes: a trusted on-disk commit graph is a fourth local hidden-history vector,
after replace refs, legacy grafts, and shallow boundaries.

The known-bad fixture wrote a reachable commit graph, changed the tip row's
parent slots to the graph sentinel for no parent, and recomputed the file's
SHA-1 trailer. The resulting file was checksum-valid but semantically invalid:
`git commit-graph verify` exited 1. Ordinary `git log` trusted the graph and
omitted the still-present hidden landing; the same log with
`-c core.commitGraph=false` included it. A separate valid-graph control
continued to include the landing.

AF8B consequently adds `-c core.commitGraph=false` to the common options used
by both Git invocation paths. Removing that option made
`test_a_checksum_valid_commit_graph_cannot_hide_an_unrecorded_landing` report
1 failed test. This is measured as a hidden-history input even though
`git commit-graph verify` can diagnose it: the broker's audit must not trust
an optional accelerator before deciding whether mutation is safe.

## Stream framing preserved

AF8B did not change AF8's alternating `OID NUL trailer NUL` parser.
`test_git_log_z_terminates_every_audit_record` observes Git 2.53.0 emit two
NUL bytes per record for `git log -z --format=%H%x00`: the explicit format
NUL plus `-z`'s record terminator. Commit objects cannot contain a NUL in
their message, so no message route can manufacture an extra field boundary.

## Gates

| Gate | Measured result |
| --- | --- |
| Module path | `/home/anvil/onetree/src/onetree/core.py` |
| Deletion/reversion controls | 11 isolated `mutation_pin.py` runs returned `PINNED`; every mutated run failed its named control and every restored control was green. |
| Full pytest | **225 passed in 102.44 s**; branch-aware coverage **92.31%** (floor 90). |
| Ruff | `All checks passed!` for `ruff check .`. |
| Mypy | `Success: no issues found in 7 source files` under `--strict`. |
| Frozen conformance self-test | **37 checks passed**. |
| AF8B receipt comparison | Suite `5372a8c29f40`; `shallow-marker-hides-landing/INV-A` fixed; 0 still violating in scope, 17 deferred, 0 regressed, 0 changed meaning; exit 0. |
| CI Python 3.11 and 3.13 | Must be observed on PR #1 at the pushed commit; no local claim is made for those interpreters. |

The conformance suite was not edited. Its informational detail change for
`hidden-by-grafts/INV-A` reflects the new typed refusal detail; the invariant
remained conforming.
