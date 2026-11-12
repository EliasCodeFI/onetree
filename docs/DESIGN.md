# onetree design

Several agent sessions, one clone, one working tree. Four parts make that safe. This document is
the technical contract: what the code guarantees and what the tests check. It is precise rather
than easy; the [README](../README.md) explains the same mechanism in plain words.

## Words used here

- **Session**: one agent working in the clone, identified by a session id.
- **Index**: git's staging area. Every session has its own.
- **Claim**: a session's exclusive right to write one path.
- **Payload**: the paths a landing changes. **Candidate**: the commit built for a landing.
- **Broker**: the code behind `onetree land`, the only way a change reaches the shared branch.
- **Payload gate**: a script the repository's operator supplies and admits; it checks every
  candidate before it lands.
- **Referee**: the operator's test command, run on the candidate before the branch moves.
- **WAL**: the write-ahead log, an append-only event log kept on the ref `refs/onetree/wal`.
- **CAS**: compare-and-swap; the branch moves only if nobody moved it since the candidate was
  built.
- **Fence**: the check, at the moment of landing, that the session still holds its claims.
- **Train**: a candidate together with the candidates queued ahead of it.
- **Baseline** and **epoch**: the state of the shared branch that `onetree protect` records; each
  protection or reconciliation starts a new epoch.
- **Adoption**: the operator's explicit acceptance of a landing the WAL did not record.

## Mechanism

1. **Per-session index.** Every session gets a private git index file
   (`.git/onetree/sessions/<actor>/index`). Staging never collides because no
   two sessions share an index. The working tree is shared; the staging layer
   is not.
2. **Path claims.** `onetree claim <path>...` takes an exclusive write-claim
   per path for the calling session. A refusal names the current holder,
   machine-readably. Claims canonicalize to repo-relative paths; a relative,
   nonexistent, or out-of-repo input is a typed error — never a silent allow.
3. **Commit broker.** `onetree land` is the only landing path:
   - audit: every path in the payload is claimed by the submitting session —
     fail-closed, an unclaimed path refuses the whole payload;
   - build: `git write-tree` against the session index, then `commit-tree` —
     the shared index and working tree are never touched;
   - payload gate: require the configured `commit_payload.py` source to match
     the SHA-256 the repository's operator admitted (`onetree gate --admit`,
     recorded as `onetree.payloadGate` and `onetree.payloadGateSha256` in the
     repository's own Git configuration; a repository with no admission refuses
     every landing), execute a private snapshot of those
     bytes, and confirm the configured source remained unchanged; invoke its
     `commit` mode over that immutable candidate, require one internally
     consistent JSON verdict for the exact candidate, repository, and payload
     paths, and proceed only on CLEAN with no findings or degraded rows;
     operational gate errors leave both the target ref and WAL unchanged;
     a trusted REFUSE or DEGRADED verdict preserves the target and records a
     `refused` WAL event, without a `land_intent` or `landed` event;
   - fence: claims must still be held at the landing moment; a reclaimed or
     expired claim aborts the landing;
   - land: compare-and-swap `update-ref` on the target branch; on a moved
     head, re-parent the built tree and retry; exactly one racer wins per
     attempt, losers retry cleanly.
4. **Test referee.** Before the ref moves, the candidate is tested
   atop everything queued ahead of it (the exact train). A red train names
   a conflicting pair only when that pair was demonstrated to fail;
   otherwise it reports the demonstrated failing combination or explicitly
   unresolved localization. Red and timed-out candidates remain refused.
   Claims that a reported combination is minimal, or about the cost of finding
   it, require supporting evidence. Plus: Git-layer detection of unexplained
   ref transitions recorded in the reflog of a protected shared clone (`switch`,
   `checkout <branch>`, destructive `reset`, `stash`, a bare commit), with
   persistent refusal of subsequent landings. Working-tree operations that move
   no ref leave no reflog transition and are NOT detected: `checkout -- <path>`,
   `restore`, `read-tree -u`, `checkout-index` erase unstaged peer work
   unobserved (measured 2026-09-10). This does not prevent the external
   operation or recover erased unstaged work. A refusal names exactly the paths it invalidated, so a
   session can repair only those.

## State model

All coordination state lives in `refs/onetree/wal` — an append-only event log
implemented as a commit chain on a dedicated ref, pure git plumbing. Each
event is a commit whose tree carries one JSON document; the parent is the
previous WAL head; CAS on the WAL ref serializes appends. Event kinds:
`session_register`, `claim`, `release`, `land_intent` (fence), `landed`,
`adopted`, `refused`, `gc`, `workspace_protect`, `workspace_reconcile`.
`workspace_protect` records
the explicitly established shared-branch, reflog, referee and payload-gate
admission baseline; it requires reader version 5 and cannot be silently replaced
by registration. Version 3 events, written before the baseline carried the
admission, remain readable and are read as protecting no admission.
`workspace_reconcile` requires reader version 5 (version 4 is read the same way
as a version 3 protection). The broker refuses every landing under an epoch that
protects no admission, including one protected while no gate was admitted, with
`workspace_gate_unprotected`, before any gate runs; admitting a gate and
reconciling records it in a new epoch. A reconciliation records an explicit
operator reason, the prior epoch id and violation, and a new observed baseline
after claims and pending intents are clear. Earlier epochs remain in the WAL;
ordinary protection and recovery cannot perform this operation. Reconciliation
does not repair branch or file state and does not dismiss other WAL/branch
divergence. The operator runs it as
`onetree reconcile --expected-epoch <current protection event id> --reason <text>`.
`adopted` records the operator's explicit
acceptance of a broker landing the WAL did not record; every other kind is
written by the broker itself. The working tree stays pristine — no state files
in the tree, ever. History cannot be edited without showing in the ref.

WAL inspection reports divergences per audit target. A repository-wide
condition such as shallow history can therefore produce separate findings for
`HEAD` and each audited branch, even when they point to the same commit. The
human summary counts audit-target findings, not distinct repository conditions
or WAL events. Each JSON divergence retains exactly `kind`, `target`, `detail`,
`commit`, and `operation`; `target` is a scalar string and is part of the
finding's identity.

Each document carries `v`, the lowest reader version that understands its
event kind — not a version of the document shape, which has not changed. A
reader accepts any version it knows and refuses a higher one by version, so a
binary that predates a kind refuses it as an unsupported version rather than
as an unknown kind, and only once that kind is actually used. Both refusals
are `wal_corrupt` — an old reader cannot be taught a new type — so the change
is in what the operator is told, not in what a machine can dispatch on. A
document whose `v` disagrees with its kind is refused: understating the
version is how a newer kind would otherwise reach a reader that refuses the
version but not the kind. `adopted` is the first kind above version 1.

One historical exception is read, never written. The pre-release writer at
`9d2ef3b` recorded `adopted` at version 1 with a scalar `operation` that
comma-joined every landing trailer value of the adopted commit, and a value
containing a literal comma persists identically. A reader accepts `adopted`
at exactly version 1 as a historical record, verifies it against the commit
it names under that writer's own trailer semantics (the stored scalar must
equal the historical parse exactly; it is never split), keeps the parent and
tree checks, and treats the verified commit as recorded by commit so the
branch audit neither reports it nor adopts it again. The current writer still
writes `adopted` at version 2 and refuses to append a version-1 document;
every other version/kind disagreement, every version above the reader's, and
every malformed document are refused as above. No other kind has a historical
version.

A WAL event that is valid JSON but cannot be decoded within the running
interpreter's limits (integer conversion or nesting depth) is refused as
`wal_corrupt` with `reason: "json_decode_limit"` and the event's commit id;
`onetree wal --json` then exits with status 2. The reason says the event could not
be decoded under the current settings, not that its stored JSON is invalid.

Each event records an actor string. Session operations use their session id;
explicit adoption records the literal `operator`. That adoption actor is a
local marker, not an authenticated identity of the person or session that
accepted the landing. Event signing (Ed25519) is a design hook behind an
optional extra — the core stays zero-dependency.

The broker is a role, not a required daemon: the landing code path itself
enforces every invariant via WAL/ref CAS, so correctness never depends on a
long-lived process. A daemon may later serialize queueing as a performance
optimization only.

Repository history is audited across every local branch plus HEAD. On the
files ref backend, valid loose and packed branch names are reconciled around
Git's listing. A loose symbolic ref or filesystem symlink whose terminal
exact ref is absent is not a branch head and is excluded. A namespace
directory or a dangling alias pathname is not exact terminal storage; a
resolving alias is audited, and a same-name packed ref prevents exclusion.
Resolving namespace symlinks are traversed under every exposed ref name with
per-path inode ancestry; a cycle refuses, and a malformed descendant remains
in storage reconciliation even when Git omits it. Stored malformed values
remain fail-closed. Successful Git stderr is not treated as ref truth:
warnings and traces unrelated to refs cannot deny every OneTree command. The
storage reconciliation carries that decision for files refs; reftable retains
its explicit broken-branch diagnostic guard.

## Invariants

Each invariant ships with a test that was observed failing before its guard
existed; a test that has never failed is not evidence.

- **INV1** No landing whose payload contains an unclaimed path.
- **INV2** No landing after fence loss (claim reclaimed, session dead).
- **INV3** Exactly one CAS winner per race; the loser re-parents and retries
  without corrupting either landing.
- **INV4** The WAL is append-only and linear; events are immutable.
- **INV5** A landed commit's diff equals the audited payload exactly — no
  foreign hunks, no missing hunks.
- **INV6** Claim refusals name the holder; path canonicalization rejects
  relative/nonexistent/out-of-repo inputs with a typed error, never a silent
  allow.
- **INV7** Kill the landing process at any point: no partial state survives;
  recovery replays the WAL to a consistent view.
- **INV8** Landing identity is a set parsed from the raw commit-object message,
  never from a patch stream. The parser unions every non-empty value reachable
  from a column-zero, case-insensitive literal `Onetree-Operation` marker over
  pinned separator and folded-continuation settings, across the whole message;
  the empty set is the ordinary non-landing result. Against the config-pinned
  Git commit-object oracle for that literal marker, onetree may see more
  candidates but never fewer. Every deliberate extra shape must have a tested
  `wal --adopt` escape that restores consistency and permits mutation. A marked
  commit is consistent when any candidate forms a recorded `(commit,
  operation)` pair. Folded candidates are decoded directly from one reusable
  byte buffer. The exact `landing_ids` interface emits them into its
  output-sized set one at a time rather than retaining a second cumulative
  byte collection. Repository audit does not materialize or decode that whole
  set. It scans each marker-position prefix family once, indexes finite
  recorded identities by round-trippable byte length, and decodes only a first
  witness, candidates still needed to escape the pending set, and lengths that
  can match a recorded identity. It stops on any recorded pair match; otherwise
  it retains only the lexicographically smallest nonpending witness (falling
  back to the overall minimum). Adopted-record verification uses the same
  bounded exact-membership reduction. Raw parsing is skipped only when the
  message contains no CR byte; horizontal trimming can otherwise expose a raw
  terminal CR even when line splitting did not remove one. Key-alias
  configurations that rename unrelated markers are deliberately excluded.
- **INV9** No candidate becomes visible on a target branch or in a WAL intent
  unless the source-admitted commit-payload gate echoed and audited that exact
  candidate and exact path set CLEAN. The gate runs from a read-only private
  copy of the admitted bytes, at a broker-chosen path under the repository's own
  git directory rather than wherever `TMPDIR` points, and in an interpreter
  whose whole environment is admitted by name. Admission is the load-bearing
  word: subtracting known-dangerous names narrows the channel without
  establishing the property, because Git, CPython and the dynamic loader each
  grow new environment controls and every name not yet thought of is admitted by
  default under subtraction. The interpreter flags do not reach far enough to
  substitute for it -- `-I` and `-S` are read by CPython, while `ld.so` resolves
  `LD_PRELOAD` and `LD_AUDIT` before `main()`, so a library named there runs
  ahead of every control the broker has, and the source digest does not help
  because it verifies the bytes on disk that the hijacked interpreter then goes
  on to read. Source digest, exit status, verdict, candidate, repository, path
  list, findings, and degraded rows are one coupled contract; no-op or partial
  substitutes fail closed. The configured source is re-read after the call, but
  that is change detection for the next landing and for an operator, not a
  control over the verdict just produced: the child executed a snapshot, so what
  the configured path holds afterwards cannot bear on what already ran.
  The gate is the leader of its own process group, and when the broker's bounded
  wait ends in a timeout or is cancelled the whole group is killed before the
  exception propagates, so a descendant the gate forked cannot outlive a refused
  landing while it holds the broker's output pipe. The broker observes the gate's
  exit without reaping it and reaps only after that signal, so the group it
  signals is always the gate's own. A descendant that calls `setsid` has left the
  group and is outside that contract, and a successful gate's descendants are not
  signalled (POSIX only).
  The gate's complete report and admitted source digest are retained in the
  intent, or in a classified gate-refusal event. Landed and recovered records
  refer to that immutable carrier. Hook suite evidence, advisories and allowed
  NO_COMPANION outcomes remain visible; historical records without evidence
  are explicitly unrecorded.

## Not in scope

Event signing, synchronisation between hosts, and a daemon are not implemented. There are no
runtime dependencies at any point. The projects this design learned from are credited in the
[README](../README.md#credits).
