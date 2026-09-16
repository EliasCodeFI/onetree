# Gate 2, task 17: competing referee trains

Base `737492689758c54b8dc5674204e8d163e20dd13b` (published main after PR 9).
Author checkout `/anvil/worktrees/onetree-g2-trains-e85636f2-20260916`, branch
`claude/onetree-g2-trains-e85636f2-20260916`, tests importing `onetree` from
that checkout's `src` (evidence `import-origin.txt`). Fixture module
`tests/test_referee_train_races.py`. Evidence directory
`/anvil/scratch/onetree-task171-20260916/task17-parent-evidence/`.

Nothing in `src/`, `tests/conftest.py` or any shared helper changed. No
implementation guard was altered to obtain evidence.

## What the existing evidence does and does not establish

- INV3 (`test_land.py::test_inv3_real_race_has_one_first_attempt_winner_and_both_payloads_land`)
  races two real lander processes with no referee configured: one wins on its
  first attempt, the other lands on a later attempt, both payloads survive.
  It says nothing about a referee.
- Round B's interleave/retest cases
  (`test_referee.py::test_referee_retests_candidate_after_wal_and_target_race`,
  `test_later_intent_cannot_overtake_pending_target`) drive a second landing
  from inside the first one's checkpoint callback, in one process. They show
  that a stale audit is invalidated and that a retry is re-tested, and that a
  pending intent refuses a later submission with `train_pending`.
- Their separate existence does not show the composition: that the losing
  *process* of a real race rebuilds atop the winner's commit and runs the
  referee against the winner's state before its own intent is recorded, or
  that a combined-red pair is refused at that retest.

## Method

Two real landers (`multiprocessing`, fork context, the INV3 shape) each run
`Coordinator.land` with a checkpoint that appends a durable phase record per
seam, keyed by the landing operation id so a lander's passes can be told
apart. The configured referee is a script that, from inside the candidate
checkout it is handed, appends one line per execution naming the candidate
and baseline it was given, the bytes of `a.txt` and `b.txt` it saw, its own
pid, the chain of processes above it read from `/proc` while they are alive
(pid, parent pid and argv, up to four levels), and its verdict. Each lander
returns its landing
receipt (or its refusal and referee receipt) through a queue.

Rendezvous. A three-party barrier at the first `before_intent` holds both
landers after their first referee run and before their intent append; the
test is the third party. Before joining it the test observes: two referee
runs, both against the base, both green, one seeing only alpha's payload and
one only beta's, executed for the live lander that built the candidate
(the recorded chain resolves to the lander's pid, see Provenance under
Controls); the branch at base; nothing appended to the WAL.
That is "both submitters start green against the same baseline", measured.

Holds. An ordered case then holds one lander on a gate file the test opens at
a chosen moment. The compatible and combined-red cases hold the loser at
`before_intent` until the winner has landed. The pending-intent case holds
the winner at `after_intent` and the loser at `before_intent`, releases the
loser first so it meets the winner's intent while it is still pending, and
releases the winner only after the loser's refusal is on the WAL. The
unordered case opens no gate: both race for the intent CAS.

Composition is read from four surfaces after the fact: the referee log, the
phase logs, the receipts the landers returned, and the branch and WAL.

## Cases and outcomes

| Case | Winner | Loser's path | Referee runs | WAL appended | Branch |
| --- | --- | --- | --- | --- | --- |
| Ordered, compatible pair | alpha, attempt 1 | one abandoned intent (WAL CAS lost), one rebuild; third referee run: baseline = winner's commit, candidate = loser's landed commit, sees `a1`+`b1`, pass, chain resolves to the loser's process; loser `attempts` = 2 | 3 | `land_intent`, `landed`, `land_intent`, `landed`; the loser's intent has `expected_head` = winner's commit and carries the retest receipt | two commits over base, both payloads; the loser's first green candidate reachable from no ref |
| Ordered, pending intent | alpha parked at `after_intent` | re-audits into the pending train; refused `train_pending`, recorded while the winner is still parked; second pass ends at `after_tree`, no referee run; the later retry (run by the test) is a third referee run against the winner and lands | 2, then 3 | `land_intent`, `refused`, `landed`, then `land_intent`, `landed` | winner's commit, then both payloads |
| Unordered real race | whichever, attempt 1 | one of: rebuild and retest in two passes; rebuild and retest after one or more abandoned audits whose refusal append lost the WAL CAS; or a recorded `train_pending` refusal followed by the retest. The case asserts the branch that occurred against the same helpers the ordered cases use | 3 | as the ordered case for the branch taken | both payloads |
| Ordered, combined-red pair | alpha, attempt 1 | rebuilt candidate carries both payloads; third referee run: baseline = winner's commit, sees `a1`+`b1`, red, exit 1, chain resolves to the loser's process; refused `referee_red` with that receipt; second pass ends at `after_tree` | 3 | `land_intent`, `landed`, `refused` with `referee_receipt.baseline` = winner and `tested` = the rebuilt candidate | winner's commit only; `b.txt` still `b0`; neither of the loser's candidates reachable |

Every case ends with `inspect_wal` consistent, zero pending intents, zero
divergences, and `recover` reporting nothing to do.

Observed distribution of the unordered race over the 15 retained runs of
the final module (`unordered-branch-tally-final.txt`, runs 24 to 38): two-pass
retest 13, retest after one abandoned audit 2, recorded refusal 0; the loser
was beta in 14 runs and alpha in 1. The 4 tallied revision-1 runs
(`unordered-branch-tally.txt`) were all two-pass retests, and revision-1
run 4 (`train-races-repeat4.txt`, a failure at the time) was the first sighting
of the abandoned-audit path. The recorded-refusal branch of the unordered case
is therefore exercised only through the deterministic pending-intent case,
which runs the same assertion helpers; the unordered case's code for it has
not been observed taking that branch on this host.

## Controls

- The referee log's tree fields are read from the checkout on every run: the
  two first runs read `a0`/`b1` and `a1`/`b0`, the retest reads `a1`/`b1`.
  `test_referee_log_reads_the_checkout_it_runs_in` runs the same script by
  hand in two hand-made checkouts and reads two different records with the
  test's own pid as parent; a script reporting anything but its checkout
  would make every `a`/`b` assertion vacuous.
- The rendezvous itself: `test_rendezvous_holds_both_landers_before_any_intent`
  never joins the barrier and, one second later, finds both landers alive,
  nothing on the WAL and the branch at base, with and without a gate.
- Known-bad input: the combined-red referee is green for each payload alone
  (the two first runs) and red only for the pair; the compatible cases with
  the same script are the positive control that the refusal is not
  unconditional.
- Provenance: every retest's recorded chain resolves to the losing lander's
  pid, and every landed intent's `expected_head` is the commit the referee was
  given as baseline, so "the loser's referee saw the winner" is read from the
  referee's own record and the WAL, not from the test's callback. The helper
  (`_lander_link`) requires the chain to be consistent link by link, to reach
  the lander, and to pass only through processes whose argv names a file
  under the installed `onetree` package directory; it returns those
  intermediates. The instrument control asserts the helper returns no
  intermediates for a script run by hand and raises for an attribution to
  the test process's own parent, which would have to pass through the test
  runner (the known-bad input for the helper).

## Found on the way

- A third path for the loser exists and is consistent with the contract:
  its retry sees the winner's intent pending, refuses `train_pending`, but the
  refusal's WAL append loses the CAS to the winner's landed mark, so
  `_record_land_refusal` returns false and the loop re-audits; the third pass
  rebuilds and retests. No refusal row results. It occurred in 2 of the 15
  final unordered runs and once in revision 1. The first revision of these
  fixtures pinned exactly two passes and failed on it (revision-1 run 4);
  the assertion now pins the shape of every pass
  (first pass through the referee, any middle passes abandoned at
  `after_tree`, last pass landing) and exactly one retest.
- The first revision of the pending-intent case held only the winner at
  `after_intent`; after the barrier the two still raced for the intent CAS,
  and when the intended loser won it the hold never engaged (3 of 12 runs).
  The case now holds both. Both corrections are fixture-side; the
  implementation was not touched.

## Gates in this checkout

- `pytest tests/test_referee_train_races.py --no-cov`: 7 passed; fifteen
  further runs of the final module 7 passed each (`train-races-run3.txt`,
  `train-races-repeat24..38.txt`).
- Local CI, the `ci.yml` steps run here because GitHub CI may be out of
  usage: install exit 0, `ruff check src tests` exit 0, `mypy` exit 0,
  `pytest` with the coverage gate exit 0 (`local-ci-run2-final.txt`,
  `local-ci-run3-final.txt`): 609 passed, 31 skipped, 0 failed, coverage
  91.79 %. The task-170 failure seen on base 78785a9 during task 16 does not
  occur on this base; this candidate has no bearing on that disposition.

## Revision r2 (after the independent review of r1, task 177)

The r1 freeze `onetree-g2-trains-r1-20260916` (manifest `08244c60c673431e`)
is preserved. The review (`task17-owner-review-r1.md`, changes requested,
integration debt not an execution miss) ran the r1 module against a private
copy of the task-171 referee-lifetime tree
(`/tmp/round-freezes/onetree-referee-lifetime-r3-20260916/tree`, pending
publication), in which the referee is launched by an installed supervisor
process rather than by the lander. Six cases failed at the r1 assertion
`run["ppid"] == first["pid"] == race.processes[actor].pid`
(`task17-review-integrated.txt`): the referee's parent is the supervisor.
Deleting the check would have removed the evidence that the losing lander's
own execution ran the retest, so r2 changes the instrument and the helper
instead. Nothing in `src/`, `tests/conftest.py` or shared helpers changed in
either tree; the integration copy carries the frozen task-171 bytes
unmodified plus the two owned files.

- The referee script now records the chain above it from `/proc` (pid,
  parent pid, argv per level, at most four levels) instead of one parent pid.
- `_lander_link(run, lander_pid)` replaces the four direct-parent assertions
  (rendezvous, compatible retest, refusal retry, combined-red retest). It
  requires a link-consistent chain that reaches the lander through
  intermediates whose argv names a file under the installed package
  directory, and the rendezvous additionally asserts the lander is alive at
  the moment its first run is attributed to it.
- Observed chains (`r2/basetemp-*-run1` referee logs): on base 7374926 every
  run's parent is the lander itself, no intermediates; on the combined tree
  every landing run's parent is `python -I .../onetree/_referee_supervisor.py`
  whose parent is the lander, one intermediate. The two runs the control case
  makes by hand have the test process as parent on both trees.
- Known-bad for the helper: the control case asserts `_lander_link` raises
  when asked to attribute a hand-run script to the test process's parent,
  because the path would cross the test runner, which is not under the
  package directory. The r1 assertion failing on the combined tree is the
  known-bad for the old form and is retained in the review's evidence.

Gates on r2, both trees, the `ci.yml` steps run locally:

| Tree | Module | Repeats | ruff / mypy | Full suite with coverage gate |
| --- | --- | --- | --- | --- |
| Submitted base 7374926 + r2 files | 7 passed | 11 of 11 | clean / clean | 609 passed, 31 skipped, 0 failed, 91.79 % (`r2/local-ci-base.txt`) |
| Task-171 r3 tree + r2 files (private copy) | 7 passed | 11 of 11 | clean / clean | 617 passed, 31 skipped, 0 failed, 91.70 % (`r2/local-ci-integrated.txt`) |

Unordered race on r2 (`r2/unordered-branch-tally-r2.txt`): base, 11 runs,
two-pass retest 10, retest after one abandoned audit 1, recorded refusal 0;
combined tree, 11 runs, two-pass 9, abandoned-audit 2, recorded refusal 0.
The recorded-refusal branch of the unordered case remains unobserved on
this host and is covered only by the ordered pending-intent case.

Not established by r2: the package-directory criterion accepts any
intermediate the installed package launches, not specifically the
supervisor; a chain longer than four levels between referee and lander
would fail the helper rather than be attributed; the combined tree was
validated as a private copy of the frozen task-171 r3 bytes, not as a
published base.

## Not established

- Landers here are forked from the test process and share its loaded
  modules; a race between two separately started CLI processes was not run.
- The unordered case's recorded-refusal branch was not observed on this
  host (see above).
- Three or more concurrent submitters, and a loser whose retest is red
  while a third submitter lands, were not exercised.
- Whether a referee that reads state outside its checkout could make a
  retest pass or fail differently is unknown; the referee here reads only
  its checkout.
