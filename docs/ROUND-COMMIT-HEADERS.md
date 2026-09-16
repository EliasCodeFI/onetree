# Git-valid parent headers

Task 185, based on `c51dda848729520b9db32466527f13f9c2df6c1e` (PR 24).

Git accepts uppercase hexadecimal parent IDs in raw commit headers through
ordinary `hash-object -w -t commit`, without `--literally`. `show --format=%P`
reports canonical lowercase IDs; `update-ref` and `fsck --strict` accept the
objects. OneTree's raw-parent parser instead rejected them as malformed.
That rejection was inherited from the per-commit parser before the task 184
batching change. Task 184 corrected a separate tree-header regression without
changing this parent behavior.

The parser now normalizes strictly decoded ASCII parent IDs before the existing
OID validation. Parent order, exact OID width, hexadecimal validation and object
availability checks retain their contracts. Public OID inputs remain canonical
lowercase; this change concerns the raw representation inside a Git object.
Adoption records the canonical first parent as `expected_head`, and later WAL
verification compares the same canonical values.

## Evidence

`tests/test_commit_headers.py` uses real Git objects in both SHA-1 and SHA-256.
Lowercase, uppercase and mixed-case headers cover ordinary and merge commits,
explicit adoption and reading an existing adopted record. Each object is read
back byte-for-byte, checked against Git's parent output and passed through
strict fsck. Successful cases preserve parent order and the branch tip, permit
subsequent registration and leave the WAL unchanged on repeated adoption.

Malformed raw-parent controls cover empty, short, long, nonhex, surrounding
whitespace and non-ASCII values. These are direct parser-input controls, not
claims that Git permits each malformed object to become a branch tip. Separate
controls preserve lowercase-only public OID validation.

Before the fix, the new module produced 16 failures and 24 passes: every failure
was an uppercase or mixed-case compatibility case raising `GitError: malformed
commit parent object ID`; lowercase and strict-validation controls passed.
The focused run after the fix, including the existing WAL verification-cost
module, passed 113 tests. Evidence is in
`/anvil/scratch/onetree-parent-headers-20260916/` (`red-first.txt` and
`focused-green.txt`). The earlier independent main-versus-frozen-r2 diagnostic
is retained in `/anvil/scratch/onetree-ledger-20260916/uppercase-parent-r2.txt`.

Full local Python 3.14.4 validation passed 869 tests without skips, with 92.29%
coverage (`full-suite-314.txt`). Ruff and strict mypy passed. The initial lint
run found three long lines in the new tests; line wrapping corrected them,
and `lint-r2.txt` records the clean check. No runtime logic changed after the
full test run started.

Independent review is pending. No remote CI success is asserted by this record.
