# Round AF12 — candidate commit-payload gate

The dated observations below preserve the AF12 history. Current estate integration
is recorded in [ROUND-ESTATE-ADOPTION.md](ROUND-ESTATE-ADOPTION.md), including the
current source admission and optional native identity metadata. Historical
empty-environment equivalence measurements do not establish identity comparison
for the newly admitted gate.

## Scope

This bounded follow-up closes the shared-index misattribution consequence
recorded in estate task 35 at the broker boundary selected by owner decision
`6f9ac69a794b`: private session indexes feed one serialized landing path, and
that path runs the estate payload audit inline.

The change is deliberately before the first irreversible protocol step. The
broker builds an immutable candidate with `commit-tree`, runs the configured
`commit_payload.py commit <candidate> --repo <root> --json`, and only then may
write a `land_intent` or compare-and-swap the target branch. A refusal can
leave a dangling Git object and records a `refused` WAL event; it cannot move the
target branch or append a `land_intent`.

## Red-first observation

Before the production guard existed,
`test_configured_refusing_payload_gate_is_not_ignored` configured a gate that
returned exit 1 / `REFUSE` with a finding. The test failed because `land()` did
not raise and moved the branch anyway. The focused run reported exactly one
failed test with `DID NOT RAISE LandingRefusedError`.

The first frozen review then showed that repository and path equality did not
bind CLEAN to the candidate object. A gate that ignored argv and emitted a
constant structurally valid CLEAN moved the branch. The added regression first
failed with `DID NOT RAISE PayloadGateError`; the protocol now requires the
gate to echo the resolved candidate OID exactly.

## Contract and controls

The gate source comes from `land --commit-payload` or its explicit
`ONETREE_COMMIT_PAYLOAD` default. It is resolved to a regular file. Its admitted
source digest is the measured SHA-256 of the estate gate candidate, so a
configured no-op or partial substitute is rejected before execution. The
admitted digest is now
`321145146022282384051a918b80dcd0ff1b61bd0487b637c2f7b32af8f2330c`, the third
value the pin has carried. Both bumps are recorded next, in order, because the
sequence is the point.

**First bump, by owner decision.** The
round originally admitted
`de50dd4bc216fc294ea374d5ca19df88f9b0c3b7c31fcf9bf1b98c8993c1c151`, the estate
tool's digest when the round was authored. `~/.claude/tools/commit_payload.py` was
then changed by estate commit `307773a` on 2026-08-27 at 19:48:12 +0000, under
owner decision `0d71b29a16b6` — roughly three hours later. From that moment every
landing configured against this machine's estate gate failed closed at the digest
comparison, before anything executed: the designed behaviour, not a regression.

The round did not re-point the pin on noticing the drift, because a pin whose
holder does that is not a pin. It was handed up as task #122 and bumped only under
owner decision `c414f6d98795`, which admits *these* bytes and is not a standing
licence to track whatever is on disk. The basis offered with that decision:
`307773a` touches `_run_check` only while the broker invokes the `commit`
subcommand, and the full allowlist measurement reproduces against the new bytes —
CLEAN under an empty environment, invariant in both directions across all fourteen
candidate names, with the instrument re-verified before its output was believed.
The second limb of that finding is untouched and still open: this repository
hardcodes the digest of a file that lives outside it and changes on the owner's
schedule, so the next drift arrives the same way.

**Second bump, 2026-09-02, and that prediction came true on schedule.** The
estate gate changed twice more after `c414f6d98795`: estate commit `8e2a7fb`
(2026-08-30 15:15:17 +0000) bound hook suites to `--repo` and chose their
containment by measurement, and `d1deaeb` (2026-08-31 14:39:01 +0000) added
field-scoped source pins to the harness pin audit — together 1031 insertions and
38 deletions against the `7db06163` bytes. Their digest is `32114514…`,
identical across the estate's `origin/main`, `HEAD` and worktree, so the fork
recorded on task #122 had resolved and the pin had one correct value again.
`tools/payload_pin_gate.py`, the estate-side comparison built for #122's second
limb, refused this checkout with exit 3 naming both sites before anything here
was changed: the detection that limb was built to provide, still firing after
the estate change rather than at it, so the limb stays open.

The basis for admitting these bytes, every item measured on 2026-09-02 against
a scratch clone of PR #2's head `462444d`, none against `/home/anvil/onetree`:

- the shipped-gate probe failed at the digest assertion naming both hashes with
  the old pin, and passed in 0.36 s with both sites bumped — the real gate lands
  a benign candidate through the real broker under the nine-name environment;
- the gate's `commit` report over that head is `CLEAN`, exit 0, zero bytes of
  stderr, and carries the six fields the broker requires among its nine keys
  (`advisories`, `hook_suites` and `hook_suite_state` are the three it ignores);
- that report is byte-identical under the full environment and under an empty
  one carrying `PATH` alone, so the allowlist still starves nothing;
- a candidate adding a credential-shaped literal is `REFUSE`d with a
  `commit-credential` finding, so the instrument discriminates and the CLEAN
  above is not a stuck reading;
- five audits completed in 0.16, 0.15, 0.15, 0.14 and 0.17 s, so the 300-second
  bound is unchanged in kind.

One earlier probe by the same session, before the standing constraint on
`/home/anvil/onetree` had been found, ran that checkout's own suite in place.
It wrote only the ignored coverage file there, changed no source, and is not
one of the basis items above; it is named so that "none against
`/home/anvil/onetree`" reads as exactly what it claims.

Two behavioural changes sit on the `commit` path, and each is worth a sentence.
The suite runtime `8e2a7fb` introduced plans a suite only when the payload
stages top-level `hooks/` JavaScript or a file one of the repository's own
`hooks/*.test.mjs` suites imports; the trigger that materialises the staged
tree is broader, any staged `.js`, `.mjs` or `.cjs` module, and a payload that
stages none of these and no harness manifest reports `no-staged-hooks`. This
repository tracks no JavaScript and no such suites. The reduced-containment
mode is reachable only when `--repo` resolves to the estate's own source root,
so it has no reach into a landing here unless the broker is one day pointed at
the estate itself, which is the deferred retrofit and not this round. The
field-scoped source pins `d1deaeb` added engage only when a payload stages a
top-level `claude-harness-manifest.json`, a file this repository does not
carry, and on that path a binding that fails becomes a finding or a degraded
report; nothing there moves a verdict toward `CLEAN`. The review seats measured
both shapes against the new bytes, and one of them re-measured the byte-identity
the broker's docstring asserts over a root commit and a staged hook suite under
the full, the empty and the broker's nine-name environment, and it held.

Unlike the first bump, this admission was taken by the authoring session under
the owner's 2026-09-02 direction to author inline and continue when blocked,
not under a recorded owner decision. It is a two-line reversible change on a
branch whose squash-merge remains held, and the owner's confirmation or reversal
is carried as an open task rather than assumed.

Two review seats read the delta on 2026-09-02, one adversarial and one hunting
silent failures. Both reproduced every basis item above, refuted the insertion
count this section first carried (966, a count that dropped the blank lines
git counts; git reports 1031) and the claim that the suite runtime was the
only behavioural change on the `commit` path; both corrections are in the text
above. One seat also caught the author editing this document twice while the
seats were reading it, a timing figure in the basis list and the matrix
paragraph in the verification register. That is a house-rule violation on the
author's side, named here rather than absorbed: both seats re-read the delta
at 00:31Z and their verdicts cover the re-read. The findings that lie outside
this delta are carried as tasks #140 (a `hooks/` file with no companion suite
lands `CLEAN`, and the broker discards the `hook_suites` key that would show
it), #141 (the only-copy checkout still pins `de50dd4b…`, outside the pin
tool's scan set) and estate task #1444 (the pin tool's own docstring names the
superseded digest, and nothing invokes the tool automatically).

An earlier revision of this document claimed the pin was "re-measured against
`~/.claude/tools/commit_payload.py` on 2026-08-27 and unchanged". That sentence was
true when written and went false three hours later; it is replaced rather than
softened.

The broker copies the admitted bytes into a private read-only file and executes
that copy rather than the configured path. The copy lands at a broker-chosen
path under the repository's own git directory (`.git/onetree/gate`) rather than
wherever `TMPDIR` points, so the filesystem that the digest-checked bytes are
written to and executed from is not caller-selected. It is a sibling of the
temporary-index directory rather than a member: the index sweeper reclaims by
name prefix, and a gate copy that outlives a `SIGKILL` must not be reclaimable by
a rule written for a different kind of residue.

The configured source is re-read after the call and a change is refused, but
**that is change detection for the next landing and for an operator, not a
control over the verdict just produced.** The child executed a snapshot, so what
the configured path holds afterwards cannot bear on what already ran. An earlier
revision listed the re-read among the broker's required controls and described it
as part of the trust chain; that framing is withdrawn here and in the `DESIGN.md`
INV9 bullet.

The child interpreter runs with `-I` and `-S`, and with its whole environment
**admitted by name** rather than inherited and filtered. Required execution
controls retain their literal allowlist. The estate integration separately admits
the three recognized native identity names when present, for attribution comparison.
The earlier control — stripping every `COVERAGE_*` name from an inherited
environment — is withdrawn as insufficient rather than merely imprecise.

Admission rather than subtraction is the load-bearing choice. Subtracting
known-dangerous names cannot establish the property, only narrow it: Git, CPython
and the dynamic loader each grow new environment controls, and under subtraction
every name not yet thought of is admitted by default. The interpreter flags do
not substitute for it. `-I` implies `-E`, `-s` and `-P`, so it closes the
`PYTHON*` variables, the user site directory and `sys.path[0]`; `-S` stops
`site.py`, so a `.pth` file in the interpreter's own site-packages no longer runs
arbitrary code at startup. Neither reaches far enough: `ld.so` resolves
`LD_PRELOAD` and `LD_AUDIT` before CPython's `main()`, so a library named there
executes ahead of every control the broker has, and the source digest does not
help, because it verifies the bytes on disk that the hijacked interpreter then
goes on to read. Because the gate's verdict is trusted, anything that can run
code inside that interpreter forges the verdict outright. `-I` and `-S` are
retained as defence in depth; the allowlist is the control.

Every admitted execution control is chosen by the broker. Native identity is
caller metadata and retains its supplied value; it cannot be reconstructed from
the candidate being audited. `PATH` is pinned to the platform default for that
reason alone. The shipped gate resolves `git` through its own default search path
and never through the value it is handed — measured on 2026-09-02 against the
pinned gate run as the broker runs it (`-I -S`, a constructed environment): CLEAN
with a logging fake `git` first on `PATH` and the fake never invoked, and CLEAN
with no `git` on `PATH` at all — so a forwarded `PATH` would be the one admitted
execution control that arrived ambient. Until `ecbfcda`'s review this paragraph, the broker
docstring and the test guarding the pin all said the gate resolves `git` through
`PATH`; the second review pair refuted it, and the measurement above replaced it.
Empty components are dropped: an empty `PATH` entry means the working directory,
and the child's working directory is the repository under audit.

The set was chosen by measurement rather than by assumption. Run against the
pinned gate over a root commit, a benign non-root commit, a staged hook suite and
a real repository, the gate's report is byte-identical under the full broker
environment and under an empty one. Nothing in the set is needed to keep the gate
working; the set records what the child may be *told*, not what it needs.

The broker requires:

- UTF-8 stdout containing one JSON object and empty stderr;
- no repeated name within that object; a duplicate is refused rather than
  resolved last-wins, so "the gate said CLEAN" and "the gate said this" cannot
  come apart;
- source bytes matching the independently admitted SHA-256, checked before
  execution;
- an exact canonical candidate OID echo matching the commit passed in argv;
- an exact repository root;
- for CLEAN and REFUSE, an exact duplicate-free payload-path set;
- a known verdict whose exit status agrees (`CLEAN`/0, `REFUSE`/1,
  `DEGRADED`/2);
- empty findings and degraded rows for CLEAN.

A candidate-bound DEGRADED report may have no complete payload census because
the gate failed before it could enumerate one. It still has to satisfy the
source, repository, candidate, schema, verdict, and exit-status checks, and it
is a typed fail-closed landing refusal rather than a malformed CLEAN carrier.

Missing configuration, path resolution failure, timeout, malformed fields,
wrong payload attribution, false-CLEAN content, verdict/status disagreement,
`/bin/true`, REFUSE, and DEGRADED all fail closed. The refusal and degradation
cases are typed landing refusals; an untrustworthy carrier is a typed payload
gate error. Every negative fixture asserts that the branch stays at its pre-gate
identity. The two families differ in what the WAL is then required to say, and
the fixtures assert the difference rather than a single blanket condition: for a
carrier failure the WAL head must be unchanged, while for a verdict against the
candidate the WAL head must advance by exactly one `refused` row carrying that
reason and that path set.

A verdict against the candidate is a landing refusal, and `land()` records every
one of its other eight refusals in the WAL before raising. The gate refusal is
now recorded the same way, with the same `reason` the exception carries. The
exception reaches one process; recovery and every peer read the WAL, so an
unrecorded refusal is one that nothing outside the calling process can see
happened. `PayloadGateError` is deliberately not recorded that way: it says the
broker could not obtain a trusted verdict at all, which is an operational
failure of this process rather than a statement about the candidate.

The fixed timeout is 300 seconds. The bound was chosen against a measurement of
the estate gate on this repository. Run directly against this repository's HEAD on
2026-08-28 — outside the broker, so no digest comparison can short-circuit the
timing; the pin was still stale when this was taken — the estate gate of that date (bytes
`7db06163e6432fc1ad2d97a195d0908c2734ed3abc059f2f0d7586319fcf446e`) completed five
audits in 0.152, 0.157, 0.155, 0.159 and 0.157 seconds, all exit 0. The bound is
roughly three orders of magnitude above that, which is operationally generous
without permitting an unbounded broker stall. The figure of 0.25 seconds previously
recorded here is superseded; it is not reproducible against this tree.

The bound is also the ceiling of the gate wait, not only its allowance, and it is
the only ceiling there is: `land()` carries no timeout of its own. Measured on
2026-09-02 in a scratch mirror of `255c734` (onetree #118): a gate that forked a
grandchild holding the broker's stdout and stderr pipes for 1200 seconds, then
printed `CLEAN` and exited, returned `PayloadGateError` "commit payload gate timed
out" after 300.131 seconds with the branch unmoved; at a 2-second timeout the same
shape, and one whose gate slept as well, returned in 2.054 and 2.052 seconds. Had
the bound been nominal, those figures would have read 1200 and 8 seconds, the
grandchild's sleeps. A `CLEAN` printed behind held pipes is therefore discarded
rather than believed, which is the closed direction, though the error names the
timeout and not the cause. The figures are POSIX figures: on timeout CPython's
`subprocess.run` kills the child and waits for it without reading the pipes again,
while its Windows branch calls `communicate()` after the kill and would block on
the handles the grandchild still holds (`subprocess.py`, `run`, the
`TimeoutExpired` branch, read in the 3.14.4 interpreter used here; unmeasured on
Windows). The grandchild itself outlived the broker's kill at the time, because the
child was not made a process group (onetree #142); the gate is now the leader of
its own group and the group is killed on timeout (`docs/ROUND-GATE-GROUP.md`).

## Remediation round

The two-lens review of the first AF12 candidate returned findings, and the owner
directed that they be closed inline rather than briefed out. What changed:

| Finding | Defect | Closed by |
| --- | --- | --- |
| #105 | `-I` alone left an ambient code-injection channel open in the interpreter whose verdict is trusted | added `-S`; `_payload_gate_environment()` now builds the child's whole environment from a named set instead of filtering an inherited one |
| #106 | a repeated name in the verdict document resolved last-wins and reported nothing | `_reject_duplicate_keys` as `object_pairs_hook`; a duplicate is now an unusable document |
| #100 | the gate refusal was the only one of nine landing refusals that never reached the WAL | routed through `_record_land_refusal` with the reason the exception carries |
| #101 | AF12 widened `_invoke` to `except (OSError, RuntimeError)`, and every typed onetree error subclasses `RuntimeError` | reverted to the `except OSError` form its seven sibling sites use |
| #107 | seven guards in `audit_commit_payload` could be deleted with a green build; the private-copy and re-read behaviour was described in this document but bound by nothing | a fixture per guard, each observed failing against the corresponding known-bad source; see the control register below |
| #108 | six `pytest.raises` sites named a class but no message, so any refusal for any reason passed | `match=` and per-case detail assertions at every site |
| #115 | the false-CLEAN table reached the refusal only through the first operand of `findings or degraded`; a gate reporting degraded rows and CLEAN was unbound | `false-clean-degraded` case asserting both the message and the reported degraded detail |
| #116, #121 | the private copy was written wherever `TMPDIR` pointed, and the test named a location it did not assert | `payload_gate_dir()` anchors the copy under the repository's git directory; the test now asserts the executed path is relative to it |
| #104 | `.gitignore` missed `.venv*/` and `.coverage.*` | `.gitignore` widened |
| #110 | this document recorded the subprocess-coverage configuration as UNKNOWN and unmeasured | measured; the configuration is complete and the figure below is not overstated — see the coverage note |
| #99 | an autouse fixture rebinds the shipped digest global, so no test in the suite could observe the shipped pin | `conftest` snapshots the pin at import, before any fixture can rebind it, and the guard now keys its skip on the gate artifact rather than on an environment variable — so it runs wherever the estate gate exists. Both limbs closed; see below |

**#99's second limb is now closed, and an earlier revision of this table read as
though it already were.** That limb was that no test in the suite executes the
real estate gate. `test_the_shipped_payload_gate_admits_a_benign_landing` could,
but it read its target from `ONETREE_TEST_SHIPPED_GATE`, a name that appeared
nowhere else in the repository — no workflow, no document, no fixture set it — so
every run reported it as the single skip. The gap had moved from "no test" to "no
test that runs", which is smaller and was not closed. CodeRabbit rediscovered the
same gap independently on PR #2 without seeing the task that already carried it.

Under owner decision `10d9b7accb9a` the guard now resolves
`~/.claude/tools/commit_payload.py` unless `ONETREE_TEST_SHIPPED_GATE` names
another, and its three outcomes are deliberately different: an absent artifact
skips, a variable naming a missing file fails, and a digest that does not match
the shipped pin fails. So it runs by default on a machine that holds the gate, and
a GitHub runner — which has no estate beside it — still skips. That skip is the
check being honest about where it cannot run, not a variable nobody set.

The change was verified as a controlled pair against a known-bad input already
present in the tree. With the new skip condition in place and the pin still
reading `de50dd4b`, the guard stopped skipping and failed in 0.41 s at the digest
assertion, naming both hashes. With the pin corrected to `7db06163` the same
command passes in 0.75 s, the additional time being the real gate executing a
landing. The first arm is what makes this evidence rather than an observation:
before the skip condition changed, that same command reported one skip and no
failure.

Findings deliberately left open rather than closed here, because each asks a
contract question that is the owner's to answer and not a round's:

- **#102** — the override path.
- **#109** — whether the WAL refusal schema is a public contract, which
  interacts with estate task 80.
- **#103 limb A** — the empty-stderr requirement makes any interpreter diagnostic
  a hard landing failure over an unpinned `sys.executable`; whether that rule
  tolerates a warning channel is load-bearing for rejecting a malformed carrier.
  Unasked, and open.
- **#122's second limb** — the pin is a source constant naming a file outside this
  repository, so the drift recorded above recurs on the owner's schedule rather
  than on this repository's.

Answered since by owner decision, and listed separately so the round is not read
as having settled them on its own authority:

- **#103 limb B** — the post-execution re-read is retained, described as change
  detection rather than as a control over the verdict it follows (`fb86d4286eb8`).
  Its retained cost is unchanged: an unrelated concurrent edit to the estate tool
  inside the 300-second window still converts a valid CLEAN into "commit payload
  gate source changed while executing", and peer sessions commit to `~/.claude`
  routinely — measured on this estate the same day, the gate changed under this
  very candidate at 19:48:12, three hours after the round admitted its digest.
- **#122** — the estate gate's new bytes are admitted (`c414f6d98795`); the pin
  paragraph above records the basis and the bound. The second admission recorded
  there, of `32114514…`, rests on the same kind of measured basis but on the
  authoring session's own authority, and its confirmation is an open task.
- **#123** — the shipped-gate guard keys its skip on the artifact rather than on
  an environment variable (`10d9b7accb9a`).

An earlier revision of this table credited this round with closing #103. It
never touched it. The row that did so also attributed to #103 an isolation claim
that belongs to #105 and a timing figure that belonged to no task at all — it was
this round's own re-measurement. Painting a red row green in a round document is
the same failure class the estate rules name for a build, which is why the
correction is recorded here rather than made silently.

## Verification register

All 35 focused candidate cases in `tests/test_payload_gate.py` pass on this
machine. The shipped-gate probe among them skipped in every run until #123 was
answered; it now executes the estate gate here, and skips only where that artifact
is absent. A standalone focused invocation is diagnostic evidence, not a green
gate: run alone it reaches 55% repository-wide coverage against the global 90%
threshold. The cases include a clean observation that records the exact candidate,
sees the pre-intent WAL head, and proves the candidate is not yet on the target
branch.
The public CLI test proves an explicit `--commit-payload` path overrides the
environment default.

Fixtures here are held to one standard: **a check counts as evidence only once it
has been observed failing against a known-bad input.** Each row below names the
deliberate known-bad input the check was run against and observed to fail on, not
a property the check merely passes with today. Where the known-bad input is a
defect introduced into `src/onetree/git.py`, the original bytes were restored and
re-verified by SHA-256 afterwards.

| Known-bad input | Observed failing |
| --- | --- |
| post-execution source re-read disabled | `test_gate_source_replaced_while_executing_is_refused` |
| regular-file check disabled | `test_unresolvable_or_non_file_gate_source_fails_closed` |
| execute the configured path, not the private copy | `test_payload_gate_executes_a_private_copy_inside_the_repository` |
| private copy written outside the repository, as the shipped code did before this round | `test_payload_gate_executes_a_private_copy_inside_the_repository` |
| `-I` dropped | `test_payload_gate_child_runs_under_isolation_flags_with_coverage_stripped` |
| `-S` dropped | `test_payload_gate_child_runs_under_isolation_flags_with_coverage_stripped` |
| read-only mode dropped on the private copy | `test_payload_gate_executes_a_private_copy_inside_the_repository` |
| `cwd=self.root` dropped | `test_payload_gate_executes_a_private_copy_inside_the_repository` |
| all seven `isinstance` field checks dropped | `test_malformed_or_mismatched_gate_output_fails_closed` |
| last-wins duplicate JSON keys restored | `test_duplicate_verdict_keys_are_refused_rather_than_resolved_last_wins` |
| the child handed an inherited environment | `test_payload_gate_child_runs_under_isolation_flags_with_coverage_stripped` |
| `TMPDIR` added to the admitted name set | `test_payload_gate_child_environment_carries_only_admitted_names` |
| `LD_PRELOAD` planted in the broker's environment | `test_payload_gate_child_is_not_reachable_through_the_dynamic_loader` |
| the parent's `PATH` forwarded into the gate child, by a runtime patch of `_payload_gate_environment` rather than a source edit | `test_payload_gate_child_path_is_the_default_search_path` — observed failing on the parent's leading directory, `attacker_bin`, present in the child's `PATH` |
| `os.defpath` handed to the child verbatim, its empty components kept, by the same runtime patch shape | `test_payload_gate_child_path_carries_no_empty_component` — observed failing with `'' not in ['', '/opt/onetree-vetted-b', '', '', '/opt/onetree-vetted-a', '']` |
| a broker stripping only the *leading* separator, `os.defpath.lstrip(os.pathsep)` | `test_payload_gate_child_path_carries_no_empty_component` — passed against the leading-only fixture this test first shipped with, and fails against the fixture it carries now |
| a broker that never reads `os.defpath`: a hard-coded `'/bin:/usr/bin'`, and separately a default captured once at import | `test_payload_gate_child_path_carries_no_empty_component` — both passed against a fixture whose non-empty entries were this host's own default, and both fail against entries no platform ships |
| a broker collapsing a doubled separator instead of dropping components, `os.defpath.replace(os.pathsep * 2, os.pathsep).strip(os.pathsep)` | `test_payload_gate_child_path_carries_no_empty_component` — passed while the fixture's empty components were all isolated, because the replacement is non-overlapping, and fails against the adjacent pair the fixture now carries |
| a broker dropping the empty components and then sorting what is left, in either direction | `test_payload_gate_child_path_carries_no_empty_component` — a two-entry fixture cannot answer both directions, because any pair is already sorted one way or the other: the ascending pair passed an ascending-sorting broker, and the descending pair written to catch that one passed a descending-sorting broker instead. Both fail against the three-entry non-monotone order the fixtures now carry, and re-sorting both fixtures turns the matching direction green again |
| a broker memoising the default on first call rather than at import, whether inside `_payload_gate_environment` or at the `subprocess` launch frame one level out | `test_payload_gate_child_path_carries_no_empty_component` — passed when this test ran alone, because that run was the first call and cached the value the test then checked. Calling the method directly warmed only the in-method cache and left the launch-frame variant passing; both fail now that the test lands twice, since only a landing walks the segment from the method's return to the child's `environ` |
| a broker correct on every call after its first and wrong on that one | `test_payload_gate_child_path_carries_no_empty_component` — passed while the first landing was a warm-up whose result was discarded; fails now that the first landing is asserted, so the cold path is sampled rather than consumed |
| a broker whose defect is applied to the test's oracle as well as to the shipped expression | `test_payload_gate_child_path_carries_no_empty_component` — passed while the oracle re-derived the expected `PATH` from `os.defpath` by the same expression the broker uses, so both moved together; fails now that both expected values are spelled out |
| a CLEAN verdict carrying degraded rows and no findings | `test_malformed_or_mismatched_gate_output_fails_closed[false-clean-degraded]` |
| the shipped digest pin replaced with sixty-four zeroes | `test_shipped_payload_gate_digest_is_pinned_to_a_literal` |
| the estate gate's actual current bytes, against the round's pin | `test_the_shipped_payload_gate_admits_a_benign_landing` (runs wherever the gate exists; skips where it does not — see #123) |
| the default gate path present as a *directory* rather than a regular file | `test_the_shipped_payload_gate_admits_a_benign_landing` — observed failing with `the default gate path exists but is not a regular file`; the identical input **skipped silently** against the previous revision, which is what makes this a pair rather than an observation |
| `_invoke` widened to `except (OSError, RuntimeError)` | `test_git_invocation_relabels_only_operating_system_errors` |

Two of those rows are worth separating from the rest, because their known-bad
input was not constructed. The `TMPDIR` row was verified by *widening* the
allowlist, observing `AssertionError: Extra items in the left set: 'TMPDIR'`, then
reverting — so the guard is sensitive to that specific name, and because its
expected set is a literal rather than an import from `git.py`, widening the
allowlist cannot silently widen its own guard. The estate-gate row's known-bad
input is the real drift described under the digest above.

The isolation control was also measured directly rather than only through an
introduced defect. Observed from inside the child: under `-I` alone
`sys.flags.no_site` is 0 and under `-I -S` it is 1; and with the loader,
interpreter, Git, coverage and temporary-file names planted in the parent
environment, `_environment()` passes them through while
`_payload_gate_environment()` yields exactly the nine admitted names and nothing
else. Each half fails on its own, so neither is redundant.

One `PATH` row still pins the transformation and never its source, and the register
would overstate it if it left that unsaid.
`test_payload_gate_child_path_is_the_default_search_path` re-derives the expected
value from the same `os.defpath` the broker reads, so a broker and a guard looking at
a hijacked default agree with each other: measured 2026-09-02, with `os.defpath`
rebound to an unvetted directory before the module loads, all 35 cases stay green.
The empty-component row no longer shares that limit — it supplies its own default and
spells the expected value out — and an earlier revision of this paragraph said the
hijack left every gate child in the session searching the unvetted directory, which
was never true of that test: it rebinds `os.defpath` in its own body, so its child
sees the fixture. Re-deriving is sound only where the default is the host's; where a
test supplies the default, re-deriving compares the code against itself. Nothing in the
suite asserts the child's `PATH` is a platform constant, names `/bin:/usr/bin`, or is
independent of process state. The limit is small only because the name is an
attribute of the broker's own process: moving it already means running code there,
which is more than this gate is defending against.

The candidate as it stood on 2026-08-28 passed, on all four interpreters that
`requires-python = ">=3.11"` admits — 3.11.15, 3.12.13, 3.13.15 and 3.14.4, a
superset of the three (3.11 / 3.12 / 3.13) the CI matrix actually pins:

- `ruff check src tests`;
- `mypy --strict src`;
- `pytest` — 389 passed and nothing skipped, total coverage 90.98% against the 90%
  gate, in 175.41 s, 172.25 s, 172.87 s and 155.21 s on the four interpreters
  respectively;
- `git diff --check`.

The second pin bump was measured the same way on 2026-09-02, one interpreter at
a time in the same checkout because the arms share one coverage data directory:
`ruff check src tests`, bare `mypy`, `mypy --strict src` and `pytest` all exit 0
on 3.11.15, 3.12.13, 3.13.15 and 3.14.4, each arm 389 passed and nothing
skipped at 90.98% total coverage, in 143.88 s, 140.26 s, 140.25 s and 125.25 s
respectively, with `git diff --check` clean over the whole delta. The 389 is the
same count as before because the shipped-gate probe was already counted as a
pass; what changed is that it now passes against the bytes the estate actually
holds rather than against a pin the estate had outrun.

That count is one higher than the register this document carried before, and the
difference is the whole of the second limb above: the run that reported *388 passed,
1 skipped* was skipping `test_the_shipped_payload_gate_admits_a_benign_landing`, so
the stale pin sat behind a green summary. The register now counts it as a pass
because it now runs.

**This machine and CI disagree here, and both are right — though not for the reason
this paragraph gave until now.** The estate gate lives outside the repository, so a
local checkout with `~/.claude` beside it executes the test; a CI runner has no
estate to point at, finds no file at the default location, and skips exactly that
one test. The skip condition is the artifact's absence rather than an unset
variable, so the CI skip is the honest reading of a check that genuinely cannot run
there — not a check nobody remembered to switch on.

The figures, read from the run rather than derived from this one: the branch tree
collects 391 cases here and passes 391, measured 2026-09-02 on 3.14.4 at 90.98%
total coverage, while CI run 33606785506 on `89ee1bb`
collected 394 and reported *393 passed, 1 skipped* on each of 3.11.16, 3.12.14 and
3.13.15 — the runner's own interpreters, two of which are not this machine's
3.11.15, 3.12.13 and 3.13.15; the 3.13.15 arm is the version this box also has. Run 33609243559 on the commit after it reported the same 394
and 393/1, so the reading is not a single observation. The extra cases are not
this branch's. The workflow triggers on
`pull_request`, so the checkout is `refs/pull/2/merge` — this branch merged into the
`af5` tip `50de097`, which adds `test_wal_document_version_tracks_the_event_kind`,
`test_adoption_is_safe_to_run_twice` and
`test_adoption_of_a_clean_branch_still_reports_the_other_branch`. A scratch clone
merged with `af5` collects the same 394; had CI tested the branch head it would have
collected 391. Earlier revisions of this paragraph put the CI figure at *388 passed,
1 skipped*, then at 390 and 1, each computed as this document's local count minus
the probe rather than read from a run. The first of those was nevertheless true when
it was written: runs 33148157967, 33151189898 and 33152647407 — heads `2e8bea3`,
`ac3e1c2` and `462444d` — each reported 388 passed and 1 skipped on all three arms.
The sentence was written by `ac3e1c2`, the second of them: `git log -S` over this file
returns `ac3e1c2` and not `2e8bea3`, and `2e8bea3`'s copy of this document does not
contain the string. An earlier revision of this paragraph named `2e8bea3` instead,
taken from a review seat and not checked against the history. It went stale as this branch and
`af5` both moved. The 390 was never reported by any run. The defect was deriving a
figure rather than re-reading it, and a derived figure that starts out right is the
worse of the two, because nothing about it looks wrong until the tree moves. A CI
count moves whenever `af5` moves, so it cannot be derived from this branch at all:
comparing a CI summary against this register means collecting the merge ref's tree,
not this one.

`ruff format --check` reports five files it would reformat — `src/onetree/core.py`,
`src/onetree/git.py`, `tests/test_landing_predicate.py`, `tests/test_recovery.py`
and `tests/test_recovery_semantics.py` — every one of them already unformatted at
the af5 tip this round branched from. This round edits one of the five,
`src/onetree/git.py`, and one of its own edits is what the formatter would move: the
multi-line `PATH` expression in `_payload_gate_environment`, which `ruff format` would
collapse onto a single line, was written by this round's `6a9cf38` and `2e8bea3` — the
line numbers are deliberately not given, since this round moves them. The round created
that hunk rather than merely editing near it. What holds is the narrower claim about a
single range — no edit in the range the previous revision covered falls in such a
region, the nearest hunk being eight lines below the docstring rewritten there — and an
earlier revision of this paragraph widened it to the whole round, where it is false. The two files this round does own were
formatted. The CI workflow runs `ruff check`, never
`ruff format --check`, which is why the drift accumulated unobserved. Estate
task #89 carries it, together with its second limb: the `ruff>=0.9` dependency
has no upper pin, so the formatter's own output can move under the repository
between runs. `mypy --strict` over `tests` as well as `src` reports errors in
three test modules this round did not touch. Neither `ruff format --check` nor
`mypy --strict … tests` is a CI gate — the workflow runs `ruff check src tests`,
bare `mypy`, and `pytest` — and both are recorded here as pre-existing rather
than silently omitted.

The subprocess-coverage question this document previously left UNKNOWN is
measured. `[tool.coverage.run]` sets `patch = ["subprocess"]` without `parallel`
or an explicit `combine`, and that configuration is complete: coverage's own
`CoverageConfig.post_process` sets `parallel = True` whenever `patch` contains
`"subprocess"`, and every reporting action combines before reporting, as does
pytest-cov's finish path. Measured on this tree with a three-arm probe over a
test whose CLI code runs only in a spawned child, `src/onetree/cli.py` reports 0
covered lines with the patch removed and 100 with it present, and adding
`parallel` plus an explicit `combine` changes nothing. The lines executed in
child processes do reach the figure above, and the `--cov-fail-under=90` gate is
not looser than it reads.

Cross-version CI and frozen two-lens review evidence are recorded only after
those independently produced artifacts exist.
