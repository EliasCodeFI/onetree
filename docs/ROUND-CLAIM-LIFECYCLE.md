# Claim lifecycle — release after deletion, 2026-09-08

Estate adoption requires a session to release its recorded claim after deleting
or renaming the claimed file. OneTree task 150 records the existing failure;
task 149's future-file acquisition contract is separate.

The regression first failed in all three cases: deletion, file rename and
parent-directory rename. `release` used the same strict filesystem resolution
as claim/check and raised `InvalidPathError: claim path does not exist` before
reading the held claim.

Release now resolves an existing prefix strictly and may append a vanished
suffix to recover the recorded key. It refuses dangling symlinks, loops,
permission errors and `..` in the unresolved suffix. Repository containment and
Git metadata exclusions still apply. The caller can release only its own
selected claim generations, and pending-landing fences remain authoritative.
Rename does not transfer ownership to the destination.

Ordinary claim acquisition and ownership checking remain strict about existence.
This change does not admit future-file intents or alter the existing directory
claim scope. The path resolver's RuntimeError translation is release-local for
older supported Python versions whose strict symlink-loop check uses that type.

Verification uses the real Git-backed `git_repo` fixture and existing
`test_defensive._expose_intent` producer for the pending-landing control. The
primary positive cases inspect the exact released generation and path, preserve
an unrelated claim, and let another session claim a recreated file. Negative
cases cover out-of-repository paths, metadata and aliases, malformed traversal,
cross-session release and pending landings. Invalid path batches leave the WAL
unchanged. Permission checks use an owned temporary directory with permissions
restored in a finally block; the case is skipped where POSIX access checks do
not apply.

Evidence is retained in
`/anvil/claude-diag-806b39cf/onetree-claim-lifecycle/`: the initial three
failures, focused controls, full-suite receipts and independent review. This
release fix alone does not complete the remaining estate adoption prerequisites.
