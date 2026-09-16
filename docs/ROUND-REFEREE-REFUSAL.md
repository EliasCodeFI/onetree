# Tasks 181/182: retain referee refusals when recording fails

The task 114 review identified an unverified asymmetry: payload-gate verdicts
survive a failure to record their refusal, while referee verdicts still appeared
replaceable by the recording error. A real-child fixture reproduced that on main
d3c5fa5. A referee that placed the WAL ref lock and then returned red or timed out
lost its LandingRefusedError to RefLockedError; JSON returned exit 2 and human
stderr reported only the lock. Six locked cases failed before this change, while
the six corresponding unlocked controls passed.

The referee path now retains its original terminal refusal and receipt, with a
structured recording_error when the terminal WAL append raises a typed OneTree
error. This matches the published payload-gate contract. The existing CLI
serializes both failures in JSON and names the recording error in human mode.
An unsuccessful append is not represented as durable evidence. Branch movement
and landing admission do not occur after the refusal.

The private _record_land_refusal helper no longer carries the unused gate_receipt
and referee_receipt parameters. Both external-audit receipt paths already use
terminal documents directly. This cleanup does not change the ordinary refusal
schema or its callers.

## Evidence and inputs

`tests/test_referee_refusal.py` crosses red/timeout with locked/unlocked recording
and API/JSON/human callers. `GitFixture`, `test_referee.configure` and `staged`
provide the real repository, command configuration and claimed index. The real
referee child appends a line to a marker and optionally touches the actual WAL
lock path resolved by Repository.ref_lock_path. It then exits 7 or sleeps until
the configured supervisor timeout. No referee return value or append outcome is
stubbed. The marker must contain exactly one invocation.

Assertions require the original typed refusal/exit status, candidate-bound
receipt, tested candidate bytes, unchanged target, no landing intent or landed
record, and a consistent WAL. Locked recording leaves the original WAL head and
no refusal record, and exposes ref_locked with its reference. Unlocked recording
retains exactly the same referee receipt as the caller receives.

Existing referee, payload receipt, and ordinary refusal tests remain the
regression controls. The new cases do not exhaust every typed append error or
force the terminal append retry limit. Logs are retained under
`/anvil/scratch/onetree-referee-refusal-20260916/`; full-suite and independent
review results are recorded separately when complete.
