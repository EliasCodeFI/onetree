# Historical AF record corrections

Task 133's original NUL claim was already corrected in `ROUND-AF8B.md` at
base `44b56752762c5dd77dfcd248229dc55321850ae0`. This follow-up checks the
remaining historical AF records for claims a later record explicitly corrects
or narrows, and puts missing forward references where the earlier claims are
read. It changes documentation only.

## Audit boundary

The corpus is the tracked `docs/ROUND-AF*.md` files at that base, listed below.
The audit compared the correction passages and their earlier claims, including
scope changes in later disposition tables. The evidence is the checked-in
document text. It does not reproduce historical test runs, certify their
counts, validate the old external conformance harness, or assert that every
historical runtime guarantee holds on current main. Those are separate tasks.
An annotation attributes a correction to the later record; it does not turn
that record into independently reproduced evidence.

## Disposition by record

| Record | Correction relationship and disposition |
| --- | --- |
| [AF](ROUND-AF.md) | AF2's F3/F4, ref-storage, retry, crash-detection and target-lock corrections are already recorded at the earlier claims. Retained. |
| [AF2](ROUND-AF2.md) | Already carries AF3's backend and pagination corrections, but repeats the intermediate scan floor. Added AF4's refutation of that floor. |
| [AF3](ROUND-AF3.md) | Added AF4's correction beside the complete-history conclusion and replaced the birth-tick closure with its read-side-only scope and write-side follow-up. |
| [AF4](ROUND-AF4.md) | Added AF5's containment follow-up beside status-loss recovery. AF4's own agreeing-warnings correction and residuals remain explicit. |
| [AF5](ROUND-AF5.md) | Added AF6's replacement of origin-dependent quarantine and its clarification of which paths walk history. Added AF6's correction of the deselected count and omitted coverage decline, with attribution. |
| [AF6](ROUND-AF6.md) | Its record-accuracy correction supplied the AF5 annotation. Its historical parser predicate and shallow-history boundary are developed further by AF8, AF8B, AF8D and AF10; this audit does not treat AF6's dated measurements as current results. |
| [AF8](ROUND-AF8.md) | The shallow-marker overclaim already points to AF8B. Added the later AF8D raw-object framing correction beside the historical delimiter description. |
| [AF8B](ROUND-AF8B.md) | The impossible-NUL claim already retracts itself and points to AF8D. Its shallow-marker section also explains the correction to AF8. Retained. |
| [AF8D](ROUND-AF8D.md) | Added AF10's rejection of final-block identity beside the earlier parser contract. Raw-object framing remains distinct from identity parsing; no legacy adopted-record compatibility is claimed. |
| [AF10](ROUND-AF10.md) | Added AF11's remaining dangling-alias, stderr and namespace omissions beside the intermediate inventory closure. The synthetic/default-Git comparison already distinguishes its baselines. |
| [AF11](ROUND-AF11.md) | Its successive namespace, prefix-memory and CPU repairs are already ordered as intermediate versus final results. Residual debt remains explicit. |
| [AF12](ROUND-AF12.md) | Already contains the digest, timing, real-gate execution, CI-count and formatting-scope corrections, plus a lead pointer to current estate admission. Retained; no new admission or timeout policy is implied. |
| [AF13](ROUND-AF13.md) | Already distinguishes the wrong-tree/coverage observations from the repaired local checks and dated release receipt. Its external AF10 README correction is recorded as such, not as a correction to AF10's in-repo comparison baselines. Retained. |

## Validation and limits

The patch was compared with the later passages it cites, and every new relative
document target and section heading was checked in the candidate. Historical
test results remain attributed to their original records. No production code,
tests, dependency configuration, frozen benchmark protocol, or external review
instrument is changed. No new test was added for these prose annotations.

The legacy adopted-record policy (task 70) and current external conformance
admission (task 183) remain unresolved. This document cannot settle either.
