# Running the suite as its own referee

Task188 was found during Gate3 preparation on `d83f7c5`. The normal local suite
had passed, but the actual referee's restricted environment made the same suite
red. The full receipt and JUnit failure details are retained under
`/anvil/scratch/onetree-g3-prep-20260916/`.

The failures occurred in the tests' adverse-input controls: inherited
`GIT_CONFIG_GLOBAL=/dev/null` prevented HOME/XDG fixtures from loading their
operator config; `GIT_NO_REPLACE_OBJECTS` and `GIT_GRAFT_FILE` prevented the
hidden-history fixture from hiding its landing; and the missing-HOME fixture
raised `KeyError` before exercising the CLI when HOME was already absent.

The correction makes those fixtures establish their intended inputs explicitly.
HOME/XDG configuration setup removes the inherited global-config redirect.
The hidden-history test removes the inherited replacement/graft controls so its
ordinary-Git comparison can observe the deliberately hidden commit. The actual
broker still invokes Git under its production restrictions and must detect the
hidden landing or refuse the graft. The missing-HOME fixture tolerates absence.

All assertions remain. No tests are skipped, no production function or guard is
replaced, and the production referee environment is unchanged. This is fixture
isolation, not permission to weaken the configured referee or its child process.

Correction evidence is under `/anvil/scratch/onetree-referee-env-20260916/`.
The disposable candidate is an unreferenced commit object in an independent
fixture object store, with the exact changed test files staged into its tree.
The real referee checks out that object and executes the focused controls and
full suite there. It does not run test files from the author's working tree.
The external JUnit output preserves details beyond the referee's bounded tail.

Author validation, independent verdict and publication are recorded in task188.
This correction alone does not activate Gate3 or settle task187's repeated
submission failure. The integrated cutover candidate still needs verification.
