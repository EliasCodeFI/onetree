# Grite evaluation for OneTree task21

Trial date: 2026-09-16. No production tracker migration.

## Recommendation

Do not adopt grite as this fleet's authoritative tracker now. Keep the existing task catalogue and coordinator. This is a workflow decision, not a finding that grite is defective: the local task lifecycle and coordination checks below worked. This fleet already has task identities, dependencies, recovery checkpoints and session-authenticated delivery. The trial mirrored task26 and task192 manually; it did not establish a migration or reconciliation path. Maintaining those mirrors as a second authority would leave progress attribution ambiguous.

## Provenance and boundaries

On 2026-09-16 Context7's grite lookup returned only unrelated Gitea entries. No relevant indexed documentation was available, so no query was made against a mismatched library. Live fallback: the [official repository](https://github.com/neul-labs/grite) and [v0.5.3 release](https://github.com/neul-labs/grite/releases/tag/v0.5.3). Saved release.json identifies v0.5.3. Downloaded x86_64 Linux GNU archive SHA256 is 2627082f7687512c61f1bb7c3eee24642c5c63fca40c293ec39c83539ebb0773, matching the release asset digest. The actual CLI reports `grite 0.5.3`.

The evidence filenames below resolve under the retained local directory `/anvil/scratch/onetree-grite-trial-20260916/`. They are local audit evidence, not a published portable reproduction archive.

Operator report for the original trial, not a retained command receipt: only the CLI was extracted into the evidence directory's bin/; the repo/ fixture was created with the existing git_fixture shelf tool after its selftest passed; grite operations used --no-daemon; no global installation, production repository initialization, remote sync, credentials, or production task transition was involved. Both trial actors were operated from the Owner shell; the contender is explicitly synthetic, not a second real seat. The r2 adverse-control reruns separately retain their actual commands, outputs and exit statuses in r2/commands.jsonl, including --no-daemon.

## Observed results

| Operation | Evidence and result |
|---|---|
| Initialize and create trial task mirrors | init.json, task26-create.json, task192-create.json, list.json: both mirrors appear |
| Lock collision | owner-lock.json succeeds; the original refusal JSON is in contended-lock.err with empty stdout in contended-lock.json. The r2/commands.jsonl row contended_lock retains the repeated conflict body and exit4 (receipt c22e92458452), between receipted owner acquisition and release |
| Checkpoint and actor replay | checkpoint.json; rebuild-second-actor.json; second-actor-read.json contains the original issue and checkpoint with the originating actor |
| Dependency cycle control | dependency-added.json records B depending on A; the original reverse-dependency refusal JSON is in cycle.err with empty stdout in cycle.json. The r2/commands.jsonl row dependency_cycle retains the repeated invalid_args cycle body and exit2 (receipt c41087554b96) |
| Close and reopen | closed.json and reopened.json show the synthetic prerequisite's state transitions |
| Rebuild preservation | export-data-before.json and export-data-after.json differ only in meta.generated_ts; removing that explicitly named export-time field produces byte-equal normalized files, SHA256 cb3aa677cdd1db18fb82a721a4243c267a7194e93704e7b1bfccc60a24f5d757 |
| Actual Git surfaces | refs-after.txt contains refs/grite/wal; main stays at the fixture seed. status-after.txt records untracked AGENTS.md and the .grite/ directory, which contains export.json |

The first export comparison compared stdout response metadata, not the exported task data. That was insufficient and is superseded by the actual export-file comparison above. A later raw-file comparison failed on the changing generated_ts field; no record differences were hidden by removing arbitrary fields.

The r1 note also misidentified the adverse controls' stdout files as containing the refusal bodies. They were empty: the JSON was on stderr. The original grite_lock_control and grite_cycle_control rows in receipts.jsonl only record successful reads of empty files, so they are not evidence of a refusal or its exit status. The r2 command receipts supersede them for those claims. Their nonzero statuses are the expected outcomes of deliberately conflicting inputs, not a passing production gate.

The default init writes AGENTS.md and export writes .grite/export.json. This matters to this fleet's existing instruction and path-custody workflow; it is not a claim that ordinary issue writes modify tracked source. Live CLI help also differs from the current main-branch README examples: v0.5.3 issue update offers no --label option, and export offers json/md rather than a CSV option. Versioned CLI evidence governs this trial.

Untested: real independent seats, simultaneous writes, remote synchronization, crash recovery, signed actor authentication, throughput, and scale. No such guarantee is inferred from the successful checks or grite doctor output. This is a bounded task-tracker evaluation, not OneTree Gate3 fleet-exit evidence.

## Credit

Credit [neul-labs/grite](https://github.com/neul-labs/grite) and Dipankar Sarkar for the Git-ref event-log, actor attribution, and optional-daemon patterns considered in OneTree's design; credit neul-labs/brat for the role and janitor patterns recorded in the approved roadmap. This recommendation does not remove those credits. The eventual public PRIOR-ART.md remains task32's deliverable and must retain them regardless of this keep-or-drop decision.
