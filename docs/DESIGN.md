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
   - fence: claims must still be held at the landing moment; a reclaimed or
     expired claim aborts the landing;
   - land: compare-and-swap `update-ref` on the target branch; on a moved
     head, re-parent the built tree and retry; exactly one racer wins per
     attempt, losers retry cleanly.
4. **Test referee** (Round B). Before the ref moves, the candidate is tested
   atop everything queued ahead of it (the exact train). A red train bisects
   to name the conflicting pair. Plus: branch-operation refusal (`switch`/
   `checkout`/destructive `reset` in a shared clone), and a typed rejection
   contract that names exactly the invalidated paths so a session repairs
   minimally.

## State model

All coordination state lives in `refs/onetree/wal` — an append-only event log
implemented as a commit chain on a dedicated ref, pure git plumbing. Each
event is a commit whose tree carries one JSON document; the parent is the
previous WAL head; CAS on the WAL ref serializes appends. Event kinds:
`session_register`, `claim`, `release`, `land_intent` (fence), `landed`,
`refused`, `gc`. The working tree stays pristine — no state files in the tree,
ever. History cannot be edited without showing in the ref.

Actor identity is a stable session id recorded on every event. Event signing
(Ed25519) is a design hook behind an optional extra — the core stays
zero-dependency.

The broker is a role, not a required daemon: the landing code path itself
enforces every invariant via WAL/ref CAS, so correctness never depends on a
long-lived process. A daemon may later serialize queueing as a performance
optimization only.

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
