# Auditing history across landing retries

Task61 requires a landing retry to avoid rescanning ancestry that the same
invocation has already audited successfully. It does not permit a permanent
history floor or an exclusion inherited from another public call.

A stable audit is a proof over the captured local branch heads and the current
WAL. A retry may exclude a previously verified head only for that same branch,
when it is still an ancestor of the current head and every recorded identity
and verified legacy-adopted commit used by the proof remains recorded. A new
distinct branch head, non-descendant head, or loss of those records receives a
complete walk. References sharing an identical head are audited together; a
new branch at an already-covered head does not require a duplicate walk.
The proof is replaced only after a successful scan whose WAL and branch heads
remain stable. An unstable scan or refusal cannot establish it.

Every retry still reads and verifies the WAL, rejects incomplete shallow history
and legacy grafts, enumerates all audited branches, and checks snapshot stability.
New history remains subject to the same unrecorded-landing refusal. Verification
of landed and historical adopted records runs before consulting the cache.
The cache belongs to one `land()` invocation; nested and later calls get fresh
proofs. Other public operations retain their complete history audits.

The cache retains the previous stable branch heads and the identities that
justified their audit. It does not persist state, advance the WAL's scan floor,
cache referee outcomes, or change the landing and recovery protocols.

## Verification and diagnostic evidence

`tests/test_retry_audit.py` forwards the real Git history walk and checks its
returned object IDs during an actual interleaved landing. Before this change,
the retry-prefix check failed because the second walk had no exclusion.
The tests also exercise independent calls, nested landings, unrecorded suffixes,
new and rewritten branches, WAL rewind, unstable scans, legacy records, and
history-visibility boundaries using disposable Git repositories.

Retained local evidence is under
`/anvil/scratch/onetree-ledger-20260916/task61-baseline-5c24cda/` (before) and
`/anvil/scratch/onetree-retry-audit-61-20260916/` (after). These are local evidence
paths, not a portable published archive. The forwarding diagnostic verifies its
import path, calibrates its subprocess observer, and replays actual Git commands.
It records the initial complete audit separately from the retry's suffix audit.
These runs happened alongside other work; they establish traversal behavior,
not an isolated latency guarantee, total landing cost, or fleet benchmark result.
