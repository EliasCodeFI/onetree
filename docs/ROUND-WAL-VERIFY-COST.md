# Task 184: one Git read per call for landed-evidence verification

Task 65 made every mutation verify the recorded landed and adopted commits
against Git before writing, the way inspection already did. Its review measured
the price: four Git processes per recorded landing on every call (a `rev-parse`
probe, two `cat-file` reads and a `show`), so a registration on a repository
with L recorded landings spawned 17 + 6L processes against 17 + 2L before.

`Coordinator._verify_landed` now asks `Repository.landed_evidence` for every
ordinary commit the records name in one `cat-file --batch`, and checks the
evidence it returns: parent (first parent for an adopted record, sole parent
for a landed one), tree, and for an adopted record whether the message carries
the recorded operation, judged one operation at a time by the same reducer the
single-object reader uses. Each raw body is parsed as it arrives and dropped;
what is kept per commit is its parents, its tree and the confirmed operations.
Any record whose object Git does not hand back typed `commit` keeps the
per-object path unchanged: `require_commit`, `commit_parents`,
`show --format=%T` and `commit_has_landing_operation`, in the same order, with
the same typed outcomes. That covers an object that is absent, a loose object
Git cannot inflate (which `--batch` also reports as `missing`), a blob, a tag,
and a name that is not an object ID. Nothing is retained across calls; the
object store is re-read by every mutation and inspection. Pending intents are
not read by this verifier and their recovery path is untouched.

The batch protocol reader `_cat_file_batch` is shared with the history walk
(`_marked_commits_from_oids`) and is a generator: it yields one `(oid, type,
body)` at a time and keeps none, so each consumer holds one object, not the
history. Requested names are deduplicated by dictionary key, in first-seen
order, so filtering does no equality scan.

## Round r2: corrections from the owner's r1 review

The r1 candidate (freeze `onetree-wal-verify-cost-r1-20260916`, left untouched)
was returned changes-requested. Its shared helper collected every raw body into
a list before returning, which changed the existing history walk: on a real
repository of 256 ordinary commits with one-mebibyte messages and no WAL, under
a 160 MiB address-space ceiling, published main read the WAL as consistent and
r1 raised `MemoryError`. Its name filter also scanned a growing list, measured
by the owner at 8128, 32640 and 130816 string comparisons for 128, 256 and 512
names. And its record attributed a 744-test run to the published base that was
in fact the task 65 r1 result on an earlier tree.

r2 makes the reader a generator and the verifier's evidence bounded as above,
keys deduplication on a dictionary, and carries both regressions as tests:

- `test_a_large_ordinary_history_is_read_within_a_bounded_address_space`
  rebuilds the owner's repository with `fast-import` and reads it in a child
  interpreter under the same 160 MiB ceiling. On the r1 tree the 256-commit
  case fails with `MemoryError` through `_unrecorded_landings` and the
  8-commit control passes (`red-first-r2-on-r1.txt`); on r2 both pass.
- `test_duplicate_filtering_does_not_grow_with_the_request` forwards every
  string comparison through an observer that proves itself on one true and
  one false comparison, then requires the count to stay at or below the number
  of names for 128, 256 and 512 names; on r2 it does. The r1 tree has no
  `landed_evidence` and the owner's `test_dedup_cost.py` is the measurement of
  its scan.

## Measured

Every count is a real child process observed at `subprocess.Popen`, which
`subprocess.run` is built on, so the batch read is counted like any other. The
counter proves itself against one known `rev-parse` before it is believed.

Processes started inside `_verify_landed`, one `register_session` or
`inspect_wal` call, both SHA-1 and SHA-256 repositories, files backend:

| recorded landings | base 44b5675 | candidate |
| ---: | ---: | ---: |
| 1 | 4 (cat-file 2, rev-parse 1, show 1) | 1 (cat-file) |
| 5 | 20 | 1 |
| 20 | 80 | 1 |

Red first: the census assertion failed on the unchanged base with those counts
(`red-first.txt`, `red-first-final.txt`, `red-first-final.xml`) and passes on
the candidate (`green-focused.txt`, `green-focused.xml`).

Wall time and total processes for one call after L adopted records, median of
five, one host, SHA-1 (`bench.jsonl`; the bench is private and not shipped).
This is the **r1 measurement**: r2 and r3 change what is retained and how a
header is normalized, not the number of processes, and the census test above
re-verifies one process per call on every revision. It has not been rerun on
r2 or r3.

| L | register base | register r1 | inspect base | inspect r1 |
| ---: | ---: | ---: | ---: | ---: |
| 0 | 0.058 s / 25 | 0.056 s / 25 | 0.053 s / 21 | 0.052 s / 21 |
| 20 | 0.316 s / 145 | 0.152 s / 65 | 0.362 s / 161 | 0.208 s / 81 |
| 100 | 1.377 s / 625 | 0.533 s / 225 | 1.795 s / 721 | 0.906 s / 321 |

The remaining per-record cost is unchanged here. Its attribution, corrected by
task 194: the `ls-tree` and `cat-file` pair is paid by `Wal.read` once per WAL
event on every call (task 66 R5), which this table shows per landing because
each adopted record is one event; task 194 replaces it with three
`cat-file --batch` reads for a log in the shape the writer produces. The
`merge-base --is-ancestor` once per recorded landing on inspection is
`_divergences`, which task 195 tracks. Neither is the history audit of task 61.

## Controls

`tests/test_wal_verify_cost.py`, real `GitFixture`, both object formats:

- `test_a_record_git_cannot_confirm_is_refused_before_any_write`: a recorded
  landing that is not the branch tip (so target resolution still succeeds and
  only verification can refuse) is damaged one way at a time and inspection,
  registration and claim must each refuse it with the WAL head and the target
  unchanged. Missing object and blob object: WalCorruptError "does not
  resolve". Tag object, wrong parent, wrong tree, wrong operation:
  WalCorruptError naming the differing field. A loose object overwritten with
  bytes Git cannot inflate, and one cut in half: GitError, not WalCorruptError.
  All 48 cases pass on the base and on the candidate; they hold the admission
  semantics fixed rather than proving the change.
- `test_an_object_that_vanishes_under_a_stable_wal_head_is_refused_until_it_returns`:
  a valid landing is admitted, its loose object is removed, the next
  registration and inspection refuse with "does not resolve" while the WAL
  head is unchanged, and the same registration succeeds once the bytes are
  back. Passes on base and candidate.
- `test_recovery_semantics.py::test_infrastructure_failure_while_verifying_a_landing_is_not_called_wal_corruption`
  used to inject its failure by replacing `Repository.require_commit`, which
  the batched read no longer consults for an object Git hands back, so the
  injection would have measured nothing. It now cuts the landed commit's loose
  object in half; on the base the failure surfaces from `rev-parse`, on the
  candidate from `cat-file --batch`, and both are GitError. It passes on both.

Not measured: a hostile filesystem race between the batch read and a later
probe (the per-object path is taken, exactly as before), wall time on other
hosts or under contention, and the reftable backend (the verifier does not
read refs).

## Round r3: uppercase tree headers keep their admission

The r2 review (owner decision `ba0327f3de7d`) found that
`Repository.commit_object_tree` validated the raw tree ID lowercase-only,
while the base compared the value `show --format=%T` reports, which Git
normalizes to lowercase. Git writes a commit whose tree line is uppercase
hexadecimal through ordinary `hash-object -w -t commit` and it passes
`fsck --strict`; r2 refused such a commit with `GitError: malformed commit
tree object ID` in both object formats where the base read it as consistent.
r3 lowercases the decoded header before validation, and only that field: the
parent lines were already validated lowercase-only by `commit_parents` on the
base, so a commit the base refused for an uppercase parent stays refused.

`test_a_tree_header_git_admits_keeps_its_admission` builds the owner's commit
(lowercase and uppercase tree, `hash-object` without `--literally`, `fsck
--strict`, an adopted record naming the lowercase tree) and requires
inspection and registration to admit it on both formats; the uppercase cases
fail on the r2 tree and pass on r3 and on the base. A tree line that is not
hexadecimal at all was tried as a third arm and is not a reachable input:
`update-ref` refuses to point a branch at such a commit even after
`--literally` has written it, so malformed-object refusal stays covered by the
corrupt and truncated loose-object cases, which are real Git failures.

## Local CI

Fresh `uv` interpreters, `pip install -e '.[dev]'`, the CI commands, estate
gate env set for the two admission tests (`full-suite-3.1x.txt`, `lint-*.txt`,
`types-*.txt`):

| Python | ruff | mypy --strict | pytest (r3) | pytest (r2, superseded) | pytest (r1, superseded) |
| --- | --- | --- | --- | --- | --- |
| 3.11.15 | clean | clean | 829 passed, 2 skipped, 92.26% | 821 passed, 2 skipped, 92.26% | 818 passed, 2 skipped, 92.25% |
| 3.12.13 | clean | clean | 829 passed, 2 skipped, 92.26% | 821 passed, 2 skipped, 92.26% | 818 passed, 2 skipped, 92.25% |
| 3.13.15 | clean | clean | 829 passed, 2 skipped, 92.24% | 821 passed, 2 skipped, 92.24% | 818 passed, 2 skipped, 92.22% |

r3 logs are `full-suite-r3-3.1x.txt`, `lint-r3-*.txt`, `types-r3-*.txt`; the r2
and r1 columns are the superseded revisions' runs, kept for the trail.

The two skips are the inert private modules named below. The published base
44b5675, extracted with `git archive` into a pristine directory and run under
the same command and interpreter (3.13.15), gives **756 passed, 92.07%**
(`base-44b5675-full-suite-313.txt`); r1's record wrongly attributed the task 65
r1 figure of 744 to the base. The candidate collects 73 more cases, all in the
new module (65 through r2, eight added in r3: two headers × two callers × two
object formats). Uncovered new lines are the error arms
for a commit object with no header terminator, a non-ASCII parent, no tree
header, or a non-ASCII tree (r3 git.py 1666, 1678–1679, 1695, 1698–1699); their
inputs are objects Git itself would refuse to write, and the base's equivalent
arms were not covered either.

## Publication paths

The product change set is exactly these five paths:

- `src/onetree/git.py`
- `src/onetree/core.py`
- `tests/test_wal_verify_cost.py`
- `tests/test_recovery_semantics.py`
- `docs/ROUND-WAL-VERIFY-COST.md`

Two further files exist in the worktree and the freeze and are **not** part of
the product: `tests/test_zz_experiment_private.py` (a throwaway experiment,
now an inert module-level skip) and `tests/test_zz_bench_private.py` (the
private wall-time bench, inert unless `BENCH_OUT` is set). The author's guard
refused their deletion and no round has authorised removing them by another
mechanism, so that cleanup is unresolved and belongs to whoever publishes.
Their skips are not product test passes.

## Provenance

Authored by claude:99edb3eb-febc-4531-8cee-8b0cffc16129 from published main
44b5675 (PR #22) under owner handoff cdel_ad9b1a884df3a21251c1281f78592957
(r1) and correction handoff cdel_392ce0894177daa9fc2a689238487dc4 (r2, after
owner review fe2547f1c3a7). Evidence in
`/anvil/scratch/onetree-wal-verify-cost-20260916/`; the owner's r1 review and
reproducers in `/anvil/scratch/onetree-wal-verify-cost-owner-review-20260916/`.
Refusals recorded verbatim: `rm -f tests/test_zz_experiment_private.py` and,
in r2, an `rm -rf` of a not-yet-created private scratch directory, both
"recursive rm on a tree is off-limits (EnterpriseAdmin) — refused"; neither
outcome was reconstructed. Nothing was committed or pushed by the author.
