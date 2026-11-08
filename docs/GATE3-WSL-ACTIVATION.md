# Gate3 dogfood activation on the WSL lab host

**Recorded 2026-11-08 by the owner seat, Claude Code session
`da28fdec-2cc5-43a2-80e7-46c4f314a2db`.** This activates the Gate3 workflow in a
new primary shared clone on the WSL host. `docs/GATE3-CUTOVER.md` records the
earlier preparation for `/anvil/onetree` on another host; nothing here
re-measures that host.

| Item | Value |
| --- | --- |
| Shared clone | `/home/lab/projects/onetree` in distro `Ubuntu-24.04-Lab` |
| Base | `6ef1c14de01f3f1d61d0974b6a077a11316511f5`, GitHub `main` at activation |
| Coordinator | `/home/lab/.local/share/onetree/releases/6ef1c14d-dogfood-py312/bin/onetree`: a non-editable install of a `git archive` of the base, CPython 3.12.15. Sessions coordinate with this build, never with the shared tree's source. |
| Dev environment | `/home/lab/projects/onetree/.venv`, built the way CI builds it: `uv venv --managed-python --python 3.12 --seed`, then `pip install -e '.[dev]'` |
| Payload gate | `/home/lab/.claude/tools/commit_payload.py`, admitted at SHA-256 `005927e6fdd0507a7be83c754efe3e6e0e19dd9d528b4c556c4c472171cef073` |
| Referee timeout | 1800 s, an operational choice, not a measured bound |
| Protection baseline | tip `6ef1c14`, target `refs/heads/main`; the full `protect` result is retained at `/home/lab/reports/onetree-dogfood-20261108/owner/protect.json` |

## Referee command

```sh
/bin/sh -c 'PYTHONPATH="$PWD/src" ONETREE_TEST_ESTATE_GATE=/home/lab/.claude/tools/commit_payload.py ONETREE_TEST_SHIPPED_GATE=/home/lab/.claude/tools/commit_payload.py exec /home/lab/projects/onetree/.venv/bin/python -m pytest -q --deselect tests/test_gate_receipts.py::test_real_hook_gate_evidence'
```

- `PYTHONPATH="$PWD/src"`: the referee runs in a temporary checkout of the
  candidate, while the dev environment's editable install points at the shared
  working tree. Without it, test subprocesses that import `onetree` would exercise
  the shared tree's source, including a peer's unlanded edits, instead of the
  candidate's. Processes started with `-I` ignore `PYTHONPATH`; that residual is
  not closed here.
- The deselection: the payload gate runs with `PATH` pinned to `os.defpath`
  (`src/onetree/git.py`, `_payload_gate_environment`), and on this host `node`
  exists only in `/usr/local/bin`. The estate gate therefore cannot run a hook's
  companion test and answers DEGRADED, so the three `test_real_hook_gate_evidence`
  cases fail on the base commit for a host reason. Only they are deselected.
  Making `node` resolvable on `os.defpath` (an owner decision; it needs root) and
  removing the deselection through a reviewed reconcile restores them.

## Baseline on the base commit

Measured 2026-11-08 in a `git clone --shared` checkout of the base, under
`env -i PATH=/bin:/usr/bin LC_ALL=C GIT_CONFIG_NOSYSTEM=1`, with the same command
minus the deselection:

- `ruff check src tests`: clean. `mypy`: no issues in 22 source files.
- `pytest`: 1634 passed, 4 skipped, 4 failed in 426 s, coverage 93.91%. The
  failures were the three hook-evidence cases above and
  `tests/test_payload_gate_group.py::test_timeout_leaves_processes_outside_the_gate_group_alive`,
  which raised `ProcessLookupError` from the test's own read of
  `/proc/<pid>/stat`: Linux fails that read with ESRCH when the pid exits after
  the open succeeded. This landing fixes the helper. Its new test
  `test_process_state_reads_a_pid_that_exits_mid_read_as_gone` failed against the
  old helper before the fix.
- The first two referee runs of this landing (2026-11-08) refused it on the four
  `test_cancellation_signals_the_group_before_the_leader_is_reaped` cases, which
  pass whenever the suite runs in the foreground. Cause: the lander was started as
  a background job of a non-interactive shell, which starts it with SIGINT
  ignored; the broker's referee and pytest inherit that disposition, so the test's
  own SIGINT never raised KeyboardInterrupt and its gate processes outlived the
  run. Shown on the base commit's test file with `env --ignore-signal=INT`. The
  test's worker now installs Python's default SIGINT handler first; a new
  `ignored` variant starts the worker with SIGINT ignored and failed before that
  line existed. A first guess, load-sensitive waits, was wrong: widening them
  changed nothing and was reverted. A failed run now also kills the gate's
  process group. The referee itself still inherits the lander's signal
  dispositions; that is reported here, not changed.

Logs: `/home/lab/reports/onetree-dogfood-20261108/baseline/` and
`/home/lab/reports/onetree-dogfood-20261108/owner/`.

## WAL adoption

This clone came from GitHub, which does not carry `refs/onetree/wal`. Session
registration therefore refused with `unrecorded_landing` for the 19 broker
landings on `main`, `882d8c8` through `187147e`. The operator ran
`onetree wal --adopt --target refs/heads/main`, which records each as an
`adopted` event with the literal actor `operator`. That accepts published
history; it does not re-verify how those landings were produced on the other
host. The bare commit `6ef1c14`, which added `uv.lock`, is the protected tip.

## Sessions

The owner seat runs on the Windows side and has no long-lived process in the
distro, so its body PID is a `sleep infinity` keeper started for this purpose
(`/home/lab/reports/onetree-dogfood-20261108/owner/keeper.pid`). Worker sessions
are headless Claude Code processes in the distro, each registered with its own
native session id and process; the roster is
`/home/lab/reports/onetree-dogfood-20261108/workers/roster.tsv`.
