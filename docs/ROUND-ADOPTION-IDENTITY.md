# Adoption identity evidence (tasks 62 and 63)

The historical findings described adoption retiring another commit that shared
an operation ID and commit-message control bytes forging a second audit record.
Existing current cases covered inspection, a copied operation, and a pending
intent, but did not alone settle the reported adoption-write outcomes.

This change adds behavior tests only, against published runtime `52044fd`.
No production guard is replaced. The new `test_adoption_identity.py` reuses
`GitFixture` from `conftest.py` and `_marked_commit`, `_pending_intent`, `_register`
and `_stage` from `test_recovery_semantics.py`. Those helpers create actual Git
commits/refs and a valid pending-intent WAL input. The existing autouse clean
payload-gate fixture remains a test instrument; it does not replace the
operation-identity, trailer-framing, adoption or WAL validation guards.

The cases check:

- Two distinct unrecorded commits with the same operation on one branch are
  both adopted. In the cross-target form, targeted adoption records only the
  selected commit; the other branch still refuses registration without a WAL
  write until separately adopted.
- A real commit message contains `0x1e` and `0x1f`, followed by either a real
  ordinary commit OID or a malformed OID. Registration refuses before adoption.
  Adoption appends exactly one event naming only the actual marked commit;
  inspection, recovery, repeated adoption, GC and registration remain usable.
- A dead pending land intent is refused by ordinary recovery. A real
  `git cherry-pick -x` creates a distinct commit carrying that candidate's
  trailer. It is detected, refuses registration without a WAL write, and is
  explicitly adopted without adopting the original refused candidate.

The constructed unrecorded commits model missing WAL records; they are not
claimed to result from an actual process death. The cherry-pick is a real Git
operation. The existing separate pending-operation interleave case remains
part of the companion validation.

The focused run passed all five new cases on Python 3.13.15 (`focused.txt`).
Runtime import provenance and logs are under
`/anvil/scratch/onetree-adoption-evidence-20260916/`. Pytest uses this checkout's
configured `src` path; the initial runner is the reconciliation checkout's
installed environment. Ruff passed. Independent review and publication are
pending. The original historical probe sources are unavailable at their task
paths, so this is current behavior evidence, not a replay of those exact files.
