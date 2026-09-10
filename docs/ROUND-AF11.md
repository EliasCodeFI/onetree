# Gate 1 Round AF11: audit escape and bounded folded identity

AF11 starts from `3809f84c66ee592c2f6b3439c38a30a9dbbdf6ec` on branch
`af5`. The measured external surface is Git 2.53.0.

## Red-first defects

The self-tested external conformance instrument has 41 rows and suite hash
`edb2fa3ab3393671a357910637329420acb73ff629c4637e852a17e31707ede9`.
Against the AF10 start it reported exactly ten violations across five rows:

- dangling symbolic branch below `refs/heads`;
- dangling filesystem symlink below `refs/heads`;
- accepted `core.fsyncObjectFiles` deprecation stderr;
- caller `GIT_TRACE=1` stderr; and
- global Trace2 configuration stderr.

Each row denied ordinary mutation and left `wal --adopt` unable to restore the
operator escape. The other 36 rows were green. Repository fixtures added
before production changes independently reproduced the two dangling-alias
omissions and unrelated rc-0 stderr refusal. A resolving filesystem symlink
and the exact folded-prefix identity set stayed green.

The sixth defect was measured outside that table. A legal message below 128
KiB with 16,000 folded continuation lines made both inspection and adoption
raise uncaught `MemoryError` under a 1.5 GiB `RLIMIT_AS`. The failing traceback
ended in `_landing_candidate_values` while it retained all cumulative byte
prefixes and then materialized all candidate values beside them. The first
candidate fix closed that LF shape, but an independent Codex review reproduced
the same exception in the accepted `Onetree-Operation :` separator form with
CRLF continuation lines: two separator positions across normalized and raw
views made four exact prefix families. The repository fixture was expanded to
the full separator-by-line-ending cross-product and observed red on that case.

The same frozen review found two more exact closure failures. A symbolic branch
whose missing terminal name was also a namespace directory, and one terminating
at a dangling filesystem symlink, were still treated as stored corruption and
bricked the audit. Separately, a CR before trailing horizontal whitespace made
the optimized parser skip raw semantics even though trimming exposed a distinct
valid identity. Each minimal repository/parser reproducer was observed red
before the reviewed implementation changed. The first streaming audit repair
then exposed one final consumer: post-adoption landed-record verification still
materialized the exact identity set and failed under the same bounded case.
A fresh two-lens Codex review of the resulting green candidate found one shared
blocker: Git traverses a resolving filesystem namespace symlink, but the files
inventory did not. A corrupt external child was therefore omitted by rc-0 Git
with a warning while audit returned clean. The valid-to-corrupt transition and
a namespace-cycle case were both observed red before the next source change.
The following frozen review cleared those repairs adversarially, then its
silent-failure lens measured a remaining operational cost: accepted spaced-
separator CRLF messages at 8,000, 16,000, and 32,000 continuations took 0.301,
1.121, and 4.307 seconds. Doubling input therefore cost approximately four
times as much because every cumulative prefix was decoded again. Before the
next source change, a structural test observed 11,141,132 decoded candidate
characters for a 1,000-continuation, 8,053-byte message and failed its 32,212-
character linear-work ceiling. The first optimization attempt also failed the
1,502-shape differential on the accepted spaced separator: stopping one
separator-position family accidentally stopped the second family too.

Context7 was queried against `/git/htmldocs` before changing the Git plumbing.
It documents `git symbolic-ref` status 0 for a printed symbolic ref, status 1
for a non-symbolic name, and status 128 for another error. It did not supply a
contract for rc-0 `for-each-ref` stderr or dangling-ref enumeration. Those two
claims therefore remain grounded in the Git 2.53.0 A/B fixtures and the bound
external rows, not inferred documentation.

## Implementation

The files-backend inventory now distinguishes exact ref storage from namespace
directories and non-resolving aliases. Dangling filesystem symlinks and
symbolic refs are excluded even when a terminal pathname is a directory that
stores legal child refs or another dangling alias. Resolving aliases remain in
the inventory, and a packed entry with the same name prevents exclusion so a
loose dangling alias cannot hide it. Invalid direct values and other exact
stored corruption still reach the stable before/after omission check and
refuse.

Resolving namespace symlinks are now traversed under their exposed branch
names. Per-path `(device, inode)` ancestry permits distinct aliases to the same
directory while detecting a recursive namespace edge; cycles refuse instead
of recursing. Descendant files participate in the same validity, dangling-
alias, packed-shadow, and stable omission rules as ordinary loose refs, so a
corrupt external child cannot disappear behind ignored stderr.

Successful `for-each-ref` stderr no longer acts as a complete corruption
channel. Files refs rely on their storage snapshots. Reftable continues to
fail closed on Git's explicit broken local-branch diagnostic, while unrelated
warnings and traces do not reframe a successful listing. Caller environment
variables are preserved.

Each marker position now uses one reusable byte buffer, tracks the exact ASCII
horizontal trim boundaries, and decodes directly from a released memory view.
The exact `landing_ids` API still returns every normalized and raw prefix and is
therefore output-sized. Operational audit instead scans each separator-position
family independently. Finite recorded identities are indexed by their encoded
byte lengths; the reducer decodes only the first family value, values still
needed to find a nonpending witness, and lengths that can exactly match a
recorded identity. It stops a family after neither a later recorded length nor
a pending escape can matter, so it no longer decodes every cumulative prefix.
It returns immediately on any recorded `(commit, operation)` match or retains
only the deterministic scalar witness the divergence/adoption schema needs.
Adopted-record verification uses the same exact-membership reduction. The raw
arm is elided only when the message contains no CR byte, preserving the case
where horizontal trim exposes CR as a terminal parser character.

## Receipts

Before the source change, the focused audit run had three expected failures
and two controls passing. The bounded end-to-end fixture failed with the
recorded `MemoryError`. Frozen-review regressions independently failed on the
separator-by-CRLF product, both terminal-alias compositions, and the
CR-before-horizontal-space identity. Post-adoption verification then failed
the bounded fixture before its streaming membership repair. After those
changes:

- the focused audit, corruption, prefix, and 1,502-shape controls passed;
- inspection and `wal --adopt` both completed for all four separator-by-line-
  ending combinations with 16,000 continuation lines under the same 1.5 GiB
  process limit;
- the external 41-row comparison reported all ten target violations fixed,
  zero remaining, zero regressions, zero changed-meaning rows, and
  `prose-marker-over-detection-has-an-escape/INV-C` still `CONFORM`;
- the complete landing-predicate file passed 55 tests in 31.75 seconds;
- Ruff passed across `src` and `tests`;
- strict mypy passed across all seven source files;
- `git diff --check` passed; and
- the pre-review configured pytest suite passed 346 tests in 175.61 seconds
  with 91.06% total coverage, above the 90% floor.

Those pre-review figures are chronology, not the final namespace-repair gate;
after the namespace repair, the external contract again passed with the same
ten fixes and zero regressions or meaning changes, Ruff/mypy/diff checks stayed
green, and the configured suite passed 348 tests in 175.72 seconds with 90.71%
total coverage.

Those namespace figures are likewise chronology, not the final CPU-repair
gate. On the same accepted spaced-separator CRLF shape, the repaired reducer
measured 0.004014, 0.007937, and 0.016428 seconds at 8,000, 16,000, and 32,000
continuations: approximately two times the time for two times the input. The
expanded 1,502-shape reducer differential checks every exact identity as a
singleton recorded query, the complete recorded set, every singleton pending
exclusion, a nonmatching recorded query, and the deterministic witness. The
four 16,000-continuation bounded CLI cases and all 57 landing-predicate tests
passed. The external comparison again reported ten target fixes, zero
remaining, zero regressions, and zero meaning changes. Ruff, strict mypy, and
`git diff --check` passed, and the configured suite passed 350 tests in 171.33
seconds with 90.81% total coverage.

Solution-specific controls also cover a resolving symbolic alias, a resolving
filesystem namespace alias, a packed branch shadowed by a dangling loose
alias, a corrupt exact terminal target, a corrupt external namespace child, a
namespace cycle, and reftable's explicit broken-branch diagnostic.

## Residual debt

AF11 does not claim an all-clear for the previously recorded non-UTF-8 branch
traceback, repeated-CR identity mismatch, branch-inventory subprocess cost,
refused-candidate laundering, audit namespaces outside local branches plus
HEAD, or the other inherited unpinned guards.
