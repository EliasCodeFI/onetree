# Gate 2, task 16 completion: session-body death and death inside commit_tree

Base `88ccf21c5411070418736b61d8223fea8ae7d6dc` (published main after PR 11,
carrying the task-171 referee supervisor and the task-18 batch checks).
Author checkout `/anvil/worktrees/onetree-g2-session-death-e85636f2-20260916`,
branch `claude/onetree-g2-session-death-e85636f2-20260916`, tests importing
`onetree` from that checkout's `src` (evidence `import-origin.txt`). Fixture
module `tests/test_referee_session_death.py`. Evidence directory
`/anvil/scratch/onetree-g2-20260916/task16-parent-evidence/final-axes/`.

`ROUND-G2-DEATH.md` left two axes open under "Not established": death inside
`commit_tree`, between the tree seam and the referee, and a submitter that is
also the registered session body dying in the referee-era windows. This
round closes the fixture work on both. It is task-16 completion, not Gate 2
closure. Nothing in `src/`, `tests/conftest.py` or any shared helper changed;
the existing conftest Git shim was not extended, because the death under
test here is reached by redirecting one process's Git layer, the instrument
`test_referee_lifetime.py` already uses, not by a kill inside `update-ref`.

## Method

Every lander is a real forked process running `Coordinator.land` with the
phase-logging checkpoint, SIGKILLed from outside, joined, and asserted to
have exited `-SIGKILL`, the shape of `test_referee_crash_windows.py`, whose
helpers (`_append`, `_records`, `_kill`, `_wait_for`, `_Referee`,
`_reap_referee`, `_assert_no_landing_trace`, `_object_type`, `_reachable`,
`_kinds`) are reused, with `_alive` from `test_referee.py`.

Session body. In the session-body cases the lander registers the session on
its own pid from inside the forked process, claims `a.txt`, stages its
payload and logs its pid, birth tick (field 22 of `/proc/self/stat`, the
same tick `onetree.proc.process_start` reads), session generation and claim
generation before landing. After the kill the test reads the WAL's
`session_register` row and, in the recorded windows, the intent's `lander`
block, and requires both to name that pid and tick. "Exact identity" is
therefore read from the WAL against the dead process's own record, not from
the test's bookkeeping.

Commit-tree. The commit-tree cases redirect the lander's Git layer
(`onetree.git._GIT_EXECUTABLE`, set in the forked process only) to a front
that passes every command to Git unchanged except the `commit-tree` whose
tree argument is the tree the `after_tree` seam logged. The WAL writes its
own event rows with `commit-tree` too, so the front must select by tree; the
first revision matched by argument position and intercepted the session
registration instead (`session-death-run1.txt`, `run2.txt`). For the
candidate tree the front writes a marker from inside the invocation carrying
its pid, parent pid, argv and mode, then blocks. In `before` mode Git never
runs. In `after` mode the front runs the real `commit-tree` first, records
the object id Git returned, and then blocks, so the object exists and its id
is held only by a process the test is about to kill. Either way the lander
is blocked in `subprocess.run` inside `Repository.commit_tree` when it dies.

What this does and does not place. Both commit-tree cases are deaths at the
wrapper boundary. The `before` mode is the last instant before Git's object
write; the `after` mode is the first instant after it. A death inside Git's
own object write, between the loose-object temp file and its rename, is not
produced by any marker outside Git, and this module does not claim it. The
two modes bound it from both sides, and the object-store census below says
what each side leaves.

## Session-body windows and outcomes

Each row: the body is one process that registered, claimed, staged and
landed, killed at the named seam (the referee window on the referee's own
marker). `k` is the WAL kind list already on the WAL before the fork.

| Window | Reached phase | WAL after death | Recovery | Dead actor | Peer claim and landing |
| --- | --- | --- | --- | --- | --- |
| `after_audit` | `["after_audit"]`; referee 0 | `k + session_register, claim`; consistent; 0 pending | 0/0/0 | `land` refused `session_inactive`, recorded | claim granted at once; lands own payload; referee 1; one `landed` |
| `after_tree` | `[..., "after_tree"]`; referee 0 | as above | as above | as above | as above |
| during the referee | `[..., "after_tree"]`; referee marker; referee 1; the supervisor's cleanup (task 171) reaps the referee and scratch after the body dies | as above | as above | as above | claim granted; lands; referee 2 |
| `before_intent` | `[..., "before_intent"]`; referee 1; candidate is a commit reachable from no ref | as above | as above | as above | claim granted; lands; referee 2 |
| `after_intent` | through `after_intent`; referee 1 | `k + session_register, claim, land_intent`; 1 pending; no divergence; branch at base; intent `lander` = {body pid, body tick}, `session_generation` and fence generations = the body's | `refused incomplete_intent` (0 repaired, 1 refused) | refused `session_inactive` | claim granted before recovery; landing refused `train_pending` before recovery; after recovery lands, 1 commit over base, one `landed` |
| `after_branch` | through `after_branch` | as above but branch at candidate | `landed` with `recovered: true` (1 repaired, 0 refused) | refused `session_inactive` | claim granted; landing refused `train_pending` before recovery; after recovery lands atop the recovered candidate, 2 commits over base, two `landed` |

Every recorded window ends `inspect_wal` consistent, zero pending, zero
divergences, and `git fsck --no-dangling` clean.

Claim and fence disposition, as observed. A dead session's claims are not
active claims: `active_claims` requires the owning session alive, so a live
peer's claim on the same path is granted immediately after the death, in
every window, without recovery. What holds the peer back in the recorded
windows is not the claim but the pending intent: with a referee configured,
a landing whose target has a pending intent is refused `train_pending`
until recovery resolves that intent, and recovery does so by the intent's
own `lander` liveness (dead, so `incomplete_intent` when the branch never
moved, `landed`/recovered when it did). No claim of the dead session fences
anything after recovery. This is the behaviour of the artifact; no contract
in `ROUND-B.md` is contradicted by it, and none is claimed for it beyond
what the rows show.

## Commit-tree windows and outcomes

| Mode | Reached phase | Object store | WAL, branch, recovery | Retry |
| --- | --- | --- | --- | --- |
| `before` | `["after_audit", "after_tree"]`; marker with the candidate tree, parent pid = the lander; referee 0 | the candidate tree exists; dangling commits: none | no landing trace; dead actor refused `session_inactive` | peer lands; referee 1; dangling commits still none |
| `after` | as above; marker carries the id Git returned, exit 0 | exactly one dangling commit: that id, tree = the logged candidate tree, parent = base, reachable from no ref | as above | peer lands a different commit; the dead attempt's commit stays dangling and unreachable |

The census is `cat-file --batch-all-objects` filtered to commits minus
`rev-list --all` (which includes the WAL ref). The same census finds the
`after` mode's object and finds nothing in `before`, so "no object was
written" is a measurement with a known positive.

## Controls

- `-SIGKILL` exit and the phase log distinguish the kill from a refusal or
  return; no case observed a `returned` record.
- The dead session is named by the broker: its actor's next `land` is refused
  `session_inactive` and the refusal is on the WAL, in all eight death cases.
- Instrument control for the front
  (`test_commit_tree_front_reports_from_inside_the_invocation`): `--version`
  passes through; a `commit-tree` of a tree the log never named passes
  through to Git and fails there without a marker; only the logged tree,
  given in the package's real argv shape (common options and `-C` before the
  subcommand), records a marker whose parent pid is the calling process.
- Failure-path cleanup (`failure-cleanup-probe.txt`): private copies of the
  module with an `AssertionError` injected after the commit-tree marker and
  after the referee marker reported the injected failures (3 cases) and left
  0 front, referee or supervisor processes after pytest exited. The copies
  were probes, deleted before the freeze; the candidate has no such line.
- The zombie-landing red control for the no-trace assertions stands in
  `test_referee_crash_windows.py` and is not repeated here.

## Gates in this checkout

- `pytest tests/test_referee_session_death.py --no-cov`: 9 passed, four
  consecutive runs (`session-death-run4..7.txt`); `run1..3` are the
  revisions described above.
- Local CI, the `ci.yml` steps run here per the owner's instruction, with
  `ONETREE_TEST_ESTATE_GATE` and `ONETREE_TEST_SHIPPED_GATE` both set to
  `/anvil/.claude/tools/commit_payload.py`: install exit 0, `ruff check src
  tests` exit 0, `mypy` exit 0, `pytest` 661 passed, 0 skipped, 0 failed,
  coverage 91.78 % (`local-ci-final-axes.txt`).

## Not established

- Death inside Git's native object write during `commit-tree` (between its
  temporary file and the rename into `objects/`) was not produced; the two
  modes bound it and no claim is made about what that interval leaves.
- The interval between the kill and the supervisor's cleanup in the referee
  window is bounded only by the fixture's 5 s wait, as in task 171.
- A session body that dies while a *different* incarnation is landing for
  it (INV2's fence re-sample with a separate lander) is covered by
  `test_inv2_session_death_between_audit_and_fence_refuses` and was not
  combined with these windows.
- The dead actor re-registering from a new process and reusing its staged
  index was not exercised; only a different live actor's fresh work was.
