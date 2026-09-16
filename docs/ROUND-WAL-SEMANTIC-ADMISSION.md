# Task 65: reject inconsistent landed evidence before writes

The task's contract requires mutation admission and inspection to agree about
recorded landed/adopted commits. On main 397c0a3, a real adopted record carrying
the right commit and operation but the wrong parent or tree fails inspection
with WalCorruptError, yet registration, claim, release and CLI session writes
still succeed. Ten refusal assertions failed on the unchanged runtime; five
valid-record controls passed.

The mutation read now calls the existing landed-evidence verifier before its
history audit. This checks the recorded Git objects, parents, trees and adopted
operation membership using the same semantics as inspection. Existing typed
infrastructure failures remain distinct from WAL corruption. No persistent
cache or repair is introduced, and pending intents retain their recovery path.

## Evidence and limits

`tests/test_wal_semantic_admission.py` uses the repository's real `GitFixture`
and CLI `_json_call` helper. It registers and claims normally, stages real bytes,
creates an actual marked commit, moves the target, and uses the WAL primitive
to append an adopted document. The malformed inputs change only its parent or
tree field. No validation predicate or append outcome is replaced. The test
first establishes inspection's answer, then checks registration, claim, release,
JSON and human callers. Refusal leaves the target and WAL unchanged; valid
controls append successfully and remain consistent.

The focused new and existing recovery modules passed 252 cases. This is not
an exhaustive enumeration of every caller or every corrupt-record shape. The
verification now does additional Git reads per mutation; no runtime overhead
bound is claimed. Full-suite and independent review results are recorded
separately in `/anvil/scratch/onetree-wal-semantic-20260916/`.
