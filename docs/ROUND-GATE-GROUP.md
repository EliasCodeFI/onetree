# Task 142: the payload gate is a process group, killed on timeout

Round AF12 measured (onetree #118, #142) that the broker's wait on the commit
payload gate is bounded by `PAYLOAD_GATE_TIMEOUT_SECONDS` and that a `CLEAN`
printed behind an output pipe a descendant still holds is discarded as a
timeout, which is the closed direction. It also measured the residual: the gate
ran under `subprocess.run(capture_output=True, timeout=...)` with no process
group, so on timeout CPython killed the direct child only, and anything the gate
had forked kept running under the broker's session after `land()` had raised.
The owner chose containment over documentation (decision `c4899a86fcfc`).

## Change

`Repository._run_payload_gate` starts the pinned gate with
`start_new_session=True`, so the gate is the leader of a new session and
process group whose id is its pid. `_drain_until_exit` then reads both output
pipes to end-of-file with a selector and observes the leader's exit with
`waitid(WNOWAIT)`, all under one deadline of `PAYLOAD_GATE_TIMEOUT_SECONDS`.
The wait ends only when both pipes are closed and the leader has exited, so the
timeout semantics AF12 measured are unchanged: a gate that exits while a
descendant holds a pipe is still a timeout, and `land()` still raises
`PayloadGateError` "commit payload gate timed out" with the branch and the WAL
head unchanged.

What changes is what happens on the way out. When the wait ends in any
exception, timeout or cancellation alike, `os.killpg(process.pid, SIGKILL)` is
sent to the whole group, and only then, in the `finally` clause, is the leader
reaped. Nothing in the helper reaps earlier: `waitid(WNOWAIT)` leaves the
exited leader a zombie, and neither `communicate`, `poll` nor `wait` is called
before the signal. A zombie keeps its pid, and with it the group id, so the
group the broker signals is always the gate's own. `SIGKILL` cannot be caught
or blocked, so every process still in the group is gone once it is delivered.

Everything after the wait is unchanged: the same `CompletedProcess` shape feeds
the same stderr, JSON, duplicate-key, field, candidate, repository, path-set and
exit-status checks, the source re-read, and the same typed errors. A successful
gate is not signalled at all: its descendants keep whatever lifetime they had.

## Round r2: cancellation reaped the leader before the signal

The r1 candidate (freeze `onetree-gate-group-r1-20260916`, left untouched)
waited with `Popen.communicate(timeout=...)` and claimed the leader was never
reaped before `killpg`. The owner's r1 review (decision `6a63e667711f`, finding
task 186) disproved that with a real `SIGINT`: CPython's `communicate` handles
`KeyboardInterrupt` by waiting up to 0.25 s for the child, and a gate that
exits inside that wait is reaped there, so the `except BaseException` arm then
signalled a numeric group id whose leader was gone. A passive audit hook on
`os.killpg` calling `waitid(WNOWAIT)` observed `ECHILD` at the signal on
Python 3.13.15 and 3.14.4. No pid reuse or unrelated-process kill was produced
or claimed; the identity guarantee was simply not held.

r2 removes `communicate` from the path and owns the wait, as above. The
regression is `test_cancellation_signals_the_group_before_the_leader_is_reaped`
in `tests/test_payload_gate_group.py`: a forked worker installs the same kind of
passive audit hook, runs the helper with a gate that forks a pipe-holding
descendant, and takes a real `SIGINT` from a thread. Two shapes:

- `gate-running`: the gate is still running when the interrupt lands and is
  released 50 ms later, the r1 review's shape.
- `gate-exited`: the gate exits as soon as it has announced readiness and the
  interrupt lands 200 ms later, while the descendant still holds the pipes; the
  leader is a zombie at the signal.

Both require the outcome to be `KeyboardInterrupt`, exactly one `SIGKILL` to
the gate's own pid as group id, the leader observed **not** reaped at that
moment, and the descendant gone afterwards. On a private copy of the r1 frozen
tree, with the r1 `git.py` imported (its path is in the record), both shapes
fail with the leader observed `reaped` (`red-first-r2-on-r1.txt`,
`tmp-cancel-r1/*/result.json`). On r2 both pass, observing the leader `running`
and `exited` respectively (`green-focused-r2.txt`, `tmp-cancel-r2/*/result.json`).

## Round r3: the cancellation fixture announced readiness before its child existed

The owner's combined referee run of the r2 candidate (task 189) failed the
`gate-running` cancellation case once with "the gate never ran its descendant":
the leader was interrupted and its group killed while the forked descendant had
opened `descendant.pid.tmp` but not yet written or renamed it. The r2 gate wrote
`ready` straight after `fork`, so readiness said nothing about the child, and the
interrupt raced the child's pid publication. Production code is not involved:
the cleanup did exactly what it should to a group whose descendant was not yet
observable.

r3 changes only `tests/test_payload_gate_group.py`. Every gate now waits until
the descendant's pid file exists before it does anything else, so `ready` (and,
for the timeout shapes, the verdict or the hang) implies the descendant is
observable, whatever the scheduler does with the child. The cancellation test
gains a `child_delay` parameter as the scheduling control: `child-delayed` makes
the child sleep 0.3 s before publishing its pid. Against a private copy of the
r2 module with that same delay and the r2 readiness, both shapes fail
deterministically with "the gate never ran its descendant", the owner's exact
observation (`red-first-r3-delayed-child-on-r2-fixture.txt`,
`tmp-cancel-r3-known-bad/`: `ready` and `release` present, no pid file). With
causal readiness all four cancellation cases pass, in five consecutive repeats
(`green-focused-r3.txt`, `green-r3-cancellation-repeats.txt`). No sleep was
lengthened and nothing is retried. The product files are byte-identical to the
r2 freeze.

## Round r4: the leader's own readiness was not atomic either

The owner's r3 review (task 190) found the same class of window one step
later: the r3 gate wrote `ready` in place with `Path.write_text`, which creates
the file before its contents exist, while the interrupting thread reacts to the
file's existence. A private input that split that write into open, a 0.3 s
stall and write made every cancellation case fail with the worker exiting on
`int('')`: the interrupt landed on a gate whose pid file existed but was still
empty. Production code and every assertion were untouched by that input.

r4 again changes only `tests/test_payload_gate_group.py`. Both pids are now
published atomically: written to a temporary name and renamed into place with
`os.replace`, so the file the sender polls for never exists without its complete
contents. The scheduling control becomes `publication_delay`, which stalls each
writer between opening its file and writing the pid, the exact shape that bit
the r3 fixture. Against a private copy of the r3 module with that stall and the
r3 in-place write, all four cancellation cases fail on `int('')`
(`red-first-r4-delayed-ready-on-r3-fixture.txt`); the r2-readiness reproduction
of the r3 round is retained as recorded there. With atomic publication all ten
cases pass, the four cancellation cases in five consecutive repeats
(`green-focused-r4.txt`, `green-r4-cancellation-repeats.txt`). The product
files are byte-identical to the r2 and r3 freezes.

## Contract boundary

- Only descendants that remain in the gate's process group are terminated. A
  descendant that calls `setsid` has left the group and survives the kill; the
  fixture for that case asserts that it survives, so the gap is recorded as a
  shape a later change would have to flip, not represented as contained.
- Processes outside the group are not signalled: a bystander started by the
  caller in its own group survives the timeout, and the broker's own process is
  untouched.
- A second interrupt arriving inside the cleanup itself, between the wait
  ending and the signal, is not handled specially; the `finally` clause still
  reaps the leader, and descendants may survive that double interrupt.
- Platform: POSIX process groups (`start_new_session`, `os.killpg`,
  `os.waitid` with `WNOWAIT`), the same boundary `referee.py` already imposes
  for the referee supervisor. The tests additionally read liveness from `/proc`
  and skip where it is absent. Windows is unsupported for this path and
  unmeasured, as it was in AF12.

## Measured

`tests/test_payload_gate_group.py`, real `GitFixture`, a gate written to a
temporary path with its parameters baked into the source (the gate's
environment is admitted by name, so nothing can be passed through it). The gate
forks a grandchild that inherits the broker's stdout and stderr pipe ends,
records its pid, and sleeps for 120 s; the timeout is 1.0 s.

Red first, on the unchanged base `a350f31` (`red-first.txt`):

| case | outcome on the base |
| --- | --- |
| gate exits CLEAN, grandchild holds the pipes | timed out, branch and WAL unchanged, grandchild 2086771 still running (state `S`) — **failed** |
| gate hangs, grandchild holds the pipes | timed out, branch and WAL unchanged, grandchild 2091954 still running (state `S`) — **failed** |
| gate hangs with both pipes closed, grandchild closed its pipes too (added in r2; `red-first-r2-timeout-shape-on-base.txt` on a pristine `git archive` of the base) | timed out, grandchild 4005355 still running (state `S`) — **failed** |
| bystander outside the group | descendant still running — **failed** at the descendant assertion; the bystander survived on the base too |
| grandchild leaves the group with `setsid` | escaped grandchild running — passed (the gap's shape) |
| gate exits CLEAN, grandchild closed its pipes | landed in well under the timeout, grandchild running — passed (successful-gate control) |

On the r4 candidate all ten cases pass (`green-focused-r4.txt`; the r3 and r2
records are the earlier fixture revisions on the same product code), run with
coverage: every line of `_drain_until_exit` and `_run_payload_gate` is
executed, the closed-pipes shape being the one that reaches the deadline in the
exit wait rather than in the pipe wait): the timeout cases return in about
1.2 s wall time including fixture setup, the
descendant is gone (no `/proc` entry, or a zombie awaiting its reaper) within
the 10 s grace the test allows, the bystander is alive, the escaped descendant
is alive, the successful-gate control lands in about 0.25 s with its detached
descendant alive, and the two cancellation shapes hold the leader identity.
Each fixture kills its own sleeper on the way out; none were left behind after
the runs. The r1 figures (`green-focused.txt`, `green-durations.txt`) are the
same five cases on the superseded helper.

Existing gate suites on the r2 candidate (`green-gate-suites-r2.txt`):
`test_payload_gate.py`, `test_gate_receipts.py`, `test_estate_gate_admission.py`
and `test_workspace_guard.py`, 171 passed, so admission, verdict validation,
typed errors, receipts and the successful path are unchanged.

## Local CI

Fresh `uv` interpreters, the CI commands, estate gate env set for the two
admission tests. The r4 runs were still executing when this record was frozen at
the owner's request, so the r4 column reads pending here by design; their outcomes,
and the r3 outcomes, live only in the external evidence (`full-suite-r4-3.1x.txt`,
`full-suite-r3-3.1x.txt`, `lint-r*-*.txt`, `types-r*-*.txt` under the evidence
directory) and are not asserted by this record. The r2 and r1 columns are the
earlier revisions' runs, kept for the trail:

| Python | ruff | mypy --strict | pytest (r4) | pytest (r3) | pytest (r2) | pytest (r1, superseded) |
| --- | --- | --- | --- | --- | --- | --- |
| 3.11.15 | pending at freeze | pending at freeze | pending at freeze (external evidence only) | see external r3 evidence | 841 passed, 92.32% | 838 passed, 92.28% |
| 3.12.13 | pending at freeze | pending at freeze | pending at freeze (external evidence only) | see external r3 evidence | 841 passed, 92.32% | 838 passed, 92.28% |
| 3.13.15 | pending at freeze | pending at freeze | pending at freeze (external evidence only) | see external r3 evidence | 841 passed, 92.30% | 838 passed, 92.26% |

## Not measured

- Wall time of the 300 s production bound; the tests pin 1.0 s and 30 s.
- A descendant that leaves the group and is then re-parented; it is outside
  the contract by construction and the fixture asserts only that it survives.
- Windows, and any platform without `/proc` (the tests skip there; the
  product path requires POSIX regardless).

## Publication paths

- `src/onetree/git.py`
- `tests/test_payload_gate_group.py`
- `docs/DESIGN.md` (INV9 paragraph)
- `docs/ROUND-AF12.md` (the sentence that recorded the gap now points here)
- `docs/ROUND-GATE-GROUP.md`

## Provenance

Authored by claude:99edb3eb-febc-4531-8cee-8b0cffc16129 from published main
`a350f31` (PR #25) under owner handoff `cdel_a0cdbda0f06117ee1baae81c9d820405`
(r1), correction handoff `cdel_b48ca1b746a9ce87396a5c33f46ed689` (r2, after
owner review `6a63e667711f`) and readiness handoff
`cdel_c605765e673f2ec1c95d1c1e19c4116e` (r3, task 189, after the owner's combined
referee run) and `cdel_ea60b56790182b260e7f72e4a7ed3722` (r4, task 190, after the
owner's r3 review), in isolated worktrees; evidence in
`/anvil/scratch/onetree-gate-group-20260916/`; the owner's r1 review and its
cancellation probe in `/anvil/scratch/onetree-gate-group-owner-review-20260916/`.
Nothing was committed or pushed by the author. Main has since advanced to
`fa8e01c`; the task 185 parent-header change published in PR 27 also touches
`git.py`, in header parsing, and its diff applies cleanly on this candidate
(`git apply --check`). The combined tree is the Owner's to measure at
integration.
