# Task-workspace spike: isolated views, recoverable checkpoints, broker landing

Dogfood sprint 2026-11-08, stream C. Question from the owner: are per-task isolated
working views (Git linked worktrees) with recoverable checkpoints, landing through
the existing broker, worth building, and at what cost? The alternative under
evaluation in the same sprint is stream D (autosave inside the shared tree). This
record covers a deliberately thin, opt-in spike, not the large proposal it was
compared with.

## What was built

`src/onetree/task_workspace.py` (Python API only; no CLI, no WAL change):

- `TaskWorkspaces(repository, views_root)` is bound to the primary clone through
  `Repository`, whose linked-worktree rejection is unchanged. A `views_root`
  inside the primary working tree or Git directory is refused
  (`views_inside_primary`).
- `create(task, base)` records `refs/onetree/tasks/<task>/base` (create-only CAS)
  and adds a detached linked worktree at `<views_root>/<task>`.
- `checkpoint(task, include=())` builds a temporary index from the view's index
  plus every entry of the previous checkpoint, runs `git add --update`, then adds
  the explicitly included paths without `--force` (an ignored path is refused,
  `include_refused`). The tree is committed (parent: previous checkpoint or base)
  and published at `refs/onetree/tasks/<task>/checkpoints/<n>` by create-only CAS.
  The view's own index and HEAD are not changed.
- `recover(task)` prunes stale worktree metadata and re-adds the view, detached
  at the latest checkpoint, after its directory was deleted.
- `propose(task, coordinator, actor, *, generation, checkpoint=None)` refuses,
  before touching anything, a coordinator of another repository
  (`foreign_repository`), a superseded checkpoint number (`stale_checkpoint`), an
  inactive session or one whose generation differs from the caller's
  (`session_inactive`, `stale_generation`), a payload path the session has not
  claimed (`unclaimed_paths`), and a path whose entry in the session's private
  index is neither the task base's nor the checkpoint's (`conflict`, the lost
  update guard). It then writes the base-to-checkpoint diff (adds,
  modifications, deletions, modes) into the private index through a temporary
  copy and `os.replace`, and calls the unchanged `Coordinator.land`. The payload
  gate, fence, CAS, re-parenting and the referee are the broker's.

Tests: `tests/test_task_workspace.py`, 16 cases, about 5 s.

## Red-first observations

Each guard was observed failing against a known-bad variant of the module before
the guard was relied on. The variants were loaded as a pytest plugin that
replaced one function each (`variants.py` and the log are retained in the
stream's report directory, `redfirst/redfirst.log`). Unmodified module: 16 passed.

| Variant (what it removes) | Guarded test | Observed failure |
| --- | --- | --- |
| `shared_view`: the "view" is a symlink to the primary working tree | the four path-form erasures (`checkout -- b.txt`, `restore`, `read-tree -u --reset HEAD`, `checkout-index -f -a`) run in task A's view | all four: `assert 'b0\n' == 'task B dirty\n'`, task B's bytes erased (16 of 16 tests fail under this variant) |
| `dangling_checkpoint`: commit kept in memory, no ref | checkpoint survives view deletion and `git gc --prune=now` | `cat-file -e <checkpoint>` exit 1: the commit was pruned |
| `no_claim_check` | unclaimed path refuses, target and session index unchanged | the broker's own INV1 refusal (`LandingRefusedError`) arrives instead, after the import already rewrote the session index |
| `whole_tree_import`: read-tree of the checkpoint instead of its diff | target moved after checkpoint (session registered after the peer landing) | referee red: the candidate reverted the peer's landed `b.txt`. The `before_peer` case passed under this variant, because the session baseline predates the peer landing |
| `no_conflict_check` | session index holds a peer's newer version | `DID NOT RAISE`: the proposal landed and silently reverted the peer's landed change |
| `no_repository_check` | proposal to a clone (same base, without the checkpoint objects) | `write-tree ... invalid object ... for 'a.txt'`: the import had already written a dangling entry into the foreign session's private index, which then refuses every landing of that session |
| `no_generation_check` | proposal bound to an expired generation | `DID NOT RAISE`: landed under the new incarnation |
| `no_checkpoint_check` | proposal of checkpoint 1 when 2 exists | `DID NOT RAISE` |

The peer-isolation test also keeps an in-test control: the same command run in
task B's own view erases B's bytes, so the instrument can see an erasure.

Two of these observations bound what the spike claims. Under `no_claim_check`
the target was still protected, by the broker; what the spike's own check adds is
refusal before the session index is written. And the moved-target guarantee is
a broker property (re-parent and referee): the spike's contribution is to import
a diff rather than a tree, which the `whole_tree_import` variant shows matters
only when the session's baseline already contains the peer's change.

## Measurements

Host: WSL2 (kernel 6.18.33.1-microsoft-standard-WSL2), 16 CPUs, ext4 on
`/dev/sdd`, Git 2.53.0, Python 3.12. Corpus: a `git clone --no-local` of this
repository at `6ef1c14` (178 tracked files, 2600 KiB of working tree, 1880 KiB
`.git`), with its 19 broker landings adopted by the operator in the clone
(the clone does not carry `refs/onetree/wal`) and the workspace protected.
Payload gate: the CLEAN echo stub from `tests/conftest.py`. No referee was
configured: the referee is one full-suite run per candidate in both workflows
(about 7 minutes on this repository in the sprint's referee), so it is excluded
from both sides. Script: `measure/measure.py` in the stream's report directory;
4 runs of 5 tasks each; the table gives the range of the per-run medians.

| Operation | Shared tree | Task view |
| --- | --- | --- |
| Stage (private index `git add`) | 1.9 to 2.5 ms | (inside checkpoint) |
| Create view | none | 25 to 26 ms |
| Checkpoint | no equivalent | 30 to 34 ms |
| Land / propose and land | 136 to 148 ms | 169 to 180 ms |
| Recover after `rm -rf` of the view | no equivalent | 23 to 25 ms |
| Disk per task, view directory | 0 | 2616 KiB (= one full checkout) |
| Disk per task, `.git/worktrees/<task>` | 0 | 52 KiB |
| Object growth per task (checkpoint, landing, WAL) | not separated | 116 to 148 KiB |

Per-view dependency install (one sample): `python3.12 -m venv` 1.93 s, then
`pip install -e '.[dev]'` 5.31 s with a warm pip cache, producing a 123028 KiB
`.venv`. The shared clone's `.venv` is 120 MiB. `.venv` is ignored, so it never
enters a checkpoint. A project with a heavier toolchain (node_modules, compiled
extensions, a build cache) pays its own install and disk per view; for onetree
the dependency environment is about 47 times the size of the checkout.

The Git-side cost of a view is small and constant on this corpus. The real
per-view cost is the dependency environment and the checkout size, which scale
with the project rather than with onetree.

## Adverse findings

1. **The primary working tree goes stale under view-based landings.** The broker
   never writes the shared working tree or shared index, by design. After five
   view-based landings, the measurement clone's primary showed `M` for each
   landed file and `D` for each landed new file against the new `HEAD`: the
   primary still holds the pre-landing bytes. A shared-tree session that then
   edits one of those files edits stale content. Refreshing the primary is a
   working-tree write over paths a peer may have dirtied, which is the operation
   class LIMITATIONS §4 says cannot be detected. So the two modes do not mix
   cleanly. Either all editing moves into views (the primary becomes
   integration-only), or a refresher must update only paths that are clean in
   the primary, which is a new guarantee needing its own red-first fixture.
2. **Claims stay the bottleneck for new files.** `propose` still needs claims on
   every payload path, and a new file needs a claim on an existing parent. In
   this sprint this stream's first `tests/` and `docs/` parent claims were
   refused at 09:46 EET and were first obtained at 11:12 EET, about 86 minutes later. First the
   owner seat held one file below each, then two peer streams did; any single
   descendant claim blocks the parent. A polling `claim` is not a workable
   wait either, because every refusal appends a `refused` event to the shared
   WAL. Waiting has to poll the read-only `wal` view instead. Views do not change
   this, because claims are taken in the shared namespace.
3. **Checkpoints are not in the WAL.** Task refs are outside the audit, outside
   `onetree gc`, carry no lease and have no owner. A peer with ordinary Git can
   delete or move them; nothing detects that.
4. **Context7 for uv was unavailable** (invalid API key) and `uv` itself needs
   approval in this seat, so the dependency measurement used stdlib `venv` and
   pip only.

## What productizing would take

- A CLI surface (`task create|checkpoint|recover|propose|list|remove`) with
  `--json`, and an owner for task refs: either WAL events (a new kind, which needs
  a reader version, currently reserved by other work) or a documented
  refs-only contract with its own audit.
- Session binding: tasks currently have no owner. A task should record the
  creating actor and generation and refuse checkpoints and proposals from
  others.
- A decision on the primary working tree (finding 1): integration-only primary,
  or a clean-path refresher with a red-first fixture.
- Garbage collection and retention for task refs and stale views, plus a
  `worktree prune` policy that does not race a live view.
- Dependency-environment provisioning per view (shared cache, `--link-mode`
  hardlinks or copy-on-write, or one environment shared read-only), measured on
  a project heavier than onetree.
- Concurrency evidence: this spike has no multi-process measurement; two
  writers checkpointing one task are serialized only by the create-only CAS.

## Recommendation

The isolation and recovery properties are real and cheap on the Git side: about
25 ms and 2.6 MiB per view, about 30 ms per checkpoint, about 30 ms of propose
overhead on top of a landing. The reflog-invisible erasures that LIMITATIONS §4
lists as undetectable in the shared tree cannot cross views. A checkpoint
survives `rm -rf` of the view and `gc --prune=now`. Those are the two things
autosave inside the shared tree cannot offer, since it shares one set of bytes.

The cost is not the Git mechanism. It is the per-view dependency environment
(47x the checkout here) and finding 1: views only pay off if the primary stops
being an editing surface. My recommendation is to build task views only together
with a decision that agents edit in views and the primary is integration-only.
If the shared tree must remain an editing surface for some workers, a
shared-tree autosave (stream D) has the smaller integration surface. Under that
choice the four path-form erasures stay undetected, as recorded today. This
judgment comes from a single-process spike on a small Python repository; it is
not a concurrency measurement.
