"""Hermetic benchmark harness for the frozen protocol in ``docs/PREREGISTRATION.md``.

Task 26. Three systems under one corpus, one seed set, one machine and one driver: onetree
(claims, private indexes, the real broker), worktree-per-agent (the incumbent) and a naive
shared tree (the floor). Synthetic writers are real child processes whose *intent* -- which
paths they touch, in which order, with which content -- is fixed by a SeedSequence-style key
derived from the run seed; their *outcome* is what the coordination layer under test makes
of that intent, and outcomes may differ between runs because processes race. The protocol
handles that difference explicitly: every number is recomputed from the raw event log this
package commits, and a re-run that disagrees with a committed number voids it rather than
replacing it (``verify`` and ``compare``).

What this package does NOT claim: no benchmark result. The run grid, task corpus and
hardware note are the task 25 amendment's to fix; every grid value here is a placeholder
until that amendment lands, and ``FREEZE.json`` says so on every run. The hermetic leg has no
model cost; API cost belongs to the real-fleet leg. The onetree arm lands through the
operator's admitted payload gate, which the harness neither ships nor re-pins, and a
corpus-invariant referee the harness does ship; the receipts name both by digest, and the
referee is not the repository's test suite.
"""

from __future__ import annotations

SCHEMA = "onetree-bench-v2"
"""Raw-data and receipt schema. Bump when an event field or metric definition changes."""

SYSTEMS = ("onetree", "worktree", "naive")
"""The three systems, in the order the protocol lists them."""

__all__ = ["SCHEMA", "SYSTEMS"]
