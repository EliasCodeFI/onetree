# Atomic claim narrowing, 2026-09-08

Estate task 149 requires a session creating files under a claimed directory to
retain those files without a gap in ownership when surrendering the directory.
The existing public release-then-claim sequence failed the visibility check:
a separate Coordinator observed `[true, false, false, true]` around its two
WAL updates. The exact failing test is archived with the round evidence.

`Coordinator.narrow(actor, parent, paths)` and `onetree narrow PARENT CHILD...`
provide the atomic operation. The existing parent and children must resolve
strictly inside repository content. Children must be proper descendants after
canonicalization; duplicate or overlapping requested children are reduced to
their covering paths. This retains existing claim scope, including subtree
coverage for a retained directory. It does not admit nonexistent path intents.

The actor must hold the exact named parent. Narrowing surrenders only that
generation; other owned claims survive, including any pre-existing ancestor or
child. Such other claims can therefore continue to cover paths outside the
retained selection. Existing generations are not replaced just to simplify the
result. New children bind the active session generation and inherit the parent
deadline, capped by the session deadline. Narrowing does not renew a lease.

New child claim documents precede the parent release document in one validated
WAL append and one live-ref CAS. Existing readers understand both event kinds;
even the intermediate immutable history prefixes retain parent coverage.
On contention the operation rereads ownership, liveness and pending fences.
It refuses a replaced parent generation instead of narrowing a newly acquired
claim. A pending landing that fences the parent refuses narrowing; a landing
that fences a preserved child does not lose its generation.

Success returns the released parent and resulting covering claims. Invalid
paths raise `invalid_path` before any WAL write. Semantic refusal writes a
`refused` event with action `narrow` and reason `not_held`, `claim_changed` or
`pending_land`, then raises `narrow_refused`. Inactive sessions raise
`invalid_identity`. CAS exhaustion raises `git_error`. JSON success and refusal
use the existing single-object CLI envelope.

The regression keeps the same peer visibility predicate and changes the caller
from the measured public composition to `narrow`. Fixtures use real temporary
Git repositories from `tests/conftest.py`, a separate Coordinator for peer
reads/writes and wrappers around the actual Git ref update. The pending-intent
producer is `tests/test_defensive.py::_expose_intent`; the CLI envelope verifier
is `tests/test_cli.py::_json_call`. Contention controls move the real WAL,
replace a claim, expire a lease, rotate a session or expose a pending landing
before the first attempted CAS. None rely on a fabricated successful append.

Evidence: `/home/anvil/claude-diag-806b39cf/onetree-atomic-narrow/` contains the
known-bad composition and failing receipt, focused and full-suite receipts,
frozen-file hashes and review/publication records. This addition resolves the
new-file claim handoff; referee, broker-only enforcement and estate cutover
remain separate adoption work.
