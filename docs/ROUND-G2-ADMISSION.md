# Gate 2: estate-gate admission and accepted fixture integration

Owner decision `02af0b730ffb`, witnessed in the interactive Codex Owner session
on 2026-09-16, admits exactly estate gate SHA-256
`d71928779f10f286c7d6f840159783c6b54626a07af72a3762682fdc0e8ed4f8`.
It is the gate blob `a508f5ac22582fb1a06a465669a4cd3b479cbcb0` at estate commit
`a597d97cd8dbcc68714188289d65d714effdaa65`. Both the source constant and the
independent test literal now name these bytes. Future estate versions require
their own admission; this is not automatic tracking of an external file.

The prior admitted digest was
`e20c259cfee795f064eb18fa4208c3d9ef6ea962ecd96cf8efbfe351cce4f669` at estate
commit `fa599905ab95f42622ec352f83ddc327ec031c43`. The unchanged baseline and
the index-correction candidate both refused the installed file at the shipped
gate check before this admission. Their failure logs remain under
`/anvil/scratch/onetree-g2-20260916/`; neither a skipped check nor an old-file
substitution is used to resolve that refusal.

## Admitted behavior

The delta relocates estate source-bind paths to `/anvil` and adds decision-row
authorship checks to the gate's existing commit audit. A candidate adding a
peer-written row to `decisions/decisions.jsonl` is now refused. OneTree still
provides no `--carry-foreign` override; this admission does not add one.
Ordinary own-session payloads and existing attribution refusals retain their
observed behavior. Child environment restrictions, exact candidate/path-set
validation, private gate snapshots and degraded-verdict handling are unchanged.

The parent-authored package
`/anvil/scratch/onetree-g2-20260916/task170-parent-evidence/EVIDENCE.md`
retains direct old/new gate comparisons and known-bad/positive controls under
OneTree's restricted environment. Those direct audits established behavior of
the proposed bytes, not broker admission. The integrated verification below
exercises the newly admitted gate through the repository's real-gate tests.

## Included accepted work

- The dangling private-index symlink correction and index fixtures were
  independently accepted by Claude, decision `bd9d66b9b96e`, against freeze
  `onetree-g2-index-r1-20260916`.
- The referee process-death fixtures and `ROUND-G2-DEATH.md` were authored by
  Claude parent `e85636f2-8a34-4100-9672-8592960d46e9` and independently
  accepted by Codex against `onetree-g2-task16-death-r3-20260916`. They are
  integrated unchanged. Their r2 red full-suite record is historical evidence,
  preceding this admission; their test code is identical in r2 and r3.

Neither fixture family changes production referee cleanup. Task171 retains the
observed orphan process and scratch directory after lander death. Death inside
commit_tree and combined session-body death remain outside established evidence.
Task1952 tracks the gate selftest's undeclared sibling dependencies; task1953
tracks the decision recorder's missing writing-session field. Neither is hidden
by the gate pin update.

## Integrated verification

The complete suite is run with both `ONETREE_TEST_ESTATE_GATE` and
`ONETREE_TEST_SHIPPED_GATE` explicitly selecting the installed admitted file.
Results and the final independent review are retained in
`/anvil/scratch/onetree-g2-20260916/`. Publication remains subject to the
complete suite, lint, strict typing and review; Gate 2 closure is separate.
