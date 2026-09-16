# AGENTS.md — how work lands in this repository

This file is read by AI coding agents. It is normative for every session
working in this clone.

## Build phases

Gate3 dogfood is active in the designated primary shared clone. All development
landings to its shared branch go through OneTree with the configured referee
and admitted payload gate. Stage through your session's private index and claim
every payload path. Bare Git commits, branch switches and resets in that shared
clone are outside this workflow. A refusal is evidence to inspect and resolve;
do not reset or reconcile the protected baseline merely to make it disappear.

- **After Gate 3 (dogfood cutover):** ALL landings go through the onetree
  broker. Bare `git commit` to shared refs is out of contract from that point;
  the broker refuses payloads containing paths your session has not claimed.

## Non-negotiables

1. Zero runtime dependencies. Dev tooling lives in the `dev` extra only.
2. Every CLI subcommand supports `--json`: exactly one JSON object on stdout,
   nothing else on stdout.
3. Every guarantee ships with a known-bad fixture that was observed FAILING
   before the guard existed (red-first). A test that has never failed is not
   evidence.
4. Enforcement lives in the git layer, never only in harness hooks.
5. The benchmark protocol in `docs/PREREGISTRATION.md` is frozen; changes are
   labeled amendments committed BEFORE any measured run they affect.
6. Adverse results publish. If onetree loses on an axis, the axis is reported.

## Repository layout

- `src/onetree/` — the package (Python ≥3.11, typed, mypy --strict).
- `tests/` — pytest suite; coverage gate enforced in CI.
- `docs/PREREGISTRATION.md` — frozen benchmark protocol.
