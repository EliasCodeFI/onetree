# Security

onetree is pre-release software (version 0.0.1a0). Only the `main` branch is maintained; there are
no supported older versions.

## Reporting a vulnerability

Report it privately through GitHub: on this repository's **Security** tab, choose
**Report a vulnerability**. Please do not open a public issue for it.

Say what you did, what you expected and what happened, and include the smallest repository state
and command sequence that shows it. A test that fails is the most useful report: this project
accepts a guarantee only with a test that was seen failing first.

This is a small project. No response time is promised.

## What counts

onetree's guarantees live in git and are enforced when a change lands. A report is a vulnerability
when a landing can do one of these without the operator asking for it:

- include a path the landing session has not claimed, or another session's changes;
- move the shared branch without first recording the landing in onetree's event log
  (`refs/onetree/wal`) and then recording that it landed;
- be accepted although the tests failed, timed out or could not run, or the payload gate refused
  it;
- be accepted for a session that has expired or whose process has ended, or under a claim that is
  no longer held;
- turn a damaged or inconsistent event log, or a changed protected configuration, into an accepted
  landing or a silently reset baseline.

## What does not

These are outside what onetree guarantees, so they are not vulnerabilities: claims cover repository
files and isolate no runtime resource (ports, servers, databases); commands run by the same user are
not sandboxed from each other; onetree works on one machine and one clone, with no server and no
network; the referee decides only what your test command decides; and Windows is not supported.
