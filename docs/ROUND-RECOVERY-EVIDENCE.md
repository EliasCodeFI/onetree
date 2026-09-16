# Recovery evidence method correction (task 179)

The current verification contract uses actual known-bad inputs and their
specific refusal, with allowed controls. Two historical fixtures on published
`bdbb53d` also replaced production guards with permissive stubs. This correction
removes those portions while retaining the original input and refusal checks.
It does not change runtime behavior or claim a newly observed runtime failure.

`test_an_unrecorded_landing_survives_the_routine_act_that_used_to_mask_it`
still creates a real unrecorded landing and checks that registration or another
landing refuses, writes no WAL event, preserves any held claim and leaves the
exact divergence observable. Recovery also refuses. Its positive control now
performs the explicit public adoption workflow, verifies that the actual
unrecorded commit was adopted, then performs the previously refused routine
action and checks a consistent report. The guard remains active throughout.

`test_a_zero_byte_reftable_table_list_refuses_instead_of_answering_absent`
still writes empty or whitespace-only table-list bytes into a real reftable
fixture containing a live WAL and claim. It observes Git's absence result and
integrity-check results, then requires typed corruption from resolution,
inspection, recovery and claim checking. Its `finally` restores the original
table-list bytes. The allowed control verifies a consistent WAL and the
original blocking claim after that restoration. The guard remains active
throughout this case as well.

The correction is limited to these observed occurrences. Historical round
records stay intact, and instrument controls are unchanged. No assertion that
all historical fixtures have been audited is made.

The six affected parameter cases passed against the unchanged published
runtime (`focused.txt`), and Ruff passed (`lint.txt`). The fixtures use the
existing `conftest.py` Git repositories and recovery helpers; pytest resolves
the checkout's `src` through the existing configuration. Evidence is retained
in `/anvil/scratch/onetree-recovery-evidence-20260916/`. Independent review and
publication remain pending.
