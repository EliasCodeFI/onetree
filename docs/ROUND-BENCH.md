# Round: hermetic benchmark harness (task 26)

Author: claude:99edb3eb-febc-4531-8cee-8b0cffc16129, in the isolated worktree
`/anvil/worktrees/onetree-bench-99edb3eb-20260916`. Revision r2, on published base f190cfa0.
The r1 candidate (base 8fc954de, freeze `onetree-bench-r1-20260916`) was reviewed
CHANGES-REQUESTED by the Owner; its freeze and review evidence are preserved unchanged. This
is a frozen candidate for independent review. It is not a benchmark result, and it makes no
headline claim.

## What was built

`src/onetree/bench` runs one seeded synthetic corpus through the three systems of
`docs/PREREGISTRATION.md`, records raw events, computes every preregistered metric from those
events alone, and writes receipts in the `FREEZE.json` + `MANIFEST.sha256` format.

```sh
python -m onetree.bench run --out RUN --commit-payload GATE --message-file MSG \
  --seed S --writers N --tasks M --files F --conflict C [--json]
python -m onetree.bench verify --run RUN [--json]
python -m onetree.bench compare --first RUN_A --second RUN_B [--json]
```

- Seeds. A SHA-256 spawn tree over `(run seed, key path)` stands in for numpy's
  SeedSequence, which is not a runtime dependency. The derivation is named in every receipt
  so a stranger recomputes it with the standard library.
- Corpus. `F` files and `M` tasks per writer. A task appends one token line to each of its
  paths by read-modify-write. With probability `conflict` a path comes from a hot pool all
  writers share, otherwise from the writer's disjoint private pool. Every attempt writes its
  own token, so a retry never stands in for an overwritten first attempt.
- Writers are forked processes released together by a barrier and bounded by a run deadline.
  A writer that does not exit cleanly makes the run incomplete; nothing is re-run.
- onetree. Each attempt waits through the broker's read-only check until no peer holds its
  paths, then registers a fresh session, claims, writes, stages privately, lands and
  releases. The payload gate is the operator's admitted gate from `--commit-payload`, landed
  with the operator's message verbatim; the harness neither ships nor re-pins a gate. The
  harness's corpus referee runs inside each landing.
- worktree. One linked worktree and branch per writer at setup. An attempt resets to the
  target, commits, and merges `--no-ff` under one integration lock. A conflicted merge is
  aborted and recorded as a refusal.
- naive. One shared checkout and index, plain `git add` and `git commit`. Git failing is an
  error, never a refusal, because no layer exists to issue a receipt.
- The worktree and naive arms land without a referee, so the same corpus referee runs over
  each of their landed commits after the writers exit, one commit at a time.

## Operational choices a reviewer may dispute

The protocol text fixes the metrics; these are the choices this harness made where the text
leaves room. Each is stated once in `metrics.py` or `receipts.py`.

- Lost work is counted per write (one path of one attempt). A write is saved by its token
  reaching a landed payload or the final target tree at that path, or by a refusal event
  that lists it. A git error is not a receipt.
- A write stays uncommitted until its writer's first landed, refusal or error event that
  lists it, so for the worktree arm "uncommitted" means not yet on the target branch.
  A collision is an unordered pair of overlapping writes to one path by different writers.
- Integrity compares each landed commit's diff against its first parent with the writes of
  the submitting attempt: a path or content the writer did not produce is a foreign hunk,
  a written path absent from the diff is a missing hunk, and a deleted path is foreign.
- Throughput counts green landings over a window that ends only after each arm's
  green-verdict work. For onetree that is the writers' window, because its referee runs
  inside each landing. For worktree and naive it runs to the last harness referee verdict,
  and `referee_seconds` records the referee time inside it. A landing with no verdict is
  unverified, never green.
- Overhead is setup duration plus the larger of two disk samples, at setup end and run end.
  Layer disk is the layer's own directories plus objects reachable only from its refs.
- `verify` trusts no field of `FREEZE.json`; every claim committed raw data can check is
  recomputed, and each inconsistency is its own void. Claims nothing committed can check are
  listed under `unchecked`.
- `compare` compares only two runs that both verify, and then voids any differing count.
  Timing fields are listed as not compared, because racing processes cannot reproduce them.

## Experimental labels pending task 25

Task 25 has not fixed the grid, corpus or hardware note, and none of the following is an
approved protocol amendment. Each stays experimental until task 25 reconciles it, before any
affected formal run.

- Every run records its grid with status `PLACEHOLDER: not amended (task 25)` and carries
  `not_a_result` in `run.json`; `verify` voids a run of an unamended grid without that label.
  No CLI default stands in for a grid value.
- The SHA-256 seed derivation in place of the named SeedSequence.
- The two-sample disk maximum. No finite sample is claimed to be the measured peak.
- The timing fields `compare` excludes.
- Serial harness refereeing after the writers exit for the baseline arms. How a baseline
  fleet referees is the protocol's decision.

Measured runs and any headline stay gated by tasks 25 and 27. The hermetic leg re-runs inside
the repository suite at a tiny grid, locally only; no hosted CI was used.

## Corrections in r2

The Owner's r1 verdict is `/anvil/scratch/onetree-bench-owner-review-20260916/verdict.md`.

- **MAJOR-1: verify did not reconcile receipt metadata.** `verify` now checks, each with its
  own void:
  - the `FREEZE.json` summary against the metrics recomputed from events;
  - the declared systems as a non-empty, supported, unrepeated list that matches `run.json`,
    with any evidence for an undeclared system void;
  - seed, grid, derivation, clock, gate and referee digests against `run.json`;
  - the grid record against the payload its fields produce, and the not-a-result label;
  - the gate record's own pin flag;
  - the referee script against its recorded digest and the shipped referee;
  - `file_count`, `excluded_dirs` and the manifest file itself against `FREEZE.json`;
  - every write event against the seeded corpus, and every event's system and writer
    against the log that holds it;
  - the completeness flag against the run events.
- **MAJOR-2: compare accepted receipts verify rejects.** `compare` verifies both runs first.
  If either has voids it returns an unsuccessful, unverified outcome and compares nothing.
  It also compares the declared systems, and it reports a system missing from either run.
- **MAJOR-3: throughput excluded baseline referee work.** Harness referee events now carry
  `duration_s` and are timed when each verdict completes. The window ends at the last one
  when that is later than the writers' window.
- The schema moved to `onetree-bench-v2` because a metric definition and an event field
  changed. r1 receipts therefore fail `verify` with a schema void; that is intended.

## Evidence

Evidence lives in `/anvil/scratch/onetree-bench-20260916/r2/`; r1 evidence stays beside it.

- Base update. The worktree branch fast-forwarded from 8fc954de to f190cfa0 without touching
  the untracked bench paths (base-before.txt, base-after.txt). The 60 r1 bench tests passed
  on the new base before any r2 edit (tests-newbase.out).
- Full repository suite, run the way the landing referee runs it (full.out):
  22:04:31Z to 22:15:28Z, 1032 passed, total coverage 93.30% against the 90% floor. Only this
  record was edited after the run started. `ruff check` and strict `mypy` pass (lint.txt,
  types.txt).
- Real-gate smoke run (smoke-4, 22:03:33Z to 22:03:40Z) used the admitted gate
  `/anvil/.claude/tools/commit_payload.py`, whose digest matched the pin, with seed 7, two
  writers, two tasks, three files and conflict 0.5. `verify` reported no voids.

| System | Landed | Green | Refusals | Collisions | Lost | Integrity failed | Errors | Window ends at |
|---|---|---|---|---|---|---|---|---|
| onetree | 4 | 4 | 2 claim races | 0 | 0 | 0 | 0 | writers' window |
| worktree | 4 | 4 | 1 merge conflict | 1 | 0 | 0 | 0 | last referee verdict |
| naive | 3 | 3 | 0 | 2 | 1 | 1 | 1 | last referee verdict |

- Receipted command controls over private copies of smoke-4 (controls-plan.json,
  controls-run.txt, receipts.jsonl, chained, 7 rows, head cfda9ed0eeb5). They mirror the
  reviewer's known-bad inputs:

| Input | Command | Exit | Reason reported |
|---|---|---|---|
| untouched smoke-4 | verify | 0 | no voids |
| untouched smoke-4 | compare with itself | 0 | no voids |
| summary `lost_work.lost` forged to 999999 | verify | 1 | summary void naming the metric |
| `FREEZE.json` systems set to `[]` | verify | 1 | scope, inputs and summary voids |
| `metrics.json` `lost_work.lost` forged | verify | 1 | manifest, file and metrics voids |
| the same forged run | compare with itself | 1 | both inputs unverified; nothing compared |
| untouched against forged | compare | 1 | second input unverified |

## Known-bad controls in the suite

`tests/test_bench.py` holds 81 tests. Each check below is exercised against a known-bad input
beside a control that differs only in the property under test. No guard was removed from the
artifact to produce any of them.

- Metrics: an excused versus an unexcused write for lost work, overlapping versus disjoint
  intervals for collisions, foreign content, foreign paths, deletions and missing hunks for
  integrity, red or unverified landings, and slow versus fast referee verdicts for the
  throughput window.
- Receipts:
  - an edited event part, a forged metrics file and a forged corpus that re-freeze their
    own manifest;
  - a forged summary, including a boolean standing in for a count;
  - empty, unsupported, repeated and non-list scopes, and a scope forged consistently in
    both records;
  - each reconciled input field;
  - a false gate flag, a relabelled grid, a replaced referee script with updated digests,
    and an edited manifest file;
  - a write token and an event system forged with every derived file rebuilt to match;
  - added and missing files, a wrong schema, an unreadable freeze, unregenerable grids and
    absent parts;
  - comparisons against unverified runs, differing counts, timing-only differences, other
    seeds and missing systems.
- Systems, against real Git and the real broker:
  - a scripted naive interleaving loses a write and lands foreign bytes;
  - the same interleaving under onetree becomes a claim refusal and a clean landing;
  - a stale session base is refused as a payload conflict with the bytes restored;
  - a worktree merge conflict is refused with the target left clean;
  - an unadmitted gate never lands;
  - a held claim abandons with its reason;
  - a stuck index lock is an error;
  - a referee that sleeps before passing stays inside the worktree arm's throughput window.
- End to end through the CLI with forked writers, including a tampered part that fails both
  `verify` and `compare`, a dead writer and a writer past the deadline.

## Limits

- Forked writer bodies are not measured by coverage; the same functions are exercised
  in-process by the scripted interleaving tests. Base f190cfa0 measures the demo's spawned
  workers through `tests/conftest.py`; the bench's forked writers are not instrumented that
  way, which is a difference this round leaves visible rather than closes.
- Disk is sampled twice, not continuously.
- The corpus referee checks corpus shape only; it is not the repository's test suite.
- `verify` cannot check the gate file or the landing message, which live outside the run; it
  lists them as unchecked.
- The package lives in `src/onetree/bench`, inside the product package and its coverage
  gate. That custody choice is the author's and is open to Owner review.
- Tests run the onetree arm with the suite's exact-output clean gate from `tests/conftest.py`,
  so they need no live session identity; real-gate runs are the smoke runs.

## Disclosures

- A guard steered an anchored `rg` definition enumeration to `ast-grep` during r1 and again
  during r2 (steer-1.txt, r2/steer-2.txt); both enumerations were redone with ast-grep, not
  re-run with rg.
- The first real-gate smoke run exposed a harness bug: blind claim retries outran a real
  landing and recorded 32 claim refusals. The fix waits through the read-only check before
  claiming; later smoke runs record genuine claim races only.
- The first r1 test run caught error details keeping the last 300 characters of git output,
  which cut off the cause; details now keep the head.
