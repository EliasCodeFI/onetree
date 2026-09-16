# Gate 1, Round AF2 implementation record

Round AF (`5da4c7e`) replaced the two-ref landing transaction with a write-ahead
protocol and passed its gates. Two-seat adversarial verification then measured
the shipped protocol and found nine defects, two of them critical, both created
by the new protocol rather than inherited from Round A. Round AF2 closes them.
It changes no feature surface: session/index, claim, WAL, payload audit and
landing are still the whole of Gate 1, and the Round B referee, branch-operation
guards and rejection-repair contract are still out of scope.

The two critical findings share one root. Write-ahead made *"intent appended,
branch not moved"* a normal transient state of every healthy landing, and two
independent readers of that state — recovery, and the WAL's own replay — still
treated it as a crash. Recovery refused live work; replay then refused to read
the record the live work produced. The repair is a pair, and each half is
useless alone:

1. **Recovery asks whether the landing is still in flight** before refusing an
   intent, and leaves it alone if it is.
2. **Every append is folded before it is written.** A document that would not
   replay is refused at the door with a typed `WalAppendRefusedError`, so no
   write can poison a read.

The first prevents the interleave; the second bounds the damage of any
interleave it does not prevent, including ones nobody has thought of. Mutation
M15 removes the first and the fixture goes red on the second; M28 removes both
and reproduces the shipped brick.

## What the round changed

**Liveness of a landing, not of a session.** `land_intent` now records
`lander: {pid, start}` — the incarnation of the process performing the landing.
Recovery refuses an intent only when that incarnation is gone; an intent with no
lander tag falls back to the owning session's liveness, which is all a
pre-AF2 intent offers. See "Deviation" below for why this is not the session
bar the brief named.

**Fold-validated append.** `Coordinator._try_append` folds each prepared
document into a replica of the state its CAS would extend, then CASes. Winning
makes the replica true; losing writes nothing. Every broker append site goes
through it. `Wal.try_append` remains the un-validated primitive and is now what
the red controls use to produce the poisoned states they assert against.

**Ref storage decides absent-versus-unreadable.** Git answers "no such ref" and
"this ref exists and I cannot read it" with the same exit status and, for
`EACCES` and `EISDIR`, the same empty stderr (measured on Git 2.53.0). The new
`Repository.ref_storage_state` reads storage belonging to the active backend:
the loose ref plus `packed-refs` for `files`, or the backend-aware existence
probe plus `tables.list` and every named table for `reftable`. It returns `None`
only when that backend can say the name is absent. This paragraph corrects
AF2's files-only claim; AF3 measured and closed the reftable gap.

**The branch-side audit is paged, not retired by use.** The scan floor is below
the immediate parents of the WAL's *first* recorded position for the target
(`_FoldedState.target_origin`), so a crash-left commit hidden by one hand commit
stays inside the walk. `DIVERGENCE_SCAN_LIMIT` bounds one Git result; the audit
continues from the same resolved head until it reaches the floor. This corrects
AF2's permanent `scan_truncated` refusal.

That AF3 floor was subsequently refuted and removed by
[AF4](ROUND-AF4.md#corrections-to-earlier-round-records): more than one hand
commit could still hide a crash-left landing. This paragraph records the
intermediate repair, not a complete-history guarantee.

**Recovery reports what it found.** `RecoveryResult` carries measured
`repaired`, `refused`, `pending` and `locks`; the `diverged` field, which was
the constant `0`, is gone. `onetree wal` leads its human line with the verdict
and names pending intents.

## Findings disposition

| ID | Severity | Finding | Disposition |
| --- | --- | --- | --- |
| D1 | critical | `recover()` during a live landing bricks the WAL; both parties told success | Closed by the guard pair above. `_landing_in_flight` gates the refusal; `_try_append` gates every write. `_mark_landed` treats an already-recorded landing as success — field for field, so a mark that disagrees on any pinned field is still refused — and otherwise raises a typed `WalAppendRefusedError` naming `onetree wal --recover`, distinct from `WalCorruptError`. Red: M15, M19, M28, M31. |
| D2 | critical | An unreadable WAL ref reads as an empty healthy WAL | AF2 closed the files backend. AF3 corrected the backend scope and applies the same refusal to reftable storage and its global lock. Red: M20 plus AF3's two-backend fixture. |
| D3 | major | Unrecorded-landing detection disarmed by routine use | AF2 fixed later registration/landing masking. AF3 moved the floor below the immediate parent and replaced the permanent truncation verdict with complete, bounded-page scanning. Red: M16, M17 plus AF3's below-first-sight and beyond-one-page fixtures. |
| D4 | major | `recover()` declares all-clear over target-branch locks | Closed. `_held_locks` enumerates the WAL ref and every target the WAL knows, with the `HEAD` association `ref_lock_paths` already carried; reported in `RecoveryResult.locks` and both CLI modes; still never deleted. Red: M21. |
| D5 | medium | `onetree wal` human line contradicts its exit code and JSON | Closed. The line leads with `consistent`/`INCONSISTENT` and names pending intents. Exit-code asymmetry settled and documented below. Red: M22. |
| D6 | medium | `RecoveryResult` fields are constants surfaced as measurements | Closed. `repaired`/`refused` are counted per batch and printed; `pending` counts intents deliberately left in flight; `diverged` removed. Red: M23. |
| D7 | low | Vacuous assertion at `test_land.py:384` | Closed. `assert counted.stdout.strip() == "1"`. Suite swept for the same class: the remaining assertions with no comparison were each read, and each is falsifiable (path existence, a CAS return value, a non-empty identifier). |
| D8 | low | `recover()` appends a fresh `branch_diverged` refusal per run | Closed. The refusal is keyed by the divergence set (`_divergence_key`, folded into `state.diverged_refusals`); an unchanged set is recorded once, a changed set is recorded again. Red: M24. |
| D9 | minor | `RefLockedError` from the branch CAS strands a pending intent | Closed typed. Both `RefLockedError` and `GitError` from the branch CAS close the intent on the way out (`target_ref_locked` / `branch_cas_failed`, appended quietly so the original error is what the caller sees), which is sound because `update_ref` raises only after confirming the ref still holds the expected value. Red: M18. |

## Suspicions

The brief listed four suspicions with no demonstration. Each was checked; all
four were real, and all four are now fixtured.

| Suspicion | Outcome |
| --- | --- |
| `update_ref` misattributes contention as `GitError` (git.py:476-479) | **Confirmed.** The lock probe runs after the failed invocation, so a peer releasing its lock in between leaves contention looking like a hard failure. Every non-moving failure is now retried within the bounded window. Fixture drives a PATH-shim git that refuses `update-ref` *without ever taking a lock file*, parametrized over "clears after 2 failures" (must win) and "never clears" (must stay `GitError`). Red: M25. |
| `_verify_landed` folds infrastructure `GitError` into `WalCorruptError` (core.py:1109-1111) | **Confirmed.** A broken toolchain was reported as a corrupt WAL, sending the reader to the wrong repair. The arm now catches `RefCorruptError` only. Red: M26. |
| `_fold` accepts any shape for `gc.temp_indexes` (core.py:401-413) | **Confirmed, and worse than stated.** A bare string is iterable, so `temp_indexes: "index-1-1-abc"` folded into fifteen single-character removals without error; unhashable shapes reached the comparison as a `TypeError`. Now `_string_list`, like every other list in the event. Red: M19 (the fixture asserts both the write-time refusal and the read-time `WalCorruptError`). |
| `recover()` one-intent-per-CAS exhausts `MAX_CAS_RETRIES` past 32 pending intents | **Confirmed.** Resolution is now one batch per pass, so the retry budget covers contention only. Fixture strands 40 intents and asserts one pass clears all 40. Red: M27. |

## Decisions where the spec left freedom

**Exit-code asymmetry between the two `wal` modes is deliberate and now
documented in the code.** Inspection is a report: exit 1 means "I looked and the
state is not clean", the way `diff` reports difference. Recovery is an action:
a divergence it cannot repair raises, and any raised `OnetreeError` exits 2,
because the command did not do what it was asked to do. `--recover` therefore
exits 0 when it left the repository consistent, 1 when landings are still in
flight (nothing to do yet, come back), and 2 when it refused.

**An audit page boundary is not a repository verdict.** AF3 corrected this
decision after measuring that `scan_truncated` permanently refused a healthy
repository. Each Git result remains bounded, but the audit consumes subsequent
pages from a pinned target OID until it reaches the WAL-derived floor.

**Locks are residue, not inconsistency.** A ref lock is held for milliseconds
during every healthy append, so folding held locks into `consistent` would fail
a sound repository whenever a peer happens to be writing. They are reported in
`locks`/`ref_locks` and on the human line, and only a lock on the *WAL* ref
refuses recovery outright.

**`_close_intent` on the branch-CAS error path appends quietly.** If closing the
intent itself fails, the caller must still see the original `RefLockedError` —
the operator path is "remove the lock", not "debug the bookkeeping". The
stranded state, if that append ever loses, is visible as a pending intent on the
D5 human line, which is the fixture's second half.

## Deviation from the spec

The brief's D1 constraint reads: *"recovery may refuse an intent only when its
owning session is dead or expired (the gc liveness bar)"*. Shipped as written,
that bar contradicts the round's own must-not-regress list. In crash windows 2
and 3 the session is deliberately **alive** — it is the pytest process — while
the forked lander has been `SIGKILL`ed inside `update-ref`; a session-liveness
bar would refuse to touch those intents and the windows would never resolve.
Measured, not reasoned: mutation M30 replaces the lander tag with the session
bar and turns `test_inv7_kill_inside_each_ref_update_recovers_to_consistent_refs
[intent-appended]` and `[branch-not-moved]` red.

The bar shipped is therefore the same *kind* of liveness — (pid, birth tick),
the gc bar — asked of the **landing** rather than of the session, because a CLI
invocation dies while its session body keeps running, and it is the landing that
can still move the branch. Intents with no lander tag (anything written before
this round) fall back to the session bar exactly as the brief specifies. Both
directions are fixtured: a live lander under a dead session is left alone; a
dead lander under a live session is refused.

## Red-first evidence

Every guard this round adds was observed failing against a named known-bad
input before it counted. Sixteen mutations, each a copy of the tree outside it
with one guard force-disabled and the named fixture run against the copy, each
red:

| Mutation | Guard removed | Fixture turned red |
| --- | --- | --- |
| M15 | the in-flight bar | `test_recovery_run_during_a_live_landing_leaves_it_alone` |
| M19 | the validating append | `test_a_landed_mark_with_no_matching_intent_is_refused_at_write_time`, `test_a_gc_event_whose_temp_index_list_is_the_wrong_shape_is_corruption` |
| M28 | both D1 guards (= shipped AF) | `test_recovery_run_during_a_live_landing_leaves_it_alone` |
| M31 | the identical-record arm of `_mark_landed` | `test_a_landing_recovery_completed_first_is_this_landings_own_outcome` |
| M20 | ref-storage discrimination | `test_an_unreadable_wal_ref_is_never_read_as_an_empty_healthy_wal[eacces,eisdir]`, `test_ref_storage_answers_present_or_absent_from_storage_not_from_git_text` |
| M16 | the WAL-bounded scan floor | `test_an_unrecorded_landing_survives_the_routine_act_that_used_to_mask_it[first_sight,later_registration,later_landing]` |
| M17 | truncation reporting | `test_a_truncated_branch_audit_is_reported_instead_of_reading_as_clean` |
| M21 | target-branch lock enumeration | `test_recovery_reports_the_target_branch_locks_it_must_not_delete` (and one Round AF lock fixture) |
| M22 | the human-line verdict | `test_the_wal_human_line_agrees_with_its_exit_code_and_its_json` |
| M23 | measured recovery counters | `test_recovery_counters_are_measurements_and_reach_the_human_line` (and both `test_recovery_resolves_an_exposed_intent` cases) |
| M24 | refusal idempotence | `test_probing_a_diverged_repository_records_one_refusal_per_divergence` |
| M18 | intent closure on a locked branch | `test_a_locked_target_branch_closes_the_intent_written_ahead_of_it` |
| M25 | retrying non-moving failures | `test_lock_contention_that_clears_is_retried_rather_than_misattributed[2]` (the `[99]` case stays green, which is the point) |
| M26 | the narrowed `_verify_landed` arm | `test_infrastructure_failure_while_verifying_a_landing_is_not_called_wal_corruption` |
| M27 | batch recovery | `test_recovery_resolves_more_stranded_intents_than_the_cas_retry_budget` |
| M30 | the lander tag (= the brief's literal bar) | `test_inv7_kill_inside_each_ref_update_recovers_to_consistent_refs[intent-appended,branch-not-moved]` |

Four of these are also in-suite controls, run on every `pytest` invocation
rather than only in the mutation harness: M15's consequence is asserted by
`test_a_recovery_that_ignores_liveness_is_stopped_at_the_wal_door`, and M16,
M17 and M18 are re-applied by `monkeypatch` at the end of their own fixtures, so
each test carries the evidence that it can fail.

The six reproduction scripts the brief supplied were re-run unmodified against
the fixed tree. Each now measures the opposite of what it measured against
`5da4c7e`:

| Script | Against AF | Against AF2 |
| --- | --- | --- |
| `exp1b_refshapes` | chmod-000 and ref-is-directory → `resolve_ref` returns `None`, `head()` is `None` | both → `RefCorruptError`/`WalCorruptError` naming the reason |
| `exp2_eacces_wal` | `check` answers `blocked=False`; `inspect_wal` answers consistent, 0 events | `check` raises `WalCorruptError` |
| `exp3_recover_race` | land succeeds, recovery reports `consistent=True refused=1`, all five entry points then raise forever | land succeeds, recovery reports `consistent=False pending=1`, all five entry points OK |
| `exp4_divergence_mask` | after an unrelated registration: `consistent=True`, unrecorded commit still on the branch | `consistent=False`, `['unrecorded_landing']`, `recover()` raises |
| `exp5_lock_and_human` | `'WAL <oid>: 3 event(s)'` at exit 1; `'recovery complete; pending=0'` at exit 0 with a stale branch lock present | `'…; INCONSISTENT; 1 pending intent(s)'` at exit 1; recovery names the lock path and exits 1 |
| `exp6_misc` | 3 refusals after 3 probes; a landing below the scan cap reads consistent | 1 refusal after 3 probes; the cut-short walk reports `scan_truncated` |

## Local verification

Python 3.14.4, Git 2.53.0, on 2026-08-17. `ruff check src tests`, `mypy` and
`pytest` are green from the repository root: 120 tests in 24.9 seconds with
93.22% branch coverage against a 90% floor. Round AF's baseline at `5da4c7e`
was 92 tests and 93.46%; the quarter-point drop is not attributed here beyond the
measurement itself. What the report shows uncovered is dominated by defensive
arms for malformed Git output — non-ASCII object IDs, `ls-tree`/`rev-list`
records that do not parse — which predate this round, together with one new
arm: the `lstat` failure in `ref_storage_state` that no fixture can provoke on
a healthy filesystem. These are local observations, not broader performance
claims.

## Known limitations

**The former scan cap was a permanent refusal, not merely a blind spot.** AF3
measured a healthy register-before repository becoming inconsistent after 4,100
ordinary commits and corrected the mechanism. `DIVERGENCE_SCAN_LIMIT` is now a
page size; history is audited to the floor without emitting `scan_truncated`,
and a real unrecorded landing below the first page is still reported.

**An intent written before this round has no lander tag.** It falls back to
session liveness, so on such an intent recovery is exactly as conservative — and
exactly as wrong in the crash-window case — as the brief's literal bar. There is
no migration: the fallback exists so that a WAL written by an older broker still
recovers at all.

**`EACCES` is unfixturable for root.** The two unreadable-ref fixtures check
`os.access` after `chmod 000` and skip themselves when the mode bits do not
bite. A root-only CI would therefore run one arm of D2, not both.

**WAL events are still unauthenticated** (Round AF's limitation, unchanged).
The validating append refuses documents that would not *replay*; it does not
and cannot decide whether the broker wrote them.

## Corrections to ROUND-AF.md

- The F4 row claimed `resolve_ref` discriminates "rather than by matching git's
  message text". True of the caret-probe pair, false of the fourth arm, which
  matched `"broken ref"`-style text — and that arm is what D2 reproduced.
  Row corrected.
- The same row listed "unreadable ref file" among four fixtured corrupt shapes.
  That fixture writes *readable* garbage text into the ref; the genuinely
  unreadable shapes were unfixtured until AF2. Row corrected.
- The crash-window section's claim that "no crash can put a broker landing on
  the branch that the WAL has no record of" is a property of the write-ahead
  order and remains true. What did not hold is *detection* of a commit already
  in that state. Scope note added.
- The F3 row is annotated with D3 and D8, which reopened it.
- "Beyond the brief" described `GitError` from `update_ref` as "never retried".
  That is the misattribution D-suspicion 1 confirmed; the paragraph now records
  the corrected rule.
- The stale-ref-lock limitation described recovery as refusing on a lock. That
  holds for the WAL ref; target-branch locks are reported, not refused. Note
  added.
