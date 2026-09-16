# Gate 2 battery brief

This tranche resumes tasks 11–19 from Gate 1 closure at `78785a9`
(decision `460fabf98d12`). It maps existing evidence before extending the
battery. It does not authorize estate cutover, a live parent fleet benchmark,
or changes to the frozen benchmark protocol.

## Inputs and method

The contracts are `DESIGN.md`, `ROUND-A.md`, `ROUND-B.md`,
`ROUND-B-AMENDMENT.md`, `ROUND-ESTATE-ADOPTION.md`,
`ROUND-CLAIM-LIFECYCLE.md` and `ROUND-GATE-EVIDENCE.md`. Gate 1 closure
receipts in task 10 are historical evidence; current executions are recorded
separately. The retired launcher and historical guard-alteration procedures in
older task rows are not this round's verification method.

New cases use known-bad inputs against the unchanged implementation before
correction. Existing passing checks retain their existing evidence attribution;
we do not claim a new passing control first failed when it did not. Every
refusal assertion checks its specific type/reason and the resulting branch/WAL
state. Positive controls distinguish fail-closed behavior from unconditional
refusal. No fixture changes an implementation guard to obtain its evidence.

The author checkout is
`/anvil/worktrees/onetree-g2-01a09765-20260916`. Its fresh development
environment imports this checkout through the editable installation and the
repository's configured `pythonpath = ["src"]`. The original source checkout's
entrypoint named `/home/anvil`; that failure is retained under task 169.
Its interpreter still worked. Reinstalling the editable project and declared
development dependencies into that explicit existing environment regenerated
its console wrappers for `/anvil/onetree`; CLI and tool version checks now
execute. The before-package list and installation log are retained. The original
untracked `uv.lock` is preserved. Environment installation uses the declared
`dev` extra, not a lock refresh.

## Existing receipts and remaining cases

The INV1–INV7 receipts at `c2ab14a`, amended by the AF rounds and retained in
`ROUND-A.md`, are reused. The later Round B contract takes precedence over
older roadmap wording about unconditional retries and pair localization.

| Task / family | Existing evidence to reuse | Gate 2 work and acceptance boundary |
| --- | --- | --- |
| 12 — stale/poisoned index | INV5 exact overlay and same-path conflict; `test_registration_is_idempotent_without_resetting_index_and_identity_refuses`; `test_git_wrapper_refusals_and_index_type` | Exercise malformed bytes, foreign-base content and forbidden index aliases. New dangling-symlink case fails before correction. Valid staged bytes on claimed paths are not authenticated provenance: the API intentionally permits ordinary Git staging. |
| 13 — bare Git bypass | `test_unexplained_git_operation_quarantines_later_landing` and `test_new_registration_cannot_erase_workspace_violation`; Round B's explicit reflog boundary | Retain the distinction between allowing unrestricted same-user Git to run and refusing later broker landings. Add only uncovered operation sequences; do not claim working-tree-only destruction is detected. |
| 14 — moved target | INV5; `test_referee_retests_candidate_after_wal_and_target_race`; protected workspace cases | Recorded broker transitions must rebuild/re-test against the new target. Unexplained external movement in a protected workspace must quarantine. Historical automatic-retry wording cannot override that guard. |
| 15 — changing referee outcome | `test_red_verdict_is_not_retried_to_green_after_wal_race`; timeout and receipt-semantic checks | Current contract does not execute a failed candidate again within the submission. Reuse its execution counter and WAL refusal evidence. No statistical flakiness classifier or new retry policy is implied. |
| 16 — death in referee-era windows | INV2 fence death/expiry; INV7 real subprocess SIGKILL around Git ref updates; referee process-group timeout case | Extend to real submitter death during candidate construction, during the test process and between green and intent. Existing sequential callbacks alone do not establish these crash windows. |
| 17 — competing referee trains | INV3 real process race; sequential Round B interleave/retest; pending-intent ordering | Combine real competing processes with receipts proving the losing candidate's referee saw the winner. Distinguish a conflict-free pair that both lands from a combined red train that must refuse. Existing separate race/retest tests do not establish their composition. |
| 18 — false-green reporting | INV6 typed paths; existing CLI JSON checks and `CheckResult.owned` | `check --help` exposes one path. Multi-path aggregation and no phantom clear row require new interface work; existing single-path tests do not complete the batch contract. Observe own-claim CLI output as a separate case. |
| 19 — Gate 2 closure | Gate 1 closure and the evidence above | Close only after every family has its disposition, observed known-bad evidence, reviewed candidate and required CI. This tranche alone does not close Gate 2. |

## Dependencies over symbols

`Repository.ensure_index` admits the session index pathname before
`Repository.write_tree` resolves its bytes. `Coordinator.land` derives the
payload relative to `SessionInfo.base`, checks claims and current target
entries, then overlays the exact payload. The payload gate and referee consume
that immutable candidate; the intent/fence and branch CAS govern publication.
The index family therefore precedes claims about what the referee tested.

For the later concurrency family, `referee.run` receipts and pending-intent
ordering must be observed together with `Coordinator.land` retries, not just
in separate test files. `workspace.violation` constrains which target movements
may enter that retry population. Crash cases must establish the process died in
the named phase, then inspect replay/recovery; a raised fixture exception is
not evidence of abrupt process death.

## First bounded correction: dangling private index

Task 12's initial cases use `tests/conftest.py::git_repo`, which creates a real
temporary Git repository, and its existing `GitFixture` methods. The autouse
payload gate is the repository's known coordination-only gate fixture; these
cases do not claim admission of the shipped estate gate. Production imports
resolve from this checkout's `src/onetree`; no frozen source is executed.

After registration and staging, the bad input replaces the private index with
a symlink to a nonexistent file. The initial implementation treats that link
as absent, replaces it with a fresh base index, and either returns successfully
from registration or reports `nothing_staged` from landing. Both violate the
existing regular-file boundary by silently replacing the evidence.

The required correction is a typed `GitError` before replacement. The link,
shared index, branch and WAL must remain unchanged. Ordinary missing-index
initialization remains the existing behavior; this is not a new recovery or
staging-provenance protocol.

The same test module also observes existing controls: bad index signatures and
truncated bytes produce typed failures preserving both refs; a valid private
index loaded from a foreign base with unclaimed `b.txt` produces
`unclaimed_paths`; restoring the registered base and staging owned `a.txt`
lands exactly that path and preserves `b.txt`.

Evidence is retained in `/anvil/scratch/onetree-g2-20260916/`.
`index-before.txt` and `index-before-final-fixture.txt` preserve the pre-fix
failures. The latter fixture only moves operation selection outside
`pytest.raises` to satisfy the repository's lint rule; both inputs still fail
against unchanged implementation. The corrected index, defensive and landing
tests pass together; lint and strict typing pass. These focused checks do not
establish whole-estate admission.

The unchanged baseline's real shipped-gate check failed with: `the shipped pin
no longer matches the configured gate; the pin is stale or the gate was
replaced`. OneTree admits
`e20c259cfee795f064eb18fa4208c3d9ef6ea962ecd96cf8efbfe351cce4f669`; the installed
gate hashes to
`d71928779f10f286c7d6f840159783c6b54626a07af72a3762682fdc0e8ed4f8`.
Task 170 recorded this admission blocker. Owner decision `02af0b730ffb` now
admits exactly the installed digest above; `ROUND-G2-ADMISSION.md` records the
pin update and integrated verification. The original failure evidence below
remains unchanged. This admission does not close the remaining Gate 2 families.

Pre-fix evidence:
`receipt:/anvil/scratch/onetree-g2-20260916/receipts.jsonl:1cdd7e746468`.
Focused correction evidence:
`receipt:/anvil/scratch/onetree-g2-20260916/receipts.jsonl:8a370ed2814b`.
Baseline evidence:
`receipt:/anvil/scratch/onetree-g2-20260916/receipts.jsonl:52991dd7656d`.
These receipts preserve reads of the original test logs; their reader success
does not turn the intentionally failing pre-fix or red baseline into a pass.
