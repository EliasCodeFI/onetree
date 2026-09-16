# Task 136: ordinary loose-ref audit cost

The current baseline, main 9e6a410, still spawned `symbolic-ref` four times per
ordinary local branch during `inspect_wal()`. The old task's percentage is not
reused: these measurements include a cold ref-name validation cache.

| Plain branches | Baseline symbolic-ref calls | Candidate symbolic-ref calls |
| --- | ---: | ---: |
| 1 | 4 | 0 |
| 10 | 40 | 0 |
| 100 | 400 | 0 |

The baseline census ran first. An assertion requiring zero symbolic-ref calls
then failed with 4, 40 and 400 respectively before the implementation changed.
The candidate retains the same check on SHA-1 and SHA-256 repositories. These
are command counts, not a wall-time speedup or a bound for arbitrary ref shapes.

A canonical direct loose-ref file is retained in the inventory without asking
Git whether it is symbolic. The read is bounded by the repository's object-id
length plus two bytes, and contents are not cached. Noncanonical contents,
symlinks, and failed reads retain the existing Git path. Git still resolves and
validates all listed object IDs; raw bytes never establish object existence,
commit type, or a complete ref listing. The existing before/after inventory and
repository audit checks remain in force.

`tests/test_audit_cost.py` uses the existing real-Git `GitFixture`. Its command
observer wraps `Repository.git`, forwards every invocation unchanged, and first
checks itself against an actual `rev-parse HEAD`. JUnit properties preserve the
loaded implementation path, branch count and command census. The hash/backend
fixture configures Git before requesting `git_repo` and asserts the resulting
repository format. `GIT_DEFAULT_HASH` and `GIT_DEFAULT_REF_FORMAT` precedence
were checked in the [official git-init documentation](https://git-scm.com/docs/git-init).
Context7 Git documentation was consulted first; it supported the ref-validation
contract but did not resolve the environment-variable question directly.

Real null IDs, absent objects, blob objects, malformed text and trailing junk
must refuse the audit, leave the target and WAL untouched, and allow inspection
after the valid ref is restored. A sequence through direct, dangling symbolic,
resolving symbolic and direct again checks that contents are re-examined.
An unreadable direct ref must still reach Git's refusal; that case skips only
if the host actually permits reading the permission-denied fixture. Existing
dangling, resolving, namespace, symlink and packed-ref controls are retained.
No production predicate or Git result is stubbed.

An early adverse assertion expected GitError for an absent object; the observed
and retained API result is RefCorruptError, so the assertion was corrected.
This round does not claim a hostile filesystem-race boundary or an exhaustive
special-file/read-error population. It does not change the reftable path.

Evidence: `/anvil/scratch/onetree-audit-cost-20260916/`, including baseline.xml,
red-first.txt, ref-controls.xml and focused.xml. Full-suite and independent
review results are recorded separately when complete.
