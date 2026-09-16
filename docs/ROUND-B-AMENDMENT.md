# Round B: failure localization amendment

Owner approved this amendment on 2026-09-08, decision `32bd1891c90b`, after
observing the counterexample below. It changes the Round B contract before
implementation. It does not claim that the referee or branch guards exist.
The benchmark protocol in `docs/PREREGISTRATION.md` is unchanged.

## Contract

The configured referee is an arbitrary repository test command. A failed
train therefore need not contain a failing pair of changes. Report a pair
only when the evidence demonstrates that pair failing against its identified
baseline. Otherwise report the demonstrated failing combination or explicitly
unresolved localization. Report which combinations were actually tested;
never label an untested pair as the cause or silently treat a prefix as a
single independent change.

Red and timed-out candidates remain refused. A localization timeout or
incomplete search cannot turn a failed candidate into an accepted one.
Claims of minimality, a guaranteed pair, or a general localization-cost bound
need supporting assumptions and evidence. The exact-train requirement and
the remaining branch-operation and typed-rejection requirements in
`docs/DESIGN.md` remain in force.

## Observed counterexample

Three independently changed files enable features A, B and C. The referee
returns failure only when all three are enabled:

| A | B | C | Exit | Result |
| --- | --- | --- | --- | --- |
| 0 | 0 | 0 | 0 | PASS |
| 0 | 0 | 1 | 0 | PASS |
| 0 | 1 | 0 | 0 | PASS |
| 0 | 1 | 1 | 0 | PASS |
| 1 | 0 | 0 | 0 | PASS |
| 1 | 0 | 1 | 0 | PASS |
| 1 | 1 | 0 | 0 | PASS |
| 1 | 1 | 1 | 1 | FAIL |

Every pair passed. There is no failing pair to identify in this example,
even though the failure predicate is monotone. This refutes the old
unconditional pair-localization clause; it is an observation of an admissible
command, not a test of OneTree referee integration.

The eight subprocess cases took 0.1769086879794486 seconds in this one local
observation. That number is not a general localization-cost bound.

## Evidence provenance

The fixture was built with the estate's admitted `git_fixture.py` instrument
after its 89 selftests passed. Its seed commit is
`cac9a45e9c232364deb3a6a170d1ecaa31cc72c9`. The predicate SHA-256 is
`3bec49ecb6cbca09f12d0a1bf34eddc5ef04718c2691964afa4f89bda880aa7f`.
The invocation was `/usr/bin/python3 -I -S -B` followed by the fixture's
`referee.py`. Python 3.14.4 resolved the traced `json` and `pathlib` imports
under `/usr/lib/python3.14`. The predicate source stayed unchanged and the
original inputs were restored; fixture verification passed before and after.

The local evidence directory is
`/anvil/claude-diag-806b39cf/onetree-gate-evidence/`:

- `round-b-higher-order-observation.json`: inputs, hashes, exits and timings.
- `round-b-higher-order-import-trace.txt`: interpreter import trace.
- `round-b-higher-order-referee.py`: exact predicate source.
- `round-b-approved-retry.md`: owner hand-up and scope.

These are local evidence artifacts, not files distributed with this package.
The fixture path ends in `20260909`; that is only a directory label. The
observed UTC date and this amendment's approval date are 2026-09-08.
