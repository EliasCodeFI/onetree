# Gate evidence retention, 2026-09-08

Estate adoption task 140 requires the broker's durable record to show the
payload gate's actual report. A CLEAN verdict does not establish that every
hook has an evidenced passing suite: the estate gate permits NO_COMPANION.
That policy remains unchanged.

Current baseline measurement corrected the task's older description: detailed
rejected findings reached the caller but were also lost from the WAL. The
existing refusing fixture emitted `findings=["known bad"]`; its real Git WAL
event retained only action, reason, operation and paths. The new end-to-end
regression then failed for CLEAN, REFUSE and DEGRADED because no receipt was
retained. The first test invocation also contained an incorrect expected reason
suffix for REFUSE; the corrected baseline failed all three on the missing
receipt predicate before implementation.

The broker now retains a versioned `gate_receipt` with its measured admitted
source SHA-256, requested `expected_paths` and complete validated `report`.
The estate report has nine fields: candidate, repository, staged_files,
findings, degraded, advisories, hook_suites, hook_suite_state and verdict. All
eight suite fields survive, including evidence, weakening and runtime. Older
six-field gate reports remain representable without inventing the absent
evidence extension. If any of the three extension fields is supplied, all three
must have the expected shape. Non-finite JSON values and malformed suite
evidence are operational failures; neither a landing nor a trusted refusal is
recorded for an unusable report.

A CLEAN receipt belongs to its `land_intent`, written before the branch CAS.
Normal and recovered `landed` events refer to that immutable intent; they do
not duplicate its receipt or rerun today's gate. Classified REFUSE and DEGRADED
receipts belong to their refused event and also reach the caller. DEGRADED may
lack a complete reported path census, so its reported staged_files are retained
separately from expected_paths. Existing exact candidate, origin repository,
source digest, exit-status and CLEAN/REFUSE path-set admission checks remain.

Replay validates the receipt envelope, report shape and relation to its event.
An intent's receipt must name that candidate, expected payload and CLEAN
verdict. A classified refusal's receipt must match its reason and requested
paths. Receipts are allowed only on these carrier event kinds. Historical
origin paths and source pins are facts about the original audit: relocation or
a later pin change must not rewrite them or invalidate them by comparison to
today's checkout. Records with no receipt remain readable.

`LandResult.gate_receipt` and `land --json` expose the receipt. `WalReport` and
`wal --json` also expose a `gate_evidence` index naming each landing-related
event's carrier or explicitly marking its evidence `unrecorded`. Adopted and
legacy records receive no synthetic receipt. A terminal event can use another
event's receipt only through the same actor and operation's intent. A
known-bad reference to another operation and a malformed historical intent
reference both failed before that index check was corrected.

Fixtures use the real Git-backed `GitFixture` and existing admitted test-gate
producer `_write_gate` from test_payload_gate.py. The fixture pin is scoped by
conftest.py's autouse gate setup. Actual estate-gate cases explicitly restore
the independently captured shipped pin and use ONETREE_TEST_ESTATE_GATE; their
hook and companion inputs come from that admitted gate's own selftest fixtures.
They measure NO_COMPANION/CLEAN, GREEN/CLEAN and RED/REFUSE, with sandboxed
runtime reported. Other cases cover recovery after branch publication without
reauditing, fresh receipt selection after a real WAL CAS loss, malformed
reports, forged WAL receipt inputs, CLI output and relocation to another clone
under a different current pin. `_expose_intent` supplies legacy records without
evidence. Forged inputs are written only into temporary fixture WALs.

Evidence is retained at
`/home/anvil/claude-diag-806b39cf/onetree-gate-evidence/`, including the observed
old refusal, corrected red baseline, evidence-link failures, actual gate report
properties, focused/full-suite receipts and review/publication records. The
referee, broker-only branch protections and estate cutover remain separate work.
