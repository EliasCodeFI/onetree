# onetree design — Gate 1 contract

N agent sessions, one clone, one working tree. Four elements make that safe;
this document is the implementation contract for the first two rounds.

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
     the independently admitted SHA-256, execute a private snapshot of those
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
4. **Test referee** (Round B). Before the ref moves, the candidate is tested
   atop everything queued ahead of it (the exact train). A red train names
   a conflicting pair only when that pair was demonstrated to fail;
   otherwise it reports the demonstrated failing combination or explicitly
   unresolved localization. Red and timed-out candidates remain refused.
   Minimality and localization-cost claims require supporting evidence
   (see `docs/ROUND-B-AMENDMENT.md`). Plus: Git-layer detection of unexplained
   ref transitions recorded in the reflog of a protected shared clone (`switch`,
   `checkout <branch>`, destructive `reset`, `stash`, a bare commit), with
   persistent refusal of subsequent landings. Working-tree operations that move
   no ref leave no reflog transition and are NOT detected: `checkout -- <path>`,
   `restore`, `read-tree -u`, `checkout-index` erase unstaged peer work
   unobserved (measured 2026-09-10). This does not prevent the external
   operation or recover erased unstaged work; the measured boundary and
   evidence-loss residuals are explicit in `docs/ROUND-B.md`.
   A typed rejection
   contract that names exactly the invalidated paths so a session repairs
   minimally.

## State model

All coordination state lives in `refs/onetree/wal` — an append-only event log
implemented as a commit chain on a dedicated ref, pure git plumbing. Each
event is a commit whose tree carries one JSON document; the parent is the
previous WAL head; CAS on the WAL ref serializes appends. Event kinds:
`session_register`, `claim`, `release`, `land_intent` (fence), `landed`,
`adopted`, `refused`, `gc`, `workspace_protect`, `workspace_reconcile`.
`workspace_protect` records
the explicitly established shared-branch, reflog and referee baseline; it
requires reader version 3 and cannot be silently replaced by registration.
`workspace_reconcile` requires reader version 4. It records an explicit
operator reason, the prior epoch id and violation, and a new observed baseline
after claims and pending intents are clear. Earlier epochs remain in the WAL;
ordinary protection and recovery cannot perform this operation. Reconciliation
does not repair branch or file state and does not dismiss other WAL/branch
divergence. The operator workflow is in `ROUND-WORKSPACE-RECONCILIATION.md`.
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

Actor identity is a stable session id recorded on every event. Event signing
(Ed25519) is a design hook behind an optional extra — the core stays
zero-dependency.

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

## Invariants — each ships with a red-first fixture

A fixture must be observed FAILING before its guard exists; a test that has
never failed is not evidence.

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
  terminal CR even when line splitting did not remove one. The measured
  predicate and deliberate exclusion of key-alias configurations that rename
  unrelated markers are recorded in `docs/ROUND-AF10.md`; the bounded large
  folded-input fixture is recorded in `docs/ROUND-AF11.md`.
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
  The gate's complete report and admitted source digest are retained in the
  intent, or in a classified gate-refusal event. Landed and recovered records
  refer to that immutable carrier. Hook suite evidence, advisories and allowed
  NO_COMPANION outcomes remain visible; historical records without evidence
  are explicitly unrecorded. The receipt and replay contract is recorded in
  `docs/ROUND-GATE-EVIDENCE.md`.

## Non-goals for Round A

Train referee, branch-op guards, and rejection contract are Round B. Signing
implementation, multi-host sync, and any daemon are later gates. No runtime
dependencies at any point.

## Prior art informing this design

safegit (per-invocation index isolation + CAS ref landing) · agentlocks (lease
fencing, staged-vs-locked verification) · Flightdeck (server-executed scoped
commits + post-commit audit) · mergetrain (exact-train testing, conflict
bisection) · grite/brat (refs-based append-only WAL, actor identity, janitor
role, daemon-optional correctness). See the launch PRIOR-ART document for the
full survey and credits.
