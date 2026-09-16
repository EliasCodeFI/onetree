# Gate 2: existing workspace and referee controls

This evidence reconciliation covers tasks 13–15 under the accepted
`ROUND-G2.md` battery brief. Its runtime baseline is published commit
`c32dfe99a26a38df77c487357618da3902a5737a`. It does not change the runtime,
authorize workspace reconciliation, or close Gate 2.

## Original failures and current checks

The original evidence directory is now
`/anvil/claude-diag-806b39cf/onetree-gate-evidence/`. The historical
`/home/anvil` pathname in those logs describes where they were produced;
it is not the current recovery path. The following artifacts were opened
again for this reconciliation, rather than inferred from source comments.

| Family | Original observed failure | Current evidence carrier and scope |
| --- | --- | --- |
| 13: bare Git bypass | `round-b-live-observation.md` records the authorized disposable probe at `3105b1a`: bare commit `542e0629` succeeded, and `round-b-live-wal-after-bare.json` reports a consistent WAL with no divergences. The same probe's reset report also reports no divergence after peer work was erased. | `test_unexplained_git_operation_quarantines_later_landing` executes bare commit, switch, checkout, same-tip reset, stash, log disabling, prefix erasure and referee changes. It checks `workspace_changed`, unchanged post-operation tip, and the WAL refusal/divergence. This is detection before a later broker landing, not prevention of the external Git command. |
| 14: recorded target movement | `referee-red-first.txt` shows `test_referee_retests_candidate_after_wal_and_target_race` failing with `DID NOT RAISE LandingRefusedError` when the initial implementation accepted the failing combination. | The current case interleaves beta's actual broker landing at alpha's `before_intent` checkpoint. Alpha then refuses as `referee_red`; the branch contains beta's bytes and the original alpha path. This is a deterministic interleave, not proof of independently scheduled competing processes; task 17 carries that composition. |
| 14: unexplained protected-workspace movement | The bare-commit and switch/reset probe above establishes the original external-operation gap. | The workspace cases refuse external changes. `test_reset_inside_the_fence_window_is_refused_at_the_fence` places a same-tip reset at `before_intent` and verifies `workspace_changed`, the held payload path and unchanged branch. It also verifies the erased peer bytes: refusal does not repair them. A same-tip reset is not evidence of a changed target OID during an executing referee. |
| 15: changing referee outcome | `referee-flaky-race-red.txt` shows `DID NOT RAISE LandingRefusedError` for the controlled first-red/later-green command after a WAL race; `referee-flaky-race-fixed.txt` preserves its subsequent pass. | `test_red_verdict_is_not_retried_to_green_after_wal_race` records an actual command execution counter, injects another registration only after the real referee refuses, and checks one execution, `referee_red`, unchanged branch and a refusal WAL event. This is a controlled changing outcome, not a statistical flakiness classifier. |

The initial workspace API tests failed because protection did not exist.
Their missing-method failures are not operation-level proof; the disposable
Git probe and its JSON reports supply that evidence. The current checks were
not made red by disabling production guards.

## Positive controls and retained boundaries

`test_broker_transitions_remain_allowed` lands disjoint alpha and beta
submissions under protection and verifies consistent WAL observations.
`test_new_registration_cannot_erase_workspace_violation`,
`test_second_workspace_protect_cannot_replace_a_violated_baseline` and
`test_recovery_does_not_report_all_clear_for_workspace_violation` retain the
quarantine through registration, attempted baseline replacement and recovery.
The workspace guard therefore has both allowed and refused populations.

The task 15 fixture wraps the real `referee.run` only to place the WAL write
after its actual refusal; it does not replace the verdict. Ordinary passing,
red, timeout and malformed-receipt cases in `test_referee.py` distinguish
the result classes. Only recording a refusal may retry after a WAL race;
the failed candidate does not execute again within that submission.

Unrestricted same-user Git can still erase working files without moving a
ref. The four `test_path_form_checkouts_erase_peer_work_without_a_reflog_transition`
cases retain this adverse outcome for path checkout, restore, read-tree and
checkout-index. Their passing assertions describe the gap, not protection.
Routine reflog expiry can also quarantine an unmoved workspace; both cases in
`test_reflog_expiry_quarantines_an_unmoved_workspace` remain explicit.
Operator reconciliation remains separately tracked by task 151. No task in
this tranche grants authority to reconcile a production baseline or silently
refresh its evidence.

## Input provenance and validation

The current checks execute the published runtime without source alterations.
Pytest resolves this checkout's `src` through its configured `pythonpath`;
`tests/conftest.py` supplies `GitFixture` and the admitted coordination-only
payload-gate fixture. `test_workspace_guard.py` imports referee-command and
configuration helpers from `test_referee.py`, which invokes the selected
interpreter with `-I -S` for the actual configured commands. These focused
checks do not establish admission of the real estate payload gate.

Current output and the exact validation command are retained in
`/anvil/scratch/onetree-g2-controls-20260916/`. Historical red logs remain in
their original evidence directory; no fresh pre-implementation run is claimed.
The current run passed all 115 checks in the two modules, with coverage
disabled for this focused run. It is not a replacement for the publication's
full local CI. The log receipt is
`receipt:/anvil/scratch/onetree-g2-controls-20260916/receipts.jsonl:284484648e6b`.
The original changing-outcome and candidate-retest failure logs were retained
by receipts `659f350caef7` and `4c5b8284d0a3` in that same receipt journal;
successful reads do not make their original failures green.

Task 14 remains open for a target OID changing during actual configured
referee execution in a protected workspace. The existing same-tip reset and
pre-submission bare commit checks do not establish that exact sequence.
Tasks 13 and 15 are ready for review of the bounded evidence dispositions
above; neither they nor Gate 2 are marked complete by this document.
