# Same-target landing contention (dogfood stream B, 2026-11-08)

Exploratory measurement, not the preregistered benchmark: nothing here is a
`docs/PREREGISTRATION.md` result and that protocol is untouched. Package under
test: `src/` of `6ef1c14` plus the instrument, copied to a temporary directory
so no concurrent edit in the shared tree was imported (`core.py` SHA-256
`b6249c5c629699629fc0023a20e7d7ed40fb4bd88e55e077f09c65eb2cdfc642`). Instrument
`src/onetree/bench/contention.py`, landed unchanged as `86da416` (SHA-256
`57e92a29b2bc915abf78739939cf6c58febfe0d86412ccfce5d7c7a5e4a1a491` for every run
below); tests `tests/test_bench_contention.py`. Host: WSL2 `Ubuntu-24.04-Lab`,
16 logical CPUs, Git 2.53.0, CPython 3.12.15, shared with two other dogfood
workers, their referees and an unrelated service; the 1-minute load average at
each run's start is in every run row (3.0 to 11.6).

Evidence directory `/home/lab/reports/onetree-dogfood-20261108/B-contention/`:
`grid/<set>/landings.jsonl` (one row per lander) and `grid/<set>/runs.jsonl`
(one row per run) for the sets `main`, `control`, `dose-<interval>`, `cliff`;
the driver `grid/run_grid.sh` and its `progress.txt`; `diag/` (offer-count
diagnostic); `redfirst/`; `shard/` (sharding re-check).

## Question

Under same-target contention, does a lander starve — lose its tested candidate
round after round, hit the landing retry limit, or wait unboundedly while
peers land? And what does contention cost in referee runs, which on the dogfood
clone are full-suite pytest runs of about seven minutes each?

## The mechanism measured

Read from `Coordinator.land`, `_tested_candidate_holds` and the
`train_pending` check in `src/onetree/core.py` at the base:

- Gate and referee run outside any lock. N landers that audit against the same
  head run N referees concurrently.
- The first to append its intent and move the branch wins. Every other lander
  then finds the target tip changed, so `_tested_candidate_holds` is false; the
  attempt is abandoned and the next one rebuilds on the new head and re-runs
  gate and referee. A lander that audits while a peer's intent is pending is
  refused `train_pending` (terminal for that call).
- An unrelated WAL append that beats an intent offer no longer voids the
  candidate (commit `187147e`): the offer is retried while the candidate holds,
  up to `MAX_CAS_RETRIES` (32) offers per attempt. If all 32 lose, the attempt
  is abandoned like a lost race, and the next attempt re-runs gate and referee.
- A `land` call makes at most `MAX_CAS_RETRIES` (32) attempts, then raises
  `landing CAS retry limit exceeded`.

## Method

`python -m onetree.bench.contention` builds a disposable repository per run
under `mktemp -d`: one file per lander, a protected workspace epoch, an
admitted CLEAN stub payload gate (the conftest stub's contract: echo the
candidate, repository and changed paths, verdict CLEAN) and a referee that
sleeps T seconds and appends one JSON line (candidate, baseline, start, end)
to a log. Each lander is a real process and the registered body of its own
session; the driver registers it on that pid, claims its one file and stages
it in the session's private index. On a shared start signal every lander runs
the real CLI, `python -m onetree.cli land --json`, as a child process, and
lands again after a `train_pending` refusal or a retry-limit error, up to 50
submissions; any other refusal is final. The optional control is one more
session, in its own process, that claims and releases an unrelated file every
`--control-interval` seconds until the landers finish.

Per landing (`landings.jsonl`): submissions and their outcomes, attempts
summed over submissions (from the CLI's `land.attempts`), referee runs (from
the referee's own log, attributed to a lander by the file its candidate
changes), intents and refusals per actor read back from the WAL, wall time
from the start signal to the landing submission's exit. Per run (`runs.jsonl`):
landed count, makespan, throughput, total and maximum referee runs, maximum
submissions, Jain's fairness index over per-lander referee runs and over
landing walls, branch commit count, and `inspect_wal().consistent`.

Grid: N ∈ {2, 4, 8} × T ∈ {0.2, 2, 10} s × 3 repetitions, no control, in an
order shuffled with seed 1108; then N ∈ {2, 4, 8} × T = 2 s × 3 repetitions
with the control at 0.5 s (seed 1109); then a dose sweep, N = 2, T = 2 s,
control interval ∈ {0.005, 0.02, 0.1, 0.5} s × 3 repetitions (seed 1110).
Sets ran in that fixed order; only cells within a set are shuffled.

## What the numbers would look like if starvation were absent (prediction)

With equal T and simultaneous starts, each round runs every remaining lander's
referee and lands one of them. The k-th lander to land then has made exactly k
attempts and k referee runs; a run of N landers makes N(N+1)/2 referee runs
(3, 10, 36 for N = 2, 4, 8), the maximum per lander is N, Jain's index over
referee runs is 3(N+1)/(2(2N+1)) (0.900, 0.833, 0.794), the makespan is about
N·T, every lander lands on its first submission, and no `train_pending` or
retry-limit outcome is required. Starvation would show as a lander with more
than N referee runs, a retry-limit or other refusal, a missing landing, or a
makespan well above N·T. The same arithmetic predicts a cliff the grid does not
reach: the last of N > 32 simultaneous landers needs N > 32 attempts.

## Results

### Main grid and the 0.5 s control (36 runs, 168 landings)

| N | T (s) | control | landed | referee runs | max per lander | max submissions | makespan (s), 3 reps | Jain (referee runs) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2 | 0.2 | — | 2,2,2 | 3,3,3 | 2 | 1 | 1.1, 1.1, 1.1 | 0.900 |
| 2 | 2 | — | 2,2,2 | 3,3,3 | 2 | 1 | 4.7, 4.7, 4.7 | 0.900 |
| 2 | 10 | — | 2,2,2 | 3,3,3 | 2 | 1 | 20.6, 20.7, 20.7 | 0.900 |
| 4 | 0.2 | — | 4,4,4 | 10,10,10 | 4 | 1 | 2.2, 2.2, 2.2 | 0.833 |
| 4 | 2 | — | 4,4,4 | 10,10,10 | 4 | 1 | 9.4, 9.4, 9.4 | 0.833 |
| 4 | 10 | — | 4,4,4 | 10,10,10 | 4 | 1 | 41.3, 41.3, 41.3 | 0.833 |
| 8 | 0.2 | — | 8,8,8 | 36,36,36 | 8 | 1 | 4.5, 4.4, 4.4 | 0.794 |
| 8 | 2 | — | 8,8,8 | 36,36,36 | 8 | 1 | 18.8, 18.9, 18.8 | 0.794 |
| 8 | 10 | — | 8,8,8 | 36,36,36 | 8 | 1 | 82.7, 82.6, 82.8 | 0.794 |
| 2 | 2 | 0.5 s (18 appends) | 2,2,2 | 3,3,3 | 2 | 1 | 4.8, 4.9, 4.9 | 0.900 |
| 4 | 2 | 0.5 s (36) | 4,4,4 | 10,10,10 | 4 | 1 | 9.8, 9.8, 9.9 | 0.833 |
| 8 | 2 | 0.5 s (70) | 8,8,8 | 36,36,36 | 8 | 1 | 19.7, 19.5, 19.6 | 0.794 |

Every one of the 168 landings landed on its first submission; the WAL recorded
no refusal of any kind, exactly one intent per lander, and every run's WAL read
back consistent. Attempts equal attributed referee runs for all 168. Landing
order is not launch order (24.5 % of positions match). The prediction above is
met exactly, in every repetition: **no starvation at N ≤ 8**, and the 0.5 s
control (about 4 appends per second, far above the dogfood clone's rate) adds
2–5 % makespan and no referee run — the `187147e` guarantee, in real processes.

The grid never produced a `train_pending` refusal. That path is not sampled
here; its behaviour is pinned deterministically by
`tests/test_referee_train_races.py`.

### Dose sweep: the offer bound

N = 2, T = 2 s, 3 repetitions per interval. Appends are the control's
claim-plus-release events over the run (its achieved rate falls short of the
nominal interval because each claim and release is itself a WAL CAS).

| control interval | appends per run | referee runs per run | max per lander | makespan (s) | outcome |
| --- | --- | --- | --- | --- | --- |
| none (main grid) | — | 3, 3, 3 | 2 | 4.7, 4.7, 4.7 | all landed, 1 submission |
| 0.5 s | 18, 18, 18 | 3, 3, 3 | 2 | 4.8, 4.8, 4.8 | all landed, 1 submission |
| 0.1 s | 64, 64, 64 | 3, 3, 3 | 2 | 5.0, 4.9, 5.0 | all landed, 1 submission |
| 0.02 s | 842, 354, 360 | 11, 5, 5 | 10, 4, 4 | 61.5, 18.0, 18.5 | all landed, 1 submission |
| 0.005 s | 3288, 3140, 2812 | 62, 49, 44 | 32, 30, 26 | 522.6, 485.0, 398.0 | all landed, 1 submission |

Up to about 13 appends per second (0.1 s: 64 appends in 5 s) the control
changes nothing. At 0.02 s (14 to 20 per second) referee runs grow; at the
0.005 s dose one lander used 32 attempts — the last one a `land` call is
allowed — and landed on it after 523 s. For scale, the dogfood clone's WAL
held 62 events from 07:40 to 08:38 UTC on 2026-11-08, at most 21 in any one
calendar minute (the sprint's setup burst): under 0.4 per second at peak
(`onetree wal --json`, 08:48 UTC).

### Diagnostic: how the extra referee runs arise

`diag/rebase_diag.py` (evidence directory) lands one file in-process with a
checkpoint counting intent offers per attempt while a control process appends
at a fixed interval (T = 0.2 s). Recorded per-attempt offers:

| interval | attempts | offers per attempt | appends | wall (s) |
| --- | --- | --- | --- | --- |
| 0.02 s | 1 | 11 | 44 | 1.3 |
| 0.02 s | 1 | 3 | 30 | 0.7 |
| 0.02 s | 7 | 32,32,32,32,32,32,29 | 486 | 29 |
| 0.02 s | 7 | 32,32,32,32,32,32,7 | 440 | 26 |
| 0.001 s | 23 | 32 × 22, then 19 | 2508 | 317 |

Every abandoned attempt made exactly 32 offers: none was voided by
`_tested_candidate_holds`; all were abandoned by the offer bound. Two further
0.001 s repetitions were stopped by the harness's time limit and are not
counted.

### The retry-limit cliff (N = 33, T = 0.2 s, one run)

All 33 landed. 551 referee runs (the prediction's 561 less ten), maximum 33 per
lander, makespan 22.2 s. `lander11` landed on its 32nd attempt; `lander20` was
refused `landing CAS retry limit exceeded` after 32 attempts on its first
submission and landed on its second (33 attempts in all). The cliff is where
the arithmetic puts it: with more than 32 simultaneous same-target landers the
last one needs a second `land` call. Raw rows: `grid/cliff/`.

## Controls and red-first observations

- Offer-bound control, deterministic. `tests/test_bench_contention.py` injects
  one unrelated claim or release at the offer seam for the first K offers.
  K = 31: one referee run, one attempt, 32 offers. Run against `c08be38` (the
  parent of `187147e`), the same test fails `assert (32, 32, 32) == (1, 1, 32)`:
  every unrelated append voided the candidate. So the test, and the grid's
  control column that agrees with it, would have shown the defect had it been
  present (`redfirst/offer_bound_on_c08be38.txt`; the same at `7e0bc81`).
- Offer-bound gap, deterministic. K = 32: two referee runs, two attempts, 33
  offers on the base. This asserts the gap's shape; a fix flips it.
- Instrument control, pre-fix code. The same instrument run against
  `c08be38`, the parent of `187147e` (N = 2, T = 2 s, control 0.5 s), was
  stopped by hand after its first cell had run about six minutes: 294 referee
  runs, no `land_intent`, no landing, branch at baseline, WAL = 560 claim and
  560 release events from the control plus the setup rows
  (`grid/prefix-c08be38.partial.txt`). The fixed code's identical cell: 3 runs,
  2 landings, 4.8 s. So the grid's control column is evidence: had unrelated
  appends still voided tested candidates, it would have shown starvation. A
  first attempt at `7e0bc81` measured nothing: that commit predates
  per-repository gate admission and every landing failed `payload_gate_error`
  (`grid/prefix-7e0bc81/`).
- The instrument's attribution: `referee_runs` per lander comes from the
  referee's own log and `attempts` from the CLI's JSON; they agree for all 168
  grid landings, and `branch_commits` equals N in every run.

## Referee suite cost (catalogue task 4), same day

- The `during-referee` case of
  `test_session_body_death_in_each_window_leaves_a_dead_session_the_broker_names`
  took 60.86 s on this tree. Cause: the window never released its blocking
  fixture referee, so the live peer's landing after the death ran a second
  referee that slept the fixture's full 60 s. The test now releases it once the
  dead body's referee is asserted dead: 0.83 s (landed as `4403284`). Red-first:
  with the referee supervisor made to ignore lander death the changed test
  fails `timed out waiting for referee cleanup`, and with a broker that treats a
  dead body's pid as alive it fails `DID NOT RAISE LandingRefusedError`; an
  unpatched copy passes (`redfirst/` in the evidence directory). The first
  dogfood referee run that carried it: 1640 passed, 4 skipped, 391 s.
- Four-way sharding (`shard/run_shards.sh`: test files of `6ef1c14`
  round-robin into 4 concurrent pytest processes, 2 repetitions): both
  repetitions failed exactly the four
  `test_cancellation_signals_the_group_before_the_leader_is_reaped` cases, all in
  the shard holding `tests/test_payload_gate_group.py`, with
  `worker.exitcode == None` after `join(timeout=20)`; every other shard passed.
  8 of 8, not a rare flake. The same four cases went red in two serial dogfood
  referee runs that day under concurrent load. The owner seat's `43b0527`
  widened that test's bounds; sharding was not re-measured after it.

## Not established

- The referee is a sleep. Real referees compete for CPU: parallel full-suite
  runs slow each other, so in production T grows with N and the quadratic
  referee work below costs wall time as well as CPU. Not modelled.
- Simultaneous starts are the worst case for wasted runs and the best case for
  fairness; staggered arrivals were not measured.
- One host, one filesystem, protected workspace on, no other WAL traffic than
  the control.

## Findings and recommendation

1. **No starvation at N ≤ 8.** The mechanism is fair in the sense measured:
   every lander lands, on its first `land` call, within N rounds.
2. **Referee work is quadratic.** N simultaneous landers cost N(N+1)/2 referee
   runs for N landings; N(N−1)/2 of them test a candidate that cannot land.
   With a seven-minute full-suite referee, three dogfood workers landing
   together spend six referee runs for three landings, concurrently. On this
   host on 2026-11-08 a dogfood referee run took 928 s instead of about 420 s
   under concurrent load, and the load-sensitive
   `test_cancellation_signals_the_group_before_the_leader_is_reaped` cases
   went red in two of the owner seat's landings. That cost, not starvation, is
   what contention does to the dogfood clone today.
3. **The offer bound converts sustained unrelated appends into referee
   re-runs**, and in the limit into the retry limit (dose sweep). The append
   rate required is far above anything the dogfood clone produces, so this is a
   recorded gap, pinned by a test, not a current failure.

Recommendation, smallest first:

- **Per-target landing lease (performance only).** Serialize the
  gate-referee-intent-branch section per target with an advisory `flock` on a
  file under the repository's Git directory, taken after the audit and
  released after the landed mark or refusal. Correctness keeps resting on the
  WAL and branch CAS; a dead holder's lock is released by the kernel; a lander
  that waits rebuilds on the head it finds. Expected effect: N referee runs for
  N landings, makespan unchanged at about N·T, and no concurrent referees
  slowing each other. Not implemented here: existing fixtures depend on two
  landings being inside that section at once —
  `test_referee_retests_candidate_after_wal_and_target_race` and
  `test_later_intent_cannot_overtake_pending_target` (`tests/test_referee.py`)
  call `land` from inside another `land`'s checkpoint in the same process,
  which the lock would deadlock, and `tests/test_referee_train_races.py` holds
  two lander processes at a barrier after both referee runs, which the lease
  forbids — so the change is not bounded to one file and belongs in its own
  round with those fixtures re-shaped.
- **Offer bound.** Keep offering while `_tested_candidate_holds` is true, and
  bound the whole call by the existing total (32 attempts × 32 offers) instead
  of abandoning the tested candidate after 32 offers. Red-first fixture:
  `test_unrelated_appends_exhausting_the_offer_bound_rerun_the_referee` flips
  from (2, 2, 33) to (1, 1, 33). Not implemented: no dogfood-rate evidence that
  it matters, and `core.py` is a shared hot file this round did not need to
  touch.
- A real train (testing candidate k atop candidates 1..k−1 speculatively) would
  cut the makespan as well; it is a design question for the owner, not a fix.
