# Repeated submissions from a live session

The Gate3 CLI rehearsal on `d83f7c5` landed a claimed README change through the
real payload gate and referee. Its next submission used the same private index
and session, but refused `payload_conflict` on that already-landed README.
Re-registering retained the original base. This is task187, a cutover blocker.

## Contract

A live session may submit again without changing identity or resetting its
private index. Its next payload excludes its own previously completed payloads
from the same registration generation and target. New staged changes remain
eligible, including changes staged while an earlier submission is being tested.
Peer landings are preserved; conflicting changes to a submitted path still
refuse. Refused or pending submissions do not advance this comparison baseline.
Adopted history does not establish ownership by a session incarnation.

The registration base remains the ancestry anchor. The comparison tree contains
that base plus this incarnation's terminal payloads, reconstructed from the
verified WAL. An existing private index is not replaced. If a missing index is
recreated through the existing registration/landing behavior, it starts from
this personal comparison tree. It does not silently restage shared working
files. Git may update index bookkeeping while writing a tree; preservation here
means staged modes and object IDs, not byte-identical index cache extensions.

No WAL event schema changes and no production workspace reconciliation are part
of this correction. Gate3 remains inactive until its other prerequisites settle.

## Evidence

Owner evidence is in `/anvil/scratch/onetree-session-progress-20260916/`.
`before.txt` records the unchanged base failing the first regression population:
same-path resubmission, disjoint resubmission, a disjoint peer landing and an
unchanged index after landing. The failures reach `payload_conflict`; they are
not missing-interface failures.

The regression module additionally exercises staging after the first candidate
was built, a real peer conflict, missing-index recreation, deletion then re-add,
registration expiry followed by a new incarnation, and a red referee followed
by a distinct submission. These cases assert branch contents and WAL consistency
where applicable. Unit fixtures use the existing admitted test gate; the original
CLI reproduction separately exercised the installed gate. No production guard
is disabled to produce a verdict.

Author validation, independent verdict and publication remain in the task187
record. This document does not assert acceptance before those steps complete.
