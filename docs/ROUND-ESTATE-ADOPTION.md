# Estate adoption — candidate reconciliation, 2026-09-08

Owner ordered OneTree adoption before coordinator consolidation on the shared
tree. This stage combines previously separate changes into a candidate; it does
not activate estate cutover or establish that the remaining adoption contracts
are implemented. Estate task 1732 carries the programme.

## Inputs and retained contracts

The candidate starts at PR1 head `50de097d98aeb609dc8960168d6c7a676c8f1cb1`.
It applies the AF12 difference from their common base
`e0538e2c3ba653c96f16ab364fa1cc6b3deb088f` to PR2 head
`4323dc678d5e6df6cdcbfc900ff1703dfc66c11d`, then brings the relevant repair from
`09201ce62bd793c1c83656a94fb5d36ac28f6c48`. The existing PR branches are preserved.
Git applied the AF12 patch cleanly; that observation does not prove behavioral
compatibility.

PR1's reader/writer version agreement, adoption idempotence, repository-wide
audit, object/ref caches and crash-instrument exits are retained. PR2's private
repository-anchored gate snapshot, `-I -S`, execution environment restriction,
duplicate-key rejection and durable refusal record are retained. The repair's
configuration-isolated trailer insertion and unarmed crash-shim forwarding are
retained alongside them.

Both source and independent test literal admit estate gate SHA-256
`e20c259cfee795f064eb18fa4208c3d9ef6ea962ecd96cf8efbfe351cce4f669`, corresponding to
estate commit `fa599905ab95f42622ec352f83ddc327ec031c43`, blob
`ff7e66db1baddf0eec205cb7fba714765bcd8b8e`.

## Identity and refusal boundaries

Execution controls remain fixed or explicitly admitted. The native identity
names `CLAUDE_CODE_SESSION_ID`, `CLAUDE_SESSION_ID` and `CODEX_THREAD_ID` are
separate caller metadata. They pass through only when present, without selecting
a vendor or Claude alias. Neither candidate trailers nor the arbitrary OneTree
actor can produce this identity. The gate keeps its own comparison semantics,
including its existing advisory when live identity is absent.

The first combined real-gate run observed four wrong-session candidates being
accepted: **4 failed, 21 passed**. The environment restriction had stripped the
independent caller identity, preventing comparison. A child-environment control
then observed **5 failed, 1 passed**: supplied native identities disappeared,
while absence was preserved. Both runs preceded the projection correction.

The first corrected gate/admission run passed **73 tests**. It covered both
Claude identity names, both vendors, missing/mismatched attribution, comment
tails, configured trailer commands, restricted execution controls, source
snapshot and parser cases. Additional cross-vendor and conflicting-alias controls
and the complete repository suite are part of final verification, recorded in
the evidence directory below.

The completed local full suite passed **435 tests in 221.72 seconds**, with
**91.12% coverage** (90% required). Ruff and strict mypy passed. Both review
lenses found the same stale unchanged-WAL promise in `DESIGN.md`; it was
corrected to the trusted-refusal/operational-error distinction below after
both review freezes were released. No code blocker was reported. Cross-version
CI evidence is separate from this local Python 3.14.4 result.

A trusted candidate refusal leaves the target unchanged and adds an exact
`refused` WAL event with its reason and paths. It must not add a `land_intent`
or `landed` event. Operational gate errors remain outside this trusted-refusal
path and preserve both refs. The real-gate tests now assert this combined
contract instead of the original repair's obsolete unchanged-WAL assumption.

## Verification and remaining adoption work

Evidence is retained under
`/anvil/claude-diag-806b39cf/onetree-adoption-integration/`, including exact
input patch, failing controls, final full-suite XML/text, hashes and reviews.
Final verification explicitly supplies both `ONETREE_TEST_ESTATE_GATE` and
`ONETREE_TEST_SHIPPED_GATE`; a skipped real-gate test is not admission evidence.
Synthetic gate fixtures remain appropriate for coordination-only tests.

Before estate cutover, verify and implement the candidate referee, broker-only
workflow, hook-evidence retention, exact new/deleted-path claim lifecycle and
repository onboarding. A permitted hand commit can be WAL-consistent; divergence
audit alone is not broker-only enforcement. PR1's reader rejects historical
`adopted` v1 records, so inspect a prospective repository's WAL before upgrading;
do not reset existing history to clear that refusal. An integrated candidate's
green checks and review do not substitute for a bounded live two-session canary.
