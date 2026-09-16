# Gate 2: evidence reconciliation

This record assembles the evidence for Gate 2 close review. The original battery
baseline is `bdbb53d282bcc9e047419f29109dd06c99c2d6bb`; the final crash cases are
published in `fa8e01c38e4ea65d7b5192a3ef683409135b9748`. The independent verdict
on this record and the resulting gate decision belong to task 19's publication
record. This document itself is not a gate-close decision and does not authorize
Gate 3 cutover.

The acceptance contract is `ROUND-G2.md`, including its explicit limits on
same-user Git, retries and staging provenance. This index connects each family
to its observed adverse input, allowed population, review and publication.
Historical logs retain their original outcomes. A passing reader or receipt
does not make a failing test pass, and new sequence coverage is not represented
as a newly observed pre-fix failure.

## Per-family evidence

| Family | Observed failure or adverse input | Current refusal and allowed evidence | Record |
| --- | --- | --- | --- |
| 12: private index | `index-before-final-fixture.txt` observes a dangling private-index link being replaced: re-registration does not raise the required `GitError`, and landing reports `nothing_staged`. | `test_index_integrity.py` requires the typed regular-file failure while preserving the link, shared index, branch and WAL. Corrupt bytes and foreign-base unclaimed content refuse; restoring valid owned content permits the exact landing. | `ROUND-G2.md` |
| 13: external Git | `round-b-live-observation.md` and `round-b-live-wal-after-bare.json` retain the disposable baseline where bare commit succeeded and WAL inspection incorrectly reported no divergence. Reset erased peer bytes without a divergence. | `test_workspace_guard.py` executes external operations and verifies later `workspace_changed` refusal and persistent quarantine. Disjoint broker landings remain allowed. External commands are not prevented and refusal does not restore erased bytes. | `ROUND-G2-CONTROLS.md` |
| 14: recorded and unexplained target movement | `referee-red-first.txt` records `DID NOT RAISE LandingRefusedError` for the combined-red candidate after an interleaved broker landing. The bare-Git observation supplies the original unexplained-operation gap. | `test_referee.py` checks rebuild/re-test after a recorded transition. `test_workspace_referee_move.py` moves the protected target during the actual referee command and then returns green: landing refuses `workspace_changed`, preserves the external tip, creates no intent, and recovery reports divergence. Its no-move pair lands the exact tested object. | `ROUND-G2-CONTROLS.md`, `ROUND-G2-TARGET-MOVE.md` |
| 15: changing referee outcome | `referee-flaky-race-red.txt` records `DID NOT RAISE LandingRefusedError` for a first-red/later-green command after a WAL race. | The execution counter in `test_red_verdict_is_not_retried_to_green_after_wal_race` requires one command execution and a `referee_red` refusal with an unchanged branch. Passing, timeout and malformed-receipt controls retain their distinct results. | `ROUND-G2-CONTROLS.md` |
| 16: death and fence reclaim | The initial real process-death fixtures exposed a surviving referee and retained scratch. `lifetime-before.txt` records actual SIGKILL inputs failing the active-group cleanup assertion for referee, clone and checkout. | Published crash and lifetime modules inspect reached phase, dead process, branch, intent, WAL and recovery; fresh valid work remains possible where recovery allows it. Lifetime tests require cleanup while preserving an unrelated process. The final cases compare the killed session body's persisted identity and generations, distinguish pending-intent recovery outcomes, and bound commit-tree object creation with before/after wrapper deaths. The shared known-bad control puts an unrecorded candidate on the branch and observes inconsistent WAL and refused recovery. | `ROUND-G2-DEATH.md`, `ROUND-G2-LIFETIME.md`, `ROUND-G2-SESSION-DEATH.md` |
| 17: competing trains | The historical missed re-test and pending-intent failures are in `referee-red-first.txt`. The new composition uses real competing landers and a configured referee that is green for each payload alone but red for their combination. | `test_referee_train_races.py` proves compatible candidates rebuild and re-test against the winner, a pending intent refuses overtaking, and a red combination leaves only the winner published. Candidate bytes, baseline, receipt, process attribution and WAL are checked together. | `ROUND-G2-TRAINS.md` |
| 18: per-path reporting | `batch-before.txt` records the unchanged single-path CLI rejecting batch arguments and incorrectly displaying an owned path as `CLEAR`; its single-path JSON compatibility control passes. | `test_cli_batch_check.py` checks blocked/clear order, invalid paths in each position, duplicate inputs, own claims and control characters. Every input receives a row; an invalid path has a typed error and no phantom clear result. The operation preserves the WAL. | `ROUND-G2-BATCH.md` |

The initial index, lifetime and CLI runs above were opened again for this
reconciliation, as were the original external-Git, candidate-retest and
changing-outcome logs. None relies on a comment asserting that a check once
failed. Missing-method or parser failures establish missing interfaces only;
the table states those boundaries rather than attributing deeper behavior to
them. New target-movement and train composition cases supplement the historical
guard evidence without claiming that their final fixtures failed before a fix.

## Review and publication chain

Independent review means a different interactive author/reviewer seat. Codex
review records carry adversarial and silent-failure lenses; Claude's records
retain its independent review. Initial changes-requested verdicts remain in
their evidence directories alongside the accepted corrections.

| Change | Accepted independent review | Publication |
| --- | --- | --- |
| Index and battery contract | Claude `bd9d66b9b96e` | PR 9, merge `7374926` |
| Exact estate-gate admission and initial crash integration | Claude `5b11517e48c6`; Codex `task16-owner-review-r3-verdict.md` for the Claude-authored crash pair | PR 9, merge `7374926` |
| Referee lifetime and collector corrections | Claude `7166550f7d5b`, after the earlier changes-requested verdict and minimum-version correction | PR 10, merge `c32dfe9` |
| Batch reporting | Claude `99ce536cc5c9` | PR 11, merge `88ccf21` |
| Existing workspace and changing-outcome evidence | Claude `61480da5ec76`; its carried pointer finding was fixed and accepted by Claude `c6c1fd431563` | PR 12, merge `14170bd`; pointer follow-up in PR 13 |
| Competing trains and supervisor-compatible attribution | Codex `task17-owner-review-r2.md`, after r1 failed on the integrated supervisor ancestry | PR 13, merge `bcb7e2a` |
| Target movement during command execution | Claude `a6dd19cf7298` | PR 14, merge `bdbb53d` |
| Final task 16 crash axes | Codex `87dd8abfaa0f`, `onetree-g2-final-axes-owner-review-20260916/verdict.md` | PR 28, merge `fa8e01c` |

Task 170's exact-digest admission is authorized by decision `02af0b730ffb`.
It does not authorize following future gate versions automatically. The
historical red admission logs remain red; the admitted integration is a
different candidate with its own local verification.

## Evidence locations and current validation

Original Round B inputs and failures are under
`/anvil/claude-diag-806b39cf/onetree-gate-evidence/`. Current round evidence:

| Directory under `/anvil/scratch/` | Contents |
| --- | --- |
| `onetree-g2-20260916/` | Index failures, admission evidence, initial task 16 author/review records and integration logs |
| `onetree-task171-20260916/` | Lifetime failures and corrections, collector compatibility, train author evidence and independent review |
| `onetree-task18-20260916/` | CLI before/after evidence, interpreter runs and batch review |
| `onetree-g2-controls-20260916/` | Workspace/changing-outcome reconciliation, original-log receipts and independent review |
| `onetree-g2-target-move-20260916/` | Target-movement fixtures, independent review and latest published integration logs |
| `onetree-evidence-pointers-20260916/` | Evidence-directory inventory and pointer repair candidate; its verdict is in the controls directory |
| `onetree-g2-final-axes-owner-review-20260916/` | Final task 16 independent review, actual zombie-landing control and current-main integration validation |

The integration at this battery baseline has an `integrated-full.txt` recording 661 passing
tests without skips on Python 3.13.15 and 91.78% coverage; its install, lint
and type steps also passed. Both installed real-gate selectors were enabled.
The earlier batch integration retained complete Python 3.11, 3.12 and 3.13
runs. These are local results under the owner's local-CI instruction, not
claims that queued GitHub jobs passed or that later main revisions were tested
by this run.

The final task 16 integration on `934af19`, published in PR 28 as `fa8e01c`,
passed 882 tests without skips on Python 3.14.4 with 92.29% coverage. Ruff and
mypy passed, both installed real-gate selectors were enabled, and the merged
tree `e5f834222e0e1050a871579405bd8262a99853e7` equals the tested candidate tree.
The independent frozen-copy review separately passed the nine new cases and
the existing real zombie-landing control. The full integration includes the
later published recovery, workspace, WAL and header corrections; the earlier
battery baseline does not stand in for this run.

Final integration receipts:
`receipt:/anvil/scratch/onetree-g2-final-axes-owner-review-20260916/receipts.jsonl:26bad699f9e3`
(full suite),
`receipt:/anvil/scratch/onetree-g2-final-axes-owner-review-20260916/receipts.jsonl:12db3b950a1f`
(lint), and
`receipt:/anvil/scratch/onetree-g2-final-axes-owner-review-20260916/receipts.jsonl:3660b3b01eeb`
(types). No remote-green result is inferred from publication.

## Limits retained for the next gate

- Same-user Git and referee execution are not a security sandbox. File-only
  operations can erase working bytes without a reflog transition; their adverse
  tests preserve that result. Task 151's explicit workspace reconciliation
  workflow was accepted by Claude `6a6fac0fc743` and published in PR 15,
  merge `52044fd`, after this battery baseline. Its author and reviewer full
  suites each passed 692 tests without skips. Production reconciliation has
  not been performed and is not authorized by this record.
- A failed candidate is not re-executed within its submission. This is not a
  statistical flakiness classifier or an automatic pair-localization policy.
- Lifetime cleanup covers cooperative process groups under ordinary scheduling.
  Deliberately detached descendants, death of both lander and supervisor, host
  failure, storage failure and uninterruptible I/O remain outside that contract.
- The final task 16 commit-tree cases place death at wrapper boundaries before
  and after Git writes the object. They do not establish an outcome for a death
  inside Git's native temporary-file/rename interval. The separate-lander fence
  re-sample remains covered by its existing fixture; the final cases instead
  make the registered session body itself the dying lander.
- The unordered train fixture did not observe its recorded-refusal branch on
  this host. The deterministic pending-intent case checks that behavior; the
  unordered run does not supply additional evidence for it.
- Batch checks are sequential observations, not an atomic snapshot or a future
  reservation. Valid staged bytes do not authenticate their human provenance.
- Gate 2 evidence does not replace later cutover, benchmark, packaging or
  operational requirements. No performance or full-estate activation claim is
  made here.
