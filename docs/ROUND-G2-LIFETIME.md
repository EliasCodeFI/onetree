# Gate 2: referee lifetime ownership (task171)

Scope: cancel work owned by a dead lander, including Git preparation and the
configured referee; remove its scratch checkout without changing landing/WAL
semantics or touching unrelated processes. No runtime dependency is added.

The predecessor at main7374926 leaves cleanup in the process that was killed.
Its accepted crash fixture reproduces an orphan and retained scratch. The new
`test_referee_lifetime.py` kills a real lander after an external marker proves
which child command is executing. Each command starts a sleeper in its group.
The test checks termination of both processes, scratch removal, preservation of
an unrelated sleeper, unchanged branch, and no new landing intent. Test cleanup
runs on failures as well as successes. The shared death-test helpers come from
`test_referee_crash_windows.py`, and `_alive` from `test_referee.py` treats a
zombie as dead, not as a process capable of continuing work.

Before the correction, the clone, checkout and referee cases each failed at
the active-group cleanup assertion. Exact outputs are retained in
`/anvil/scratch/onetree-task171-20260916/lifetime-before.txt`. These are real
SIGKILL inputs against the unchanged predecessor, not edited production guards.

The implementation launches the installed supervisor with an isolated Python
interpreter. It owns the temporary directory and launches each command in its
own session. It monitors the lander's lifetime pipe and parent relationship.
Group leaders remain waitable until after group termination, preventing their
PID from being reused for an unrelated group during cleanup. It returns a
receipt only after cleanup. Referee environment and receipt schema are preserved.

A development regression forked another child after the supervisor launched,
retaining the lifetime writer. Pipe closure alone failed to terminate the
referee; `inherited-lifetime-before.txt` records the failure. The parent check
handles that case while leaving the inheriting child alone. An earlier variant
also retained the request-input pipe and blocked startup; request framing now
uses a newline and checks lifetime while reading, rather than waiting for EOF.
The earlier diagnostic is `inherited-writer-before.txt` and is not represented
as a referee-execution observation.

Another development control delayed clone preparation beyond a short configured
referee timeout. Applying that timeout to preparation produced `referee_error`
without running the command (`preparation-timeout-before.txt`). Preparation now
remains cancellable on parent death without changing when the referee execution
timeout begins. Supervisor death before request delivery must yield a typed
referee error, never a passing landing.

The historical task16 fixture now requires production cleanup, while retaining
its branch/WAL checks and fresh-candidate retry. Its old orphan observation in
`ROUND-G2-DEATH.md` is explicitly historical rather than current behavior.

Limits: the tests establish eventual cleanup under ordinary OS scheduling,
not a real-time service guarantee. A process that deliberately leaves its group
is outside this cooperative cleanup contract. Killing the supervisor as well
as the lander, host failure, storage failure during directory cleanup, and
uninterruptible kernel I/O are not claimed to be recoverable by this process.
This does not provide containment of arbitrary referee file/network access.
Other task16 death windows and overall Gate2 closure remain separate.

## R1 review correction: measure the isolated supervisor

Independent Claude review0afce498b576 accepted the cleanup mechanism and requested
changes because the required coverage gate failed. The full frozen suite passed
its tests, but the collector reported no executed supervisor lines. Two causes
were observed: the production environment excludes ambient collector configuration,
and the standalone script module name falls outside the collector's package-name
selection. Forwarding configuration alone still produced no supervisor coverage;
the collector trace explicitly identified the source-selection mismatch.

The correction is confined to test instrumentation. An autouse fixture passes
the active collector configuration only to the exact installed supervisor launch,
adding the same package directory to the child's source selection. Production
environment, interpreter isolation, receipt checks, and the coverage floor are
unchanged. The referee's own commands still receive the narrow production
environment, checked by the existing hostile-environment tests.

`test_supervisor_execution_is_present_in_collected_data` verifies fresh child
coverage data after a real passing referee execution. Before the instrument
correction it fails because execution was not recorded; the exact output is
`collector-guard-before.txt` in the evidence directory. It checks collector data,
not just a passing subprocess or a configuration string. It explicitly skips
only when coverage is disabled; the normal full CI command enables coverage.

## R2 follow-up: supported collector versions (task176)

The r2 review accepted the correction on the installed collector and left its
dependency-floor compatibility unverified. Before publication, installing
`pytest-cov==7.0.0` and its declared minimum `coverage==7.10.6` in a separate
copy exposed a setup failure: that coverage version has no
`CoverageConfig.deserialize` class method. The unchanged r2 failure is retained
in `floor-focused.txt`, alongside `floor-versions.txt` and `floor-install.txt`.

The fixture now lets Coverage's configuration constructor decode the serialized
child configuration through the same `:data:` input used by its own subprocess
startup. Both the minimum and current installed coverage versions provide that
constructor path. The fixture still alters only the child source-directory
selection and serializes it for the exact supervisor invocation. No dependency
floor, production launch, or coverage threshold is changed. Compatibility is
validated by executing the collector guard and lifetime/environment cases at
the declared floor as well as with the current collector.
