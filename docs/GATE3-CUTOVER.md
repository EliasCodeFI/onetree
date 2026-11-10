# Gate3 cutover preparation

Status: activated. The Gate3 dogfood workflow was activated on 2026-11-08 on the
WSL host; `docs/GATE3-WSL-ACTIVATION.md` is the activation record and governs
where it and this document differ. The rest of this paragraph is the
preparation history, kept as written before activation.

Gate2 is closed by task19. The initial
self-hosting rehearsal found task187 (same-session repeat submissions) and
task188 (environment-dependent test controls). Both require accepted corrections
and combined validation before this procedure can activate the shared clone.
Both corrections received independent acceptance. The first combined referee
refused the candidate on task142's cancellation fixture (task189): the leader
announced readiness before its child published a PID. Independent review then
found incomplete publication of the leader's own PID (task190). The r4 correction
received independent acceptance; the corrected combined run is pending at this
document's freeze. Its external result and publication remain activation gates.
The referee command and timeout below are the preparation choice; activation
remains contingent on that validation and the reviewed cutover.

## Owner activation sequence

1. Stop ordinary branch publication and settle outstanding author/review work.
   Choose the primary shared clone and inspect its exact HEAD, branch, shared
   index and working files. Preserve unrelated work. OneTree rejects linked Git
   worktrees; do not point these commands at a preparation worktree.
2. Inspect `onetree wal --repo /absolute/shared/clone --json`. Resolve any
   inconsistent history or pending intent under its own documented workflow.
   Protection requires no active claims or pending intents. An existing protected
   epoch is not silently refreshed by registration or `protect`.
3. Choose an absolute Python interpreter with the repository's dev dependencies,
   and the exact admitted payload gate. The referee runs in a temporary checkout
   of the candidate with a restricted environment: a relative `.venv` in the
   shared tree and inherited shell variables are not a usable configuration.
4. Admit the reviewed payload gate with
   `onetree gate --repo /absolute/shared/clone --admit "$gate" --sha256 "$reviewed_sha256" --json`
   and check the reported path and digest. Landing refuses while the gate's bytes
   differ from that digest; after a reviewed gate change, admit it again.
   The admission is part of the protection baseline. Once the repository is
   protected, admitting a different gate, editing either admission key, or
   removing the admission makes landing refuse with `workspace_changed` until an
   operator reviews it and runs `onetree reconcile`; re-admitting the identical
   gate changes nothing. A repository protected before admissions were recorded
   (`workspace_protect` version 3, `workspace_reconcile` version 4) still
   verifies, and its epoch reads as having no admission, so its first admission
   comes under protection through that same reviewed reconcile. Until then, and
   likewise under an epoch protected while no gate was admitted, every landing
   refuses with `workspace_gate_unprotected` and writes no intent, whatever the
   admission reads: admit the reviewed gate, then run `onetree reconcile` to
   record it in the epoch, before the first landing.
   Configure the repository-local referee, verify the effective command and
   timeout, then establish the protection baseline. Record the command, runtime
   environment, base commit and returned baseline in the activation receipt.
5. Land the reviewed `AGENTS.md` phase transition through OneTree itself, using
   the owner's private index, path claim and required commit trailers. This is
   the first protected commit. Verify its payload/referee receipts and WAL,
   run the publication gates, and push the exact broker-landed history. Do not
   merge a separate bare-Git transition onto the protected branch. Record the
   activation commit and the handoff to all participating sessions. From that
   activation onward all shared-branch development lands through the broker.

The proposed command is the full local pytest suite, with both installed-gate
selectors explicitly configured. For the designated shared clone, the proposed
configuration uses these observed installations; recheck their identity and
admission before executing the cutover:

Set `actor` to the executing seat's verified native session identifier before
running these commands. Do not substitute another seat's identity.

```sh
git -C /anvil/onetree config --local onetree.referee \
  '/usr/bin/env ONETREE_TEST_ESTATE_GATE=/anvil/.claude/tools/commit_payload.py ONETREE_TEST_SHIPPED_GATE=/anvil/.claude/tools/commit_payload.py /anvil/onetree/.venv/bin/python -m pytest -q'
git -C /anvil/onetree config --local onetree.refereeTimeout 1800
/anvil/onetree/.venv/bin/onetree protect --repo /anvil/onetree --session "$actor" --json
```

The corrected task188 full referee passed in 441.2462 seconds on this host,
with 882 passing tests and 92.29% coverage. The initial red run remains red.
The 1800-second timeout is an operational choice, not a measured upper bound.
The combined prerequisite candidate still requires its own full run. Evidence:
`receipt:/anvil/scratch/onetree-referee-env-20260916/receipts.jsonl:9f17a83eaac0`.
A focused CLI command was used for the disposable onboarding rehearsal only;
it does not replace the full suite.

The proposed phase transition replaces the pre-broker paragraph in `AGENTS.md`
with the following operative text at activation, while retaining the other rules:

> Gate3 dogfood is active in the designated primary shared clone. All development
> landings to its shared branch go through OneTree with the configured referee
> and admitted payload gate. Stage through your session's private index and claim
> every payload path. Bare Git commits, branch switches and resets in that shared
> clone are outside this workflow. A refusal is evidence to inspect and resolve;
> do not reset or reconcile the protected baseline merely to make it disappear.

Production reconciliation is a separate explicit operation. If inspection calls
for it, preserve the old epoch and violation evidence, use the observed expected
prior epoch and a concrete reason, and obtain the applicable authorization. It
does not restore files or move the branch. An empty, unprotected WAL needs an
initial `protect`, not a fabricated reconciliation event.

## Session onboarding

Use the session's real native identifier and long-lived harness PID. Do not use
a short-lived shell PID. OneTree's actor syntax is path-safe; the coordinator's
vendor-prefixed `vendor:uuid` form is not the OneTree actor value. The actor is a
cooperative identity label, not authentication of a human.

The examples assume `onetree` resolves to the reviewed installation. Set
`repo`, `actor`, `body_pid`, `session_record` and `gate` to the inspected values.
Store the session record outside the shared working tree. Use a lease that covers
the intended work and referee execution; verify the returned expiry and warnings.

```sh
onetree session --repo "$repo" --session "$actor" --pid "$body_pid" \
  --target refs/heads/main --ttl 3600 --json > "$session_record"
index=$(jq -er '.session.index | select(type == "string" and length > 0)' "$session_record")
```

The JSON field is `.session.index`. Check the command exit status before using
it; a missing field must not become an index named `null`.

Claim existing absolute paths before editing. For a new file, claim an existing
parent directory that covers it. Stage only the intended paths in the private
index and inspect the staged payload before landing:

```sh
onetree claim --repo "$repo" --session "$actor" --json "$repo/path/to/file"
GIT_INDEX_FILE="$index" git -C "$repo" add -- path/to/file
GIT_INDEX_FILE="$index" git -C "$repo" diff --cached -- path/to/file
onetree land --repo "$repo" --session "$actor" --target refs/heads/main \
  --commit-payload "$gate" -m "$message" --json
onetree wal --repo "$repo" --json
onetree release --repo "$repo" --session "$actor" --json "$repo/path/to/file"
```

Include the repository's required author/session trailers in `message`. Keep each
JSON result and inspect failures before proceeding. After task187 is published,
repeat submissions use the same actor and private index; they must preserve new
staged changes, ignore this incarnation's already-landed payload, retain peer
changes and continue to refuse real conflicts. Do not cycle identities or rewrite
an index to work around a refusal.

## Rehearsal and exit evidence

The preparation evidence is in `/anvil/scratch/onetree-g3-prep-20260916/`.
The disposable CLI rehearsal observed an unclaimed-path refusal with unchanged
branch, then a claimed README landing through the installed payload gate and a
focused referee. The next submission exposed task187 before reaching its intended
red-test control; it is not counted as a referee-red result.

After the task187 correction, that same real actor and private index reached
the intended `referee_red` control without moving the branch, then landed a
distinct valid second payload at `42f024d` with a consistent WAL. Evidence is
`original-cli-red.json`, `original-cli-green.json` and `original-cli-wal.json`
under `/anvil/scratch/onetree-session-progress-20260916/`. No identity reset or
index replacement was used to obtain the result.

The separate full-suite referee ran the exact published candidate in its actual
restricted environment and exposed task188. The original red receipt remains
red. Setup errors (unsupported linked worktree, invalid actor syntax and wrong
JSON accessor) are retained separately from those behavioral observations.

Preparation completion requires accepted corrections, a full green combined
candidate in the actual referee, a working repeated-submission rehearsal, the
reviewed phase transition and the recorded activation sequence. Gate3 closure
also requires its separately tracked concurrent-session dogfood history. A
single disposable rehearsal does not satisfy that exit receipt.
