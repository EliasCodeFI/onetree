# Gate 1 Round AF8: hidden history, audit framing, and stale shallow markers

AF8 builds on `6f6d1a666122305be8dc34017b7e91681213fb0d` on branch
`af5`; it does not merge or move `main`. Before any edit, `HEAD`, the branch,
and `origin/main` were verified as the brief specified, and
`onetree.core.__file__` resolved to `/home/anvil/onetree/src/onetree/core.py`.
The local interpreter is Python 3.14.4 and Git is 2.53.0. CI's Python 3.11
and 3.13 matrix remains an external post-push gate and is not claimed by this
pre-push record.

## Disposition

| Contract row | Disposition | Executable guard |
| --- | --- | --- |
| `hidden-by-replace-ref` | Closed. Every onetree Git process ignores replacement refs, so the audit walks the stored graph rather than the locally rewritten graph. | The behavioral fixture proves ordinary Git omits the hidden landing, then requires onetree to report its real commit and refuse mutation. |
| `hidden-by-grafts` | Closed with two independent layers. Git is pointed at an empty graft file for the walk, while a non-empty `$GIT_DIR/info/grafts` is also reported as `legacy_grafts` and independently refuses mutation and adoption. | The hidden-landing fixture requires the real commit in the report. A separate clean-history fixture requires the non-empty graft boundary even if Git's undocumented override were to stop working. |
| `trailer-carries-record-separator` | Closed. The stream is framed as repeating `(OID NUL trailer NUL)` fields. Raw `0x1e` and `0x1f` remain trailer data and cannot create a second record. | The known-bad commit still contains the control bytes, but exactly its real OID is reported. |
| `trailer-malformed-record` | Closed by the same framing. The crafted bytes no longer interrupt the whole-branch walk. | The audit completes and reports the real landing instead of raising `malformed commit listing record`. |
| `stale-shallow-marker-complete-repo` | Closed. `is_shallow()` now means that a commit listed in `$GIT_DIR/shallow` names a parent object that is absent, not merely that the marker exists. | The stale-root fixture proves Git says `true` while onetree permits the complete repository; the existing real depth-3/depth-4 clones still report and refuse `shallow_history`. |

The exact receipt comparison changed all 10 AF8 violations to conform. It left
the 17 AF9 violations deferred, with zero regressions and zero vanished or
changed checks. The suite hash remained `84d4d02f60e8`.

## Mechanisms and boundaries

`Repository._environment()` pins `GIT_NO_REPLACE_OBJECTS=1`, points
`GIT_GRAFT_FILE` at the platform's empty device, and removes an ambient
`GIT_REPLACE_REF_BASE`. Current Git documentation carries the replacement-ref
switch and describes `info/grafts` as outdated with
`git replace --convert-graft-file` as its migration. It does not document
`GIT_GRAFT_FILE`, so that variable is not the sole carrier of the guarantee.
`Repository.nonempty_graft_file()` inspects the actual
`$GIT_DIR/info/grafts`; read-only WAL inspection exposes a boundary divergence,
and every mutating WAL read plus adoption refuses with the file and repair in
the typed error details.

`Repository.marked_commits()` invokes one `git log -z` process with
`%H%x00%(trailers:...)`. Its incremental parser alternates OID and trailer
fields across 64 KiB chunks and refuses an incomplete pair at end of stream.
It still waits for the child and checks its exit status before returning any
result, drains stderr through the existing temporary file, and retains the
existing object-ID and UTF-8 validation. This changes framing only; it does not
change the AF9 operation-identity skip set or audit-target selection.

`Repository.is_shallow()` reads the repository's own shallow marker. For each
listed commit it verifies that the object is a commit, reads its raw parent
headers with replacement refs and grafts disabled, and asks whether every
parent object is locally present as a commit. A missing boundary commit or
parent remains fail-closed. A marker whose full parent graph is present is
stale metadata, not truncated history.

AF8 does not change `_audit_targets`, the operation-ID recorded predicate,
`adopted` event fields, the WAL/branch read order, root or merge adoption, or
detached-HEAD handling. Those are the 17 violations assigned to AF9.

## Red-first evidence

The AF7 oracle passed all 37 self-checks before its output was used. Against
the unmodified AF8 base it reproduced 27 violations across 12 of 17 rows; the
receipt comparison named exactly the required 10 as still violating and found
no regression.

The six new behavioral cases were then added before source changes and run as
one focused selection. The result was **6 failed, 110 deselected**:

- both hidden-history arms omitted the real landing;
- a harmless non-empty graft file did not create a visible/refusing boundary;
- the separator payload fabricated a 40-hex OID;
- the malformed payload raised `GitError: malformed commit listing record`;
- the complete repository with a stale marker returned `is_shallow() is True`.

After the implementation, the expanded framing/history selection, including
the existing malformed-OID, chunk-seam, and full-stderr controls, was
**9 passed, 107 deselected**.

## Preserved safeguards

| Safeguard | Measured result |
| --- | --- |
| `_reapplied` containment | Depths 0/1/2/3/5/10/40 on both files and reftable, plus the WAL-ref status-loss guard: green in the 17-test preservation selection. |
| Absent-WAL load | 2,500 probes on files and 2,500 on reftable; zero spurious `RefCorruptError` on either backend. |
| Worktree refusal precision | The linked worktree was refused; the primary checkout, `--separate-git-dir`, symlinked `.git`, symlinked repository path, and submodule all constructed. |
| Genuine shallow policy | Both existing depth fixtures still report `shallow_history` and refuse mutation. |

## Gates

| Gate | Measured result |
| --- | --- |
| Module path | `/home/anvil/onetree/src/onetree/core.py` |
| AF7 `--selftest` | **37 checks passed** |
| Exact AF8 receipt comparison | Exit 0; **10 fixed**, **0 still violating**, **17 deferred**, **0 regressed**, **0 changed meaning** |
| `ruff check src tests` | `All checks passed!` |
| `mypy` (strict project configuration) | `Success: no issues found in 7 source files` |
| `pytest` | **208 passed in 98.56 s**; branch-aware coverage **91.73%** (floor 90) |
| CI Python 3.11 and 3.13 | Must be observed on PR #1 at the exact pushed head; not assumed locally. |

Ruff's formatter is not a repository gate. `ruff format --check` against the
three touched Python files proposed broad rewrites of pre-existing lines in
`core.py` and `test_recovery_semantics.py`; those unrelated changes were not
applied. The configured lint gate itself is green.
