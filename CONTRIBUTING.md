# Contributing

Issues and pull requests are welcome. onetree is pre-release software, so expect interfaces to
change.

## Before you write code

Read `docs/DESIGN.md` for the mechanism and `LIMITATIONS.md` for what onetree deliberately does not
do. For anything larger than a fix, open an issue first and say what you want to change and why.

## The rules a change has to meet

These are the project's non-negotiables (`AGENTS.md` has them for coding agents):

1. **No runtime dependencies.** Development tools go in the `dev` extra only.
2. **Every CLI subcommand supports `--json`**: exactly one JSON object on stdout and nothing else
   on stdout.
3. **Red first.** Every guarantee ships with a known-bad fixture that was seen failing before the
   guard existed. Say in the pull request which test failed on the code before your change, and
   show its output. A test that has never failed is not evidence.
4. **Enforcement lives in the git layer**, never only in a harness hook.
5. **The benchmark protocol in `docs/PREREGISTRATION.md` is frozen.** A change to it is a labelled
   amendment, committed before any measured run it affects.
6. **Adverse results are published.** If onetree loses on an axis, the axis is reported.

## Setting up and checking

Python 3.11 or newer, on Linux. From a clone:

```
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
ruff check src tests
mypy
pytest
```

`pytest` includes the coverage gate (90 % of `src/onetree`, branches counted). CI runs the same
four commands on Python 3.11, 3.12 and 3.13, and a pull request needs all of them green.

## Pull requests

- One change per pull request, with a description of what was wrong, what changed, and the red and
  green test output.
- Do not weaken or delete an existing test to make a change pass.
- If an AI coding tool wrote or helped write the change, say which one in the description.
  `AI_USAGE.md` records how this repository itself was built.

## Licence of contributions

onetree is licensed under the Apache License, Version 2.0. Under section 5 of that licence, a
contribution you submit for inclusion is licensed under the same terms, with no additional terms.
There is no separate contributor agreement.

## Security

Do not report a vulnerability in an issue or a pull request: see `SECURITY.md`.
