# Explicit workspace reconciliation (task 151)

This round implements the task 151 operator contract. It does not authorize
reconciling a production workspace or turn ordinary retries into reconciliation.
The original expiry cases on published `bdbb53d` still produce
`workspace_changed` with an unchanged branch. Their latest execution is retained
in `/anvil/scratch/onetree-g2-closure-20260916/reconciliation-residual.txt`.

## Contract

`reconcile_workspace(actor, expected_epoch=..., reason=...)` accepts the
current observed workspace only when the caller names the current protection
event's `event_id`, supplies a nonblank reason, and there are no active claims
or pending intents. `onetree reconcile --expected-epoch ID --reason TEXT`
exposes the same operation with ordinary repository/session selection and JSON
output. The epoch id comes from the latest `workspace_protect` or
`workspace_reconcile` event in `onetree wal --json`.

The operation appends a versioned `workspace_reconcile` event containing the
prior epoch id, operator reason, observed prior violation and new baseline.
The original epoch, refusal and landing records remain immutable and reachable
through the WAL. Historical landing verification and repository-wide divergence
checks remain in force; reconciliation cannot adopt an unrecorded landing or
erase a missing recorded landing. A healthy workspace may also be explicitly
reconciled, recording a null prior violation.

Replay requires the exact prior epoch and an idle claim/intent state. Expired
or dead-session claim residue may be collected in a preceding ordinary `gc`
event in the same WAL append, after the writer establishes no active claims.
No private index, work file or residue file is deleted by this operation.

The replacement observation must name the current attached branch and commit,
enabled files-backend reflogs, and readable referee configuration. Reflog files
must be regular files and contain complete valid transition identities when
nonempty. Their reviewed prefix and the observed branch tip are separate
anchors: expiry can remove a current-tip entry with an old date while keeping
older transitions with newer dates. Empty regular reflogs are valid after
expiry and are recorded with their empty digest; missing or malformed logs
refuse. Initial protection retains its existing nonempty-log requirement.
The writer checks the observation and audit heads again before the WAL CAS;
later unexplained changes still cause quarantine. This is not an exclusion
lock against unrestricted same-user Git.

Only the coordination WAL ref advances. Branch refs, reflogs, the shared index
and work files are observed, not repaired. A new epoch does not authenticate
working bytes or grant permission to discard peer work. Operator identity has
the existing CLI actor semantics, not a new operating-system authority boundary.

## Verification requirements

Before/after evidence must distinguish the missing workflow from its desired
behavior. Refusal cases cover an absent or stale epoch, missing reason, active
claims, pending intent, malformed observation and separate WAL/branch
divergence. Replay must independently reject malformed records and a replacement
that does not name its predecessor. Valid controls include ordinary expiry,
empty-log expiry, explicitly accepted external movement, repeated epochs and
fresh protected landings. Tests inspect preserved history and unchanged branch,
index and work-file evidence, not just a successful return value.

## Author evidence and review state

The initial interface checks failed on the unchanged baseline: the API and
CLI command did not exist, and the old WAL writer did not recognize the new
kind (`before.txt`). Those failures establish a missing workflow, not that
each new guard once accepted a bad request. The pre-existing expiry fixture
independently establishes the operational quarantine this workflow addresses.

An early implementation incorrectly required the last retained reflog entry
to equal the current tip. Actual ordinary expiry of an old-dated current-tip
entry left earlier entries with newer dates while leaving the branch unchanged;
the new `routine_expiry` case then failed with `reflog does not end at the
observed tip` (`routine-expiry-before.txt`). The correction binds the observed
tip separately from the reviewed log prefix. The final case reconciles that
state, preserves the old evidence and branch/files, then lands fresh work.
Both completely empty and partially retained logs are covered. No guard was
disabled to obtain these inputs.

The final focused reconciliation module passes 31 cases (`focused-r3.txt`).
Before adding the ordinary-expiry and extra observation cases, the broader
workspace, WAL, defensive and recovery run passed 324 cases (`focused-r2.txt`).
The first broader run's failure was a test typo (`WalReport.pending` instead
of `pending_intents`); it is not a production finding. Lint and strict typing
pass. Complete local CI and independent review are still required before
publication; these focused runs do not substitute for either.

The fixtures import this checkout through pytest's configured source path and
use `tests/conftest.py`'s real Git repositories and coordination-only gate.
The CLI checks use the real parser and coordinator. Clock advancement supplies
an expired-lease input; interleave wrappers perform real competing claim/epoch
writes or external commits before the original operation continues. Refusal
checks verify their specific type/reason and absence of the requested WAL
append. Replay tests separately supply malformed event data, and valid controls
replay the accepted record. No production workspace is reconciled in these runs.

Evidence is retained in `/anvil/scratch/onetree-reconcile-20260916/`, including
the initial failures, corrected runs, installation and tool logs. Receipts over
log files prove the files read; they do not convert a failed test into a pass.
