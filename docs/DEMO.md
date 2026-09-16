# Local synthetic fleet demo

Run independent child processes against a new sample repository:

```sh
python -m onetree.demo \
  --output /absolute/path/to/new-demo \
  --commit-payload /absolute/path/to/admitted/commit_payload.py \
  --message-file /absolute/path/to/commit-message.txt \
  --workers 3 --timeout 120 --json
```

Use an installed OneTree package, Python 3.11 or later, and Git. The demo adds no
runtime dependencies. The payload gate is an explicit prerequisite: supply the
gate matching this package's admitted digest. The package does not distribute
that gate. A missing or mismatched gate cannot produce a successful run.

The message file supplies the commit message, including any operator identity
trailers required by the gate. The demo does not manufacture session identities,
change identity environment variables, or opt into foreign-author admission.
Its `demo-worker-N` actors identify synthetic writers in this sample repository;
they are not independent agent seats. Baseline setup uses a synthetic Git author.

`--output` must name a new directory whose parent already exists. Existing paths,
including dangling symlinks, are refused. The demo creates no remotes and does
not target an existing repository. It retains its output on success or failure;
there is no automatic recursive cleanup.

Each worker registers its own PID and claims its own sample file. All workers
reach the readiness barrier before proceeding. Worker zero holds `shared.txt`;
the other workers demonstrate an actual claim refusal on that path. Only after
these refusals do the workers stage their own changes through private indexes
and attempt broker landings. Expected claim refusals remain visible as refusals
in the report. Unexpected refusals make the overall run fail.

The sample referee accepts each worker file only in its initial state or that
worker's expected final state, and requires `shared.txt` to remain unchanged.
It runs through OneTree's existing referee machinery. This sample check is not
the project's full test suite and does not replace any production referee.

Completion requires joined workers with successful outcomes, distinct worker
PIDs and commits, their expected payloads and committed contents, matching WAL
intent and landing records, consistent WAL replay, and no pending intents or
active claims. Missing worker evidence cannot establish success. The timeout
is a configured waiting limit after sample setup, not a measured runtime bound;
cleanup and final evidence inspection can take additional time. On failure,
the controller terminates and joins its owned workers. This does not promise
containment of deliberately detached descendants, nor cancellation of commits
that already landed. Inspect retained evidence before deciding what to do next.

Without `--json`, lifecycle progress and the final summary appear on stdout.
With `--json`, progress goes to stderr and stdout contains one final JSON object.
Exit status is zero only when the final report's `ok` field is true. An interrupted
or failed run returns a nonzero status. Abrupt termination of the controller can
prevent a final report; absence of that report is not completion evidence.

Retained output:

| Path | Contents |
| --- | --- |
| `demo.json` | Synthetic-run marker and controller PID |
| `repo/` | Sample files, commits, private indexes, and immutable WAL |
| `workers/N.json` | Each worker's observed phases, refusal, and landing or error |
| `report.json` | Final status, worker outcomes, and available WAL evidence |

This demonstrates local coordination, not benchmark throughput, real-agent
fleet acceptance, or API cost. Those results require separate evidence.
