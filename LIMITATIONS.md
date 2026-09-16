# Limitations

> **Draft, Gate 3.** This document is a candidate receipt for task 33. It has
> not passed the dispute dry-run (task 41) and is not a README claim. Every
> statement below is bounded by a named in-repo record at commit `882d8c8`;
> where a boundary has been measured, the measurement date is the record's,
> not this document's. Nothing here is inferred from source comments.

onetree lets N agent sessions work in one clone and one working tree. What it
guarantees lives in the git layer and is enforced by the landing path. This
document states what is **not** guaranteed, so that a reader does not extend
the guarantees past the evidence.

## 1. Runtime resources are not isolated

Claims are exclusive **write-claims on repository paths** (`docs/DESIGN.md`,
Mechanism §2). They do not isolate anything outside the repository's files:
network ports, development servers, database state, caches, or other shared
runtime resources. Two sessions that both bind a port, both start a dev
server, or both mutate one database will interfere exactly as they would
without onetree. An operator-supplied referee may exercise a service or other
runtime behaviour as part of its test command; a passing referee result is a
test outcome for that candidate and does not establish runtime isolation.

This is a statement about isolation, not about observation. The mechanism does
observe and manage processes where its own contract needs it: session
registration records the harness PID, its start token and an expiry, and a
session is treated as alive only while both hold (`src/onetree/core.py`,
`SessionInfo` and `_session_alive`); the referee and the payload gate run as
real subprocesses in their own process groups, under an environment the broker
constructs, and are killed as a group on timeout (`src/onetree/referee.py`,
`src/onetree/git.py`; `docs/ROUND-B.md`; `docs/ROUND-GATE-GROUP.md`). Those
controls exist for landing integrity and cleanup. They are not a runtime
namespace, sandbox or resource lock, and they do not detect or arbitrate
conflicts between sessions' own processes.

Evidence limits: the exclusion above is the task's contract, not a conclusion
drawn from word absence. As a literal search result, no file among the 35 under
`tests/` contains the words port, server, database, socket or listen
(whole-word, case-insensitive search, 2026-09-16). That search says nothing
about transitive runtime effects, about tests that exercise runtime behaviour
under other names, or about what may have been stated or tested in the past.

## 2. Single host, single clone, no daemon, no network

The protected object is one clone on one filesystem. All coordination state is
a commit chain on `refs/onetree/wal` in that clone (`docs/DESIGN.md`, State
model). Multi-host synchronisation, a daemon, and event signing are recorded as
later gates or optional hooks, not shipped behaviour (`docs/DESIGN.md`,
"Non-goals for Round A" and the passage on the broker being "a role, not a
required daemon"). The adoption actor recorded in the WAL is "a local marker,
not an authenticated identity" (`docs/DESIGN.md`, same section). Remote pushes
are outside the broker entirely; `AGENTS.md` governs only landings to the
shared branch of the designated clone.

## 3. Guarantees live in the git layer only

Every invariant is enforced by ref compare-and-swap and the WAL, never only by
harness hooks (`AGENTS.md`, Non-negotiable 4; `docs/ROUND-B.md`: "Harness-only
hooks are not the protection mechanism"). The consequence is the boundary in
the next section: what git does not record, onetree does not see.

## 4. The Round B branch-operation residual, as recorded

`docs/ROUND-B.md` ("Workspace protection contract") is the authority; this
section restates it without softening.

- Protection is **persistent detection and refusal of later landings**, not
  prevention. It "does not prevent unrestricted same-user Git commands from
  running or restore unstaged work already erased".
- The evidence is the **reflog**, so only operations that move a ref are
  visible: an unexplained bare commit, branch switch, same-tip reset, changed
  referee, missing log or rewritten prefix blocks later landings as
  `workspace_changed`.
- Working-tree operations that move no ref are **not detected**:
  `checkout -- <path>`, `restore`, `read-tree -u` and `checkout-index` "erase
  unstaged peer work without a reflog transition and are not detected
  (measured 2026-09-10; the same damage from `reset --hard` is)". The suite
  keeps four cases that assert this adverse outcome; "Their passing assertions
  describe the gap, not protection" (`docs/ROUND-G2-CONTROLS.md`).
- **Sampling leaves a window** between a check and a later external write.
  "Suppressing and restoring every distinguishing observation can escape
  detection; reflog suppression/rewriting is refused when observable."
- Routine reflog expiry by `git gc` rewrites the recorded prefix and
  quarantines an unmoved workspace. Lifting a quarantine is an explicit
  operator reconciliation (`docs/ROUND-WORKSPACE-RECONCILIATION.md`), never a
  silent refresh by `protect`, registration or recovery.
- A refusal's `paths` identifies the submission being held, "not a forensic
  claim about every file an external command damaged".

Where prevention was impossible, detection plus refusal-to-land is the bar
that was met. Nothing more is claimed.

## 5. Same-user execution is not a sandbox

The referee "is a test runner, not a sandbox for hostile commands"; its
executable and external dependencies are trusted operator inputs
(`docs/ROUND-B.md`, Referee contract). "Same-user Git and referee execution are
not a security sandbox" (`docs/ROUND-G2-CLOSURE.md`, "Limits retained for the
next gate"). The payload gate executes an operator-admitted script whose source
digest is checked before and after the call; the after-check "is change
detection for the next landing and for an operator, not a control over the
verdict just produced" (`docs/DESIGN.md`, payload gate passage). The referee
checkout copies tracked files only; "untracked build dependencies are not
copied" (`docs/ROUND-B.md`).

## 6. Process and platform boundary

- The payload gate and the referee supervisor require POSIX process groups and
  wait-without-reaping (`docs/ROUND-GATE-GROUP.md`, Platform; `docs/ROUND-B.md`,
  task 171 passage). "Windows is unsupported for this path and unmeasured",
  and unsupported platforms refuse rather than weaken cleanup.
- A descendant that calls `setsid` has left the gate's process group and "is
  outside that contract"; a successful gate's descendants are not signalled
  (`docs/DESIGN.md`; `docs/ROUND-GATE-GROUP.md`).
- A second interrupt arriving inside the cleanup itself is not handled
  specially; descendants may survive it (`docs/ROUND-GATE-GROUP.md`).
- Deliberately detached children, loss of both lander and supervisor, and host
  failure are outside the referee cleanup contract (`docs/ROUND-B.md`).
- Bounds: the payload gate's wall-clock limit is 300 s
  (`PAYLOAD_GATE_TIMEOUT_SECONDS` in `src/onetree/git.py`); the referee default
  is 300 s (`DEFAULT_TIMEOUT` in `src/onetree/referee.py`), configurable through
  `onetree.refereeTimeout`. The 300 s production bound has not been measured
  end to end; tests pin shorter values (`docs/ROUND-GATE-GROUP.md`).

## 7. What the referee does not decide

A red train does not identify a minimal conflicting pair. A pair is named only
when it was demonstrated to fail; otherwise the receipt reports the failing
combination or "explicitly unresolved localization". Minimality and
localization-cost claims require evidence that does not yet exist
(`docs/ROUND-B-AMENDMENT.md`, owner decision `32bd1891c90b`). Red, timed-out
and execution-failed candidates are refused; a refusal is not a diagnosis.

## 8. What has not been demonstrated

- **Concurrency.** The Round B evidence used "two protocol actors attached to
  one actual process; this was not a concurrency measurement" (`docs/ROUND-B.md`,
  Evidence). The Gate 3 cutover record states that "A single disposable
  rehearsal does not satisfy that exit receipt" and that closure "requires its
  separately tracked concurrent-session dogfood history"
  (`docs/GATE3-CUTOVER.md`). No multi-session landing history is in this
  repository at `882d8c8`.
- **Benchmark numbers.** `docs/PREREGISTRATION.md` freezes metrics and decision
  rules; its amendment log is "(none yet)" and no measured run exists. The
  README carries no claims for that reason.
- **Historical results as current guarantees.** The round records date their
  measurements. `docs/ROUND-AF-CORRECTIONS.md` states that historical test
  results "remain attributed to their original records" and that its audit does
  not "assert that every historical runtime guarantee holds on current main".
- **Open policy.** The legacy adopted-record policy (task 70) and external
  conformance admission (task 183) are recorded as unresolved
  (`docs/ROUND-AF-CORRECTIONS.md`).

## Reading this document

A limitation listed here is a shape a later change may flip; a limitation not
listed here is not thereby absent. Where a record and this summary disagree,
the record wins.
