# AI usage

> **Draft, Gate 3.** This document is a candidate receipt for task 35. It has
> not passed the dispute dry-run (task 41) and is not a README claim. It
> records how this repository was actually built, with each statement bounded
> by a named in-repo record at commit `882d8c8` or by a measurement dated
> here. It distinguishes the historical construction practice (Gates 0–2)
> from the current Gate 3 workflow.

## 1. Who wrote the code

This repository was built by AI coding-agent sessions working under a human
owner who directs, decides and publishes. That is the practice the round
records describe. It is **not** a line-level authorship proof: the in-repo
attribution is the vendor trailer on a commit and the seat named in a round
record, and neither covers every commit. What the evidence establishes is the
attributed contribution below; unattributed commits stay unattributed.

Provenance measured from `git log` at `882d8c8` on 2026-09-16. Trailer rows
are counted per trailer line: 46 of the 62 non-merge commits carry at least one
`Co-Authored-By` trailer, 42 carry exactly one and 4 carry both a Codex and a
Claude trailer, so the four trailer rows sum to 50 lines over 46 commits. No
merge commit carries a trailer.

| Measure | Value |
| --- | --- |
| Commits on `main` | 86, of which 24 are pull-request merges |
| First commit | `8b37772`, 2026-08-17, "Gate 0: scaffold" |
| Non-merge commits | 62 |
| `Co-Authored-By: Codex` | 32 |
| `Co-Authored-By: Claude Opus 5` | 8 |
| `Co-Authored-By: Claude Fable 5` | 6 |
| `Co-Authored-By: Claude` (unversioned) | 4 |
| Non-merge commits with no vendor trailer | 16 |
| Commits carrying a broker `Onetree-Actor` trailer | 25, re-measured at `0adeed7` on 2026-11-10 with `git --no-optional-locks log --grep='^Onetree-Actor: ' --format=%h 0adeed7 \| wc -l` (the first is `882d8c8`, the Gate 3 activation) |

The commit trailer is the attribution the table above measures; round records
attribute changes separately, as described below. Sixteen
non-merge commits carry none; their git author is the owner's identity. Their
authorship is unknown to this document and is not inferred. Git author names
are the owner's accounts plus `Codex` on 8 commits and `onetree session` on 1;
they identify the publishing account, not the writing seat. Round records name
the authoring and reviewing seats for the changes they cover (for example the
review table in `docs/ROUND-G2-CLOSURE.md`), which is attribution of those
changes, not of every line in the tree.

## 2. How the work was organised (historical, Gates 0–2)

The unit of work is a **round**: a bounded brief with a contract, an
implementation, and a record under `docs/ROUND-*.md`. There are 44 such records
at `882d8c8`. The design contract is `docs/DESIGN.md`; each round record names
its base commit, its scope and what it does not claim.

- **Briefed rounds.** Round records open with the brief's scope and base
  (for example `docs/ROUND-A.md`, `docs/ROUND-AF8B.md`, `docs/ROUND-G2.md`).
  Gate 1 rounds were launched through an estate round launcher that
  `docs/ROUND-G2.md` records as "retired" and "not this round's verification
  method"; Gate 2 rounds were dispatched between interactive seats. The
  dispatch mechanism itself is estate tooling outside this repository and is
  not evidenced here.
- **Cross-vendor split.** Codex and Claude seats both author and both review.
  `docs/ROUND-G2-CLOSURE.md` defines "Independent review means a different
  interactive author/reviewer seat" and tabulates, per change, the accepting
  reviewer's decision id and the publishing pull request (Claude decisions
  `bd9d66b9b96e`, `5b11517e48c6`, `7166550f7d5b`, `99ce536cc5c9`,
  `61480da5ec76`, `a6dd19cf7298`; Codex verdicts `task16-owner-review-r3`,
  `task17-owner-review-r2`, `87dd8abfaa0f`). `docs/ROUND-AF11.md` records an
  independent Codex review reproducing a defect after a Claude-side fix.
- **Adversarial verification after rounds.** Round A "was then measured by
  two-seat adversarial verification" (`docs/ROUND-AF.md`); Round AF likewise,
  finding "nine defects, two of them critical" (`docs/ROUND-AF2.md`). Round B
  was followed by a chain of eight fix rounds, BF through BF8, each opened by
  a two-seat read of the previous diff (`docs/ROUND-B.md`, dated 2026-09-10).
  `docs/ROUND-AF12.md` records "two review seats … one adversarial and one
  hunting silent failures". Adversarial verification is recorded for these
  rounds; this document does not claim it for every round.
- **Red-first evidence bar.** "Every guarantee ships with a known-bad fixture
  that was observed FAILING before the guard existed … A test that has never
  failed is not evidence" (`AGENTS.md`, Non-negotiable 3). Round records carry
  known-bad tables naming the input, the observed baseline and the regression
  carrier (for example `docs/ROUND-B.md`, Evidence). Of the 49 markdown
  records at `882d8c8` (`docs/*.md`, `README.md`, `AGENTS.md`), 22 use the
  phrase "red-first" and 21 name a "known-bad" input (case-insensitive text
  counts, files containing the phrase).
- **Verdict ledgers.** Acceptance and changes-requested verdicts are recorded
  by decision id and kept beside the accepted corrections: "Initial
  changes-requested verdicts remain in their evidence directories alongside the
  accepted corrections" (`docs/ROUND-G2-CLOSURE.md`).
- **Receipts, not dates.** "Every claim that will ever appear in this README
  must resolve to an in-repo receipt" (`README.md`). The benchmark protocol
  accepts a metric only "via receipts a stranger can recompute from the
  committed raw data" (`docs/PREREGISTRATION.md`).

## 3. Corrections the records made to themselves

The round records are the receipts, including where they were wrong.

- `docs/ROUND-A.md` carries inline amendments where Round AF "measured it
  wrong", starting with the retraction of the atomic two-ref landing.
- `docs/ROUND-AF-CORRECTIONS.md` audits records AF through AF13 for claims a
  later record corrects or narrows, and places forward references at the
  earlier claims. It states its own limit: an annotation "does not turn that
  record into independently reproduced evidence".
- `docs/ROUND-AF12.md` records reviewers refuting the record's own insertion
  count (966 stated, 1031 reported by git) and catching "the author editing
  this document twice while the seats were reading it".
- `docs/ROUND-B-AMENDMENT.md` changed the Round B contract before
  implementation after the owner observed a counterexample (decision
  `32bd1891c90b`).
- `docs/ROUND-REFEREE-ENV.md` records that "The normal local suite had passed,
  but the actual referee's restricted environment made the same suite red"
  (task 188), found during Gate 3 preparation.

## 4. Current workflow (Gate 3, from `882d8c8`)

`AGENTS.md` is normative: development landings to the shared branch of the
designated clone go through the onetree broker with the configured referee and
the admitted payload gate; sessions stage through private indexes and claim
every payload path; bare commits, switches and resets in that clone are
outside the workflow. The referee is the repository's own test suite run in a
restricted environment (`docs/ROUND-REFEREE-ENV.md`); the payload gate is an
operator-admitted external script checked by source digest (`docs/DESIGN.md`).
The owner activated this workflow in `882d8c8` following the sequence in
`docs/GATE3-CUTOVER.md`. Independent review and owner acceptance precede a
landing; publication to the remote remains the owner's.

## 5. Cost and telemetry

No cost figure is claimed for any seat. Billable cost is **UNKNOWN** for every
seat, and UNKNOWN is not zero. `docs/PREREGISTRATION.md` requires that for the
real-fleet leg "API cost is measured and stated BEFORE the leg runs"; that
statement has not been made, and no fleet baseline exists.

What has been observed, and for which seat:

- **One Claude Code seat** (the seat that drafted this document, session
  `99edb3eb-febc-4531-8cee-8b0cffc16129`): its transcript carries per-message
  token counters (input, cache creation, cache read, output) and no cost field;
  it writes one row per content block, so a counter must be taken once per
  message id and a row sum overcounts. Its account is a subscription. These
  facts were measured on that transcript on 2026-09-16 and are not asserted for
  any other seat, Claude or otherwise.
- **The Codex owner seat**: its retained counter artifact is a different
  schema, cumulative `total_token_usage` and `last_token_usage` objects with
  input, cached-input, cache-write, output, reasoning-output and total token
  fields, not a per-message transcript. That artifact records its billable
  cost as UNKNOWN and carries no billing-mode field; its billing mode was not
  verified.
- **Every other seat**: no counters or billing mode have been examined.

A fleet cost statement will need, for each seat, its counter source, its
counting rule, and a verified billing mode or rate. A complete per-seat cost
baseline of that kind, including verified billing or rates, has not been
established.

## 6. This document's own provenance

Drafted by a Claude seat (model `claude-fable-5-1`, native session
`99edb3eb-febc-4531-8cee-8b0cffc16129`) under an owner handoff on 2026-09-16,
draft start 18:19:23 UTC, from a fresh worktree at `882d8c8`. Revision 2 was
authored by the same seat on 2026-09-16 from 18:31:28 UTC after an independent
Codex review returned changes-requested (decision `aa389865c4a3`); revision 1
is preserved frozen. The task row records planner authorship (decision
`26b023943cfa`); the dispute dry-run of task 41 is still required before any
statement here becomes normative.

## What is not claimed

No productivity, throughput or quality number. No claim that every line was
written by an agent, or by any particular agent; attribution stops where the
trailers and round records stop. No claim that review found every defect; the
corrections above show it did not on the first pass. No claim about rounds
whose records do not state their verification. No claim about any seat's
billing or cost beyond the one observed transcript. No claim about work
outside this repository's history.
