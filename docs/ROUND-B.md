# Round B: referee and protected workspace

This round implements the owner-approved Round B contract and the localization
amendment in `ROUND-B-AMENDMENT.md`. The estate activation authorization is
decision `5635aa0a5469`. Full estate cutover remains gated on the final review,
CI, Gate 2 battery and live parent fleet receipts.

## Referee contract

`onetree.referee` is an owner-configured command, parsed as quoted arguments
without a shell. A shell pipeline requires an explicitly named shell in that
command. Its executable and external dependencies are trusted operator inputs;
this is a test runner, not a sandbox for hostile commands. A missing setting
is explicitly reported as `not_configured`, meaning structural checks only.
Duplicate or empty settings and invalid timeouts refuse landing.

The referee runs from a temporary checkout of the immutable candidate, with
the exact candidate and baseline commit IDs in its receipt. The shared working
tree, private session index and unrelated unstaged peer changes are not its
test inputs. The checkout includes tracked files and local Git object access;
untracked build dependencies are not copied. Configure an absolute executable
when the required interpreter is outside the system command path. The referee
receives a narrow environment, not ambient credentials, Python search paths or
caller Git routing. Commands requiring additional inputs must name them
deliberately, and their provenance belongs in the round receipt.

A configured referee runs before `land_intent`. An earlier unresolved intent
for the same target refuses a later submission with `train_pending`. The WAL
CAS invalidates stale audits; a retry builds and tests a new candidate atop the
new target. The configured command is checked again before admission.
Once a referee fails, only recording its refusal may retry after a WAL race;
the failed candidate is not executed again within that submission. The
controlled changing-outcome fixture observed one execution after correction.

Red, timeout and execution failures preserve the branch and record a typed
refusal. The receipt distinguishes actual execution from failures before a
process began. It names the candidate combinations actually tested and reports
localization as `unresolved`; it does not invent a conflicting pair or claim
minimality. Referee output is captured separately from CLI JSON and retained
as labelled tails. Process-group cleanup is required for configured execution,
so unsupported platforms refuse instead of silently weakening cleanup.

## Workspace protection contract

`onetree protect --repo REPO --session OPERATOR --json` records the current
shared branch baseline in a `workspace_protect` WAL event requiring reader
version 3. Initial protection requires no active claims or pending intents,
the files ref backend, and explicitly enabled reflogs. It binds the branch,
tip, HEAD and branch reflog prefixes, and the configured referee. Registering
another session cannot replace the baseline.

Before a landing and again before its intent, Git observations must continue
that baseline through transitions attributable to recorded broker intents.
Reflog action text is not authority. An unexplained bare commit, branch switch,
same-tip reset, changed referee, missing log or rewritten prefix blocks later
landings. The refusal is recorded as `workspace_changed`; `wal --json` also
reports the workspace condition. Refusal `paths` identifies the submission
being held, not a forensic claim about every file an external command damaged.

This is persistent detection and refusal of later landings. It does not
prevent unrestricted same-user Git commands from running or restore unstaged
work already erased. Sampling leaves a window between a check and a later
external write. Suppressing and restoring every distinguishing observation
can escape detection; reflog suppression/rewriting is refused when observable.
The evidence is the reflog, so only operations that move a ref are visible:
`checkout -- <path>`, `restore`, `read-tree -u` and `checkout-index` erase
unstaged peer work without a reflog transition and are not detected (measured
2026-09-10; the same damage from `reset --hard` is). Routine reflog expiry by
`git gc` rewrites the recorded prefix and quarantines an unmoved workspace;
the reconciliation that lifts it is task #151, not this round.
This boundary is the explicit compensating guard for the measured bare-Git
bypass. Harness-only hooks are not the protection mechanism.

A quarantined baseline is not silently refreshed by `protect`, registration,
or ordinary recovery. Reconciliation requires a separate explicit operator
disposition. An unreadable referee setting under protection is not a
quarantine: it refuses as `referee_config` (or `referee_config_changed` inside
the fence window), `wal --json` reports a `referee_config` divergence, and
correcting the setting is the whole remedy. Do not remove the WAL or disable the guard to restore a green
status. A successful broker CAS remains a successful landing even if a later
external action creates a separate workspace violation.

## Rejection union and repair scope

Existing error classes remain part of the public union. A missing path census
on an early operational error means the affected payload was not established;
clients must not interpret it as proof that no paths were affected.

| Error type / reason | Repair scope |
| --- | --- |
| `invalid_identity` | Correct the actor or session body; no payload is admitted. |
| `invalid_path`, including `git_internal` | Correct the named input path; Git internals are not claimable. |
| `claim_conflict` | Resolve the reported holders and overlapping claims. |
| `land_refused: session_inactive`, `wrong_target`, `nothing_staged` | Establish the intended live session, target and staged payload before submitting. |
| `land_refused: unclaimed_paths` | Claim only the reported missing payload paths. |
| `land_refused: payload_conflict` | Rebase the reported overlapping payload paths. |
| `land_refused: target_diverged` | Reconcile the target before resubmitting the affected payload. |
| `land_refused: session_died_at_fence`, `claim_expired_at_fence` | Re-establish the session or affected claims; retained paths identify the held submission. |
| `land_refused: referee_red`, `referee_timeout`, `referee_error` | Inspect the exact candidate receipt; no failing pair is inferred. |
| `land_refused: referee_config`, `referee_config_changed` | Correct or reconcile the configuration before a new submission. |
| `land_refused: train_pending` | Resolve the earlier intent, then submit against the resulting target. |
| `land_refused: workspace_changed` | Explicit workspace reconciliation; ordinary retries do not refresh the baseline. |
| `land_refused: workspace_busy`, `workspace_unavailable` | Establish an observable baseline with no active claims or pending intents. |
| `land_refused: payload_gate_refused`, `payload_gate_degraded` | Use the retained gate report and its exact audited path set. |
| `payload_gate_error` | The gate produced no trusted verdict; no candidate was admitted. |
| `wal_diverged`, `unrecorded_landing`, `ref_corrupt`, `wal_corrupt` | Reconcile the reported history or corrupt object; do not invent a file-level repair set. |
| `ref_locked` (`RefLockedError`) | Inspect the named lock and holder; the broker never deletes it. |
| `wal_append_refused` (`WalAppendRefusedError`) | A proposed append was rejected, not persisted as corruption. Re-read the fresh WAL and follow the reported operation; if the branch already moved, inspect/recover that intent before resubmitting. |
| `git_error`, `shallow_history`, `legacy_grafts`, `unsupported_worktree` | Resolve the named repository or plumbing condition before resubmission. |
| `release_refused`, `narrow_refused` | Repair the reported ownership/claim request without changing another actor's claims. |

## Evidence

The initial approved disposable probe ran against `3105b1a` on 2026-09-09.
It used two protocol actors attached to one actual process; this was not a
concurrency measurement or a substitute for the Gate 3 fleet.

| Known-bad input | Observed baseline | Regression carrier |
| --- | --- | --- |
| Configure `/usr/bin/false`, then submit claimed `a.txt` | Candidate `f708842a` landed despite the configured failure | `test_red_referee_refuses_and_retains_exact_candidate` |
| Shared working files differ from the staged candidate | Initial regression had no referee receipt | `test_referee_reads_index_candidate_not_shared_work` |
| Referee sleeps beyond its configured timeout | Initial regression did not raise | `test_referee_timeout_never_moves_target` |
| A peer lands while another candidate is being admitted | Initial regression accepted the failed combination | `test_referee_retests_candidate_after_wal_and_target_race` |
| Later intent attempts to overtake an unresolved earlier intent | Initial regression did not raise | `test_later_intent_cannot_overtake_pending_target` |
| Bare commit with hooks disabled | Commit `542e0629` remained WAL-consistent | `test_unexplained_git_operation_quarantines_later_landing[bare]` |
| Branch switch, then same-tip reset over unstaged peer work | Switch/reset succeeded; `b.txt` reverted and audit reported no divergence | Workspace `switch` and `reset` cases |
| Malformed evidence or command failing before execution | Six receipt regressions failed before correction | `test_malformed_referee_evidence_is_rejected`, `test_command_parse_failure_does_not_claim_execution` |

The first six referee regressions failed before their implementation, then
passed. Seven workspace API regressions first failed for missing protection;
their operation-level baseline is the actual probe above. The first combined
focused receipt is 19 passed in 7.05 seconds. Later cases extend configuration,
output and protection coverage. The first full run returned 527 passes and
one stale version-diagnostic assertion, with 89.48% coverage below the required
90% floor; it was not accepted. Review then identified additional malformed
evidence, recovery, configuration and refused-intent paths. Twelve targeted
checks failed before correction; the corrected focused run returned 91 passes.
A separate changing-outcome/WAL-race regression also failed before its fix.
Three exact-path regressions also failed before correction: target divergence,
session death at the final fence, and claim expiry under a directory claim.
The corrected focused run passed 55 checks. Both independent review lenses
accepted the runtime and final exact-path changes and released their freezes.
The final full run passed 559 tests in 206.07 seconds with 90.79% coverage,
including the real estate payload-gate cases; that count needs
`ONETREE_TEST_ESTATE_GATE` set. CI sets nothing and runs 529 passed with 30
skipped per matrix leg (run 34425990890). Ruff and strict mypy passed.
The bounded detection decision is `3a9692e15528`; it does not authorize a
workspace reconciliation or replace the remaining cutover gates.

Input chain: pytest imports `tests/conftest.py` and its `GitFixture`, plus
`onetree.core`, `onetree.git`, `onetree.referee` and `onetree.workspace` from
this checkout's `src`. The existing autouse fixture supplies the small admitted
payload-gate stub; real estate-gate cases require
`ONETREE_TEST_ESTATE_GATE=/home/anvil/.claude/tools/commit_payload.py` and restore
the independently captured shipped pin. Referee test commands explicitly
invoke the test interpreter with `-I -S`; no external Python package is needed
by those commands. Production referee dependency provenance remains a cutover
requirement.

Local evidence is retained in
`/home/anvil/claude-diag-806b39cf/onetree-gate-evidence/`:
`round-b-live-observation.md`, `round-b-live-*.json`, `referee-red-first.txt`,
`workspace-red-first.txt`, `referee-receipt-red.txt`, `round-b-focused-v2.txt`,
`round-b-review-red-confirmation.txt`, `round-b-review-corrections.txt`,
`referee-flaky-race-red.txt`, `referee-flaky-race-fixed.txt`,
`rejection-paths-red.txt`, `rejection-paths-fixed.txt`, and
`round-b-full-v4.txt` / `round-b-full-v4.xml`.
The disposable reset used the admitted fixture operation under the owner's
previously approved scope. No production repository was reset.

### Round BF (fix round after the Round B DUO, 2026-09-10)

Two DUO seats read the Round B diff at `15842ba`: the adversarial review
returned new-debt and the silent-failure hunt returned execution-miss, because
guards this document states as delivered (the at-fence re-check, reflog prefix
rewrite detection) had no fixture that fails without them. Round BF fixes
three behaviours and adds the missing fixtures:

| Defect or unmeasured guard | Fixture | Observed red |
| --- | --- | --- |
| `GIT_CONFIG` in the lander's environment demoted a configured red referee to `not_configured` and the candidate landed | `test_inherited_git_config_environment_cannot_demote_a_red_referee` | `DID NOT RAISE` at `15842ba` |
| An unreadable referee setting under protection refused as `workspace_changed` | `test_invalid_referee_setting_under_protection_is_not_a_quarantine`, `test_setting_broken_inside_the_fence_window_is_a_configuration_change` | reason `workspace_changed` at `15842ba` |
| A `land_intent` without referee evidence, or naming another referee, replayed under a protected epoch | `test_forged_intent_evidence_is_refused_on_replay` | `DID NOT RAISE WalCorruptError` at `15842ba` |
| Reflog prefix rewrite, malformed transition, dead-lander intent skip, unlogged external move, at-fence re-check, silent baseline replacement, landing outside the protected branch, intent referee checks, duplicate or empty setting, verdict/exit agreement, narrow child environment, process-group kill, referee stdout capture | the correspondingly named tests in `test_workspace_guard.py` and `test_referee.py` | each fails with its guard neutralised in a scratch clone |

Receipts: `round-bf-prefix-red.txt` (the three defects against pre-fix source)
and `round-bf-red-first.txt` (fifteen guard-neutralised runs, all failing) in
the local evidence directory named above. The shared-index preservation claim
in `DESIGN.md` gained its direct byte-level measurement
(`test_build_leaves_shared_index_and_worktree_untouched`), and the INV6
control now executes the fail-open reference evaluator instead of asserting a
tautology. CI additionally refuses a runner environment without `HOME` or with
a relative `PATH` entry (measured on run 34425990890).
