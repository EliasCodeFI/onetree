# Task 114: retain terminal payload-gate refusals

A payload gate can return a trusted REFUSE or DEGRADED verdict after an
unrelated session registration advances the WAL. Previously the stale refusal
append lost its compare-and-swap and restarted the entire landing, including the
gate. On main 16e65737, a real registration during every gate invocation produced
32 executions and `GitError: landing CAS retry limit exceeded` for either verdict.
The target did not move; the terminal gate evidence was lost.

The gate refusal now uses the existing terminal-document append path. It holds
one operation, candidate receipt, and refusal while recording against fresh WAL
snapshots. Recording contention cannot restart the candidate audit. Successful
recording preserves the complete receipt in the WAL before raising the original
`LandingRefusedError`.

If the recording itself raises a typed OneTree error, the caller still receives
the original refusal, with a `recording_error` payload containing that failure.
The JSON CLI carries both; the human CLI explicitly prints the recording error.
This is a refusal with unsuccessful recording, not a claim that evidence was
persisted. The target stays unchanged. Operational errors that occur before a
trusted gate verdict retain their existing behavior.

## Evidence and provenance

`test_gate_refusal_survives_a_real_wal_advance_without_reauditing` crosses REFUSE
and DEGRADED with an ordinary registration alone or registration plus a real WAL
ref lock, through the API, JSON CLI, and human CLI. Each gate process imports the
actual checkout's `onetree.core.Coordinator` using the absolute source directory
resolved from `onetree.git.__file__`; it registers a peer and emits a complete
candidate-bound gate report. The existing `_register_and_stage` helper admits the
fixture gate's actual source digest. `GitFixture` supplies real Git objects and
refs, and `_json_call` verifies one JSON object and empty stderr. No production
refusal predicate or CAS result is stubbed.

Assertions require one gate invocation, an unchanged target, no landing intent
or landed event, and the peer registration retained. Successful recording must
retain the exact gate source digest, candidate, paths, findings, degraded rows,
advisories and hook state. The lock case must have no refusal row and must expose
`ref_locked` with the WAL reference to API/JSON callers and human stderr.
Existing CLEAN receipt and successful retry tests supply allowed controls.

The first lock runs failed with gate stderr: inspection of the child log showed
two invocations, with the second registration hitting the lock left by the first
invocation. This was another consequence of restarting the gate. Early assertions
used incorrect event/field names (`session` and `ref`); they were corrected to the
observed schema (`session_register` and `reference`). Human CLI cases then failed
on missing recording-error text before that output was added.

Logs are under `/anvil/scratch/onetree-gate-refusal-20260916/`. This round does not
claim arbitrary continuous-contention progress, an exhaustive recording-failure
population, or a hostile same-user sandbox. The append helper's existing bounded
retry policy remains unchanged; its exhaustion is not separately forced here.
