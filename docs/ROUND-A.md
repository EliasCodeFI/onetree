# Gate 1, Round A implementation record

Round A implements the session/index, claim, WAL, payload-audit, and landing
core described by `docs/DESIGN.md`. It does not implement the Round B train
referee, branch-operation guards, or rejection-repair contract.

## Decisions where the design left freedom

### One atomic visibility boundary

A landing builds two adjacent WAL commits, `land_intent` followed by `landed`,
without moving a ref. Git then performs one `update-ref --stdin` transaction
that compares and swaps both the target branch and `refs/onetree/wal`. A
successful transaction exposes the candidate commit and both events together;
a failed or killed transaction exposes none of them. The WAL CAS also acts as
the claim fence: any release, reclamation, registration change, or unrelated
WAL append invalidates the prepared transaction and forces a fresh audit.

Claim and session expiry/liveness are sampled again immediately before that
transaction. There is necessarily no atomic primitive spanning a Git ref lock
and the operating system's process table or wall clock; “landing moment” in
Round A therefore means this final sample followed by the Git transaction.

### Exact payload replay on a moved head

A session registration records the target commit from which its private index
was initialized. Landing compares that base tree with the session index tree,
producing mode/blob transitions for every changed path. On a moved target,
landing requires:

1. the session base is still an ancestor of the target;
2. every payload path still has its audited base entry; and
3. applying only those transitions produces exactly the expected target-to-
   candidate tree delta.

The candidate is then committed with the current target as its parent. A
same-path target change refuses as `payload_conflict`; disjoint intervening
changes survive. Thus retrying does not re-parent a stale full tree that would
erase another landing.

### Session identity and leases

Session ids are explicit `--session` values or one unambiguous value from
`ONETREE_SESSION_ID`, `CODEX_THREAD_ID`, and `CLAUDE_SESSION_ID`. They are
restricted to path-safe identifiers because the private index lives at
`.git/onetree/sessions/<actor>/index`. Existing indexes must be regular,
single-link files below a non-symlink session directory, preventing an index
from aliasing the shared index or another file.

Registration records a long-lived PID, its Linux `/proc` birth tick when the
kernel exposes one, an incarnation id, and an expiry. The CLI defaults the PID
to its parent and also accepts `--pid` for harnesses that can identify their
durable body. A claim carries both its own generation and the session
incarnation, so release/re-register/reclaim cannot create an ABA-valid fence.

### Claim paths

Claim and check inputs must be absolute, resolve to an existing filesystem
entry, and resolve inside the repository. Stored paths are canonical
repository-relative POSIX paths. Directory claims cover descendants, and a
batch claim is all-or-nothing. A conflict reports the current actor id in both
the typed exception and JSON output. A deleted claimed path remains releasable
through `release` with no path arguments.

This deliberately follows INV6 even though it means a new file is claimed via
an existing parent directory before creation.

### WAL representation and recovery

Each WAL commit has zero parents for the root or exactly one parent thereafter.
Its tree contains exactly one regular blob named `event.json`. The JSON common
envelope and event-specific state fields are validated during replay; duplicate
event, session, claim, and landing-operation identities refuse replay.
WAL updates request a reflog explicitly, so prior ref heads remain locally
visible even though custom refs do not receive a reflog from Git by default.

Normal Round A landing cannot expose an unresolved intent because of the
two-ref transaction. `wal --recover` also handles an intent exposed by an
older implementation or manual fault injection: if its immutable candidate is
already on the target's ancestry, recovery appends `landed`; otherwise it
appends `refused`. Intervening WAL events do not prevent repair.

Session index files and short-lived temporary indexes are Git metadata. All
coordination history is in `refs/onetree/wal`; no coordination state file is
written into the working tree. Object writes from an aborted attempt may be
unreachable until ordinary Git garbage collection, but no ref makes them live
coordination state.

## Red-first receipts for INV1–INV7

Every invariant fixture contains an unsafe Git control as well as the guarded
refusal and allow/recovery paths. The test modules were first run before the
core modules existed and failed collection with `ModuleNotFoundError:
onetree.core`; the controls below then reproduced each substantive unsafe
state against real isolated repositories.

| Invariant | Known-bad state observed before/without the guard | Guarded refusal and allow evidence |
| --- | --- | --- |
| INV1 | Raw `write-tree` / `commit-tree` / `update-ref` landed `a.txt` and `b.txt` while only `a.txt` was claimed. | `test_inv1_unclaimed_payload_refuses_whole_land_then_claimed_payload_lands` refuses `b.txt`, leaves the branch unchanged, then lands both after the second claim. |
| INV2 | A target-only CAS still succeeded after the claim was released. | `test_inv2_release_between_audit_and_cas_breaks_fence_then_reclaim_allows` moves the WAL during the final checkpoint, invalidates the prepared transaction, and lands only after reclaim. The dead-session and expiry-at-fence fixtures separately refuse those fence losses. |
| INV3 | Two real processes ran one-shot CAS updates from one expected head: one returned 0, one returned 128, and only one payload remained. | `test_inv3_real_race_has_one_first_attempt_winner_and_both_payloads_land` starts two broker processes at the same transaction barrier. One result reports one attempt, the loser reports at least two, both commits are ancestors of the final head, and both files survive. |
| INV4 | Raw plumbing forced `refs/onetree/wal` to a merge-shaped event commit. | `test_inv4_malformed_non_linear_wal_refuses_and_valid_append_is_linear` observes replay refusal, restores the prior immutable event, appends normally, and verifies the exact parent chain and original blob bytes. |
| INV5 | Naively re-parenting a stale session tree made the candidate diff contain both the intended `a.txt` change and a foreign `b.txt` reversion. | `test_inv5_stale_full_tree_has_foreign_hunk_but_overlay_is_exact_and_conflict_refuses` lands an overlay whose diff is only `a.txt`, preserves `b.txt`, and separately refuses an intervening same-path change. |
| INV6 | Resolving a relative nonexistent path manufactured an absolute-looking key with an empty holder/warning result—the measured reference defect. | `test_inv6_invalid_paths_refuse_and_conflicts_name_holder` gets typed `relative`, `nonexistent`, and `outside_repository` errors; an absolute existing path is allowed, and a live conflict names `alpha`. |
| INV7 | A split target/WAL sequence exposed a moved branch with no `landed` event. | `test_inv7_kill_at_each_land_phase_recovers_to_consistent_refs` sends real `SIGKILL` after audit, after tree construction, before the transaction, and after it. Replay always sees both refs moved or neither, reports no pending intent, and an eventual retry contains the payload. Exposed-intent fixtures also exercise both repair outcomes. |

## CLI surface

Round A provides `session`, `claim`, `release`, `check`, `land`, `wal`, and
`gc`, plus the existing `status`. Every subcommand accepts `--json`; successful
and refused JSON invocations write exactly one object and nothing else to
stdout. `check` exits 1 when another live actor holds the path, while typed
input/operational errors exit 2.

The private index path returned by `session --json` is supplied to ordinary
Git staging through `GIT_INDEX_FILE`. The broker itself invokes only plumbing
against that private index or a short-lived index below `.git/onetree/tmp`.

## Local verification

The final pre-handoff suite on 2026-08-17 used Python 3.14.4 and Git 2.53.0.
It completed 53 tests in 7.19 seconds with 91.01% branch coverage. The required
`ruff check src tests`, `mypy`, and `pytest` gates were green. These are local
observations, not broader performance claims.
