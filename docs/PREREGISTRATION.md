# Benchmark preregistration — v0 (frozen 2026-08-17)

This document freezes the benchmark's METRICS, BASELINES, and DECISION RULES
before the system under test exists. The run grid (exact N×M values, task
corpus, hardware note) is finalized by a labeled amendment committed BEFORE the
first measured run. Nothing in this file may change after a measured run has
used it, except by an amendment that reports what changed and why — and no
amendment may retroactively reinterpret an already-produced result.

## Systems compared

1. **onetree** — N concurrent agent sessions on ONE clone, ONE working tree:
   per-session git index files, path-level write-claims, serialized commit
   broker (fail-closed payload-vs-claims audit, CAS ref landing), fast test
   suite as semantic referee.
2. **worktree-per-agent** (incumbent baseline) — same N sessions, each in its
   own `git worktree`, merged back at task end.
3. **naive shared tree** (floor baseline) — same N sessions on one clone with
   no coordination beyond stock git.

Same task corpus, same seeds, same machine, same agent driver for all three.

## Metrics (definitions frozen now)

- **Lost-work event:** any byte a session wrote that reaches neither a landed
  commit nor an explicit, attributed refusal/rollback receipt. Counted per run.
- **Collision:** two sessions writing the same path while both writes were
  uncommitted, however resolved.
- **Refusal:** a coordination-layer rejection with a machine-readable reason
  (claims refusal, broker payload refusal, train-test red).
- **Landed-commit integrity:** for every landed commit, the diff equals the
  submitting session's audited payload exactly (no foreign hunks, no missing
  hunks). Binary pass/fail per commit.
- **Throughput:** tasks completed to green-referee landing, per wall-clock hour.
- **Overhead:** setup time and peak disk of the coordination layer itself
  (worktree creation/duplication counts as setup+disk for baseline 2).

## Decision rules

- All metrics on all three systems publish, including every axis onetree loses.
- A system "supports" a metric only via receipts a stranger can recompute from
  the committed raw data (FREEZE.json + MANIFEST.sha256 format).
- Hermetic leg: deterministic synthetic writers, SeedSequence-keyed; the full
  leg re-runs in CI; a reproduction mismatch voids the affected number, and the
  void is reported rather than silently re-run.
- Real-fleet leg: 6–8 real Claude Code sessions building features on this
  repository through the broker; ledger + terminal recording are the receipts;
  API cost is measured and stated BEFORE the leg runs.
- No headline may use a number produced outside this protocol.

## Amendment log

- (none yet)
