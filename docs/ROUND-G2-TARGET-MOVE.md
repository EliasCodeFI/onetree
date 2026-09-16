# Gate 2: target movement inside referee execution

Task 14 separates two populations under the accepted `ROUND-G2.md` contract:
recorded broker transitions may rebuild and re-test, while unexplained target
movement in a protected workspace must quarantine. This fixture establishes
the latter with an actual changed target OID during the configured command.
Its runtime baseline is published `c32dfe9`; no runtime code changes.

`tests/test_workspace_referee_move.py` constructs an external commit object
with different `b.txt` content through `GitFixture.raw_commit`, without moving
the branch. It configures a real referee command, protects the baseline, and
stages alpha's disjoint `a.txt` payload through the existing `staged` helper.
Inside the candidate checkout, the command reads its actual HEAD and verifies
the alpha/base bytes. The adverse case then calls Git's compare-and-update
operation on the shared repository, moving its target from the expected base
to the external commit. The command records its PID, candidate and shared tip
before/after that operation in an external JSONL file and exits successfully.
The move therefore occurs inside the running referee, before it returns green.

The broker refuses with `workspace_changed`, reports alpha's payload path,
preserves the external commit and records no landing intent. WAL inspection
and ordinary recovery both report the workspace divergence; recovery does not
replace the external commit. The fixture reads the rejected candidate object
to verify its original parent and bytes, confirms it is not the published tip,
and checks that the command ran once. The external Git command itself is
allowed to execute: this is later admission refusal, not same-user isolation.

The paired positive case runs the same command and observations without the
external update. It verifies a passing receipt on the original baseline,
the candidate's exact publication and a consistent WAL. These are two
deterministic process executions, not independently scheduled competing trains;
task 17 covers that separate composition.

## Evidence and limits

The new cases passed on the unchanged published runtime. They are additional
sequence coverage, not a newly observed pre-fix failure. Original failure
evidence remains the authorized bare-Git probe at `3105b1a` and
`referee-red-first.txt` for the missed candidate re-test, retained under
`/anvil/claude-diag-806b39cf/onetree-gate-evidence/` and described in
`ROUND-B.md`. No implementation guard was changed to manufacture a failure.

The initial draft failed collection with an unterminated triple-quoted command
string. That fixture syntax error was corrected before executing the cases;
it is not evidence against the workspace guard. The corrected focused run
passed both cases and Ruff passed. Logs are retained under
`/anvil/scratch/onetree-g2-target-move-20260916/`.

Input chain: pytest's configured `pythonpath` imports this checkout's `src`;
`tests/conftest.py` supplies the real temporary Git repository and admitted
coordination-only payload gate; `test_referee.py` supplies command quoting and
session staging. The referee uses the current test interpreter with `-I -S`
and the fixture-resolved Git executable, not a mock referee result. Its
absolute marker/shared-repository paths are explicit test inputs. No sandbox
claim is made. Git's documented object-creation and compare-and-update
contracts were checked through Context7's `/git/htmldocs` before authoring.

This candidate needs independent review and publication before task 14 can
close. It does not reconcile quarantined workspaces, prevent file-only Git
destruction, or complete Gate 2.
