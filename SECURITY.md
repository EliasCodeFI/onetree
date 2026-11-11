# Security

onetree is pre-release software (version 0.0.1a0). Only the `main` branch is maintained; there are
no supported older versions.

## Reporting a vulnerability

Report it privately through GitHub: on this repository's **Security** tab, choose
**Report a vulnerability**. Please do not open a public issue for it.

Say what you did, what you expected and what happened, and include the smallest repository state
and command sequence that shows it. A fixture that fails is the most useful report: this project
accepts a guarantee only with a known-bad case that was seen failing first.

This is a small project. No response time is promised.

## What counts

onetree's guarantees live in the git layer and are enforced on the landing path. A report is a
vulnerability when a landing can do one of these without the operator asking for it:

- include a path the landing session has not claimed, or another session's payload;
- move the shared branch without the write-ahead intent and the landed record;
- be admitted although the referee was red, timed out or could not run, or the payload gate
  refused it;
- be admitted for a session that is expired or dead, or under a claim that is stale;
- turn a malformed or inconsistent write-ahead log, or a changed protected configuration, into an
  admitted landing or a silently reset baseline.

## What does not

`LIMITATIONS.md` states what onetree does not guarantee, and those are not vulnerabilities. In
short: claims are write-claims on repository paths and isolate no runtime resource (ports,
servers, databases); commands run by the same user are not sandboxed from each other; onetree
works on a single host and a single clone, with no daemon and no network; the referee decides only
what the operator's test command decides; and Windows is not supported for the POSIX referee path.
