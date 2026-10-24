# WAL JSON decoding errors

A WAL event must be readable under the active interpreter's JSON decoding limits.
Valid JSON can exceed its integer-conversion or nesting budget. A caller's
remaining stack can also affect that budget, as observed with CPython 3.11.15:
a deeply nested API caller can fail to decode an event that a shallow caller
can read.

These decoder-limit failures use the existing `wal_corrupt` error code with
`reason: "json_decode_limit"`, the originating WAL commit's `oid`, and this message:

> WAL event.json could not be decoded within the active interpreter limits

The reason identifies a decoding failure under the current interpreter settings.
Use that distinction when diagnosing the event: the message does not establish
that its persisted JSON syntax is invalid. The Python API retains the original
`ValueError` or `RecursionError` as the exception's `__cause__`.

`onetree wal --json` returns exit status 2 and one JSON error object on stdout for these
failures. The JSON object carries the reason, message and commit ID; it does not
serialize the Python exception cause. Invalid UTF-8 and JSON syntax retain the
existing malformed-JSON diagnostic without the decoder-limit reason.

Reading does not raise interpreter limits, retry with a larger stack, or change
the WAL. Recursion exhaustion outside JSON decoding remains outside this new
handler. Operations that require the unreadable event fail closed.
