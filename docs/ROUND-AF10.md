# Gate 1 Round AF10: commit-object landing identity

AF10 starts from `9d2ef3baa4e3369b13b86081836511e261e75a0c` on
branch `af5`. The local external surface is Git 2.53.0. Context7 was queried
before relying on that surface, but its resolver refused the request with
`Monthly quota exceeded`; the round therefore uses the supplied facts pass,
local Git 2.53.0 measurements, and the Git 2.53.0 manual and source as the
versioned evidence.

## The predicate and its measured boundary

`landing_ids(raw_message: bytes) -> frozenset[str]` reads the bytes after the
commit object's header terminator. It does not read configuration, spawn Git,
or raise. A column-zero line is a candidate when its first 17 bytes match
`Onetree-Operation` case-insensitively. It unions the value after every
separator position reachable by changing `trailer.separators`, and every
prefix of the immediately following indented continuation run. One trailing
CR is removed per line, and the union also retains Git's raw internal CR when
CRLF lines are unfolded. Values trim only ASCII horizontal whitespace, then
use strict UTF-8 where possible and `surrogateescape` otherwise; Unicode
whitespace is identity data, not formatting.

No paragraph location, `---` divider, diff start, comment, scissors line,
conflict block, generated-prefix ratio, or trailing-prose rule can hide a
candidate. Those are rules of other inputs or config-steered ways Git chooses
a final trailer block; they are not safe gates for broker identity in a raw
commit object.

The supplied AF10 harness measured the line-union predicate over 1,502 message
shapes and 240 parsing configurations. It had zero missed shapes and zero
missed values against the Git union when configurations were restricted to the
literal `Onetree-Operation` marker. Including
`trailer.<name>.key=Onetree-Operation` aliases produced 75 apparent misses:
those settings rename unrelated tokens into the requested key and are outside
the identity contract, rather than evidence that a literal onetree marker was
missed. The repository's seeded corpus independently scores 1,502 shapes,
including CRLF crossed with continuation runs (a combination the supplied
corpus did not cross), under 303 literal-marker configurations. It measured
zero missed shapes and values; 595 shapes carried 1,063 extra values versus
the Git union -- the union of what Git detects across all 303 literal-marker
configurations, which is the strictest baseline available and not the same as
default Git. The default-Git comparison is a different, larger number; see
"False-refusal cost and escape" below. Those two totals are this round's
measurement on the pinned external surface, not an asserted invariant: they
count the gap to Git, so Git's own trailer behaviour moves them, and a total
that has to be re-pinned on every Git upgrade reads as a regression when it is
not one. The configuration count is pinned, because nothing about it depends on
Git. The test pins every parsing setting through `GIT_CONFIG_COUNT`
variables, proves those pins override a blinding local
`trailer.separators` configuration, and asserts the one-directional relation
`git <= onetree`, an exact independent predicate model, and readable
allow-lists for each named extra category.

The alternative final-block predicate also had zero synthetic misses, but
missed one real folded-squash landing. It was rejected. Deleting only the
`---` truncation increased fail-open shapes from 259 to 261 and was rejected.

## False-refusal cost and escape

The line union deliberately detects more than default Git: 869 of the 1,502
synthetic shapes, and one of 18 porcelain messages. This 869 is measured
against default Git, so it is necessarily larger than the 595 reported above,
which is measured against the Git union across all 303 configurations. The two
numbers use different baselines and do not contradict each other. The porcelain false
refusal is `prose-mentions-token`. The extra categories asserted in this tree
are a marker used as the title or prose, a marker before a later paragraph, a
non-default separator, a marker followed by divider/reviewer text, and the
unfolded-prefix variants of continuation lines (including the normalized and
Git-preserved internal-CR forms), plus a literal token followed by a reachable
separator suffix. For each operator-visible category an end-to-end test creates
the commit, observes
`unrecorded_landing`, runs `wal --adopt`, observes `consistent`, and then
registers a session to prove mutation is re-permitted. An extra detection that
the in-tool escape cannot clear is a defect, not an accepted cost.

Multiple candidate ids remain one commit-level decision. A recorded commit is
consistent when any parsed id forms its recorded `(commit, operation)` pair.
If none does, the scalar divergence and frozen adopted-event schema use one
deterministic witness, preferring an id that is not a pending operation for a
different candidate. One adopted pair then satisfies the same ANY rule. If the
only witness is also pending for a different commit, adoption records the
distinct pair without cancelling that live intent; its later `landed` or
`refused` event closes the pending work normally.

## Other AF10 closures

- A terminal `landed` or `refused` event folds and CAS-appends directly against
  the WAL. It does not rerun the repository mutation audit after an irreversible
  branch CAS, so an unrelated landing cannot turn completed success into a
  caller-visible refusal.
- `wal --adopt --target` derives its candidate walk from the named branch
  before commit deduplication; the final report remains repository-wide.
- Implicit `land` resolves only symbolic HEAD and refuses detached HEAD.
  Session registration retains the detached repository escape used by the
  repository-wide audit.
- The local-ref inventory uses Git's `for-each-ref` ref-filter path. Git 2.53.0
  intentionally warns and skips malformed ref *names* while still returning
  every valid local branch and HEAD. Onetree accepts that locale-pinned warning
  class across ref namespaces and embedded control bytes. For the files
  backend it also reconciles valid loose and packed branch names before and
  after the listing, so a warningless dangling symref or a non-UTF-8 broken ref
  cannot disappear behind a malformed warning. Every emitted name, oid, and
  object type is still validated by Git-backed checks.
- That inventory closure was incomplete. [AF11](ROUND-AF11.md#red-first-defects)
  records dangling aliases that still prevented adoption from restoring access,
  unrelated successful-command stderr that caused refusal, and a resolving
  namespace symlink that could hide a corrupt child. AF11 records the subsequent
  repairs; AF10's inventory paragraph describes an intermediate implementation.
- The WAL/head snapshot rechecks retained by mutation paths, inspection, and
  recovery each have a deterministic peer or branch interleave. The two
  adoption rechecks were removed because the WAL CAS and repository-wide final
  report already express their observable outcomes.
