# Gate 2: per-path check reporting (task18)

The task18 contract requires a verdict for every input path, a nonzero exit
when any path is blocked, typed refusals for nonexistent paths, and visibility
of a session's own claims. The predecessor's CLI accepted one path only. Its
JSON included own claims; its human output called an owned path `CLEAR`.

`onetree check PATH [PATH ...] --repo REPO --session ACTOR` now checks every
argument in order. Repeated arguments retain separate rows. Each check remains
read-only and uses the existing core path canonicalization and ownership rules.
The batch consists of sequential observations, not one atomic snapshot or a
reservation. A subsequent claim or landing still needs its normal validation.

## Output contract

A single path retains the existing JSON object: `ok` and either `check` or
`error`. The check contains canonical repository-relative `path`, `blocked`,
`holders`, and `owned`. A valid blocked check is `ok: true` with exit status 1;
`ok` describes completion of the check, not permission to write.

Multiple paths produce exactly one JSON object with `ok`, `blocked`, and
`checks`. Each row has the original `input`, `ok`, and either a `check` object
with the single-path fields or a typed `error`. An invalid path has no `check`
object, so it cannot be mistaken for a clear row. Path errors do not suppress
later rows. An identity, repository, or WAL error refuses the whole operation
through the existing top-level error contract.

The batch exit is 2 if any path is invalid, otherwise 1 if any path is blocked,
otherwise 0. Top-level `blocked` reports observed peer claims even when another
row is invalid. Top-level `ok` is false if any path check failed.

Human output has one line per path: `BLOCKED`, `OWNED`, `CLEAR`, or `ERROR`.
Own claims remain visible even when a directory also overlaps a peer's claim.
Paths, identities, and error messages are escaped to keep filename linebreaks
and terminal control characters from creating apparent additional verdicts.
Ordinary human path rendering is preserved (`CLEAR a.txt`). The first candidate
unnecessarily added quotes to that output; the existing defensive CLI test
failed on the change, so only the JSON string contents are used for successful
path display. Control characters remain escaped without adding outer quotes.

## Evidence

`tests/test_cli_batch_check.py` uses the repository's GitFixture, real core
session/claim operations and CLI parser. It imports no replacement coordinator
or result generator. The unchanged predecessor at main7374926 fails batch
cases at argument parsing and fails the human owned-claim assertion. Its
single-path JSON own-claim control passes. The initial run is retained at
`/anvil/scratch/onetree-task18-20260916/batch-before.txt`; these are missing
interface and visibility failures, not evidence of an existing last-path-wins
implementation. No production guard was disabled for a control.

The cases cover blocked/clear order, invalid paths before/between/after valid
paths, duplicate inputs, own claims, linebreak-containing filenames, and
unchanged WAL state. Existing CLI lifecycle tests remain part of validation.
