# Gate 1 Round AF13: CI and verification truth

AF13 was developed from `e660215d39ae6c4b1db92ffa0827485dc295f415` in a
standalone clone while AF12 owned the main checkout. Integration onto AF12 is
recorded only after that round lands and releases its path claims.

## Red-first observations

The Python support claim was wider than the workflow: `requires-python` was
`>=3.11`, but CI exercised only 3.11 and 3.13. The same workflow used
`actions/checkout@v4` and `actions/setup-python@v5`; the exact AF11 CI run
reported that both Node 20 actions were being forced onto Node 24.

Subprocess coverage was configured nowhere. A focused child-only probe imported
`onetree.proc` and called `process_tag()`, but the parent coverage report showed
zero executed lines in that module. The measurement used Python 3.14.4,
pytest-cov 7.1.0, and coverage.py 7.15.4.

The shared editable environment also resolved the wrong tree. From the isolated
checkout, Python imported
`/home/anvil/onetree/src/onetree/__init__.py`; a red provenance test expected
the isolated checkout's own `src/onetree`. Mypy independently listed all seven
package sources below `/home/anvil/onetree/src`, so a type-only gate could be
false green for the same reason.

Two task records were stale rather than missing code. The deterministic
complexity guard requested by task 60 already existed as
`test_the_branch_audit_uses_one_log_without_skip_pagination`: it drives
`Coordinator.inspect_wal()`, records Git history walks, requires exactly one
`log`, and rejects both `--skip` and `--max-count`. It passed before this round
changed source or configuration. The AF10 conformance README still said four
of eight parser shapes disagreed and overstated what deleting divider handling
would repair, although the executable oracle remained self-consistent.

Context7 was queried before changing each moving external surface. Official
action documentation identifies `actions/checkout@v7` and
`actions/setup-python@v7` as the current Node 24 majors. Pytest-cov 7 delegates
child measurement to coverage.py's `patch = ["subprocess"]`. Pytest documents
that `pythonpath` entries are prepended relative to its root directory, and
mypy documents `$MYPY_CONFIG_FILE_DIR` as the configuration-relative anchor for
`mypy_path`.

## Implementation

The CI matrix now covers Python 3.11, 3.12, and 3.13, and both GitHub actions
use v7. A repository contract test pins the complete matrix and refuses any
other checkout or setup-python major in the workflow.

The development dependency floor is pytest-cov 7.0 and coverage.py enables its
subprocess patch. The child-only probe remains in the suite so the configured
coverage run has an observable package line that only a child can execute.

Pytest prepends this checkout's `src` directory for every invocation. The
provenance test compares the imported package path with the checkout containing
the collected test, so the shared-editable failure is explicit. Mypy resolves
packages through `$MYPY_CONFIG_FILE_DIR/src`, which also binds invocations
launched below the repository root.

The external AF10 README now says five of eight shapes disagree. It also states
the measured boundary precisely: deleting divider handling closes the three
divider-specific rows, but four trailing-region rows remain red and broad-
corpus under-detection worsens from 259 to 261. No executable oracle byte was
changed.

## Local receipts

After configuration, the same focused child-only run recorded 15 executed
lines in `src/onetree/proc.py`, or 57.14% of that module, on Python 3.14.4.
All five CI-contract tests pass without an ambient `PYTHONPATH`. Strict mypy
passes from the repository root and, when launched from `tests/` with the
parent configuration, lists every `onetree` source below this checkout.

The external conformance instrument passes all 84 self-checks. Its SHA-256 is
`edb2fa3ab3393671a357910637329420acb73ff629c4637e852a17e31707ede9`,
unchanged from the bound AF11 base and result receipts. The task-60 focused
complexity test passes. Ruff, strict mypy, and `git diff --check` pass. The
configured local suite passed 355 tests in 178.56 seconds with 90.81% total
coverage on Python 3.14.4.

## Release receipt still required

The local Python version does not stand in for the supported matrix. Release
requires the combined post-AF12 commit to be the exact local, remote, pull-
request, and workflow SHA; Python 3.11, 3.12, and 3.13 must all pass at that SHA;
the run must contain no Node 20 deprecation annotation; and the staged manifest
must contain only the named AF12 and AF13 paths. Those facts are appended here
only after they are observed.
