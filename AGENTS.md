# AGENTS.md — how work lands in this repository

This file is read by AI coding agents. It is normative for every session
working in this clone.

## Build phases

Gate3 dogfood is active in the designated primary shared clone. All development
landings to its shared branch go through OneTree with the configured referee
and admitted payload gate. Stage through your session's private index and claim
every payload path: the broker refuses a payload that contains a path your
session has not claimed. Bare Git commits to shared refs, branch switches and
resets in that shared clone are out of contract. A refusal is evidence to
inspect and resolve; do not reset or reconcile the protected baseline merely to
make it disappear.

## Non-negotiables

1. Zero runtime dependencies. Dev tooling lives in the `dev` extra only.
2. Every CLI subcommand supports `--json`: exactly one JSON object on stdout,
   nothing else on stdout.
3. Every guarantee ships with a known-bad fixture that was observed FAILING
   before the guard existed (red-first). A test that has never failed is not
   evidence.
4. Enforcement lives in the git layer, never only in harness hooks.

## Repository layout

- `src/onetree/` — the package (Python ≥3.11, typed, mypy --strict).
- `tests/` — pytest suite; coverage gate enforced in CI.
- `docs/` — the design contract and the project's web page.
- `plugins/onetree/` and `.claude-plugin/` — the Claude Code plugin and its marketplace entry.


<!-- BEGIN CODEX PR REVIEW RULES v1 -->
# Code Review Rules

These instructions apply only to native Codex pull-request reviews. They do not mandate pull requests or approvals, authorize edits, or change repository authoring workflows.

- Report only actual material defects introduced by this PR. Give a changed file:line, a reachable trigger, expected versus actual behavior, and a reproducer or decisive source-path proof. Unvalidated or speculative concerns, missing newly requested tests, style, naming, and unrelated existing debt are advisory.
- Existing required executable acceptance checks retain their current role; actual failures are not waived. An unavailable interpreter or check is an honest unvalidated coverage gap, never PASS and never an automatic code-fix request.
- Review is read-only: do not edit, invoke a fix agent, or start an automatic fix/re-review loop.
- Follow-ups resolve original unresolved findings and evidence-backed serious regressions caused by fixes. No new nits; do not reopen a closed finding without new evidence.
- Use current supported contracts and documented safe exceptions. Do not revive retired CodeRabbit or mutation-testing requirements.

## Repository-specific checks

- Scope: `src/onetree/core.py`, `src/onetree/git.py`, `src/onetree/wal.py`. Flag a reachable landing that includes unclaimed/foreign payload, overwrites a peer's unrelated tree/index, admits an expired/dead session or stale claim fence, or moves the shared ref without the existing write-ahead intent/CAS/landed evidence. Name the exact candidate, baseline and failed admission transition. Safe exceptions: Disposable tests intentionally use bare Git and unsafe controls; the designated shared-clone dogfood rule does not prohibit these fixtures or ordinary external GitHub operations. Bounded CAS retries after genuine contention are expected.
- Scope: `src/onetree/core.py`, `src/onetree/git.py`, `src/onetree/wal.py`, `src/onetree/referee.py`, `src/onetree/_referee_supervisor.py`, `src/onetree/workspace.py`. Flag a change that converts malformed/semantically inconsistent WAL or payload/referee evidence, changed protected workspace configuration, red/timeout/error, or unresolved recovery divergence into an admitted landing or silent baseline reset. Safe exceptions: Operator reconciliation/adoption may be explicitly authorized and valid; historical supported event schemas are legitimate. Current referee behavior without a configured command is not automatically a defect outside the configured dogfood path. Claims do not isolate ports/databases/runtime resources, same-user commands are not sandboxed, and Windows is unsupported for the configured POSIX referee path.

## Relevant existing checks

Use current CI results and only affected existing checks when authorized and available in a disposable test environment. These examples do not require all commands on every PR. Missing prerequisites remain unvalidated; do not use live providers, deploy/provision resources, or run mutating snapshot/replay builders as a review.

- `pytest` — The workflow runs each supported Python on a fresh GitHub-hosted Linux runner; unavailable runner/POSIX capabilities are infrastructure limits, not grounds for automated code fixes.
- `ruff check src tests` — existing CI.
- `mypy` — existing strict package CI.

<!-- END CODEX PR REVIEW RULES v1 -->
