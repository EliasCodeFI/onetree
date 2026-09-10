# Gate 1 Round AF8D: bounded audit-visible Git inputs

AF8D started from `3a389dd6a85740dc3f597efcc4bc22a4dd82ae7c` on
branch `af5`. The imported module resolved to
`/home/anvil/onetree/src/onetree/git.py`. The local measurement surface was
Git 2.53.0 and Python 3.14.4; Python 3.11 and 3.13 are CI observations, not
inferences from the local interpreter, and are recorded only after push.

This round closes two demonstrated fail-open inputs:

| Input | Unpinned observation | Retained control |
| --- | --- | --- |
| Git trailer configuration | `trailer.separators='='` empties the requested value. Source inspection and a second red-first probe found a larger class: `trailer.Onetree-Operation.key=X` and an unrelated `trailer.hostile.key=Onetree-OperationX` also rewrite the parsed key and empty it. | The audit no longer invokes Git's configurable trailer or pretty-message parser. It walks fixed OID fields, reads size-framed raw commit objects, and applies a fixed final-trailer-block parser for literal colon-delimited `Onetree-Operation`. |
| A checksum-valid MIDX with the landing's `OOFF` entry copied from a trailer-less commit | `git log`, `git show`, and `git cat-file commit` silently return the decoy bytes under the landing OID. `git multi-pack-index verify` exits 1 with `incorrect object offset`; ordinary reads do not run that verification. | Every Git process receives `-c core.multiPackIndex=false`, and `_environment()` removes `GIT_TEST_MULTI_PACK_INDEX`, which Git 2.53 applies after config and which otherwise re-enables MIDX reads. |

The common options reach `_invoke()`'s captured `subprocess.run`, the streamed
`git log` `subprocess.Popen`, and `marked_commits()`'s temporary-file-backed
`git cat-file --batch` run. The walk forces `--encoding=UTF-8` because a
measured `i18n.logOutputEncoding=UTF-16` encodes even `%H` with a BOM and
embedded NUL bytes. It also forces `--no-show-signature`: a fake signed landing
plus `log.showSignature=true` made the unpinned walk invoke the
repository-configured GPG program. Raw `cat-file` reads do not use the pretty
or signature display paths.

The first body-based fixed-parser design was rejected before the final gates.
`git hash-object --literally` created a hash-consistent commit containing a NUL
before its final trailer block; `git show --format=%B` silently stopped at the
NUL. That directly disproved AF8B's statement that a commit message cannot
contain NUL. The final walk therefore emits only the already-proven
`OID NUL empty NUL` records. A second, size-framed `cat-file --batch` process
reads the exact raw objects into a temporary file, so message bytes never
become delimiters and whole-walk output size is not accumulated in memory. The
single-commit path uses `cat-file commit` for the same reason. The NUL fixture
requires both APIs to recover the final operation, report the landing, and
refuse mutation.

The fixed parser is deliberately narrower than `git interpret-trailers`: after
removing trailing blank lines it examines only the final paragraph, requires
every non-continuation line there to be an ASCII token followed by literal
`:`, unfolds continuation lines, matches `Onetree-Operation`
case-insensitively, and joins duplicate values with the same comma convention
the old format requested. A test places an exact-looking operation line in an
earlier paragraph and proves it is ordinary prose. The existing control-byte
fixtures prove record and unit separators remain value bytes, not framing.

## What “bounded” means here

The bounded surface is the set of non-racing local inputs that Git 2.53.0 can
consult while executing the three commands behind the two audit APIs:

```text
git <common options> -C <root> log -z --encoding=UTF-8 --no-show-signature --format=%H%x00 <target> <excludes> --
git <common options> -C <root> cat-file --batch
git <common options> -C <root> cat-file commit <commit>
```

The list was closed in four passes:

1. The Git 2.53.0 documentation was enumerated for repository discovery,
   object storage, configuration scopes, revision traversal, trailers,
   commit-graphs, MIDX, bitmaps, and reverse indexes. Official documentation
   was retrieved through Context7 before relying on trained knowledge.
2. An ignore-independent `command grep -r` inventory over Git v2.53.0 source
   at `67ad42147a7acc2af6074753ebd03d904476118f` enumerated `GIT_*`
   environment reads, config callbacks, and candidate index opens. Every
   reachable hit was assigned to a row or an explicit trust-boundary group;
   the consumers were then inspected, not merely counted:
   `config.c`, `environment.h`, `setup.c`, `repo-settings.c`, `trailer.c`,
   `commit-graph.c`, `midx.c`, `pack-bitmap.c`, and `pack-revindex.c`.
   This closed config ordering, trailer aliases, output encoding, post-config
   test overrides, and master-disable relationships that the prose
   documentation does not fully state.
3. The exact `log`, batch `cat-file`, and single-object `cat-file` commands were
   run in repositories carrying the candidate inputs. `strace openat` was used
   for derived indexes, with a positive-control Git command whenever the exact
   audit did not open a file.
4. Every AF8D control retained in code was removed or specifically weakened
   alone in an isolated `mutation_pin.py` worker. Tests also contain raw,
   unpinned controls for the two required failures and for each newly scrubbed
   redirect.

This is not a claim that onetree authenticates Git itself, the kernel, refs, or
the essential object store. A hostile Git executable, a hash collision,
concurrent file mutation, or deliberate corruption of authoritative loose
objects, pack `.pack`/`.idx` pairs, or ref-backend files is outside this round's
claim. Those files are the repository being audited, not optional alternate
interpretations of it. Optional derived indexes can be ignored without changing
repository meaning and therefore are ignored. A broader cryptographic
object-store integrity contract would require its own correctness and
performance design; AF8D does not silently imply one.

AF9's branch-target enumeration remains out of scope. In particular this round
does not narrow the existing every-local-branch-plus-HEAD audit and does not
turn `GIT_REF_PARANOIA` or ref-race questions into AF8D work.

## Configuration input inventory

Git's effective order is system, global, local, worktree, then command. Includes
are read at the scope of their includer. `GIT_CONFIG_COUNT` pairs and the legacy
`GIT_CONFIG_PARAMETERS` feed command scope, but Git's explicit `-c` entries are
later and win. There is no documented environment switch that skips repository
local config, so trying to scrub only global or system config cannot close an
open-ended parser family. AF8D instead removes Git's trailer parser from the
decision. Explicit command options fix the message encoding, while `-c` remains
appropriate for the two finite master switches controlling optional indexes.

| Input | Audit effect and disposition | Evidence |
| --- | --- | --- |
| System config and `GIT_CONFIG_SYSTEM` | May supply every semantic key below. Finite accelerator keys lose to explicit pins; trailer settings are not consumed. | **Measured** with a replacement system file containing `trailer.separators='='`; raw Git was blind and both onetree paths were not. Ordering also **inspected** in `config.c`. |
| Global/XDG config and `GIT_CONFIG_GLOBAL` | Same. | **Measured** with a replacement global file; raw Git was blind and onetree was not. Ordering **inspected**. |
| `HOME` and `XDG_CONFIG_HOME` | Select the ordinary global/XDG files, but do not create a scope above command config. | Selection and precedence **inspected**; not separately measured because replacement of that effective scope through `GIT_CONFIG_GLOBAL` was the measured arm. |
| Repository-local `.git/config` | Same, and was the conformance input. | **Measured** red under Git's parser and green under the fixed parser. Two additional local key-rewrite forms were observed red before that change. |
| Worktree `.git/config.worktree` with `extensions.worktreeConfig=true` | Same; it outranks local but remains below command scope. | **Measured** with a real primary-worktree config. Raw Git was blind and both fixed-parser reads recovered the operation. |
| Config `include.path` and `includeIf.*` | Can import the same keys but cannot create a scope above the includer. | **Inspected** in Git's include/config sequence; not separately measured because the five effective scopes are the precedence boundary. |
| `GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_<n>`, `GIT_CONFIG_VALUE_<n>` | Inject command-scope values. A trailer value is irrelevant to the fixed parser; onetree's later `-c` wins for MIDX/commit-graph. Missing/malformed pairs terminate Git non-zero and therefore fail closed. | **Measured** with `trailer.separators='='` and `core.multiPackIndex=true`; accelerator precedence was measured in both directions. Malformed count was also measured to exit 128. Ordering **inspected** in `config.c` and Git option parsing. |
| `GIT_CONFIG_PARAMETERS` | Legacy command-scope injection. A separator override blinded raw Git but is not consumed by onetree. | **Measured** and **inspected**. |
| `GIT_CONFIG_NOSYSTEM` | Can omit the system file, not add a higher scope. It cannot defeat explicit pins. | **Inspected** in `config.c`. |
| `GIT_CONFIG` | Selects a source for the `git config` builtin; it is not part of normal `log`/`cat-file` config discovery. | **Measured** with a file containing the hostile separator (no output change), then **inspected** in `builtin/config.c`. No scrub was added. |
| Explicit onetree `-c` | The final command-scope values disable commit-graph and MIDX reads in all three command sites. | **Measured** against local config and command-environment config; command-site delivery was mutation-pinned with doctored graph/MIDX inputs. |

The semantic config keys reachable from the exact commands were then classified:

| Key or family | Result | Evidence |
| --- | --- | --- |
| `trailer.separators` | Changes whether Git recognizes the literal colon trailer. Ignored by onetree's fixed parser rather than pinned. | **Measured** through all five effective scopes. |
| `trailer.<token>.key` | Can rewrite a parsed token even when `:` remains a separator. An exact token section and an unrelated section whose configured key begins with `Onetree-Operation` both blinded Git. The family is ignored by the fixed parser. | **Measured** red-first in both forms and **inspected** at `token_matches_item()`/`parse_trailer()` in `trailer.c`. This finding is why the initial separator-only pin was removed. |
| Remaining `trailer.*` keys | Insertion/placement/command policy is not consumed while reading fixed message bytes. | **Inspected** in `trailer.c`; no per-key claim is needed by the parser. |
| `i18n.logOutputEncoding` (with `i18n.commitEncoding` as fallback) | Can encode the entire pretty buffer as UTF-16, introducing a BOM and embedded NULs even when the only field is `%H`. The walk uses explicit `--encoding=UTF-8`; raw `cat-file` content is not transcoded. | **Measured** red-first with more than 40 NULs in the unpinned OID/body control and again against the OID-only walk. The walk option was deletion-pinned. Encoding selection was **inspected** in `environment.c` and the pretty-print path. |
| `log.showSignature` and GPG configuration | A signed commit can launch the configured verifier before the formatted walk record. The walk uses `--no-show-signature`; `cat-file` does not enter signature display. | **Measured** with a deliberately signed landing and a fake `gpg.program`: the unpinned walk created its invocation marker; neither onetree API did. The walk option was deletion-pinned; the call order was **inspected** in `log-tree.c`. |
| `core.commitGraph` | Enables both monolithic graphs and split chains, which can supply false parents. Pinned false. | **Measured** with a checksum-valid false-root graph in both a monolithic file and the lower layer of a real two-layer split chain. |
| `commitGraph.*`, `gc.writeCommitGraph`, `fetch.writeCommitGraph` | Subordinate read tuning or write policy. None can read a graph after `core.commitGraph=false`. | Master relationship **inspected** in `repo-settings.c` and `commit-graph.c`. |
| `core.multiPackIndex` | Enables monolithic or incremental MIDX and its extensions. Pinned false. | A monolithic `OOFF` substitution was **measured**; the master loader relationship was **inspected**. |
| `pack.readReverseIndex` | Controls pack `.rev` consumption, but none of the three exact audit commands asks for disk-size, bitmap traversal, or another reverse-offset operation. No pin retained. | **Measured** with a real `.rev` and `GIT_TEST_REV_INDEX_DIE_ON_DISK=1`: exact `log`, `cat-file --batch`, and `cat-file commit` exited 0; `cat-file --batch-check=%(objectsize:disk)` was the positive control and exited 128 with the requested death. Call sites were also **inspected**. |
| `core.useReplaceRefs` | Cannot re-enable replacement reads while `GIT_NO_REPLACE_OBJECTS=1`. | **Measured** in AF8C, including a working custom replacement namespace, and **inspected** in `replace-object.c`. |
| Remaining pretty/log output settings (`format.pretty`, color, decoration, notes, abbreviation, mailmap) | The walk's explicit full-OID format, captured non-TTY output, and empty pathspec remove their semantic consumers; commit messages come from raw object reads. They cannot rewrite the fixed parser's trailer key. | Notes, `log.showNotes`, and color settings were **measured** against the exact walk bytes; format consumers were **inspected**. |

## Repository files and derived indexes

| Local input | Audit effect and disposition | Evidence |
| --- | --- | --- |
| `HEAD`, loose/packed branch refs, and the configured files or reftable backend | Authoritative target OIDs. Retained as repository truth; target enumeration is AF9's frozen scope. | Existing files/reftable fixtures are **measured**; backend selection and ref reads were **inspected**. |
| Loose objects; pack `.pack` and `.idx`; repository-owned `objects/info/alternates` | Authoritative object topology. Retained as repository truth. Ambient object-directory and alternate injection is separately scrubbed below. | Ordinary loose/packed reads are **measured** throughout the suite; the trust-boundary classification is explicit, not an integrity claim. |
| Replacement refs | Can substitute object bytes. Disabled by `GIT_NO_REPLACE_OBJECTS=1`; a custom `GIT_REPLACE_REF_BASE` cannot undo that. | **Measured** with negative and positive controls in AF8C. |
| `.git/info/grafts` | Can rewrite parents. Git is pointed at the empty device, while onetree separately refuses an effective repository graft file. | **Measured** and mutation-pinned in AF8C. |
| Repository `.git/shallow` | Can cut parents. A genuine or ambiguous shallow state is an explicit refusal boundary. | **Measured** and mutation-pinned in AF8C. |
| Monolithic commit-graph | Can replace parsed parents. Ignored through `core.commitGraph=false`. | **Measured** with a repaired file checksum and failing `commit-graph verify`. |
| Split commit-graph chain | Same across graph layers. Ignored by the same master switch. | **Measured** with two chain layers and the lower layer doctored. |
| Monolithic MIDX | Can substitute pack offsets. Ignored through `core.multiPackIndex=false`. | **Measured** with a repaired MIDX checksum and failing `multi-pack-index verify`. |
| Incremental MIDX and MIDX `RIDX`/`BTMP` extensions | Enter through the same MIDX loader and cannot be consumed when the master setting is false. No duplicate checks retained. | Loader and chunk gates **inspected** in `midx.c`; not separately doctored. |
| MIDX bitmap file | Each exact unpinned command opened the MIDX but not its bitmap. A bitmap-enabled `rev-list --use-bitmap-index --objects --all` positive control opened both the MIDX bitmap and pack `.rev`. With the retained pin, all three exact commands opened neither MIDX file. | **Measured** with `strace openat`; master-disable relationship **inspected**. |
| Pack `.bitmap` and `.rev` | None of the three exact audit commands opens them. No guard retained merely to disable an unused path. | **Measured** by final-command `strace` and the reverse-index death positive control; consumers **inspected**. |

## Environment inventory

| Input | Audit effect and disposition | Evidence |
| --- | --- | --- |
| `GIT_DIR` | Can redirect the exact commands to another repository. Removed. | **Measured** with a second repository; raw Git returned only its commit while onetree returned the requested repository's landing. Deletion-pinned. |
| `GIT_COMMON_DIR` | Can replace common repository storage and make the exact walk read the wrong common state or fail. Removed. | **Measured** with a second common directory; raw Git failed `bad object HEAD`, while onetree completed against its repository. Deletion-pinned. |
| `GIT_OBJECT_DIRECTORY` | Replaces the primary object database. Removed. | **Measured** with an empty object directory; raw Git failed, while onetree found the real landing. Deletion-pinned. |
| `GIT_ALTERNATE_OBJECT_DIRECTORIES` | Can supply objects absent from the repository and make an incomplete repository appear complete. Removed. | **Measured** by moving the only pack to an ambient alternate. Raw Git succeeded only with the variable; onetree ignored it and failed closed. Deletion-pinned. |
| `GIT_SHALLOW_FILE` | Can make the status probe and walk describe a different history. Removed. | **Measured** and deletion-pinned in AF8C. |
| `GIT_GRAFT_FILE` | Selects a parent-rewriting file. Set to the empty device, not inherited. | **Measured** and deletion-pinned in AF8C. |
| `GIT_NO_REPLACE_OBJECTS` | Set to `1`; subsumes `GIT_REPLACE_REF_BASE` for reads. | **Measured** in AF8C and **inspected** in Git source. |
| `GIT_TEST_COMMIT_GRAPH` | Git 2.53 can re-enable commit-graph reads after config. Removed. | **Measured** against a doctored graph and deletion-pinned. |
| `GIT_TEST_MULTI_PACK_INDEX` | Git 2.53 can re-enable MIDX reads after config. Removed. | **Measured** against the doctored MIDX and deletion-pinned. |
| Other MIDX/bitmap/reverse-index `GIT_TEST_*` variables | They tune consumers below a disabled MIDX loader or consumers the exact audit never enters. | **Inspected** at every Git 2.53 `getenv` consumer; reverse-index non-entry also **measured**. |
| `GIT_WORK_TREE`, `GIT_INDEX_FILE` | Existing shared-environment isolation removes them, but the exact read-only, pathless walk and object output is unchanged without those removals. They are not credited as AF8D audit guards. | **Measured** byte-identical exact output. No AF8D deletion input exists; their broader staging/worktree role remains outside this audit-path claim. |
| `GIT_NAMESPACE`, `GIT_CEILING_DIRECTORIES`, `GIT_DISCOVERY_ACROSS_FILESYSTEM`, `GIT_PREFIX`, `GIT_IMPLICIT_WORK_TREE` | With the explicit `-C` repository and fully specified revisions/path terminator, they did not change the exact fields. Discovery-boundary variants can at most make setup fail. No scrub added. | `GIT_NAMESPACE`, ceiling, prefix, implicit worktree, and the exact output were **measured**; discovery rules were **inspected**. Branch-ref enumeration remains AF9 work. |
| `GIT_REPLACE_REF_BASE` | Selects replacement namespace, but cannot defeat `GIT_NO_REPLACE_OBJECTS=1`. Not scrubbed after AF8C deliberately removed the redundant check. | **Measured** in both directions in AF8C. |
| `GIT_NOTES_REF`, `GIT_NOTES_DISPLAY_REF`, pathspec variables, `GIT_ATTR_SOURCE`, `GIT_EXTERNAL_DIFF` | Notes and other presentation inputs are outside the walk's explicit OID field; it has an empty pathspec, and `cat-file` has no diff or notes presentation. | Notes were **measured**; remaining consumers were **inspected**. |
| `GIT_NO_LAZY_FETCH` and promisor configuration | Can decide whether a missing object is fetched by OID. A failed fetch fails the audit; a successful fetch supplies the requested hashed object rather than an alternate interpretation of an existing object. No pin added. | **Inspected**; not measured with a remote. This is named as unmeasured, not counted as closure evidence. |
| `GIT_QUARANTINE_PATH` | Alone disables ref updates; receive-pack normally pairs it with object-directory variables, which are scrubbed. It cannot blind these reads and can make later mutation fail closed. | **Inspected** in `setup.c` and `tmp-objdir.c`; not measured. |
| `PATH`, `GIT_EXEC_PATH`, loader injection, and the Git executable | Execution trust boundary, not repository interpretation. `_GIT_EXECUTABLE` is resolved once and this round assumes it is the tested Git. | Explicit boundary; no security claim or guard. |

## Checks retained, removed, and mutation evidence

The valid AF8D deletion run used worker-relative `PYTHONPATH=src`. An earlier
attempt used the original checkout's absolute `PYTHONPATH`; all mutants then
imported unmutated code and reported `SURVIVED`. Those results were invalid and
discarded, not averaged into the result. One later parser mutation mistakenly
inserted a colon into a comprehension and died at collection; that result was
also discarded and rerun as the valid behavioral mutant `if False`. The
final post-edit run reported `PINNED` for all 16 counterfactuals below. Thirteen
delete a control or its delivery; the fixed-key mutant makes the predicate
false, and the two raw-object mutants truncate at NUL. Those behavioral
mutants preserve execution while recreating the specific blindness more
precisely than an unbound-variable deletion would.

| Deleted or weakened control | Observing input |
| --- | --- |
| Fixed match of the literal `Onetree-Operation` key | All eight separator/key-rewrite scope fixtures failed when the match was made false. |
| Size-framed raw object in the batch read | Truncating the object at its first NUL made the NUL-bearing landing fixture fail. |
| Raw object in the single-commit read | The same targeted truncation made `landing_operation()` fail on that fixture. |
| `--encoding=UTF-8` on the streamed walk | The UTF-16 configured-output fixture reframed the OID stream and failed. |
| `--no-show-signature` on the streamed walk | The fake signed landing invoked the configured GPG program. |
| `-c core.multiPackIndex=false` | The doctored MIDX fixture failed. |
| Removal of ambient `GIT_TEST_MULTI_PACK_INDEX` | The same fixture re-enabled the doctored MIDX and failed. |
| Existing `-c core.commitGraph=false` | Both monolithic and two-layer split-chain fixtures failed. |
| Removal of ambient `GIT_TEST_COMMIT_GRAPH` | The post-config override re-enabled the doctored graph and failed. |
| Removal of ambient `GIT_OBJECT_DIRECTORY` | The empty ambient database replaced the real objects and failed. |
| Removal of ambient `GIT_ALTERNATE_OBJECT_DIRECTORIES` | The incomplete repository silently consumed its ambient pack instead of failing closed. |
| Removal of ambient `GIT_DIR` | The exact read used the second repository. |
| Removal of ambient `GIT_COMMON_DIR` | The exact read failed against the substituted common storage. |
| `_GIT_COMMON_OPTIONS` removed from `_invoke()` | The `landing_operation()` run path consumed the doctored MIDX. |
| `_GIT_COMMON_OPTIONS` removed from streamed `log` | The walk consumed the doctored commit graph in both monolithic and split forms. |
| `_GIT_COMMON_OPTIONS` removed from batch `cat-file` | The batch message read consumed the doctored MIDX. |

AF8C's earlier eleven isolated pins remain the evidence for unchanged shallow,
graft, typed-error, stream, and advice controls; they are recorded in
`docs/ROUND-AF8B.md`. AF8D reran the commit-graph deletion because it expanded
that claim to split chains.

The following candidate checks were considered and not retained:

| Candidate | Why it is not a guard |
| --- | --- |
| `git multi-pack-index verify` before every audit | Once MIDX reads are disabled, verification would make mutation availability depend on an unused cache and add repository-size work. Ignoring the optional cache directly removes its interpretation. |
| `-c pack.readReverseIndex=false` | Exact audit reads did not enter the reverse-index path, and the positive control proved the fixture could detect entry. A deletion mutation would observe nothing, so the option would be an unearned guard. |
| A separate MIDX-bitmap or incremental-MIDX check | `core.multiPackIndex=false` is the master read gate. Separate subordinate checks add no independent boundary and would not survive a meaningful deletion test. |
| `git fsck`, `verify-pack`, or per-object rehashing | These would create a broader object-store-integrity contract, not merely remove optional interpretations. That contract and its cost were not specified in AF8D. The authoritative-object-store boundary above is stated instead. |
| `-c trailer.separators=:` or enumerating `trailer.*` | Removed after the key-rewrite red controls disproved completeness. Git offers no wildcard reset for arbitrary trailer subsections; the fixed parser removes the whole configurable family from the decision. |
| Clearing all config files or the entire `GIT_CONFIG_*` family | Git offers no environment switch that skips local config, and repository config also carries legitimate repository format. Fixed syntax plus finite accelerator master switches close the relevant consumers without pretending the files are absent. |

The only retained shared-environment removals that could not be made observable
on these exact reads are the pre-existing `GIT_WORK_TREE` and `GIT_INDEX_FILE`
entries. They are retained for the wider Repository plumbing used for staging
and worktree isolation, but AF8D makes no audit-safety claim from them.

## Red-first and gate record

Before the source change, the focused in-repository controls failed for the
separator scopes, two trailer-key rewrites, UTF-16 output framing, configured
signature verification, NUL-truncated pretty messages, MIDX substitution,
object-directory redirect, ambient alternate, and commit-graph override. The
frozen external base receipt
`af8d-base-3a389dd.json` reported both required rows RED on INV-A, INV-E, and
INV-F with mutation allowed: six required violations and zero pre-existing
regressions.

The conformance suite deliberately removes the ambient variables it gives
onetree. Therefore its green result cannot cover the ambient config,
object-directory, alternate, or Git test-override routes. The in-repository raw
controls and isolated mutations carry those claims. Similarly,
`core.commitGraph=false` still has no independent oracle row: dropping it is
caught by this repository's monolithic/split fixtures, not by the contract gate.

| Gate | Measured result |
| --- | --- |
| Module path | `/home/anvil/onetree/src/onetree/git.py` |
| Valid mutation controls | **16/16 `PINNED`** in isolated final-snapshot workers; snapshot SHA-256 `51c12ebca907b57f669603c3111b8cc26a3a5800b36c98a2e488d0775a5488a6`. |
| Full pytest | **243 passed in 107.90 s**; branch-aware coverage **91.23%** (floor 90). |
| Ruff | `ruff check .`: **All checks passed**. |
| Mypy strict | **Success: no issues found in 7 source files**. |
| Frozen conformance self-test | **PASS, 44 checks**. |
| AF8D receipt comparison | Exit 0, `VERDICT: CONTRACT SATISFIED`; suite `b63f6fdc4cc4`, **6 fixed, 0 still violating, 0 regressed, 0 changed meaning**, against `af8d-base-3a389dd.json`. |
| CI Python 3.11 and 3.13 | Must be observed after push; no local claim is made. |

The conformance suite was not edited. Git behavior claims above are scoped to
2.53.0. CI's Git remains unpinned, so the raw known-bad assertions are the
version guard: they do not skip when the MIDX, trailer, encoding, signature,
NUL, commit-graph, or split-chain behavior changes. A runner on which a premise
no longer reproduces goes red rather than silently licensing the 2.53.0 claim.
