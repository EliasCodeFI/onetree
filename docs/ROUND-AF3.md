# Gate 1 Round AF3: recovery across ref backends

Round AF3 starts from `0b2073b`. Its scope is the recovery surface measured by
the two AF2 verification seats and re-run by the session lead: backend-correct
ref inspection, a branch audit that does not brick an honestly growing
repository, and recovery that never lets a live landing mask a divergence.

## Mechanism

**The ref backend is explicit.** `Repository` records Git's
`--show-ref-format` result and supports `files` and `reftable`. Files refs are
classified through their loose path and `packed-refs`. Reftable refs are
classified with the backend-aware `show-ref --exists` result, while
`tables.list` and every table it names provide the failure reason. This also
refuses readable-but-malformed tables instead of equating readability with
absence. Reftable's single `reftable/tables.list.lock` is the lock returned for
every ref, so lock reporting and `update_ref` discrimination follow storage Git
actually uses. An unborn reftable branch no longer touches the `refs/heads`
compatibility stub.

Storage is observed after two failed `rev-parse` probes, so it is not an atomic
snapshot with them. When storage says a ref is present or unreadable,
`resolve_ref` repeats both probes and the storage observation before declaring
corruption. A peer that creates the ref in that window is resolved normally.

**The scan limit is a page size.** Divergence inspection resolves the target
once, then reads `marked_commits` pages from that OID until the WAL-derived
floor is reached. Each Git result remains bounded by
`DIVERGENCE_SCAN_LIMIT`; crossing one page is no longer a `scan_truncated`
divergence. The floor retains the first observed position and its immediate
parents, closing the measured one-hand-commit mask. Missing parents in a
shallow clone still degrade to no exclusion, so all locally visible history is
audited.

**Audit and liveness are independent.** Recovery still leaves a live lander's
intent untouched, but it verifies recorded landings and computes divergences
before returning the pending result. A divergence raises `WalDivergedError`
even when `pending` is non-zero. Recovery JSON is `ok=false` whenever recovery
leaves inconsistent state and always carries a `divergences` array; a divergent
error carries the same recovery measurements and the divergence records. The
human refusal names both the branch/WAL disagreement and the number of landings
still in flight.

Land intents persist the same `degraded_liveness` warning sessions already
used when a process birth tick is unavailable. `LandResult` and both CLI output
modes surface that warning.

## Findings disposition

| ID | Severity | Disposition | Guard |
| --- | --- | --- | --- |
| D1 | critical | **Closed.** Both ref backends distinguish absent from unreadable or malformed, reftable unborn refs return absent, and reftable's global lock participates in reporting and update refusal. | `test_both_ref_backends_refuse_unreadable_wal_storage_and_report_their_lock`; `test_readable_but_malformed_reftable_storage_is_not_read_as_absent`; immutable `v_f1_reftable.py` |
| D2 | major | **Closed.** Honest history beyond 4,096 commits remains consistent; a real unrecorded landing below page one is still found. No `scan_truncated` state is produced, so recovery cannot be trapped in it. | `test_honest_history_past_the_scan_page_never_becomes_inconsistent`; `test_an_unrecorded_landing_beyond_one_scan_page_is_still_reported`; immutable `v_f2_scancap.py` |
| D3 | major | **Closed.** Live intent detection defers mutation only. Recorded-landing verification and divergence audit always run, and CLI JSON cannot say `ok=true` over the divergent state. | `test_a_live_landing_defers_repair_but_never_defers_the_divergence_audit` |

## Verification-seat leads

| Lead | Outcome |
| --- | --- |
| (a) ref created between probes and `lstat` | **Confirmed and closed.** The deterministic fixture creates the ref at the storage-observation seam; AF2 raises `RefCorruptError`, AF3's fresh probes return the commit. Guard: `test_a_ref_created_after_both_probes_is_retried_not_called_corrupt`. |
| (b) lander birth tick unpinned; degradation silent | **Confirmed and closed.** `test_lander_birth_tick_is_part_of_the_in_flight_bar` contains a bare-PID counterfactual that changes the measured recovery result. `test_a_degraded_lander_surfaces_the_same_warning_as_a_degraded_session` pins persistence and surfacing. |
| (c) immediate parent excluded by the floor | **Confirmed and closed.** One permitted hand commit no longer masks the crash-left broker commit. Guard: `test_the_commit_immediately_below_first_sight_stays_inside_the_audit`. |
| (d) four `GitError` append-refusal holes | **Confirmed and closed.** `_try_append` translates validation failures from target, expected head, candidate, and tree into `WalAppendRefusedError`; it still writes nothing. Guard: `test_all_four_git_validators_refuse_an_unreplayable_append_at_the_wal_door`. |
| (e) pruned landed commit has no recovery path | **Confirmed, with the recoverable boundary made explicit.** The WAL does not contain the missing commit object and cannot reconstruct it. The typed error now names restore/fetch and includes the commit OID; the fixture restores the exact object and proves the normal divergence audit resumes. Guard: `test_a_pruned_landed_object_names_the_restore_path_before_recovery_can_audit`. |
| (f) comparison raises `KeyError` | **Confirmed and closed.** The comparison now uses `.get`, so a missing result key produces the assertion diff instead of aborting diagnostic construction. |

## Red-first evidence

The AF3 fixtures were run against `0b2073b` before implementation. The
reftable/unborn, creation-race, beyond-page, 4,100-honest-commit,
below-first-sight, in-flight-divergence, degraded-lander, and four typed-append
cases all failed in their known-bad shapes. The birth-tick behavior already
existed but had no guard; its fixture therefore includes and observes the
bare-PID counterfactual. The pruned-object probe raised the same
`WalCorruptError` forever before the restore path was named.

The files-backend arm is the D1 control and continued to refuse unreadable
storage. Existing AF2 fixtures continue to cover fold/CAS snapshot fidelity,
the live landing/recovery window, field-for-field landed agreement, batched
recovery, measured counters, exit codes, the D7 assertion sweep,
`marked_commits` truncation arithmetic, and shallow-floor degradation.

## Decisions where the brief left freedom

1. Reftable is supported rather than refused. This keeps repositories created
   by the tested Git version usable while making storage and lock behavior
   backend-specific.
2. The 4,096 bound limits one subprocess result, not repository age. The target
   is pinned to an OID before pagination so concurrent branch movement cannot
   reorder page boundaries.
3. A real divergence retains recovery's documented exit 2 even when a landing
   is live. Pending-only recovery retains exit 1, but JSON says `ok=false`
   because the requested consistency outcome has not been achieved.
4. Missing landed objects are not fabricated or silently retired. Exact object
   restoration is the only sound local transition because the WAL does not
   carry enough bytes to reproduce the commit.

## Immutable repro disposition

- `v_f1_reftable.py` is unchanged and now refuses all four reftable read paths,
  matching its files control.
- `v_f2_scancap.py` is unchanged and all three arms now report consistent,
  including register-before with 4,100 ordinary commits.
- `v_f3_inflight_mask.py` is unchanged, but its only divergence was the D2
  false `scan_truncated` state. Closing D2 necessarily removes that precondition:
  its after-row is pending-only, with `ok=false` and an empty `divergences`
  array. The independent `landed_not_on_target` fixture above is the valid D3
  measurement and produces exit 2, `ok=false`, `pending=1`, and the divergence.

## Corrections to Round AF2

`ROUND-AF2.md` now states that storage classification is backend-specific,
that the scan limit is a page boundary rather than a permanent divergence, and
that the immediate parent of first sight remains inside the audit. These replace
AF2's files-only storage claim, its false "anything undecidable is unreadable"
invariant, and its framing of `scan_truncated` as merely a reported blind spot.

## Local gates

| Gate | Result |
| --- | --- |
| `ruff check src tests` | green |
| `mypy` | green; 7 source files |
| `pytest` | 134 passed in 39.20s; 92.06% branch coverage |
