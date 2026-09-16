# Recovery controls using concrete inputs (task 180)

This bounded follow-up covers the additional legacy guard replacements
observed while reviewing task 179 and checking adoption evidence. It changes
only tests and this record; production recovery and Git behavior are unchanged.
Historical round records remain historical evidence, not current instructions.

| Previous alteration | Replacement input and required behavior |
| --- | --- |
| `_landing_in_flight` forced false | Ordinary recovery first observes the live intent and leaves it pending. The fixture then appends a concrete refusal event for that intent through the WAL API. The later branch update succeeds, but its landed record is rejected at the WAL door; no landed event is appended, the refusal head remains unchanged, and inspection reports `refused_candidate_on_target`. |
| `_close_intent` replaced with a no-op | Keep the actual locked-branch failure and verify that its intent closes and its claim can be released. Then create a separate pending intent with the existing fixture helper. Release refuses with `pending_land` and CLI output names the intent. After removing the actual lock, ordinary recovery closes that dead intent and release succeeds. |
| `_reapplied` forced false | A real competing Git commit changes the claimed payload after this submission writes its intent. The submission loses the actual ref CAS and reports `payload_conflict`; the peer commit remains, the refused candidate is absent, no landed event exists and recovery remains consistent. |

The first input deliberately supplies a persisted refusal that ordinary live
recovery does not produce. It tests the WAL boundary given that input, not a
claim that normal recovery ignores liveness. The pending-intent helper models
crash residue without killing a process. The competing commit is an actual Git
write through a separate index, without broker trailers or a WAL landing.

The existing allowed controls remain: ordinary recovery leaves a live lander
alone, and real Git death after an applied ref update completes the landing on
both files and reftable backends. The Git-death fixture routes Git through its
existing fault instrument; it does not replace the production reapplication
predicate. No new runtime-guard neutralization was used.

Initial focused validation passed the changed cases and live-recovery control
(four cases). The companion run also included both ref-backend cases for the
applied-update/lost-status control: six passed. Ruff passed. Logs are under
`/anvil/scratch/onetree-recovery-controls-20260916/`. Initial runs use the installed
Python 3.13 environment of the recovery-evidence checkout; pytest's configured
`src` path selects this checkout. A dedicated environment is prepared for full
local CI. Independent review and publication remain pending.

Inputs resolve from this checkout's `tests/conftest.py` (`GitFixture`, the
admitted test gate, and Git-death instrument), the existing recovery helpers
(`_register`, `_stage`, `_pending_intent`, `_arm_kill_git`), and the unchanged
`Coordinator`, `Repository` and WAL implementations. This record makes no claim
that every historical fixture has been audited.
