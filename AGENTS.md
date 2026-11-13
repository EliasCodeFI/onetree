# AGENTS.md — how work lands in this repository

This file is read by AI coding agents. Every session working in this clone
follows it.

## Working in a shared clone

When several agents share one clone of this repository, every landing goes
through onetree, with the referee and payload gate the clone has set up. Claim
every file before you edit it and stage in your session's own index: onetree
refuses a landing that contains a file your session has not claimed. Do not
commit to the shared branch directly, switch branches or reset in that clone. A
refusal is something to read and fix, not something to reset away.

## Rules

1. Zero runtime dependencies. Dev tooling lives in the `dev` extra only.
2. Every CLI subcommand supports `--json`: exactly one JSON object on stdout,
   nothing else on stdout.
3. Test first: every guarantee comes with a test that was seen failing before
   the code that makes it pass existed. A test that has never failed proves
   nothing.
4. Enforcement lives in git, never only in a hook of some agent tool.

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

- Scope: `src/onetree/core.py`, `src/onetree/git.py`, `src/onetree/wal.py`. Flag a reachable landing that can include a file its session did not claim, overwrite another session's unrelated work or index, let an expired or dead session or a stale claim land, or move the shared branch without its write-ahead record and compare-and-swap. Name the commit, the base and the check that should have refused it. Not defects: tests that use plain git and unsafe settings on purpose in throwaway repositories, and a bounded number of retries after a real race.
- Scope: `src/onetree/core.py`, `src/onetree/git.py`, `src/onetree/wal.py`, `src/onetree/referee.py`, `src/onetree/_referee_supervisor.py`, `src/onetree/workspace.py`. Flag a change that turns a broken or inconsistent event log, gate or referee result, a changed protected setting, a failing or timed-out test run, or an unresolved recovery problem into a landing or a silent reset of the shared branch. Not defects: an operator's explicit reconcile or adopt, older event formats onetree still reads, a repository with no referee command, and the known limits: claims do not isolate ports, databases or other runtime resources, commands run by the same user are not sandboxed, and the referee does not run on Windows.

## Relevant existing checks

Use current CI results and only affected existing checks when authorized and available in a disposable test environment. These examples do not require all commands on every PR. Missing prerequisites remain unvalidated; do not use live providers, deploy/provision resources, or run mutating snapshot/replay builders as a review.

- `pytest` — The workflow runs each supported Python on a fresh GitHub-hosted Linux runner; unavailable runner/POSIX capabilities are infrastructure limits, not grounds for automated code fixes.
- `ruff check src tests` — existing CI.
- `mypy` — existing strict package CI.

<!-- END CODEX PR REVIEW RULES v1 -->
