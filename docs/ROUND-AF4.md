# Gate 1 Round AF4: crash-consistent landing and a scan floor that is sound

Round AF4 starts from `c2ab14a`. Its scope is the seven items in the AF4 brief:
one execution-miss AF3 recorded as closed and was not, one write-side crash
window neither AF nor AF3 had a name for, the scan floor AF3 sized instead of
justifying, and four surfaces that shipped correct but unfalsified.

The through-line is that three of the seven defects are the same mistake in
three places: a guard was written over the *read* side of a fact and the
*write* side left unheld, and each time the round record said "closed."

## Mechanism

**A killed `update-ref` is asked what it did, not only whether it failed.**
`Repository.update_ref` used to classify every non-zero invocation whose ref
had moved as a lost CAS race. A process killed after the kernel applied its ref
update but before its exit status was collected therefore reported a lost race
over a ref holding *its own* candidate. The new `Repository._reapplied` seam
answers the missing question -- did this invocation's own update land? -- and
the classification asks it in a deliberate order: a no-op CAS (`new == old`,
which is how a lock is probed) stays on the retry path and never reports itself
reapplied. Soundness rests on candidate uniqueness: a broker candidate carries
this operation's and this intent's UUIDs in its commit trailer and a WAL append
carries a fresh event id, so no other process can produce the OID being written
here. `Onetree-Operation` was already what made broker commits self-identifying;
this is the first code that depends on it for correctness rather than for
reporting.

**The reftable absent-arm is taken from storage, not from the tables.**
`show-ref --exists` returning 2 means "no table names this ref," which is an
answer *about the tables* and is therefore worth exactly what they are worth.
That arm now consults `_reftable_storage_state`, which gained a never-healthy
check: on Git 2.53.0 `git init --ref-format=reftable` writes a non-empty
`tables.list` before it returns and every later write replaces it by rename, so
an empty table list is not a state Git leaves behind -- not even transiently.
That is an implementation observation, not a Git guarantee: a ref backend that
did publish an empty list, even transiently, would turn this refusal into a
false positive. Zero bytes is decidable, and the decision is refusal.

**The scan floor is empty.** `_scan_floor` returns `()`. The reasoning and its
accepted cost are in their own section below, because this is the one item
where the brief left the mechanism open and the answer is a deletion.

**Three crash states, named.** `land()`'s crash-atomicity contract previously
described two. It describes three now, and points at `Repository._reapplied`
for the third: intent written and CAS not applied; intent written and CAS
applied and landed mark not written; and intent written, CAS applied, and the
applying process dead before it learned that.

## Per-finding disposition

| Item | ID | Severity | Disposition | Guard |
| --- | --- | --- | --- | --- |
| 1 | F-B1 | major (execution-miss) | **Closed by guard; no source change.** `land()` already wrote `lander_start = _pid_start(os.getpid())`; nothing held it there. AF3 guarded only the read side. The mutation to `None` survived the entire 134-test baseline. | `test_a_healthy_landing_records_the_birth_tick_it_will_be_judged_by` |
| 2 | A-F1 | major (new-debt) | **Closed.** A git death after the branch CAS applied now completes the landing on both backends instead of raising `payload_conflict` over its own successful write. | `test_a_git_death_after_the_branch_cas_applied_completes_the_landing[files\|reftable]`; RED control `test_the_shipped_cas_reading_turns_that_death_into_a_false_payload_conflict` |
| 3 | A-F2 ≡ B-F4 | major | **Closed.** The grandparent floor is gone; a crash-left unrecorded landing is found under any number of hand commits, measured at depths 0, 1, 2, 3 and 5 on both backends. | `test_a_crash_left_landing_is_found_under_any_number_of_hand_commits[0\|1\|2\|3\|5 × files\|reftable]`; `test_page_boundaries_never_drop_the_commit_they_land_on[1..6]` |
| 4 | F-B2 | major | **Closed by guard; no source change.** The `land_intent.lander.warnings` cross-check shipped at `c2ab14a` with no fixture. Disabling the cross-check now takes all four refusal params red. | `test_a_lander_whose_warnings_contradict_its_birth_tick_is_refused` (4 params, all four contradictory shapes) paired with `test_a_lander_whose_warnings_agree_with_its_birth_tick_is_accepted` (2 params, the only two agreeing pairs) |
| 5 | B-F3 | major | **Closed.** A zero-byte `reftable/tables.list` refuses instead of answering absent, while the files backend keeps answering absent for a deleted loose ref. | `test_a_zero_byte_reftable_table_list_refuses_instead_of_answering_absent`; files control `test_a_deleted_loose_wal_ref_still_answers_absent_on_the_files_backend` |
| 6 | B-F5 | major | **Closed as specified; see residuals.** `_reftable_storage_state`'s body now carries the absent-arm decision rather than being decision-dead, proven by a discriminating RED control. Its individual sub-shapes remain unfalsified — stated honestly below, not painted green. | RED control inside `test_a_zero_byte_reftable_table_list_refuses_instead_of_answering_absent`, patching `_reftable_storage_state` to `return None` |
| 7 | B-F6 | minor | **Closed by guard; no source change.** `cli.py:191` (the human-mode warning suffix) was uncovered; `--json` was not. Deleting the human branch was invisible to the suite. | `test_land_warnings_reach_both_cli_output_modes` |

## Red-first evidence

Full captures: `/tmp/af4-gates/red-first-items-2-3-5.txt` (fixtures observed
failing against unfixed src at `c2ab14a`) and
`/tmp/af4-gates/red-first-items-1-4-7.txt` (mutation probes).

**Items 2, 3 and 5 were observed failing before any fix existed.** Running the
round's own tests against `c2ab14a` src: **10 failed, 153 passed** over the full
suite. The AF4 selection alone (`-k` over the new guards) was 10 failed, 19
passed — the same 10 failures by name, which is the figure the capture file
records. Every one of the 10 is an AF4 guard; no pre-existing test regressed.

- Item 2, both backends: `LandingRefusedError: target changed on payload paths`,
  reason `payload_conflict`, raised over a landing whose candidate is already on
  the branch. The known-bad input is a PATH-shim git that runs the real
  `update-ref` and then `kill -KILL`s *itself* (`self_after` mode, new in this
  round), so the ref moves and the broker survives to misread it. The RED
  control's row in that capture is `AttributeError: <class
  'onetree.git.Repository'> has no attribute '_reapplied'` — red for the right
  reason, naming the seam that did not exist yet.
- Item 3, both backends: `AssertionError: assert [] == ['unrecorded_landing']`
  at depths 2, 3 and 5. Depths 0 and 1 passed, which is the measurement that
  matters: the grandparent floor worked exactly as far as AF3 measured it and
  no further. Depth 5 establishes that the class is unbounded in depth rather
  than a two-commit accident.
- Item 5: `Failed: DID NOT RAISE RefCorruptError`. The asserts *above* that
  line all passed, and they are the substance of the finding — with
  `tables.list` truncated to zero bytes, `show-ref --exists` returns 2,
  `git fsck` returns 0, `git refs verify` returns 0, and the `*.ref` tables
  holding the WAL are still on disk. Git blesses the repository; onetree read
  the live WAL as absent and answered `blocked: false` over a held claim.

**Items 1, 4 and 7 pass against unfixed src**, because the shipped behaviour is
correct and only unguarded. "The test failed once" is therefore unavailable for
them, and the falsifiability bar is the surviving mutation:

| Probe | Mutation | Result |
| --- | --- | --- |
| M-1 | `core.py:1117` `lander_start = _pid_start(os.getpid())` → `None` | 1 failed, 162 passed. `assert ('degraded_liveness',) == ()`. The same mutation passed the whole 134-test baseline. |
| M-4 | `core.py:331` `if "warnings" in value:` → `if False and "warnings" in value:` — the whole cross-check disabled, which is the mutation F-B2 itself names | 4 failed, 159 passed. `Failed: DID NOT RAISE WalAppendRefusedError` on **all four** params: `[4242-warnings0]`, `[None-warnings1]`, `[4242-warnings2]`, `[4242-degraded_liveness]`. |
| M-4b | `core.py:334` drop only `tuple(warnings) != _liveness_warnings(start)`, leaving `isinstance(warnings, list)` standing | 3 failed, 160 passed. The fourth param `[4242-degraded_liveness]` is the non-list shape `"degraded_liveness"`, so the surviving `isinstance` check still catches it. A narrower second probe, not F-B2's mutation. |
| M-4c | `core.py:334` condition → `if True:` — a guard that refuses everything | 35 failed, 130 passed, including **both** params of `test_a_lander_whose_warnings_agree_with_its_birth_tick_is_accepted`. This is what the acceptance fixture exists to catch. |
| M-7 | `cli.py:190-191` delete the human-mode warning suffix | 1 failed, 162 passed. `assert False` on `.endswith('[warning: degraded_liveness]')`. |

Every probe was reverted from a byte copy taken before it; `src/onetree/cli.py`
is absent from the committed change set, which is the check that no probe
leaked.

**Refusal fixtures need an acceptance half.** All four params of the item-4
refusal fixture are contradictory shapes — `(4242, ["degraded_liveness"])`,
`(None, [])`, `(4242, ["something_else"])` and the non-list `(4242,
"degraded_liveness")`. None of them is an acceptance control, so on its own that
fixture cannot tell a correct cross-check apart from a guard that refuses
everything: an unconditional `raise` at the top of `_validate_lander` satisfies
all four. `test_a_lander_whose_warnings_agree_with_its_birth_tick_is_accepted`
supplies the missing half with the only two agreeing pairs the cross-check
admits — `(None, ["degraded_liveness"])` for the degraded direction and
`(<tick>, [])` for the healthy one — and asserts both survive the append *and*
the read back out, since the validator runs again on every fold. M-4c is the
measurement that it discriminates. In fairness to what that probe shows: 35
tests go red under it, because every healthy and every degraded landing
exercises acceptance incidentally. The new fixture's contribution is that the
property is now *stated* and named, rather than being a side effect of unrelated
tests that could be rewritten away without anyone noticing.

**Two poison controls** guard against fixtures that would pass for the wrong
reason. Item 2's control restores the old conflation by patching
`Repository._reapplied` to `False` and asserts the *harm*, not merely a
different code path: the caller is told `payload_conflict` while the branch
holds the candidate with the right bytes, `inspect_wal` reports
`refused_candidate_on_target`, and — because nothing retires a `refused` event
— `recover()` raises `WalDivergedError` on every subsequent call, permanently,
over a repository whose data is fine. Item 6's control guts
`_reftable_storage_state` to `return None`, the shape that left its whole body
decision-dead, and asserts that the live WAL then reads as an empty healthy one
at exit 0 with the claim gone.

## The scan floor: why it is empty

AF3 set the floor to the first observed position and its immediate parents,
and recorded that this "clos[ed] the measured one-hand-commit mask." AF4's
measurement is that the fix was sized to the fixture rather than to the defect.

Every exclusion this walk can express is `git log ^X`, and `^X` drops X *and
its entire ancestry*. So a floor is not a window; it is a guillotine placed at
a fixed genealogical distance `d` below first sight. Whatever `d` is, `d` hand
commits put the crash-left broker commit underneath it, and hand commits are
the single most ordinary thing that happens to a branch. The depth was never
the defect — the *existence* of a genealogical floor was. Raising `d` from 1 to
2 to 10 buys nothing but a larger number in the next round's repro.

What is left after removing the guillotine is the empty floor: the branch-side
audit skips no history at all. `_scan_floor` returns `()` and keeps its
`(self, state, target)` signature so the existing M16 monkeypatch control can
still substitute a non-empty floor and observe the difference.

**Accepted consequence, stated plainly.** The audit now walks the target's
whole reachable history on every divergence inspection, in
`DIVERGENCE_SCAN_LIMIT`-sized pages, instead of stopping a few commits below
first sight. On a long-lived branch that is more pages and more subprocess
work per `wal` or `recover` call — a real cost, paid on every invocation, that
grows with repository age. It is accepted because the alternative is a
correctness hole that no value of `d` closes, and because the page mechanism
AF3 built is what makes the unbounded walk affordable: each Git result stays
bounded, the target is pinned to an OID before pagination so concurrent branch
movement cannot reorder page boundaries, and `test_honest_history_past_the_scan_page_never_becomes_inconsistent`
(4,100 ordinary commits) still passes. If this cost ever needs reducing, the
sound direction is a *positional* stopping rule derived from WAL-observed
positions — not a genealogical one.

`test_page_boundaries_never_drop_the_commit_they_land_on[1..6]` runs with
`DIVERGENCE_SCAN_LIMIT` monkeypatched to 2 and walks the crash-left commit
across offsets 1 through 6, so the commit lands *on* a page boundary, one
before it, and one after it. The AF page-boundary contiguity property is held
by construction rather than by luck.

## Deliberate deletions

Two pieces of state were removed rather than updated, because with the floor
empty they had no remaining reader and leaving them would have left dead
scaffolding that a later round would read as intentional:

- `Repository.existing_commits` — deleted entirely, along with the now-unused
  `Iterable` import. Its two assertions in `tests/test_recovery.py` went with
  it. It existed to compute the grandparent floor and has no other caller.
- `_FoldedState.target_origin` — deleted from the dataclass, `empty()` and
  `copy()`. `observe_position` simplifies to `seen.update(positions)`.
  `target_positions` remains and is what the audit actually needs: it names the
  targets a branch-side audit has to *visit*, and never bounds how far down it
  looks.

## Corrections to earlier round records

`ROUND-AF3.md` contains two claims this round measured to be false. They are
corrected here rather than edited there, so the record shows what each round
believed at the time.

1. **Lead (b), "lander birth tick unpinned; degradation silent — Confirmed and
   closed."** It was not closed. AF3's two named fixtures
   (`test_lander_birth_tick_is_part_of_the_in_flight_bar`,
   `test_a_degraded_lander_surfaces_the_same_warning_as_a_degraded_session`)
   hold the *read* side: recovery judging an intent's recorded tick, and the
   warning surfacing. Neither holds the *write* side — that `land()` records a
   real tick instead of `None`. Setting `lander_start = None` passed all 134
   AF3 tests. This is the AF4 execution-miss, and it is why item 1 needed a
   guard and no source change.
2. **"The floor retains the first observed position and its immediate parents,
   closing the measured one-hand-commit mask … so all locally visible history
   is audited."** The first clause is accurate about what AF3 built; the
   conclusion is false. Locally visible history was *not* all audited: two hand
   commits masked a crash-left unrecorded landing on both backends, and so did
   three and five. "Closing the measured mask" is the precise description of
   the error — the mask was measured at depth one and the fix was sized to the
   measurement.

### Correction to this round's own first commit

`bc67d2c`, the first AF4 commit, described the item-4 fixture's fourth
parametrization as "the agreeing-warnings control" and reported M-4 as "3 red
under mutation, 1 agreeing-warnings control stays green." That was false, and a
verification seat caught it. All four params are contradictory shapes; the
fourth is the non-list `(4242, "degraded_liveness")`. The 3/160 result came from
a narrower mutation that dropped only the value comparison and left
`isinstance(warnings, list)` standing, which is what spared the fourth param —
not an acceptance property it never had.

The correction is recorded here rather than by amending `bc67d2c`, because the
ref was already published and the history should show it. Three things changed:
the M-4 row now reports F-B2's own named mutation at 4 failed / 159 passed with
the narrower probe kept and correctly described as M-4b; the acceptance fixture
the doc wrongly claimed to have now actually exists; and the red-first figure is
stated as a full-suite number.

The underlying guard was never wrong — it was stronger than the record claimed,
which is the direction that hides rather than breaks. A round record that
misdescribes its own fixture is exactly the class AF4 exists to punish, and it
would have been dishonest to let the finding stand as an AF3-only failing.

## Honest residuals

Not closed, and not to be read as closed:

- `src/onetree/git.py` lines 306-307, 309, 317-318, 327, 331-332, 335-336, 338
  remain uncovered. These are the individual sub-shapes inside
  `_reftable_storage_state` — the distinct failure reasons it can report.
  B-F5's done-means is met: the body carries the absent-arm decision and a
  discriminating RED control proves it. But each sub-claim is still unfalsified,
  and a mutation to any one of them would likely survive.
- `src/onetree/core.py` lines 325, 329, 331 — the three
  `land_intent.lander is invalid` raises — remain uncovered. This is
  pre-existing and outside item 4's scope; item 4 covered line 335
  (`lander.warnings is invalid`), which is the line F-B2 named.
- `src/onetree/cli.py` line 278 (`raise AssertionError("unreachable")`) and
  `src/onetree/proc.py` lines 23, 37-38 are unchanged from the AF3 baseline.

## Local gates

Verbatim output: `/tmp/af4-gates/gates.txt`. The shared `.venv` is installed
against the main checkout, so every run below was made with
`PYTHONPATH=<worktree>/src` and with `onetree.core.__file__` /
`onetree.git.__file__` asserted to resolve under the worktree first.

| Gate | Result |
| --- | --- |
| `ruff check src tests` | `All checks passed!` |
| `mypy --strict src/onetree` | `Success: no issues found in 7 source files` |
| `pytest` | `165 passed in 39.58s`; `Total coverage: 92.26%`, floor 90 |

Baseline at `c2ab14a`, re-measured in this worktree before any edit: 134
passed, 92.06%. Delta: +31 tests, +0.20 points. Newly covered:
`core.py:335`, `git.py:323`, `cli.py:191`. The two acceptance params add no
coverage, which is expected and is the point: the accepting path through
`_validate_lander` was already walked by every landing in the suite, and what
was missing was a fixture that *says so*. Coverage cannot see that gap, which is
why line counts are not the falsifiability bar here.

The must-not-regress set was re-run by name and all 19 selected cases pass:
AF CRIT-1 (`test_recovery_run_during_a_live_landing_leaves_it_alone`,
`test_a_dead_session_still_bars_recovery_from_touching_a_live_landers_intent`),
D1 (`test_both_ref_backends_refuse_unreadable_wal_storage_and_report_their_lock`
on both backends, `test_readable_but_malformed_reftable_storage_is_not_read_as_absent`,
`test_a_ref_created_after_both_probes_is_retried_not_called_corrupt`),
D2 (`test_honest_history_past_the_scan_page_never_becomes_inconsistent`,
`test_an_unrecorded_landing_beyond_one_scan_page_is_still_reported`,
`test_the_commit_immediately_below_first_sight_stays_inside_the_audit`,
`test_an_unrecorded_landing_survives_the_routine_act_that_used_to_mask_it`),
D3 (`test_a_live_landing_defers_repair_but_never_defers_the_divergence_audit`),
the birth-tick pair, and the four typed-append validators. Reftable unborn
parity is additionally re-held by the new
`test_registering_on_an_unborn_branch_refuses_the_same_on_both_backends[files|reftable]`.
