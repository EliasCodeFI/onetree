# Prior art and design credits

**Draft, checked 2026-09-16.** This is a source survey for task32, pending independent review and the task41 dispute dry-run. It does not establish a launch-priority claim. Documentation describes a project's intended contract; this survey does not certify its implementation or reproduce its performance claims.

OneTree combines session-private Git indexes, path write-claims, an auditing landing broker, and an operator-supplied test referee for a shared working tree. Its individual mechanisms have substantial prior art. Shared-tree development, concurrency control, guarded Git operations, and agent coordination are not inventions of this project.

## Related systems

The links below identify the inspected source revisions, rather than a moving default branch. “Documented” means the named project states the behavior; it does not mean OneTree independently ran that project's guarantees.

| Project | Documented mechanism and credit |
|---|---|
| [safegit](https://github.com/stricttools/safegit/blob/dc021db65133355e5181295df3bcad28bfc4a43f/README.md) | Uses temporary indexes per invocation and per-ref locking with compare-and-swap retry. Credit its separation of payload preparation from ref publication. OneTree's design considered this index-and-landing pattern; persistent session indexes are a particular choice within that broader approach. |
| [Quilt](https://github.com/wkoverfield/quilt/blob/709111d50f986b952bf78355ddcdde145b70da32/README.md) | Describes per-line attribution and reconstruction of each agent's changes in one shared checkout, with edit capture and claims. Credit its direct treatment of authorship and same-file coordination. This is the `@quilt-dev/cli` project, not unrelated products with similar names. |
| [Agentlocks](https://github.com/simke9445/agentlocks/blob/35d394595c3590d56c3ea45977f517ffb82288a9/README.md) | Explicitly describes cooperative advisory leases, ownership, fencing around Git-index coordination, and checking staged paths against locks. Credit its lease and staged-versus-held-path contracts. Its own documentation states that participants ignoring the protocol are outside advisory protection. |
| [Flightdeck](https://github.com/justinchuby/flightdeck/blob/b8190f68b58dd6c1d0b1959f6676fd6e488af396/README.md) | Documents a server-executed scoped COMMIT command using locked files and checking the resulting commit. Credit the server-side publication boundary and post-commit inspection, without treating this documentation as a proof of exact payload isolation under every race. |
| [GitButler](https://github.com/gitbutlerapp/gitbutler-docs/blob/fde5f9265e7f8aa37377919c851cc744a97f1e8a/content/docs/features/branch-management/virtual-branches.mdx) | Documents parallel branches and separate staging areas within one working directory. Credit its approach to keeping independent lines of work in a shared workspace. This survey does not infer which OneTree invariants are present or absent from that description. |
| [DOS / dos-kernel](https://github.com/anthony-chaudhary/dos-kernel/blob/efe9ab87c28471e6b987f87f3f2c938392fff22d/README.md) | Describes a referee for a fleet of agents working on one repository, checking completion claims against Git and other external evidence. Credit both the shared-repository framing and the referee's independent inspection of artifacts. DOS states that its referee establishes what happened while code quality remains with tests and reviews; OneTree runs the operator's test command on the candidate before landing. This survey has not executed DOS's referee or reproduced its published evaluations. |
| [mergetrain](https://github.com/yongjip/mergetrain/blob/c9a41c91e105c8c66b913f1dcba587e7ed2dac28/README.md) | Describes integration of committed branches from coding-agent worktrees, combined-train gates, and recovery around publication. Its [design document](https://github.com/yongjip/mergetrain/blob/c9a41c91e105c8c66b913f1dcba587e7ed2dac28/docs/design.md) assigns failure isolation and bisection to the train runner. Credit testing combinations, bisection of combined-only gate failures, and retaining publication evidence. Its documented worktree integration model is relevant prior art even though OneTree's target is one shared tree. |
| [Jujutsu / jj](https://github.com/jj-vcs/jj/blob/e6dd2c0d60f22ef8d21d171b5fb8e4178f0af1d9/web/docs/src/content/docs/operation-log.md) | Documents an operation log containing repository views, undo and restore, and concurrent commands whose divergence or conflicts are reported by subsequent status/log commands. Credit explicit operation history and recoverability. Repository-operation concurrency does not by itself establish a particular agent write-claim or referee contract. |
| [grite](https://github.com/neul-labs/grite/blob/33afee09adda9e7e5be464e846bca800da56212c/README.md) | Documents a Git-ref event log, actor identity, optional signing, and a daemon optional for correctness. Credit neul-labs and [Dipankar Sarkar](https://github.com/neul-labs/grite/blob/33afee09adda9e7e5be464e846bca800da56212c/LICENSE) for these patterns, alongside agent discoverability and CLI design. OneTree takes design ideas, not a grite runtime dependency. |
| [brat](https://github.com/neul-labs/brat/blob/612e39abf64d33dac3bf35b83ba92bd94dac47c8/README.md) | Documents Mayor, Witness, Refinery, and Deacon roles for orchestration, execution, integration, and cleanup. Credit neul-labs and [Dipankar Sarkar](https://github.com/neul-labs/brat/blob/612e39abf64d33dac3bf35b83ba92bd94dac47c8/LICENSE) for the role separation, janitor concept, and demonstration workflow that informed OneTree's roadmap. The roadmap also explicitly credits neul-labs' packaging discipline as an influence on OneTree's planned distribution. |

## Research

- [CoAgent: Concurrency Control for Multi-Agent Systems](https://arxiv.org/abs/2606.15376v1), Hongtao Lyu, Dingyan Zhang, Mingyu Wu, Xingda Wei, and Haibo Chen. The authors describe a predetermined serialization order, speculative writes, notification-driven repair, and registered inverse operations. This credits concurrency control spanning agents' reasoning and tool effects. We inspected the primary abstract and metadata, not a reproduced implementation or evaluation; its reported results are not OneTree measurements.
- [ATM: CID-Brokered Pre-Write Admission for Multi-Agent Code Co-Synthesis](https://arxiv.org/abs/2607.00041v1), Eagl Huang. The author describes admission of write intents within one governance domain, semantic atomization, and a neutral steward applying governed writes. Credit pre-write admission and explicit scope/evidence obligations. Its evaluation and superiority are not independently established here; the abstract itself bounds its conclusions.

## What OneTree adopted

The design record credits safegit for index isolation and ref publication, Agentlocks for leases and checking payload paths, Flightdeck for scoped publication and subsequent inspection, and mergetrain for testing the integrated result and conflict bisection. Both the design record and roadmap credit grite/brat patterns. The roadmap's credits include ref-based coordination history, actor attribution, daemon-independent correctness, cleanup roles, discoverability, demo discipline, and neul-labs' packaging discipline.

Credit does not imply every source feature shipped in OneTree. In particular, optional signing, distributed synchronization, and a daemon are not established OneTree guarantees by virtue of appearing in these sources. Likewise, declining grite as this fleet's tracker does not withdraw credit for the design patterns.

## Survey method and limits

The seed set is the approved design/roadmap bibliography above. Context7 was queried first for safegit, Agentlocks, Quilt, DOS, Flightdeck, mergetrain, GitButler, Jujutsu, and brat; the earlier task21 pass queried grite. Relevant libraries were found and queried for DOS, Flightdeck, GitButler, and Jujutsu. The other lookups returned no relevant match; unrelated results were not substituted.

Public web discovery then used these exact searches. The original result sets, underlying search provider, and Context7 responses were not retained as standalone artifacts; the query list alone is not a complete replay record. The pinned primary-source snapshots support the descriptions above.

```text
"safegit" "index" git agents
"agentlocks" git
"quilt" "shared" "agents" git
"dos-kernel" git
"Flightdeck" "scoped" "commit" agents github
"mergetrain" github bisection
"CoAgent" "ATM" multi agent git
"2606.15376"
```

Primary repositories, official documentation, registry metadata for project identity, and the original arXiv entries were inspected. Search snippets, aggregators, unrelated same-name projects, and a proxy mirror were discovery leads only. GitHub's commit API resolved the repository revisions used in the links above; source bytes at those revisions were retained locally. The grite executable trial is separate from this documentation survey.

Two source corrections matter: the npm website refused the direct Quilt page request, so the official registry identified `wkoverfield/quilt`; its current registry version differed from the search excerpt. The old safegit repository name redirected to `stricttools/safegit`, and the old `docs/architecture.md` path returned 404 at the selected revision. The current README and `.stricttools/docs/architecture.md` were then fetched. Neither old page nor search-cache version is presented as the current artifact.

**Absence statement:** this bounded survey does not establish that no earlier system combines OneTree's proposed elements. It is not an exhaustive code, package, publication, or historical-release search. No runtime comparison of the related tools, apart from the separately bounded grite trial, has been completed here. The claim “first full carrier” therefore remains **UNKNOWN**, not a conclusion of this document. A future launch statement requires the dispute dry-run and the planned same-day re-sweep; ambiguity must remain visible rather than turn into an absent-feature checkmark.
