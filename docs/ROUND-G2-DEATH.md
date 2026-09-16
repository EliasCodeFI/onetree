# Gate 2, task 16: submitter death in the referee-era windows

Base `78785a9def581f9e54307ccd3f5936169d7fb4b7`. Author checkout
`/anvil/worktrees/onetree-g2-task16-e85636f2-20260916`, branch
`claude/onetree-g2-task16-e85636f2-20260916`, tests importing `onetree` from
that checkout's `src` (evidence `import-origin.txt`). Fixture module
`tests/test_referee_crash_windows.py`. Evidence directory
`/anvil/scratch/onetree-g2-20260916/task16-parent-evidence/`.

This round extends the INV2 fence-death and INV7 ref-update receipts to the
windows the referee added: a landing now builds a candidate, runs an external
test process over it, and only then appends its intent. Death in those windows
was covered by sequential checkpoints and by kills inside `update-ref`; it was
not covered by a real submitter process dying while the window was open.

## Method

Each case forks a lander (`multiprocessing`, fork context, the INV7 shape)
that runs `Coordinator.land` for real with a checkpoint callback. The callback
appends a durable phase record before it acts, so the log names every seam the
lander crossed. At the window under test the lander blocks; the test observes
the phase record, sends `SIGKILL` from outside, joins, and asserts the exit
status is `-SIGKILL`. A lander that refused or raised would exit differently,
so the status and the log together establish the reached phase. The referee
window cannot be reached from a Python seam, because the broker is blocked in
`process.wait`; there the referee process itself writes a marker, from inside
its own execution, carrying its pid, process group, candidate and working
directory, and the test kills the broker once the marker exists.

The referee is a counting script under `sys.executable -I -S`, the shape
`tests/test_referee.py` uses. It records every execution and, until released,
blocks; released, it exits green. The referee's scratch clone is created under
a per-test directory (`tempfile.tempdir` set in the lander) so residue is
observable instead of lost under the system temporary directory.

The session body is the test process, so the death under test is the landing
incarnation's, not the session's. That is the case the intent's `lander`
block exists for, and the case INV2 does not cover (INV2 kills the session
body and expects `session_died_at_fence`).

Nothing in `src/` or in shared fixtures was changed. No implementation guard
was altered to obtain evidence.

## Windows and outcomes

| Window | Reached phase, as observed | Branch | WAL | Recovery | Retry |
| --- | --- | --- | --- | --- | --- |
| Candidate construction, `after_audit` | log `["after_audit"]`; no candidate, no tree; referee executions 0 | at base | no new event; consistent; 0 pending; no divergence | consistent, 0/0/0, no locks | same actor lands the same payload; referee runs once; events `land_intent`, `landed` |
| Candidate construction, `after_tree` | log `["after_audit", "after_tree"]`; tree object exists (`cat-file -t` = `tree`), no candidate commit; referee executions 0 | at base | as above | as above | as above |
| During the referee | log `["after_audit", "after_tree"]`, no `before_intent`; referee marker present with the candidate; referee alive at the kill | at base | as above | as above | new candidate built and tested (`tested == [new commit]`); referee executions 2; the dead attempt's commit is not what lands |
| After green, before intent | log `[..., "before_intent"]`; referee executions 1; candidate is a commit over the audited payload, reachable from no ref; scratch clone removed | at base | as above: the green receipt existed only in the dead process | as above | referee runs again (executions 2) over a new candidate before anything is recorded |
| `after_intent` (INV7 row `intent-appended`) | log through `after_intent`; intent on the WAL; referee executions 1 | at base | 1 pending intent; no divergence | refused 1, repaired 0; consistent | same actor lands a new candidate; referee executions 2; one `landed` |
| `after_branch` (INV7 row `branch-moved`) | log through `after_branch`; intent on the WAL | at candidate | 1 pending intent; no divergence | repaired 1, refused 0; consistent | none needed; branch holds the candidate; referee executions stay 1; one `landed` |

Every window ends with `inspect_wal` consistent, zero pending intents, zero
divergences, exactly one `landed` event, one commit over base carrying the
payload, and a clean `git fsck --no-dangling` in the recorded-window cases.

## Existing guards, reported as passing

The two recorded windows re-observe rows of INV7's matrix
(`tests/test_land.py::test_inv7_kill_inside_each_ref_update_recovers_to_consistent_refs`,
rows `intent-appended` and `branch-moved`) with a second instrument, a
Python-seam death instead of a kill inside `update-ref`. Their expected
outcomes are the matrix's own; nothing new is claimed for them, and they are
included so the mission's three windows and the existing matrix are read
against one phase log. The write-ahead order's invariant, that no window
leaves a branch move the WAL has no record of, held in all six cases.

The referee timeout's process-group kill
(`tests/test_referee.py::test_referee_timeout_kills_the_whole_process_group`)
is a different path from submitter death and is not exercised here.

## Observation for disposition: the referee outlives its broker

When the broker dies during the referee, the referee process survives it and
its scratch clone is never removed. The cause is structural: the referee is
started with `start_new_session=True` so that a timeout can kill its whole
group, and the group kill plus the temporary-directory cleanup both live in
the broker's `finally` blocks, which a `SIGKILL` never runs. The fixture pins
that shape: the referee is alive immediately after the broker's death, and
exactly one `onetree-referee-*` directory remains, containing the referee's
working directory. The test reaps the orphan's process group itself, from a
cleanup scope that spans the whole lifetime of both spawned processes, so an
assertion failing anywhere between the referee's marker and the reap still
exits through it with its own failure preserved (task 172; the r1 revision
placed three assertions before that scope, which the review caught).

No contract in `ROUND-B.md` speaks to submitter death during the referee, so
this is reported as an observed gap, not a red case. If a later change reaps
the referee on broker death (for example a parent-death signal, or a
watchdog), the two pinned assertions flip and should be updated with that
change. The landing outcome is unaffected either way: the branch, WAL and
recovery show no trace, and a retry tests a new candidate.

## Controls

- The `-SIGKILL` exit status and the phase log distinguish a kill in the
  named window from a lander that refused, raised or returned; a lander that
  returned appends a `returned` record, which no case observed.
- Red control for the consistency assertions: in the before-intent case the
  dead lander's green candidate is placed on the branch by hand, the zombie
  landing that window could produce. `inspect_wal` then reports
  `unrecorded_landing` naming that commit and `recover` raises
  `UnrecordedLandingError`; the branch is put back and the no-trace
  assertions pass again. Without this the assertions could not fail.
- The referee counter distinguishes "the referee never ran" (construction
  windows: 0, then 1 after the retry) from "it ran and its green was lost"
  (before-intent: 1, then 2), from "it ran and recovery completed the landing
  without a second run" (`after_branch`: 1).
- `_alive` is the helper `tests/test_referee.py` proves against a killed
  sleeper.

## Gates in this checkout

Evidence files are named in parentheses.

- `pytest tests/test_referee_crash_windows.py -v --no-cov`: 6 passed in
  4.52 s; three further runs 6 passed each (`crash-windows-run1..4.txt`).
- Full suite: 1 failed, 596 passed, 31 skipped in 227.98 s, coverage 91.79 %
  (`full-suite-run1.txt`). The failure is
  `tests/test_payload_gate.py::test_the_shipped_payload_gate_admits_a_benign_landing`,
  the installed estate-gate admission mismatch task 170 records; it fails
  identically on the unchanged base (owner baseline, 590 passed). This
  candidate does not change that pin or its disposition and must not be
  published as a green build while task 170 is open.
- `ruff check src tests`: clean. `mypy`: clean, 9 source files
  (`mypy-ruff-gates.txt`). `ruff format --check` is not a CI gate and two of
  four sampled existing files fail it; the module follows the repository's
  existing style rather than the formatter (`ruff-run2.txt`).

## Revision r2 (after the independent review of r1)

The r1 freeze `onetree-g2-task16-death-r1-20260916` is preserved. The review
(`task16-owner-review-verdict.md`, changes requested) found two fixture-only
defects; nothing in `src/` changed in either revision.

- Task 172, failure-path cleanup. In r1 three assertions sat between the
  referee's marker and the cleanup scope that reaps the referee's group, so an
  assertion failing there exited through lander-only cleanup. In r2 one scope
  spans both spawned processes from the fork onward, re-reads the marker on
  exit so a referee that started after a failed wait is still found, and
  reaps without asserting so the original failure is preserved; the success
  path asserts the reap afterwards. Observed against the known-bad: with an
  `AssertionError` injected after the marker in private copies of each
  revision, r1 left 1 referee process alive after pytest exited and r2 left
  0, both reporting the injected failure (`r2-cleanup-probe.txt`; the copies
  were probes, not the candidate).
- Task 173, confinement prose. The sentence that the orphan "only reads a
  scratch clone" is withdrawn; the "Not established" entry below now states
  what was observed and leaves an arbitrary orphan's influence unknown.

Revision r3 (after the r2 review, `task16-owner-review-r2-verdict.md`,
task 172 accepted) corrects one sentence in the last section: the r2 wording
said the retry ran while the fixture's orphan was alive, but the fixture
reaps the orphan and asserts it dead before the retry. No code changed; the
r1 and r2 freezes are preserved.

Gates on r2: module 6 passed, two runs; `ruff check src tests` and `mypy`
clean (`r2-module-and-gates.txt`); full suite 1 failed, 596 passed, 31
skipped, coverage 91.79 % with the same task-170 failure
(`full-suite-run2-r2.txt`).

## Not established

- Death inside `commit_tree` itself, between the tree seam and the referee,
  is reached only through the object store and was not exercised; the
  `after_tree` case bounds it on one side and the referee case on the other.
- A submitter that is also the session body dying in these windows (INV2's
  axis) was not combined with them.
- Whether a surviving arbitrary referee can affect later work is unknown and
  is not bounded here. The fixture's own referee writes its marker and
  counter outside the scratch clone, the production referee is an ordinary
  `Popen` and `ROUND-B.md` disclaims any sandbox, so nothing in this round
  supports a containment claim. What was observed is only that, after the
  fixture had reaped its own orphan and asserted it dead, the retry built
  and tested a new candidate successfully; what a surviving referee could
  do to later work was not observed and remains unknown (task 173).
