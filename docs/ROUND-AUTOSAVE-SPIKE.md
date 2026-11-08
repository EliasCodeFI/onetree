# Autosave spike: recoverable claimed bytes in one shared working tree

Dogfood sprint 2026-11-08, stream D. Measured on Ubuntu 24.04 under WSL2
(kernel 6.18.33.1), Git 2.53.0, CPython 3.12.

## The gap this addresses

`git checkout -- <path>`, `git restore`, `git read-tree -u --reset HEAD` and
`git checkout-index -f -a` move no ref, so the reflog guard has no evidence of
them and they erase a peer's unstaged work undetected (`docs/ROUND-B.md`,
`LIMITATIONS.md` §4; the four cases in
`tests/test_workspace_guard.py::test_path_form_checkouts_erase_peer_work_without_a_reflog_transition`).
That boundary test is unchanged by this round and still passes: autosave
neither detects nor prevents these operations.

The question for this spike: with one shared working tree kept, can a session's
claimed-path bytes be made **recoverable** after such an erasure, and at what
cost? Stream C asks the alternative question (a linked worktree per task).

## Mechanism

`src/onetree/autosave.py`, exposed as `onetree autosave
{save,list,restore,status}` (every form supports `--json`).

- **save** snapshots the working bytes of the session's live claims, or of named
  paths that its claims cover. A directory claim covers its non-ignored
  descendants: tracked paths of `HEAD` plus untracked paths that
  `--exclude-standard` does not ignore. An explicitly claimed file is saved even
  if an ignore rule matches it. Bytes are hashed with `hash-object -w
  --no-filters` (working bytes, no clean filter); modes are `100644`, `100755`
  or `120000` (the link target as the blob). The tree holds only the saved scope.
  The commit message records format, actor, base (`HEAD`), time, scope and the
  deletions (paths tracked in `HEAD` under the scope but absent from the working
  tree). The commit is chained on `refs/onetree/autosave/<actor>` by
  compare-and-swap. A save whose tree, scope and deletions equal the newest
  snapshot's writes nothing. A session with no claims saves nothing. Naming a
  path no claim of the session covers refuses (`autosave_refused`, reason
  `unclaimed`). No WAL event is written and the reader version is unchanged.
- **list** walks the chain newest first.
- **restore** picks the newest snapshot whose scope covers the path (or a named
  one), and either prints the bytes (`--stdout`: raw bytes, or JSON with
  `content_utf8`/`content_hex`; read-only, needs no claim, may read a peer's
  chain with `--from`) or writes them back. Writing requires that no other live
  session claims an overlapping path (`claim_conflict`, naming the holders), that
  the caller claims it (`unclaimed`), and, unless `--force`, that the current
  working bytes are either the base's or held by some snapshot in the chain
  (`unsaved_changes`): restore must not itself erase bytes nothing holds. A
  recorded deletion is restored by removing the file. Writes are atomic
  (`os.replace` of a sibling temporary).
- **status** compares each claimed path's working state with the newest
  snapshot holding it and with `HEAD`, writing no objects: `saved`,
  `reverted_to_base` (equals the base while the snapshot does not: the shape a
  path-form erasure leaves, or a deliberate revert) or `unsaved` (held by no
  snapshot). It exits 1 when any path is not `saved`. It is the victim's only
  way to notice an erasure; it does not tell erasure from an intended revert.

### Not wired automatically

The brief asked to consider an automatic save on `claim` and before `land`. I did
not wire either, for a measured-in-principle reason: at claim time the claimed
bytes are normally the base's, so a snapshot holds nothing recoverable; before a
landing the staged bytes are already objects in the session index and the landing
itself erases nothing. The bytes at risk are the ones written *between* those
points, so a save is only useful right after writes. That is what the optional
hook below does. Leaving `core.py` untouched also kept this stream off the hot
files.

### Optional Claude Code hook (convenience, not the mechanism)

The mechanism is the ref and the commands; any caller can run `onetree autosave
save`. A Claude Code session can run it after each tool call with a
`PostToolUse` hook. **Not executed in this round**; the hook input field name
`session_id` and `$CLAUDE_PROJECT_DIR` are taken from Claude Code's hook
documentation as I understand it and must be checked against the installed
build before use:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit|NotebookEdit|Bash",
        "hooks": [
          {
            "type": "command",
            "command": "sid=$(jq -r .session_id); onetree autosave save --repo \"$CLAUDE_PROJECT_DIR\" --session \"$sid\" --json >/dev/null 2>&1 || true"
          }
        ]
      }
    ]
  }
}
```

The `|| true` makes the hook advisory: a refused or failed save never blocks the
agent, and nothing in the protection contract depends on the hook having run.
Unclaimed writes stay unsaved by design.

## Evidence (red first)

`tests/test_autosave.py`, 19 cases. Every guarantee this record claims was
observed failing before the code providing it existed (receipts in
`/home/lab/reports/onetree-dogfood-20261108/D-autosave/`):

- the first 13 against a stub module with the same public names and no
  behaviour (save a no-op, restore writes nothing, no refusals, no CLI
  subcommand): 13 failed (`red-first-autosave.txt`);
- the status case against a `status` stub returning nothing: failed
  (`red-first-status.txt`);
- the leading-double-quote case against the code before its guard:
  `git hash-object failed with exit 128: fatal: line is badly quoted`
  (`red-first-quote.txt`);
- the edge-shape case's "an unmodified, never-saved claimed path is not at
  risk" assertion against the status code before that rule:
  `assert 'a.txt' not in {...}` failed (`red-first-status-base.txt`). The other
  assertions in the last four cases (refusal paths, root claim, inactive
  session, malformed chain, human CLI lines) were written after the code; they
  are coverage of existing behaviour, not red-first evidence.

With the implementation: 19 passed; `ruff check src tests` and `mypy` clean;
`autosave.py` line+branch coverage from these tests alone is 93%.

| Case | Asserts |
| --- | --- |
| `test_saved_peer_bytes_survive_each_path_form_erasure` ×4 | for `checkout --`, `restore`, `read-tree -u --reset HEAD`, `checkout-index -f -a`: a claimed, saved dirty file is erased to `HEAD` bytes, then restored byte-exact; the protected workspace epoch reports no violation (the autosave ref is not a guarded reflog) |
| `test_bytes_written_after_the_last_save_are_reported_not_recoverable` | v1 saved, v2 written: restore refuses `unsaved_changes` while v2 exists; after erasure restore yields v1, and no blob reachable from the chain holds v2 |
| `test_saving_an_unclaimed_path_is_refused` | `unclaimed`, no ref created; claim-less save is a no-op |
| `test_restoring_onto_a_path_another_session_holds_refuses` | `claim_conflict` naming the holder, holder's bytes untouched; `--stdout` still works; unclaimed restore refuses even with `--force` |
| `test_snapshots_survive_gc_prune_now` | after `gc --prune=now` (zero loose objects left) a deleted untracked file and an erased tracked file are restored |
| `test_executable_and_symlink_modes_round_trip` | tree modes `100755`/`120000`; deleted files come back executable / as the same link |
| `test_directory_claim_saves_non_ignored_descendants_and_deletions` | ignored file excluded, untracked included, tracked deletion recorded and restored as a deletion |
| `test_save_is_a_no_op_until_claimed_bytes_change` | unchanged save writes nothing; chain parentage; explicit older snapshot |
| `test_status_names_reverted_and_unsaved_paths` | after a save, an erased file reads `reverted_to_base`, a file rewritten after the save and a new file read `unsaved`; CLI exits 1 |
| `test_a_path_beginning_with_a_double_quote_is_saved_verbatim` | `--stdin-paths` would C-unquote it; such paths are hashed one by one |
| `test_cli_save_list_restore`, `test_save_runs_as_a_subprocess_hook_would` | one JSON object per command; raw `--stdout`; refusal exit 1; the command works from a separate process as the hook would run it |

## Costs (measured 2026-11-08)

Instrument: `bench_autosave.py` in the stream's report directory, run once
against fresh `git init` repositories in `mktemp` directories. Files are 4096
bytes each under one claimed directory; each latency figure is the median of 5
saves. Wall-clock times include the WAL read used to check claims, which is
small in the fixture; on the dogfood clone (40 WAL events at the time) the read
and fold took a median 13.3 ms of 5.

| Claimed files | All files changed | One file changed | Nothing changed (no-op) |
| ---: | ---: | ---: | ---: |
| 1 | 23.6 ms | 23.1 ms | 19.2 ms |
| 10 | 24.8 ms | 23.0 ms | 20.0 ms |
| 100 | 32.4 ms | 25.6 ms | 23.6 ms |
| 1000 | 107.9 ms | 44.9 ms | 44.9 ms |

The no-op is not free: every save rehashes every file in scope (there is no stat
cache), so a 1000-file claim costs about 45 ms per save even when nothing changed.

Growth over 100 consecutive saves, 100 claimed files:

| Changed per save | Loose objects added | Loose size added | `.git` before → after saves | after `gc --prune=now` |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 400 (blob, 2 trees, commit per save) | 1600 KiB | 556 → 2176 KiB | pack 67 KiB, `.git` 224 KiB |
| 10 | 1300 | 5200 KiB | 556 → 5776 KiB | pack 167 KiB, `.git` 324 KiB |

Loose objects cost a filesystem block each (4 KiB here) until packed; packed, the
chain is about 0.7 KiB per one-file save. Nothing the save path runs triggers
`gc --auto`.

## What is recoverable and what is not

Recoverable, byte- and mode-exact, under the session's own claims:

- the newest saved bytes of every file in a saved scope, after any of the four
  path-form erasures, after `git clean`, `rm`, or an editor overwrite, and after
  `gc --prune=now` (the ref keeps every snapshot reachable);
- untracked files, executable bits, symlinks, and deletions;
- any older snapshot in the chain, by commit.

Not recoverable or not covered:

- **Bytes written after the last save.** This is the boundary, and it is the
  whole cost of not isolating: the loss window equals the save cadence. Restore
  refuses to overwrite such bytes while they still exist, but once a peer has
  erased them nothing holds them.
- **Unclaimed work.** Save refuses unclaimed paths; a session that edits without
  claiming is exactly as exposed as before.
- **Erasure is still undetected by the broker.** Nothing refuses or alarms; the
  owner of the claim learns of it only by running `autosave status` (which
  cannot tell an erasure from a deliberate revert) and must run restore. There
  is no bulk "restore everything erased under my claims" command yet; restore is
  per file.
- **Ref deletion.** `update-ref -d refs/onetree/autosave/<actor>` or
  `for-each-ref`-driven cleanup removes the chain, and after the next prune the
  objects. The chain is protected from `gc`, not from a deliberate ref removal.
- **Torn reads.** A save reads files one by one; a file being written during a
  save can be captured half-written. A later save corrects it.
- **Staged-only state** in the session's private index is not part of the
  snapshot (the index is not erased by these operations).

## Retention and compaction (proposal, not implemented)

1. A snapshot chain belongs to a session incarnation's recovery window. Keep it
   while the session is live and for a grace period after it ends (proposal:
   7 days), then delete the ref; `onetree gc` already decides when a session is
   dead and is the natural place to apply the grace rule.
2. While a session is live, compact a chain longer than 200 snapshots to its
   newest 50 by re-parenting (rewriting the chain with commit-tree, CAS on the
   ref), so the history a person might browse stays short. Packed growth is
   small enough (≈0.7 KiB per one-file save) that compaction is about legibility
   more than disk.
3. Run `git gc --auto` after every 100th created snapshot so loose objects (≈16
   KiB of blocks per one-file save) do not accumulate; `gc --auto` is a no-op
   below Git's own thresholds.
4. After a landing, snapshots of landed paths remain useful only until the next
   edit; no special rule is proposed for them.

## Comparison with a worktree per task (stream C)

Not measured side by side in this round; stream C's numbers are in its own
report. The comparison below uses this round's measurements and facts measured
on the dogfood clone at 6ef1c14 (178 tracked files, 2.1 MB; `.venv` 120 MB;
`.git` 2.7 MB).

| Axis | Autosave (one tree) | Worktree per task |
| --- | --- | --- |
| Path-form erasure of a peer's work | happens; saved bytes recoverable afterwards | cannot reach another task's files: prevented |
| Loss window | everything since the last save | none for files in the task worktree |
| Detection | none by the broker; the victim's `autosave status` shows `reverted_to_base` | not needed for this class |
| Per-save / per-task cost | 20–45 ms per save up to 1000 files; ≈0.7 KiB packed per one-file save | one checkout per task (2.1 MB tracked here) plus any untracked build state (a 120 MB `.venv` here if it must be per-tree) |
| Fit with the current code | new module, no WAL change; existing contract untouched | `Repository` refuses linked worktrees today (`UnsupportedWorktreeError`, "linked Git worktrees are unsupported in Gate 1"), so the coordinator must change |
| Agent discipline needed | claim before editing (already required) plus a save cadence | work in the assigned directory |
| Product premise ("N sessions, one tree") | kept | given up for task files |

## Recommendation

Autosave is a cheap, contained mitigation: about 20–45 ms per save, sub-KiB
packed growth per save, no change to the WAL or the landing path, and it turns
the four undetected erasures from silent loss into loss bounded by the save
cadence. It does not close the gap: work written after the last save is lost,
and nothing notices the erasure. If the owner needs the erasure class
*prevented*, isolation (stream C) is the only design here that does that, at the
cost of giving up the shared tree and lifting the linked-worktree refusal. If the
shared tree remains the premise, adopt autosave with a save after every write
(the optional hook, or the agent calling it), add a bulk restore and the
retention rule above, and keep the boundary test as the record that detection is
still absent. The two are not exclusive: inside a task worktree, autosave would still protect
a task's bytes against the same erasures run there by its own agent.
