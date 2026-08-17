# Gate 1, Round AF implementation record

Round A (`dc8f82e`) passed its gates and was then measured by two-seat
adversarial verification. Round AF closes those findings. It changes the
landing protocol, not the Round A feature set: session/index, claim, WAL,
payload audit and landing are still the whole surface, and the Round B train
referee, branch-operation guards and rejection-repair contract are still out
of scope.

## The protocol change

Round A landed through one `update-ref --stdin` transaction that compared and
swapped the target branch and `refs/onetree/wal` together. On the files ref
backend that transaction is two sequential loose-ref renames, branch first.
It is therefore not crash-atomic, and no crash-atomic two-loose-ref primitive
exists on that backend — the protocol had to stop needing one.

Landing is now write-ahead, in three steps, each of which is a single-ref
compare-and-swap:

1. **`land_intent`** is appended to `refs/onetree/wal`. One ref, one rename:
   the append either happened or it did not. This append is also the fence —
   any release, reclamation, registration change or unrelated append since the
   audit snapshot moves the WAL head, the CAS loses, and the attempt re-audits
   from scratch. Fence semantics are unchanged from Round A; only the primitive
   carrying them is.
2. **The branch CAS** moves the target from the audited head to the candidate.
   A crash inside it leaves the branch either moved or not, never half-moved.
   A lost CAS means the target moved under us: the intent is closed with a
   `refused` event (`target_moved`) and the attempt re-audits.
3. **`landed`** is appended, recording the candidate, its target, the expected
   head, the tree and the payload.

Claim expiry and session liveness are re-sampled immediately before step 1, so
the fence sample and the intent append are adjacent. There is still no atomic
primitive spanning a Git ref lock and the operating system's process table or
wall clock; "landing moment" means this final sample followed by step 1.

Two consequences worth naming:

- **Adjacency is gone.** Round A could require the `landed` commit to sit
  directly on its `land_intent`, because one transaction wrote both. The branch
  CAS now sits between them and an unrelated append may legitimately intervene.
  Replay instead pins `landed` to its intent field by field — `commit`,
  `target`, `expected_head`, `tree`, and the payload path list — so a forged or
  mismatched mark still refuses replay.
- **`update_refs_atomically` is deleted**, not deprecated. It was the two-ref
  primitive whose only caller was the old landing path; leaving it on the
  `Repository` surface would leave the next caller a primitive that reads as
  atomic and is not.

## Crash windows

Every window below is exercised by a real `SIGKILL` delivered *inside* the git
subprocess, not at a Python checkpoint: a PATH-shim `git` counts matched
`update-ref <ref>` invocations and, on the Nth, either kills before handing off
to real git (`before`) or runs real git first and then kills (`after`). The
lander is a forked process; the fixture asserts `exitcode == -SIGKILL`, so a
window that silently stopped killing would fail rather than pass vacuously.

| # | Killed at | State left behind | `recover()` | Retry |
| --- | --- | --- | --- | --- |
| 1 | before the intent append | nothing moved | no-op | lands |
| 2 | after the intent append | intent pending, branch unmoved | refuses the intent | lands |
| 3 | before the branch CAS | intent pending, branch unmoved | refuses the intent | lands |
| 4 | after the branch CAS | branch moved, `landed` missing | appends `landed` (repair) | already landed |
| 5 | before the `landed` append | branch moved, `landed` missing | appends `landed` (repair) | already landed |
| 6 | after the `landed` append | complete | no-op | already landed |

In every window `inspect_wal().divergences` is empty: no crash can put a broker
landing on the branch that the WAL has no record of. That is the property the
write-ahead order buys, and it is asserted in each of the six.

The de-ordered control (`test_inv7_de_ordered_protocol_leaves_an_unrecorded_
landing_that_is_detected`) is the unsafe half of the pair, in the style
ROUND-A.md tabulates: it moves the branch before the intent exists and then
dies, reproducing the Round A shape. `inspect_wal` reports exactly
`["unrecorded_landing"]`, `recover()` raises `WalDivergedError`, and the
refusal is itself recorded as a WAL event. Force-disabling the write-ahead
order in the shipped code (mutation M4) turns the six-window fixture red.

## Findings disposition

| ID | Finding | Disposition |
| --- | --- | --- |
| F1 | Two-ref transaction is not crash-atomic | Closed. Write-ahead protocol above; `update_refs_atomically` deleted; six crash windows fixtured with in-subprocess kills. |
| F2 | INV7 fixture kills only at Python checkpoints | Closed. `tests/conftest.py` ships the PATH-shim kill wrapper; the fixture asserts `-SIGKILL` and covers every window of the new protocol. |
| F3 | `inspect_wal`/`recover` blind to branch-vs-WAL divergence | Closed. `_divergences` reports `target_missing`, `landed_not_on_target`, `refused_candidate_on_target` and `unrecorded_landing`; `recover()` records a `refused` event (`branch_diverged`) and raises `WalDivergedError`; `onetree wal` exits 1 on divergence. |
| F4 | `resolve_ref` reads a corrupt ref as absent | Closed. `resolve_ref` is three-way — oid, absent, or `RefCorruptError` — discriminated by a second `rev-parse` probe rather than by matching git's message text. `Wal.head()` translates it to `WalCorruptError`, so every read path surfaces corruption; `recover()` refuses loudly. Fixtured over four corrupt shapes: blob, tree, dangling oid, unreadable ref file. |
| F5 | Landing-moment `_session_alive` sub-term is mutation-dead | Closed. `test_inv2_session_death_between_audit_and_fence_refuses` kills the session between the audit and the fence and asserts the `session_died_at_fence` refusal; the in-suite unsafe control shows the dead session's payload landing when the sub-term is reduced to expiry. Mutation M1 is red. ROUND-A.md line 100 corrected. |
| F6 | Temp-index residue leaks unbounded | Closed. Temp indexes carry an owner tag (`index-<pid>-<start>-<12hex>`); `inspect_wal` reports residue; `gc` sweeps only residue whose owning incarnation is gone, recording the swept names in the WAL event before unlinking. |
| F7 | `.git`-internal paths accepted as claim targets | Closed. Typed `InvalidPathError` with `reason="git_internal"`, via two independent arms — containment in the resolved git dir, and a `.git` path component. |
| F8 | Liveness degrades silently without `/proc` | Documented and surfaced. Registration emits a `degraded_liveness` warning in `SessionInfo.warnings`, the `session_register` event, and both CLI output modes. See the limitation note below. |
| — | `update_ref` CAS-lost vs real-error discrimination | Closed, and it was wrong in a second way: see "Beyond the brief". |
| — | `_verify_landed`'s three raise arms | Closed. Parametrized over `missing_commit`, `wrong_parent`, `wrong_tree`. The missing-commit arm was previously unreachable and now runs through `require_commit`. |
| — | `update_refs_atomically`'s genuine-failure raise | Not applicable: the function is deleted rather than tested. |

## Beyond the brief

Three things surfaced that the brief did not have. The first two are defects in
code this round introduced, found by re-running the F1 reproduction against the
shipped protocol rather than by reading it.

**Transient lock contention was a hard failure.** Round A's `update_ref`
treated any non-zero `update-ref` exit whose expected value had not moved as a
genuine git failure. During a real race the loser can instead be told the ref
is locked for a few milliseconds. `update_ref` now separates three outcomes:
the expected value moved (a lost race, `False`, caller re-audits), a lock is
held (bounded retry, then `RefLockedError`), or git failed for its own reasons
(`GitError`, never retried).

**A crash inside a branch update can leave `HEAD.lock`, not the branch lock.**
Moving the branch `HEAD` points at also writes the HEAD reflog, so git takes
`HEAD.lock` alongside `refs/heads/<name>.lock`. A `SIGKILL` between them leaves
either one. Reporting only the branch's own lock sent an operator to a file
that was not there, and the retry afterwards failed with an unattributed
`GitError` naming a lock nothing had reported — measured, in exactly that
order, on the strace-injected reproduction. `Repository.ref_lock_paths` now
enumerates both (re-reading `HEAD` each call, because a checkout moves the
association), `update_ref` discriminates against the whole set, and
`inspect_wal().ref_locks` reports it.

**Residue reached JSON but not the human line.** `onetree wal` printed an
unqualified event count while holding a stale lock that would refuse the next
land. The human line now names held locks and temp-index residue. It does not
fold them into `consistent`: a ref lock is held for milliseconds during every
healthy append, so a repo with a live peer writing would fail an all-clear
check that means something else.

## Known limitations

**WAL events are unauthenticated this round.** Any process that can write to
the repository can append a well-formed event, and a forged `gc` event can
evict live claims. This is by design for Gate 1 — event signing is a later
hook — and it bounds what the WAL proves: it is a record of what the broker
did, not proof that only the broker wrote it. The signing hook is where this
closes; until then, WAL provenance assumes a trusted local repository.

**A stale ref lock is reported, never removed.** A lock file names no owner, so
it cannot be liveness-checked the way temp-index residue can. Deleting one that
a live peer holds would corrupt exactly the update the lock protects. Recovery
therefore refuses with `RefLockedError` naming the lock path and leaves the file
in place; removing it is a deliberate operator act. The cost is that a crash
requires one manual step before recovery can proceed, and that is the intended
trade.

**Liveness without `/proc` degrades to bare PID existence (F8).** A session
records its PID and, when the kernel exposes one, that PID's birth tick. Where
no birth tick is available the pair collapses to the PID alone, and a reused
PID keeps a dead session looking alive — fencing peers and evading `gc` until
its TTL expires. This is not reachable through the honest CLI on Linux; it is
reachable on a `/proc`-less kernel. Registration emits the `degraded_liveness`
warning so the condition is visible at the moment it starts mattering, and TTL
remains the backstop.

## Corrections to ROUND-A.md

- The "One atomic visibility boundary" section described the two-ref
  transaction as the visibility boundary. Rewritten to the shipped write-ahead
  protocol.
- The INV2 row claimed a dead-session fence fixture. Round A's dead-session
  test refused at the top-of-loop audit, not in the audit-to-fence window; the
  claimed fixture did not exist. Row corrected, and the fixture now does.
- The INV7 row described kills at four Python checkpoints. Row corrected to the
  six in-subprocess crash windows.
- "WAL representation and recovery" asserted that Round A landing could not
  expose an unresolved intent "because of the two-ref transaction". The premise
  was false and the conclusion is now the opposite: an exposed intent is a
  normal, expected crash outcome that recovery completes or refuses.
- The "Local verification" counts are refreshed to this round's measurements.

## Local verification

Python 3.14.4, Git 2.53.0, on 2026-08-17. `ruff check src tests`, `mypy` and
`pytest` are green from the repository root: 92 tests in 15.46 seconds with
93.46% branch coverage against a 90% floor. Round A's baseline at `dc8f82e`
was 53 tests and 91.01%. These are local observations, not broader performance
claims.

Guards are evidence only where a known-bad input was observed failing against
them, so each guard this round adds is also run against a copy of the tree with
that guard force-disabled. Seventeen such mutations are recorded, each red on
the fixture named for it: the landing-moment liveness sub-term (M1), the
absent-versus-corrupt ref discrimination (M2), divergence detection (M3), the
write-ahead order itself (M4), each git-internal arm separately (M5a, M5b), the
temp-index liveness check (M6), lock-versus-failure discrimination (M7), the
landed-commit resolution check (M8), the degraded-liveness warning (M9), the
recovery lock refusal (M10), landed-versus-intent field agreement (M11), the
claim-expiry fence sample (M12), the landed payload shape check (M13), and the
three arms of the `HEAD.lock` window — enumeration, discrimination and
reporting (M14a, M14b, M14c).

Two of those deserve their history recorded, because they were green first. A
single mutation disabling one git-internal arm survived: the two arms are
redundant for an in-tree `.git`, and only a gitlink plus a
`--separate-git-dir` repository separates them. And the field-agreement check
that replaced adjacency had no fixture at all when it was written. Both are
cases where the guard existed and the evidence did not.
