# Legacy adopted-v1 WAL compatibility (task 70)

Base `10b44c4c3ceda6ed27b2ea57131d2bf4fb4b9e31`, as the owner's mission named
it. Author checkout `/anvil/worktrees/onetree-g3-legacy-wal-e85636f2-20260916`,
branch `claude/onetree-g3-legacy-wal-e85636f2-20260916`. Owner decision
`9927c33712da`: support persisted legacy WALs rather than declare them an
unsupported boundary. Evidence directory
`/anvil/scratch/onetree-g3-e85636f2/task70/`; the owner's historical-writer
provenance under `/anvil/scratch/onetree-legacy-adoption-20260916/` is
preserved unchanged.

## The defect, reproduced before any change

The 9d2ef3b writer recorded `adopted` events at `v: 1` with one scalar
`operation` that comma-joined every `Onetree-Operation` trailer value of the
adopted commit. A commit with two trailers `a` and `b` and a commit with one
trailer `a,b` therefore persist the same scalar and mean different things, so
splitting the stored scalar is not a migration.

Fresh fixtures were built with the actual 9d2ef3b CLI in this round's
evidence directory (`reproduce_current_reader.py`, repositories retained under
`fixtures/{single,repeated,literal_comma}/repo`) and read with this checkout's
unchanged source (`current-reader-before.jsonl`): all three shapes were refused
with exit 2, `wal_corrupt`, "WAL event version does not match its kind",
because `50de097` set the `adopted` kind's version to 2 and the reader refuses
any other. The AF10 reader, which had no version check, accepted single and
literal-comma and refused repeated at the trailer check (owner's
`results.jsonl`). The WAL refs were unchanged by every read.

## Compatibility contract

- **Recognised historical version.** `adopted` at `v: 1` is a recognised
  historical version of that kind (`_LEGACY_KIND_VERSIONS` in `wal.py`),
  accepted on read only. Every other version/kind disagreement, every version
  above the reader's, and every malformed document are refused exactly as
  before. No other kind has a historical version.
- **Writer unchanged.** `Wal.document` still writes `adopted` at `v: 2`, and
  the write path validates with `writing=True`, so a `v: 1` document cannot be
  appended by the current writer (`test_current_writer_never_writes_the_legacy_version`).
- **Verification under the historical writer's semantics.** A `v: 1` record is
  verified against the commit it names by `Repository.legacy_landing_operation`,
  a verbatim port of 9d2ef3b's `_landing_operation_from_message` (all-trailer
  rule, 25 % generated-trailer quorum, the two built-in generated prefixes,
  `#` comments, the `---` divider, folded continuations, comma join). The
  result must equal the stored scalar exactly; the parent and tree checks
  every adopted record receives still apply. The scalar is never split, and
  the legacy parser feeds nothing but this check.
- **Recorded-ness by commit, after verification.** A legacy record's scalar is
  not an identity, so the branch audit treats the commit a verified legacy
  record names as recorded (`_FoldedState.legacy_adopted`), whatever
  identities the current parser reads from it. Any other commit carrying those
  identities is judged on its own record. `adopt_landings` now verifies every
  recorded landing before its audit, as inspection and recovery already did,
  so an unverified legacy record can suppress nothing: a forged one raises
  `wal_corrupt` on every entry point.
- **No exclusion floor, no broad admission.** Nothing in the audit walk, the
  scan floor, or the current identity parser changed. A broker commit the WAL
  never recorded is still reported as `unrecorded_landing` and adopted at
  `v: 2` after legacy history is opened
  (`test_ordinary_work_continues_after_legacy_history`).
- **Raw history retained.** Legacy records are read as written; nothing is
  rewritten or upgraded in place. `EventRecord.version` and `.legacy` expose
  the fact for callers.

## Evidence

Red first (`red-first-before.txt`, unchanged source): 14 failed, 3 passed.
The three that passed are the guards that must not move (current writer
version, `v: 3` mismatch, `v: 5` unsupported).

| Case | Before | After |
| --- | --- | --- |
| Legacy record opens and verifies: single, repeated, literal-comma | refused at the version check | consistent; `v: 1` and scalar retained; recovery 0/0/0 |
| Forged or mismatching legacy records: repeated commit recorded as one value, single commit recorded as a pair, literal-comma commit recorded as one value, another operation, no trailer at all | refused at the version check (wrong reason) | refused "legacy adopted commit does not carry the recorded landing trailer under the historical writer's semantics" on inspect, recover and adopt |
| Legacy record with a wrong tree | version check | "landed commit tree differs from WAL" |
| Ordinary work after legacy history | cannot open | adopt finds nothing; a new landing appends `v: 1` session/claim/intent/landed rows and stays consistent; a new unrecorded broker commit is reported and adopted at `v: 2`; the legacy commit is not re-adopted |
| Current writer refuses `v: 1` | pass | pass |
| `adopted` at `v: 3`, any kind at `v: 5`, malformed legacy data | refused | refused with the same messages (malformed data now reaches the field check) |
| Actual 9d2ef3b CLI output read by the current reader (skips where the extracted writer is absent) | refused | consistent for all three shapes |
| Legacy parser rules on raw messages: 12 cases plus non-UTF-8 | n/a | as the historical writer |

After the change, the persisted fixtures from the reproduction read consistent
with `v: 1` records and untouched WAL refs (`current-reader-after.jsonl`).

## Gates

Local CI, the `ci.yml` steps run here per the owner's instruction, with
`ONETREE_TEST_ESTATE_GATE` and `ONETREE_TEST_SHIPPED_GATE` set to the estate
gate (`local-ci-candidate-final.txt`): install 0, `ruff check src tests` 0,
`mypy` 0, `pytest` 863 passed, 0 failed, coverage 92.38 %. The pristine base
in a separate copy (`local-ci-pristine-base.txt`): 833 passed, 92.24 %.

An instrument finding on the way (`local-ci-candidate.txt`, first full run):
73.85 % coverage with 850 passed. The replay case spawns the 9d2ef3b CLI with
`PYTHONPATH` at the extracted historical source, and the project's
`patch = ["subprocess"]` collector measured that package as `onetree`,
folding 1869 historical statements into the gate
(`coverage-report-candidate.txt`). The replay case now drops
`COVERAGE_PROCESS_CONFIG`/`COVERAGE_PROCESS_START` from the historical
writer's environment; no coverage configuration changed.

## Integration with current main

`origin/main` advanced to `882d8c8` (Gate 3 activation) after the mission
named its base; main changed `core.py` and `git.py` in other areas since
`10b44c4c`. The candidate's diff (`task70-candidate.patch`) applies cleanly on
a private copy of `882d8c8` (`integration-main/`), where `ruff`, `mypy` and
the WAL-related modules (`test_legacy_adopted_wal`, `test_defensive`,
`test_adoption_identity`, `test_recovery`, `test_recovery_semantics`,
`test_wal`) pass, 314 tests (`integration-main-targeted-final.txt`). Final
integration and publication are the owner's; the author tree stays on the
named base.

## Revision r2 (after the owner's r1 review, decision 231b0eda3403)

The r1 freeze `onetree-legacy-adoption-r1-20260916` (manifest
`ec02f741e3240406`) is preserved. The review
(`/anvil/scratch/onetree-g3-fleet-20260916/task70-owner-r1-review.md`,
changes requested, no runtime defect asserted) named two corrections.

- History preservation had no before-state. The r1 opening case compared
  two immediate reads of the WAL ref after recovery, which cannot show that
  reading preserved anything. r2 captures the WAL head before inspection,
  compares it after inspection and recovery, and then shows the check can
  fail: one more raw event appended to the same history moves the head the
  check compares, and the preserved identity is the parent of what was
  appended (`_wal_head`, `test_legacy_adopted_record_opens_and_verifies_against_its_commit`).
- The version contract in `DESIGN.md` contradicted the read path. Its
  paragraph on `v` now states the one historical exception: `adopted` at
  exactly version 1 is read as a historical record and verified under the
  9d2ef3b semantics, the current writer never writes it, and every other
  disagreement, higher version and malformed document is refused as before.
  Custody expanded to `docs/DESIGN.md` for this bounded correction.

No runtime source changed in r2; `wal.py`, `core.py` and `git.py` are
byte-identical to r1 (digests in `import-origin-r2.txt`).

## Not established, and limits

- Only `adopted` has a historical version. No other kind was ever released at
  a different version, and none is admitted.
- Each legacy record costs one `cat-file` of its commit at verification; the
  batched evidence reader is not used for it. Legacy records are pre-release
  history and few; no cost measurement was made.
- `landed_operations` is keyed by the legacy scalar as it always was for
  adopted records; a scalar that is itself a valid identity (the single
  shape) behaves exactly as a `v: 2` record's operation would.
- `DESIGN.md`'s per-kind version paragraph was corrected in r2 (above).
- The current fresh-commit parser policy, task 69's predicate conflict and
  task 183's conformance build refusal are untouched; no dependency on them
  was needed for this change.
